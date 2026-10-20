import os
from hmac import compare_digest
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, List

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Header, Response, status
from pydantic import BaseModel, Field, model_validator
from pwdlib import PasswordHash
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.schemas import User, Dosen, Mahasiswa

router = APIRouter()

# Never use a source-controlled fallback here.  A missing or obviously
# placeholder secret must prevent issuing/accepting sessions instead of making
# every deployment share the same signing key.
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = "HS256"
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "plagiarism_session")
AUTH_NO_STORE_VALUE = "no-store"


def _read_positive_int_setting(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} harus berupa bilangan bulat positif.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} harus lebih besar dari nol.")
    return value


def _read_boolean_setting(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} harus bernilai true atau false.")


JWT_EXPIRE_MINUTES = _read_positive_int_setting("JWT_EXPIRE_MINUTES", 1440)
# Secure-by-default. Local HTTP development can opt out explicitly with
# SESSION_COOKIE_SECURE=false; production must never rely on APP_ENV spelling.
SESSION_COOKIE_SECURE = _read_boolean_setting(
    "SESSION_COOKIE_SECURE",
    default=True,
)
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "lax").strip().lower()
if SESSION_COOKIE_SAMESITE not in {"lax", "strict", "none"}:
    raise RuntimeError("SESSION_COOKIE_SAMESITE harus lax, strict, atau none.")
if SESSION_COOKIE_SAMESITE == "none" and not SESSION_COOKIE_SECURE:
    raise RuntimeError("SESSION_COOKIE_SECURE harus true ketika SESSION_COOKIE_SAMESITE=none.")
SESSION_COOKIE_DOMAIN = os.getenv("SESSION_COOKIE_DOMAIN", "").strip() or None
SESSION_COOKIE_PATH = os.getenv("SESSION_COOKIE_PATH", "/").strip() or "/"
if not SESSION_COOKIE_PATH.startswith("/"):
    raise RuntimeError("SESSION_COOKIE_PATH harus diawali '/'.")

password_hash = PasswordHash.recommended()


def _jwt_secret_key() -> str:
    """Return a deployment-specific signing key or fail closed."""
    secret = (JWT_SECRET_KEY or "").strip()
    insecure_values = {
        "change-this-secret",
        "your-secret-key",
        "secret",
        "changeme",
        "replace-with-a-unique-random-secret-at-least-32-characters",
    }
    if len(secret) < 32 or secret.lower() in insecure_values:
        raise RuntimeError(
            "JWT_SECRET_KEY harus dikonfigurasi dengan secret acak minimal 32 karakter."
        )
    return secret


def validate_auth_configuration() -> None:
    """Fail at startup if the JWT signing configuration is unsafe."""
    _jwt_secret_key()


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = AUTH_NO_STORE_VALUE
    response.headers["Pragma"] = "no-cache"


def _auth_exception(detail: str, *, status_code: int = status.HTTP_401_UNAUTHORIZED) -> HTTPException:
    headers = {
        "Cache-Control": AUTH_NO_STORE_VALUE,
        "Pragma": "no-cache",
    }
    if status_code == status.HTTP_401_UNAUTHORIZED:
        headers["WWW-Authenticate"] = "Bearer"
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        max_age=JWT_EXPIRE_MINUTES * 60,
        samesite=SESSION_COOKIE_SAMESITE,
        domain=SESSION_COOKIE_DOMAIN,
        path=SESSION_COOKIE_PATH,
    )


