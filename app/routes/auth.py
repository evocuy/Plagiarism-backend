import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional, List

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Header, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.schemas import User, SUPPORTED_USER_ROLES
from app.services.login_throttle_service import LoginThrottleService
from app.services.sads_auth_service import SadsAuthService, SadsLoginSettings, StudentAuthSettings
from app.services.sads_course_service import SadsActiveCoursesService
from app.services import impersonation_service

router = APIRouter()

# Never use a source-controlled fallback here.  A missing or obviously
# placeholder secret must prevent issuing/accepting sessions instead of making
# every deployment share the same signing key.
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = "HS256"
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "plagiarism_session")
IMPERSONATION_COOKIE_NAME = SESSION_COOKIE_NAME + "_impersonation"
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
IMPERSONATION_EXPIRE_MINUTES = _read_positive_int_setting("IMPERSONATION_EXPIRE_MINUTES", 15)
if IMPERSONATION_EXPIRE_MINUTES > 60:
    raise RuntimeError("IMPERSONATION_EXPIRE_MINUTES maksimal 60 menit.")
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
_DUMMY_PASSWORD_HASH = password_hash.hash(secrets.token_urlsafe(32))


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
    LoginThrottleService()
    if StudentAuthSettings.from_environment().provider == "sads":
        SadsLoginSettings.from_environment()


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


def _set_session_cookie(
    response: Response, token: str, *, cookie_name: str = SESSION_COOKIE_NAME, max_age: int | None = None,
) -> None:
    response.set_cookie(
        key=cookie_name,
        value=token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        max_age=JWT_EXPIRE_MINUTES * 60 if max_age is None else max_age,
        samesite=SESSION_COOKIE_SAMESITE,
        domain=SESSION_COOKIE_DOMAIN,
        path=SESSION_COOKIE_PATH,
    )


