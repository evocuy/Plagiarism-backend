import enum
from datetime import datetime, timezone
from typing import Optional, List, Union, Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, String, Integer, Float, DateTime, ForeignKey, Text, Boolean, JSON
from sqlalchemy.orm import relationship
from ..database.session import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ==========================================
# 1. SQLAlchemy ORM Models
# ==========================================

class User(Base):
    """
    Tabel users (Khusus autentikasi dan role dasar):
    - id (Primary Key)
    - identifier (String, unique, index) -> NIM mahasiswa atau NIP dosen
    - password (String) -> untuk simpan hashed password
    - role (String) -> 'mahasiswa' atau 'dosen' (juga mendukung 'admin')
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    identifier = Column(String(100), unique=True, nullable=False, index=True)
    password = Column(String(255), nullable=False)
    role = Column(String(30), nullable=False, index=True)

    # Relasi One-to-One ke profil ekstensi (One-to-One Profile Extension)
    dosen = relationship("Dosen", back_populates="user", uselist=False, cascade="all, delete-orphan")
    mahasiswa = relationship("Mahasiswa", back_populates="user", uselist=False, cascade="all, delete-orphan")

    # Relasi dokumen & pengecekan plagiarisme
    documents = relationship("Document", back_populates="user", cascade="all, delete-orphan")
    checks = relationship("PlagiarismCheck", back_populates="user")

    # Helper properties agar ORM / Pydantic dapat mengakses data flat secara elegan
    @property
    def nama_lengkap(self) -> str:
        if self.dosen and self.dosen.nama_lengkap:
            return self.dosen.nama_lengkap
        if self.mahasiswa and self.mahasiswa.nama_lengkap:
            return self.mahasiswa.nama_lengkap
        return self.identifier

    @property
    def name(self) -> str:
        """Alias backwards-compatibility untuk nama_lengkap."""
        return self.nama_lengkap

    @property
    def program_studi(self) -> Optional[str]:
        return self.mahasiswa.program_studi if self.mahasiswa else None

    @property
    def fakultas(self) -> Optional[str]:
        return self.mahasiswa.fakultas if self.mahasiswa else None

    @property
    def dosen_pembimbing_id(self) -> Optional[int]:
        return self.mahasiswa.dosen_pembimbing_id if self.mahasiswa else None

    @property
    def dosen_pembimbing_nama(self) -> Optional[str]:
        if self.mahasiswa and self.mahasiswa.dosen_pembimbing:
            return self.mahasiswa.dosen_pembimbing.nama_lengkap
        return None


class Dosen(Base):
    """
    Tabel dosen (Profil detail dosen):
    - id (Primary Key, sekaligus Foreign Key me-referensi ke users.id dengan relasi One-to-One)
    - nama_lengkap (String)
    """
    __tablename__ = "dosen"

    id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    nama_lengkap = Column(String(255), nullable=False)

    # Relasi One-to-One kembali ke User
    user = relationship("User", back_populates="dosen")

    # Relasi One-to-Many ke Mahasiswa bimbingan
    mahasiswa_bimbingan = relationship("Mahasiswa", back_populates="dosen_pembimbing")


class Mahasiswa(Base):
    """
    Tabel mahasiswa (Profil detail mahasiswa):
    - id (Primary Key, sekaligus Foreign Key me-referensi ke users.id dengan relasi One-to-One)
    - nama_lengkap (String)
    - program_studi (String)
    - fakultas (String)
    - dosen_pembimbing_id (Foreign Key me-referensi ke dosen.id)
    """
    __tablename__ = "mahasiswa"

    id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    nama_lengkap = Column(String(255), nullable=False)
    program_studi = Column(String(255), nullable=False)
    fakultas = Column(String(255), nullable=False)
    dosen_pembimbing_id = Column(Integer, ForeignKey("dosen.id", ondelete="SET NULL"), nullable=True)

    # Relasi One-to-One kembali ke User
    user = relationship("User", back_populates="mahasiswa")

    # Relasi Many-to-One ke Dosen Pembimbing
    dosen_pembimbing = relationship("Dosen", back_populates="mahasiswa_bimbingan")

    @property
    def dosen_pembimbing_nama(self) -> Optional[str]:
        return self.dosen_pembimbing.nama_lengkap if self.dosen_pembimbing else None


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    title = Column(String, nullable=False)
    document_type = Column(String, default="skripsi")
    file_path = Column(String, nullable=False)
    extracted_text = Column(Text, nullable=True)
    cleaned_text = Column(Text, nullable=True)
    extraction_metadata = Column(JSON, nullable=True)
    text_extracted_at = Column(DateTime, nullable=True)
    text_extraction_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now)

    user = relationship("User", back_populates="documents")
    checks = relationship("PlagiarismCheck", back_populates="document", cascade="all, delete-orphan")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")
    source_similarity_results = relationship("SimilarityResult", back_populates="source_document")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_type = Column(String(50), default="sentence", nullable=False, index=True)
    chapter = Column(String(50), nullable=True, index=True)
    page_number = Column(Integer, nullable=True, index=True)
    chunk_index = Column(Integer, nullable=False)
    raw_text = Column(Text, nullable=False)
    cleaned_text = Column(Text, nullable=False)
    embedding_model = Column(String(255), nullable=True, index=True)
    embedding_generated_at = Column(DateTime, nullable=True)
    embedding_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now)

    document = relationship("Document", back_populates="chunks")


class PlagiarismCheck(Base):
    __tablename__ = "plagiarism_checks"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    overall_similarity = Column(Float, nullable=False)
    status = Column(String, default="completed")
    approval_status = Column(String, default="belum disetujui")  # 'belum disetujui', 'disetujui', 'revisi'
    reviewed_at = Column(DateTime, nullable=True)
    reviewer_note = Column(Text, nullable=True)
    highlighted_file_path = Column(String, nullable=True)
    created_at = Column(DateTime, default=utc_now)

    document = relationship("Document", back_populates="checks")
    user = relationship("User", back_populates="checks")
    similarity_results = relationship("SimilarityResult", back_populates="check", cascade="all, delete-orphan")


class SimilarityResult(Base):
    __tablename__ = "similarity_results"

    id = Column(Integer, primary_key=True, index=True)
    check_id = Column(Integer, ForeignKey("plagiarism_checks.id", ondelete="CASCADE"), nullable=False, index=True)
    source_document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    chapter = Column(String(50), nullable=True, index=True)
    similarity_score = Column(Float, nullable=False)
    created_at = Column(DateTime, default=utc_now)

    check = relationship("PlagiarismCheck", back_populates="similarity_results")
    source_document = relationship("Document", back_populates="source_similarity_results")
    matches = relationship("SimilarityMatch", back_populates="result", cascade="all, delete-orphan")


class SimilarityMatch(Base):
    __tablename__ = "similarity_matches"

    id = Column(Integer, primary_key=True, index=True)
    result_id = Column(Integer, ForeignKey("similarity_results.id", ondelete="CASCADE"), nullable=False, index=True)
    source_text = Column(Text, nullable=False)
    submitted_text = Column(Text, nullable=False)
    similarity_score = Column(Float, nullable=False)
    page_number = Column(Integer, nullable=True, index=True)
    start_position = Column(Integer, nullable=True)
    end_position = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utc_now)

    result = relationship("SimilarityResult", back_populates="matches")


# ==========================================
# 2. Pydantic Response Schemas (Clean & Flat)
# Tanpa struktur nested JSON, tanpa data null atau redundan
# ==========================================

class DosenResponse(BaseModel):
    """Response khusus Dosen: bersih dari data null/redundant mahasiswa."""
    id: int
    identifier: str
    role: Literal["dosen"] = "dosen"
    nama_lengkap: str

    model_config = ConfigDict(from_attributes=True)


class MahasiswaResponse(BaseModel):
    """Response khusus Mahasiswa: data spesifik mahasiswa tanpa nested profile null."""
    id: int
    identifier: str
    role: Literal["mahasiswa"] = "mahasiswa"
    nama_lengkap: str
    program_studi: str
    fakultas: str
    dosen_pembimbing_id: Optional[int] = None
    dosen_pembimbing_nama: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class AdminResponse(BaseModel):
    """Response khusus Admin."""
    id: int
    identifier: str
    role: str
    nama_lengkap: str

    model_config = ConfigDict(from_attributes=True)


# Discriminated Union untuk respon dinamis berdasarkan role pengguna
UserResponseUnion = Annotated[
    Union[MahasiswaResponse, DosenResponse, AdminResponse],
    Field(discriminator="role")
]