def _clear_session_cookie(response: Response) -> None:
    # Cookie scope must exactly match the login cookie; otherwise browsers can
    # retain the old cookie alongside the expired one.
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        domain=SESSION_COOKIE_DOMAIN,
        secure=SESSION_COOKIE_SECURE,
        httponly=True,
        samesite=SESSION_COOKIE_SAMESITE,
    )

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
    secret = _jwt_secret_key()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode(
        {
            "sub": str(user.id),
            "identifier": user.identifier,
            "role": user.role,
            "sv": user.session_version,
            "exp": expires_at,
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Parse Authorization strictly so a malformed Bearer header cannot fall back to a cookie."""
    if authorization is None:
        return None

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise _auth_exception("Format header Authorization tidak valid.")
    return parts[1]


def _decode_access_token(token: str) -> tuple[int, int]:
    try:
        payload = jwt.decode(
            token,
            _jwt_secret_key(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "sub", "sv"]},
        )
    except jwt.PyJWTError as exc:
        raise _auth_exception("Token autentikasi tidak valid atau telah kedaluwarsa.") from exc

    subject = payload.get("sub")
    session_version = payload.get("sv")
    try:
        user_id = int(subject)
    except (TypeError, ValueError) as exc:
        raise _auth_exception("Token autentikasi tidak valid.") from exc

    if user_id <= 0 or isinstance(session_version, bool) or not isinstance(session_version, int) or session_version < 0:
        raise _auth_exception("Token autentikasi tidak valid.")
    return user_id, session_version


def _rotate_session_version(db: Session, user: User) -> None:
    """Invalidate every token minted before this point for one account."""
    db.execute(
        update(User)
        .where(User.id == user.id)
        .values(session_version=func.coalesce(User.session_version, 0) + 1)
    )
    db.commit()
    db.refresh(user)


def get_current_user(
    authorization: Optional[str] = Header(default=None),
    plagiarism_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User:
    bearer_token = _extract_bearer_token(authorization)
    bearer_claims = _decode_access_token(bearer_token) if bearer_token else None
    cookie_claims = _decode_access_token(plagiarism_session) if plagiarism_session else None

    if not bearer_claims and not cookie_claims:
        raise _auth_exception("Autentikasi diperlukan. Silakan login terlebih dahulu.")

    if bearer_claims and cookie_claims:
        if bearer_claims[0] != cookie_claims[0]:
            raise _auth_exception("Credential Bearer dan cookie berasal dari akun yang berbeda.")
        if bearer_claims[1] != cookie_claims[1]:
            raise _auth_exception("Credential Bearer dan cookie berasal dari sesi yang berbeda.")

    user_id, token_session_version = bearer_claims or cookie_claims

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise _auth_exception("User tidak ditemukan.")

    if user.session_version != token_session_version:
        raise _auth_exception("Sesi ini sudah tidak berlaku. Silakan login kembali.")

    return user


def get_optional_current_user(
    authorization: Optional[str] = Header(default=None),
    plagiarism_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> Optional[User]:
    # Optional only means anonymous requests are accepted.  Once a client sends
    # any credential, an invalid or conflicting credential must remain a 401.
    if authorization is None and plagiarism_session is None:
        return None
    return get_current_user(
        authorization=authorization,
        plagiarism_session=plagiarism_session,
        db=db,
    )


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
    validate_auth_configuration()
    _set_no_store(response)
    user = db.query(User).filter(User.identifier == request.identifier).first()
    if not user:
        raise _auth_exception("NIM / NIDN / NIP tidak ditemukan.")

    # Existing installations may still contain legacy plaintext credentials.
    # If one is successfully used, upgrade it atomically with the session
    # rotation below; new credentials are always Argon2 hashes.
    valid = False
    upgrade_legacy_password = False
    try:
        valid = password_hash.verify(request.password, user.password)
    except Exception:
        valid = compare_digest(request.password, user.password)
        upgrade_legacy_password = valid

    if not valid:
        raise _auth_exception("Kata sandi yang Anda masukkan salah.")

    if upgrade_legacy_password:
        user.password = password_hash.hash(request.password)

    # Login is a session boundary: revoke all old Bearer and cookie JWTs for
    # this account before minting the new pair.
    _rotate_session_version(db, user)
    token = create_access_token(user)
    _set_session_cookie(response, token)

    return {
        "token": token,
        "user": _build_user_response(user),
    }


@router.get("/me", response_model=UserResponse)
def get_me(response: Response, current_user: User = Depends(get_current_user)):
    _set_no_store(response)
    return _build_user_response(current_user)


@router.post("/logout")
def logout(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _set_no_store(response)
    _rotate_session_version(db, current_user)
    _clear_session_cookie(response)
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

    # Validasi & update password jika diisi. A password reset must also revoke
    # existing sessions, otherwise a stolen pre-reset JWT would still work.
    password_changed = False
    if request.password and len(request.password.strip()) >= 6:
        target_user.password = password_hash.hash(request.password.strip())
        password_changed = True

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

    if password_changed:
        db.execute(
            update(User)
            .where(User.id == target_user.id)
            .values(session_version=func.coalesce(User.session_version, 0) + 1)
        )
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