def _clear_session_cookie(response: Response, *, cookie_name: str = SESSION_COOKIE_NAME) -> None:
    # Cookie scope must exactly match the login cookie; otherwise browsers can
    # retain the old cookie alongside the expired one.
    response.delete_cookie(
        key=cookie_name,
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


class ImpersonationRequest(BaseModel):
    nim: str = Field(min_length=1, max_length=100)

    @field_validator("nim")
    @classmethod
    def normalize_nim(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("NIM wajib diisi.")
        return value


class StopImpersonationRequest(BaseModel):
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")

class UserResponse(BaseModel):
    """
    Response model flat:
    - Tidak ada nested JSON profil pada respons
    - Tidak ada data redundan
    - Field prodi dan fakultas hanya terisi bila role mahasiswa
    """
    id: int
    identifier: str
    role: str
    nama_lengkap: str
    name: Optional[str] = None
    program_studi: Optional[str] = None
    fakultas: Optional[str] = None

    class Config:
        from_attributes = True

def _build_user_response(user: User) -> dict:
    """Helper: konversi ORM User ke dictionary flat tanpa nested null."""
    res = {
        "id": user.id,
        "identifier": user.identifier,
        "role": user.role,
        "nama_lengkap": user.name,
        "name": user.name,
    }
    if user.role == "mahasiswa":
        res["program_studi"] = user.program_studi
        res["fakultas"] = user.fakultas
    return res

class LoginResponse(BaseModel):
    token: str
    user: UserResponse

class CreateUserRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=100)
    nama_lengkap: Optional[str] = None
    name: Optional[str] = None
    password: str = Field(min_length=6, max_length=256)
    role: Literal["mahasiswa", "admin"]
    program_studi: Optional[str] = None
    fakultas: Optional[str] = None

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
    role: Optional[Literal["mahasiswa", "admin"]] = None
    program_studi: Optional[str] = None
    fakultas: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def populate_name(cls, data):
        if isinstance(data, dict):
            val = data.get("nama_lengkap") or data.get("name")
            if val is not None:
                data["nama_lengkap"] = val
                data["name"] = val
        return data


def create_access_token(
    user: User, *, sads_courses_verified: bool = False,
    impersonation_id: str | None = None, expires_at: datetime | None = None,
) -> str:
    secret = _jwt_secret_key()
    expires_at = expires_at or datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),
        "identifier": user.identifier,
        "role": user.role,
        "sv": user.session_version,
        "sads_courses_verified": sads_courses_verified,
        "exp": expires_at,
    }
    if impersonation_id is not None:
        payload["impersonation_id"] = impersonation_id
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Parse Authorization strictly so a malformed Bearer header cannot fall back to a cookie."""
    if authorization is None:
        return None

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise _auth_exception("Format header Authorization tidak valid.")
    return parts[1]


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: int
    session_version: int
    sads_courses_verified: bool
    expires_at: datetime
    impersonation_id: str | None = None

    @property
    def session_identity(self) -> tuple:
        return self.user_id, self.session_version, self.sads_courses_verified, self.impersonation_id


def _decode_access_token(token: str) -> AccessTokenClaims:
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
    impersonation_id = payload.get("impersonation_id")
    if "impersonation_id" in payload and (
        not isinstance(impersonation_id, str) or len(impersonation_id) != 32
        or any(character not in "0123456789abcdef" for character in impersonation_id)
    ):
        raise _auth_exception("Token impersonasi tidak valid.")
    try:
        expires_at = datetime.fromtimestamp(int(payload["exp"]), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise _auth_exception("Token autentikasi tidak valid.") from None
    return AccessTokenClaims(
        user_id, session_version, payload.get("sads_courses_verified") is True, expires_at, impersonation_id,
    )


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
    impersonation_session: Annotated[Optional[str], Cookie(alias=IMPERSONATION_COOKIE_NAME)] = None,
    http_request: Request = None,
) -> User:
    bearer_token = _extract_bearer_token(authorization)
    bearer_claims = _decode_access_token(bearer_token) if bearer_token else None
    # The parent cookie stays private and intact; the overlay is the effective
    # student session. An invalid overlay never falls back to admin privileges.
    effective_cookie = impersonation_session if impersonation_session is not None else plagiarism_session
    cookie_claims = _decode_access_token(effective_cookie) if effective_cookie is not None else None
    if impersonation_session is not None and not cookie_claims.impersonation_id:
        raise _auth_exception("Cookie impersonasi tidak valid.")

    if not bearer_claims and not cookie_claims:
        raise _auth_exception("Autentikasi diperlukan. Silakan login terlebih dahulu.")

    if bearer_claims and cookie_claims:
        if bearer_claims.user_id != cookie_claims.user_id:
            raise _auth_exception("Credential Bearer dan cookie berasal dari akun yang berbeda.")
        if bearer_claims.session_identity != cookie_claims.session_identity:
            raise _auth_exception("Credential Bearer dan cookie berasal dari sesi yang berbeda.")

    claims = bearer_claims or cookie_claims

    user = db.query(User).filter(User.id == claims.user_id).first()
    if not user or not user.is_active or user.role not in SUPPORTED_USER_ROLES:
        raise _auth_exception("User tidak ditemukan.")

    if user.session_version != claims.session_version:
        raise _auth_exception("Sesi ini sudah tidak berlaku. Silakan login kembali.")

    impersonation = None
    if claims.impersonation_id is not None:
        if not claims.sads_courses_verified:
            raise _auth_exception("Sesi belum memverifikasi mata kuliah aktif.")
        impersonation = impersonation_service.resolve_impersonation(db, claims.impersonation_id, user)
    elif user.role == "mahasiswa":
        student_auth = StudentAuthSettings.from_environment()
        if student_auth.provider == "sads" or user.auth_provider == "sads":
            if student_auth.provider != "sads" or user.auth_provider != "sads":
                raise _auth_exception("Metode login mahasiswa berubah. Silakan login kembali.")
            if not claims.sads_courses_verified:
                raise _auth_exception("Sesi belum memverifikasi mata kuliah aktif. Silakan login kembali.")

    if http_request is not None:
        http_request.state.access_claims = claims
        http_request.state.access_token = bearer_token or effective_cookie
        http_request.state.impersonation = impersonation
    return user


def get_optional_current_user(
    authorization: Optional[str] = Header(default=None),
    plagiarism_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: Session = Depends(get_db),
    impersonation_session: Annotated[Optional[str], Cookie(alias=IMPERSONATION_COOKIE_NAME)] = None,
    http_request: Request = None,
) -> Optional[User]:
    # Optional only means anonymous requests are accepted.  Once a client sends
    # any credential, an invalid or conflicting credential must remain a 401.
    if authorization is None and plagiarism_session is None and impersonation_session is None:
        return None
    return get_current_user(
        authorization=authorization,
        plagiarism_session=plagiarism_session,
        db=db,
        impersonation_session=impersonation_session,
        http_request=http_request,
    )


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in ("admin", "super_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya administrator yang memiliki izin untuk operasi ini.",
        )
    return current_user


def require_original_admin(
    http_request: Request,
    authorization: Optional[str] = Header(default=None),
    plagiarism_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User:
    """Recovery requires the parent credential, never the student's overlay."""
    user = get_current_user(
        authorization=authorization, plagiarism_session=plagiarism_session, db=db,
        impersonation_session=None, http_request=http_request,
    )
    if user.role not in ("admin", "super_admin") or http_request.state.access_claims.impersonation_id:
        raise _auth_exception("Sesi admin asli diperlukan untuk kembali dari impersonasi.", status_code=403)
    return user


