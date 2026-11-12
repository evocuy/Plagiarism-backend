import enum
from datetime import datetime, timezone
from typing import Optional, List, Union, Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, String, Integer, Float, DateTime, ForeignKey, Text, Boolean, JSON, CheckConstraint, Index, text
from sqlalchemy.orm import relationship
from ..database.session import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


REPOSITORY_STATUS_DRAFT = "draft"
REPOSITORY_STATUS_PUBLISHED = "published"
SOURCE_ORIGIN_ALUMNI = "alumni"
SOURCE_ORIGIN_STUDENT = "student"
SUPPORTED_USER_ROLES = ("admin", "super_admin", "mahasiswa")


# ==========================================
# 1. SQLAlchemy ORM Models
# ==========================================

class User(Base):
    """
    Tabel users (Autentikasi, role, dan profil pengguna):
    - id (Primary Key)
    - identifier (String, unique, index) -> NIM mahasiswa atau identifier admin
    - password (String) -> untuk simpan hashed password
    - role (String) -> 'mahasiswa' atau 'admin'
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    identifier = Column(String(100), unique=True, nullable=False, index=True)
    password = Column(String(255), nullable=False)
    role = Column(String(30), nullable=False, index=True)
    nama_lengkap = Column(String(255), nullable=True)
    program_studi = Column(String(255), nullable=True)
    fakultas = Column(String(255), nullable=True)
    auth_provider = Column(String(20), nullable=False, default="local", server_default=text("'local'"))
    # Retired demo accounts retain document/profile references but cannot log in.
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    # Incremented on login and logout so JWTs minted before the latest session
    # change cannot remain valid server-side.
    session_version = Column(Integer, nullable=False, default=0, server_default=text("0"))

    # Relasi dokumen & pengecekan plagiarisme
    documents = relationship("Document", back_populates="user", cascade="all, delete-orphan")
    checks = relationship("PlagiarismCheck", back_populates="user")

    @property
    def name(self) -> str:
        """Nama tampilan dengan fallback untuk akun lama tanpa profil."""
        return self.nama_lengkap or self.identifier

class LoginThrottle(Base):
    """Expiring login counters shared by all API processes and restarts."""
    __tablename__ = "login_throttles"

    key = Column(String(80), primary_key=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime, nullable=False, index=True)


class ImpersonationSession(Base):
    """Revocable support sessions and their audit trail; never store JWTs."""
    __tablename__ = "impersonation_sessions"

    id = Column(String(32), primary_key=True)
    actor_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    target_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_identifier = Column(String(100), nullable=False)
    target_identifier = Column(String(100), nullable=False)
    actor_session_version = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    expires_at = Column(DateTime, nullable=False, index=True)
    ended_at = Column(DateTime, nullable=True)
    end_reason = Column(String(40), nullable=True)


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
    deleted_at = Column(DateTime, nullable=True, index=True)
    # Only admin-imported alumni material can be a comparison source.
    source_origin = Column(
        String(20), nullable=False, default=SOURCE_ORIGIN_STUDENT,
        server_default=text("'student'"), index=True,
    )
    # Student uploads always remain drafts and never enter the repository.
    repository_status = Column(
        String(20),
        nullable=False,
        default=REPOSITORY_STATUS_DRAFT,
        server_default=text("'draft'"),
        index=True,
    )
    repository_published_at = Column(DateTime, nullable=True)
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
    # Offsets in the heading-filtered page text used to place exact highlights.
    page_start_position = Column(Integer, nullable=True)
    page_end_position = Column(Integer, nullable=True)
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
    progress = Column(Integer, nullable=False, default=0)
    processing_stage = Column(String(64), nullable=True)
    processing_message = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    # Queue metadata keeps long-running checks durable outside the web request
    # process.  A separate worker claims pending rows one at a time.
    candidate_limit = Column(Integer, nullable=False, default=20, server_default=text("20"))
    auto_index_embeddings = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    auto_index_repository_limit = Column(Integer, nullable=False, default=50, server_default=text("50"))
    # ``reference_document_id`` is set only for the legacy direct comparison
    # flow.  Its presence lets the same queue/worker bound that workflow too.
    reference_document_id = Column(Integer, nullable=True)
    queued_at = Column(DateTime, nullable=True, index=True)
    claimed_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)
    comparison_status = Column(
        String(30), nullable=False, default="available", server_default=text("'available'"),
    )
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
    page_start_position = Column(Integer, nullable=True)
    page_end_position = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utc_now)

    result = relationship("SimilarityResult", back_populates="matches")


class SupportTicket(Base):
    """A student's help request; retained independently of document deletion."""
    __tablename__ = "support_tickets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'in_progress', 'waiting_student', 'resolved')",
            name="ck_support_ticket_status",
        ),
        Index("ix_support_tickets_owner_created_at", "owner_id", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Snapshots retain the history if an administrator removes an account.
    owner_name = Column(String(255), nullable=False)
    owner_identifier = Column(String(100), nullable=False)
    category = Column(String(100), nullable=False)
    subject = Column(String(200), nullable=False)
    status = Column(String(30), nullable=False, default="new")
    created_at = Column(DateTime, nullable=False, default=utc_now)
    updated_at = Column(DateTime, nullable=False, default=utc_now, onupdate=utc_now)

    messages = relationship("SupportMessage", back_populates="ticket", cascade="all, delete-orphan")


class SupportMessage(Base):
    __tablename__ = "support_messages"
    __table_args__ = (
        CheckConstraint("author_role IN ('mahasiswa', 'admin')", name="ck_support_message_role"),
        Index("ix_support_messages_author_created_at", "author_id", "created_at"),
        Index("ix_support_messages_ticket_created_at", "ticket_id", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    author_name = Column(String(255), nullable=False)
    author_role = Column(String(30), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now)

    ticket = relationship("SupportTicket", back_populates="messages")


# ==========================================
# 2. Pydantic Response Schemas (Clean & Flat)
# Tanpa struktur nested JSON, tanpa data null atau redundan
# ==========================================

class MahasiswaResponse(BaseModel):
    """Response khusus Mahasiswa: data spesifik mahasiswa tanpa nested profile null."""
    id: int
    identifier: str
    role: Literal["mahasiswa"] = "mahasiswa"
    nama_lengkap: str
    program_studi: str
    fakultas: str

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
    Union[MahasiswaResponse, AdminResponse],
    Field(discriminator="role")
]
