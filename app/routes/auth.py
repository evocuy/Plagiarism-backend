import os
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, List

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Header, Response, status
from pydantic import BaseModel, Field, model_validator
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.schemas import User, Dosen, Mahasiswa

router = APIRouter()

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "b9148d88e4e7e609348b61c944f7743fa100234a9bebc04b3cfb9f5e")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440")) # Default 24 hours
SESSION_COOKIE_NAME = "plagiarism_session"

password_hash = PasswordHash.recommended()

# Pydantic Schemas
class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)

class UserResponse(BaseModel):
    """
    Response model flat:
    - Tidak ada nested JSON (dosen_profile / mahasiswa_profile dihapus)
    - Tidak ada data redundan
    - Field prodi, fakultas, pembimbing hanya terisi bila role mahasiswa
    """
    id: int
    identifier: str
    role: str
    nama_lengkap: str
    name: Optional[str] = None
    program_studi: Optional[str] = None
    fakultas: Optional[str] = None
    dosen_pembimbing_id: Optional[int] = None
    dosen_pembimbing_nama: Optional[str] = None

    class Config:
        from_attributes = True

def _build_user_response(user: User) -> dict:
    """Helper: konversi ORM User ke dictionary flat tanpa nested null."""
    res = {
        "id": user.id,
        "identifier": user.identifier,
        "role": user.role,
        "nama_lengkap": user.nama_lengkap,
        "name": user.nama_lengkap,
    }
    if user.role == "mahasiswa" and user.mahasiswa:
        res["program_studi"] = user.mahasiswa.program_studi
        res["fakultas"] = user.mahasiswa.fakultas
        res["dosen_pembimbing_id"] = user.mahasiswa.dosen_pembimbing_id
        res["dosen_pembimbing_nama"] = user.mahasiswa.dosen_pembimbing_nama
    return res

class LoginResponse(BaseModel):
    token: str
    user: UserResponse

class CreateUserRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=100)
    nama_lengkap: Optional[str] = None
    name: Optional[str] = None
    password: str = Field(min_length=6, max_length=256)
    role: Literal["mahasiswa", "dosen", "admin"]
    program_studi: Optional[str] = None
    fakultas: Optional[str] = None
    dosen_pembimbing_id: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def populate_name(cls, data):
        if isinstance(data, dict):
            val = data.get("nama_lengkap") or data.get("name") or ""
            data["nama_lengkap"] = val
            data["name"] = val
        return data

class UpdateUserRequest(BaseModel):
    identifier: Optional[str] = None
    nama_lengkap: Optional[str] = None
    name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[Literal["mahasiswa", "dosen", "admin"]] = None
    program_studi: Optional[str] = None
    fakultas: Optional[str] = None
    dosen_pembimbing_id: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def populate_name(cls, data):
        if isinstance(data, dict):
            val = data.get("nama_lengkap") or data.get("name")
            if val is not None:
                data["nama_lengkap"] = val
                data["name"] = val
        return data


def create_access_token(user: User) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode(
        {
            "sub": str(user.id),
            "identifier": user.identifier,
            "role": user.role,
            "exp": expires_at,
        },
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )


def get_current_user(
    authorization: Optional[str] = Header(default=None),
    plagiarism_session: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
    elif plagiarism_session:
        token = plagiarism_session

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autentikasi diperlukan. Silakan login terlebih dahulu.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = int(payload.get("sub"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token autentikasi tidak valid atau telah kedaluwarsa.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User tidak ditemukan.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def get_optional_current_user(
    authorization: Optional[str] = Header(default=None),
    plagiarism_session: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    try:
        return get_current_user(authorization=authorization, plagiarism_session=plagiarism_session, db=db)
    except HTTPException:
        return None


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in ("admin", "super_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya administrator yang memiliki izin untuk operasi ini.",
        )
    return current_user


def seed_initial_users(db: Session) -> None:
    """Inisialisasi akun bawaan jika database masih kosong"""
    initial_accounts = [
        {
            "identifier": "198804152011011002",
            "name": "Bambang Wijaya, S.Kom.",
            "email": "admin.it@univ.ac.id",
            "password": "password123",
            "role": "admin",
            "program_studi": "Biro TI & Sistem Informasi",
            "fakultas": "Pusat Komputer Kampus",
        },
        {
            "identifier": "cihuy",
            "name": "Super Admin Cihuy",
            "email": "cihuy@localhost",
            "password": "akuraja",
            "role": "admin",
            "program_studi": "Biro TI & Sistem Informasi",
            "fakultas": "Pusat Komputer Kampus",
        },
        {
            "identifier": "0412087501",
            "name": "Dr. Ir. Hendra Gunawan, M.Kom.",
            "email": "hendra.gunawan@lecturer.univ.ac.id",
            "password": "password123",
            "role": "dosen",
            "program_studi": "Teknik Informatika",
            "fakultas": "Fakultas Ilmu Komputer",
            "gelar": "Dr. Ir.",
        },
        {
            "identifier": "20210801142",
            "name": "Ahmad Rizky Pratama",
            "email": "ahmad.rizky@student.univ.ac.id",
            "password": "password123",
            "role": "mahasiswa",
            "program_studi": "Teknik Informatika",
            "fakultas": "Fakultas Ilmu Komputer",
            "angkatan": "2021",
        },
    ]

    for acc in initial_accounts:
        existing = db.query(User).filter(User.identifier == acc["identifier"]).first()
        if not existing:
            new_user = User(
                identifier=acc["identifier"],
                password=password_hash.hash(acc["password"]),
                role=acc["role"],
            )
            db.add(new_user)
            db.flush()  # dapatkan new_user.id tanpa commit

            if acc["role"] == "dosen":
                db.add(Dosen(
                    id=new_user.id,
                    nama_lengkap=acc["name"],
                ))
            elif acc["role"] == "mahasiswa":
                first_dosen = db.query(Dosen).first()
                db.add(Mahasiswa(
                    id=new_user.id,
                    nama_lengkap=acc["name"],
                    program_studi=acc.get("program_studi", "Teknik Informatika"),
                    fakultas=acc.get("fakultas", "Fakultas Ilmu Komputer"),
                    dosen_pembimbing_id=first_dosen.id if first_dosen else None,
                ))
    db.commit()


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.identifier == request.identifier).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="NIM / NIDN / NIP tidak ditemukan.",
        )

    # Validasi kata sandi (argon2 atau fallback)
    valid = False
    try:
        valid = password_hash.verify(request.password, user.password)
    except Exception:
        valid = (request.password == user.password)

    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Kata sandi yang Anda masukkan salah.",
        )

    token = create_access_token(user)

    # Set HTTP-only Cookie
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=JWT_EXPIRE_MINUTES * 60,
        samesite="lax",
    )

    return {
        "token": token,
        "user": _build_user_response(user),
    }


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return _build_user_response(current_user)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return {"message": "Berhasil keluar dari sesi."}