def seed_initial_users(db: Session) -> None:
    """Bootstrap only the requested admin, without source-controlled credentials.

    Existing accounts are left unchanged. An empty deployment can opt in by
    supplying BOOTSTRAP_ADMIN_PASSWORD; normal restarts never reset a password.
    """
    if db.query(User).filter(User.identifier == "cihuy").first():
        return
    bootstrap_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not bootstrap_password:
        return
    if len(bootstrap_password) < 12:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD harus minimal 12 karakter.")
    db.add(User(identifier="cihuy", password=password_hash.hash(bootstrap_password), role="admin"))
    db.commit()

@router.post("/login", response_model=LoginResponse)
def login(
    request: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
    http_request: Request = None,
):
    validate_auth_configuration()
    _set_no_store(response)
    user = None
    sads_profile = None
    used_sads_login = False
    sads_denial = None
    student_auth = StudentAuthSettings.from_environment()

    def verify_credentials() -> bool:
        nonlocal user, sads_profile, used_sads_login, sads_denial
        user = db.query(User).filter(User.identifier == request.identifier).first()
        # Campus credentials are never tried as a local administrator password.
        # When SADS is selected, students have no local-password fallback.
        is_local_admin = bool(user and user.role in ("admin", "super_admin") and user.auth_provider == "local")
        if student_auth.provider == "sads" and not is_local_admin:
            if user and (not user.is_active or user.role != "mahasiswa"):
                return False
            used_sads_login = True
            try:
                sads_profile = SadsAuthService().authenticate(request.identifier, request.password)
            except HTTPException as error:
                if error.status_code not in (403, 422):
                    raise
                # A structured rejection is still a failed login and retains
                # account throttling, but does not claim the password is wrong.
                sads_denial = error
                return False
            return sads_profile is not None

        stored_hash = user.password if user else _DUMMY_PASSWORD_HASH
        try:
            valid = password_hash.verify(request.password, stored_hash)
        except (UnknownHashError, ValueError):
            # Malformed/legacy plaintext records need an explicit migration or
            # reset; they must never become an alternate password mechanism.
            password_hash.verify(request.password, _DUMMY_PASSWORD_HASH)
            valid = False
        return bool(user and user.is_active and user.auth_provider == "local" and user.role in SUPPORTED_USER_ROLES and valid)

    throttle = LoginThrottleService()
    if not throttle.verify(db, request.identifier, throttle.client_ip(http_request), verify_credentials):
        if sads_denial is not None:
            raise sads_denial
        if used_sads_login:
            raise _auth_exception(
                "SADS menolak autentikasi (HTTP 401). "
                "Periksa NIM, kata sandi, dan header integrasi yang diwajibkan SADS."
            )
        raise _auth_exception("Identifier atau kata sandi tidak valid.")

    if sads_profile is not None:
        # Enrollment is an authorization check, separate from password failures.
        # Verify before provisioning a profile or issuing/rotating a session.
        try:
            SadsActiveCoursesService().require_eligible_student(sads_profile.username)
        except HTTPException as error:
            if error.status_code == 403 and user is not None and user.auth_provider == "sads":
                # A definitive loss of eligibility also revokes a previous grant.
                _rotate_session_version(db, user)
            raise
        user = SadsAuthService.provision_student(db, sads_profile, password_hash.hash)

    # Login is a session boundary: revoke all old Bearer and cookie JWTs for
    # this account before minting the new pair.
    _rotate_session_version(db, user)
    token = create_access_token(user, sads_courses_verified=sads_profile is not None)
    _clear_session_cookie(response, cookie_name=IMPERSONATION_COOKIE_NAME)
    _set_session_cookie(response, token)

    return {
        "token": token,
        "user": _build_user_response(user),
    }


@router.post("/impersonation")
def start_admin_impersonation(
    request: ImpersonationRequest,
    http_request: Request,
    response: Response,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _set_no_store(response)
    claims = http_request.state.access_claims
    target, session = impersonation_service.start_impersonation(
        db, admin, request.nim, actor_claims=claims,
        minutes=IMPERSONATION_EXPIRE_MINUTES, hash_password=password_hash.hash,
    )
    token = create_access_token(
        target, sads_courses_verified=True, impersonation_id=session.id,
        expires_at=session.expires_at.replace(tzinfo=timezone.utc),
    )
    # Keep the overlay cookie until the parent expires. After the shorter JWT
    # expires, requests must fail instead of silently reverting to admin rights.
    remaining = max(0, int((claims.expires_at - datetime.now(timezone.utc)).total_seconds()))
    _set_session_cookie(response, token, cookie_name=IMPERSONATION_COOKIE_NAME, max_age=remaining)
    return {
        "token": token,
        "user": _build_user_response(target),
        "impersonation": impersonation_service.impersonation_data(session),
    }


@router.get("/impersonation")
def get_impersonation_status(
    http_request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
):
    _set_no_store(response)
    session = http_request.state.impersonation
    return impersonation_service.impersonation_data(session) if session else {"active": False}


@router.post("/impersonation/stop")
def stop_admin_impersonation(
    request: StopImpersonationRequest,
    http_request: Request,
    response: Response,
    admin: User = Depends(require_original_admin),
    db: Session = Depends(get_db),
):
    _set_no_store(response)
    claims = http_request.state.access_claims
    impersonation_service.stop_impersonation(
        db, admin, request.session_id, session_version=claims.session_version,
    )
    # Return the existing credential without extending the admin session.
    token = http_request.state.access_token
    remaining = max(0, int((claims.expires_at - datetime.now(timezone.utc)).total_seconds()))
    _clear_session_cookie(response, cookie_name=IMPERSONATION_COOKIE_NAME)
    _set_session_cookie(response, token, max_age=remaining)
    return {"token": token, "user": _build_user_response(admin), "impersonation": {"active": False}}


@router.get("/me", response_model=UserResponse)
def get_me(response: Response, current_user: User = Depends(get_current_user)):
    _set_no_store(response)
    return _build_user_response(current_user)


@router.post("/logout")
def logout(
    response: Response,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _set_no_store(response)
    session = http_request.state.impersonation
    if session is not None:
        impersonation_service.end_impersonation(db, session.id, "logout")
        _clear_session_cookie(response, cookie_name=IMPERSONATION_COOKIE_NAME)
        return {"message": "Sesi impersonasi dihentikan."}
    _rotate_session_version(db, current_user)
    _clear_session_cookie(response, cookie_name=IMPERSONATION_COOKIE_NAME)
    _clear_session_cookie(response)
    return {"message": "Berhasil keluar dari sesi."}


@router.get("/users", response_model=List[UserResponse])
def list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Admin Only: Ambil seluruh daftar pengguna dengan profil flat tanpa nested null"""
    users = db.query(User).filter(
        User.is_active.is_(True), User.role.in_(SUPPORTED_USER_ROLES)
    ).order_by(User.id.desc()).all()
    return [_build_user_response(u) for u in users]


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    request: CreateUserRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin Only: Buat akun mahasiswa atau admin."""
    if db.query(User).filter(User.identifier == request.identifier).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Pengguna dengan identifier '{request.identifier}' sudah terdaftar.",
        )

    new_user = User(
        identifier=request.identifier,
        password=password_hash.hash(request.password),
        role=request.role,
        nama_lengkap=request.nama_lengkap or request.identifier,
    )
    db.add(new_user)
    if request.role == "mahasiswa":
        new_user.program_studi = request.program_studi or "Teknik Informatika"
        new_user.fakultas = request.fakultas or "Fakultas Ilmu Komputer"

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
    """Admin Only: Perbarui akun pengguna dan profil mahasiswa."""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user or target_user.role not in SUPPORTED_USER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pengguna tidak ditemukan.",
        )

    if target_user.auth_provider == "sads" and (
        (request.identifier and request.identifier != target_user.identifier)
        or request.password is not None
    ):
        raise HTTPException(
            status_code=400,
            detail="NIM dan kata sandi akun SADS dikelola melalui SADS.",
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
    if nama:
        target_user.nama_lengkap = nama
    if target_user.role == "mahasiswa":
        if request.program_studi or target_user.program_studi is None:
            target_user.program_studi = request.program_studi or "Teknik Informatika"
        if request.fakultas or target_user.fakultas is None:
            target_user.fakultas = request.fakultas or "Fakultas Ilmu Komputer"

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

    nama = target_user.name
    db.delete(target_user)
    db.commit()
    return {"message": f"Pengguna '{nama}' berhasil dihapus."}