@router.get("/users", response_model=List[UserResponse])
def list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Admin Only: Ambil seluruh daftar pengguna dengan profil flat tanpa nested null"""
    users = db.query(User).order_by(User.id.desc()).all()
    return [_build_user_response(u) for u in users]


@router.get("/users/dosen")
def list_dosen(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Admin Only: Daftar semua dosen (untuk dropdown assign pembimbing)"""
    dosens = db.query(Dosen).order_by(Dosen.nama_lengkap).all()
    return [
        {
            "id": d.id,
            "user_id": d.id,
            "nidn": d.user.identifier if d.user else "",
            "nama_lengkap": d.nama_lengkap,
        }
        for d in dosens
    ]


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    request: CreateUserRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin Only: Buat akun pengguna baru (Mahasiswa, Dosen, atau Admin)"""
    if db.query(User).filter(User.identifier == request.identifier).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Pengguna dengan identifier '{request.identifier}' sudah terdaftar.",
        )

    new_user = User(
        identifier=request.identifier,
        password=password_hash.hash(request.password),
        role=request.role,
    )
    db.add(new_user)
    db.flush()  # dapatkan new_user.id

    # Buat profil ekstensi One-to-One spesifik sesuai role
    if request.role == "dosen":
        db.add(Dosen(
            id=new_user.id,
            nama_lengkap=request.name,
        ))
    elif request.role == "mahasiswa":
        db.add(Mahasiswa(
            id=new_user.id,
            nama_lengkap=request.name,
            program_studi=request.program_studi or "Teknik Informatika",
            fakultas=request.fakultas or "Fakultas Ilmu Komputer",
            dosen_pembimbing_id=request.dosen_pembimbing_id,
        ))

    db.commit()
    db.refresh(new_user)
    return _build_user_response(new_user)


@router.put("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    request: UpdateUserRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin Only: Perbarui akun pengguna & profil mahasiswa / dosen"""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pengguna tidak ditemukan.",
        )

    # Validasi identifier jika diubah
    if request.identifier and request.identifier != target_user.identifier:
        exist = db.query(User).filter(User.identifier == request.identifier, User.id != user_id).first()
        if exist:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Identifier / NIM / NIP '{request.identifier}' sudah terdaftar pada pengguna lain.",
            )
        target_user.identifier = request.identifier

    # Validasi & update password jika diisi
    if request.password and len(request.password.strip()) >= 6:
        target_user.password = password_hash.hash(request.password.strip())

    nama = request.nama_lengkap or request.name

    # Update profil sesuai role
    if target_user.role == "dosen":
        if not target_user.dosen:
            target_user.dosen = Dosen(id=target_user.id, nama_lengkap=nama or target_user.identifier)
        elif nama:
            target_user.dosen.nama_lengkap = nama
    elif target_user.role == "mahasiswa":
        if not target_user.mahasiswa:
            target_user.mahasiswa = Mahasiswa(
                id=target_user.id,
                nama_lengkap=nama or target_user.identifier,
                program_studi=request.program_studi or "Teknik Informatika",
                fakultas=request.fakultas or "Fakultas Ilmu Komputer",
                dosen_pembimbing_id=request.dosen_pembimbing_id,
            )
        else:
            if nama:
                target_user.mahasiswa.nama_lengkap = nama
            if request.program_studi:
                target_user.mahasiswa.program_studi = request.program_studi
            if request.fakultas:
                target_user.mahasiswa.fakultas = request.fakultas
            if "dosen_pembimbing_id" in request.model_fields_set:
                target_user.mahasiswa.dosen_pembimbing_id = request.dosen_pembimbing_id

    db.commit()
    db.refresh(target_user)
    return _build_user_response(target_user)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin Only: Hapus akun pengguna"""
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Anda tidak dapat menghapus akun Anda sendiri.",
        )

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pengguna tidak ditemukan.",
        )

    nama = target_user.nama_lengkap
    db.delete(target_user)
    db.commit()
    return {"message": f"Pengguna '{nama}' berhasil dihapus."}
