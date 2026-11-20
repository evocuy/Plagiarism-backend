import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional, List

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Header, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.schemas import User, AuthRefreshSession, SUPPORTED_USER_ROLES, utc_now
from app.services.login_throttle_service import LoginThrottleService
from app.services.sads_auth_service import SadsAuthService, SadsLoginSettings, StudentAuthSettings
from app.services.sads_course_service import SadsActiveCoursesService
from app.services import impersonation_service
from app.services.csrf_service import create_csrf_token
from app.services.account_mode_service import (
    LoginModeUser,
    account_user,
    can_choose_login_mode,
    is_sads_tester,
    user_for_login_mode,
)
from app.services import refresh_token_service as refresh_tokens

router = APIRouter()

# Never use a source-controlled fallback here.  A missing or obviously
# placeholder secret must prevent issuing/accepting sessions instead of making
# every deployment share the same signing key.
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = "HS256"
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "plagiarism_session")
IMPERSONATION_COOKIE_NAME = SESSION_COOKIE_NAME + "_impersonation"
REFRESH_COOKIE_NAME = SESSION_COOKIE_NAME + "_refresh"
REFRESH_COOKIE_PATH = "/api/auth"
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
REMEMBER_ACCESS_EXPIRE_MINUTES = _read_positive_int_setting("REMEMBER_ACCESS_EXPIRE_MINUTES", 15)
if REMEMBER_ACCESS_EXPIRE_MINUTES > 60:
    raise RuntimeError("REMEMBER_ACCESS_EXPIRE_MINUTES maksimal 60 menit.")
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


def _set_refresh_cookie(response: Response, raw: str, session: AuthRefreshSession) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME, value=raw, httponly=True, secure=SESSION_COOKIE_SECURE,
        samesite=SESSION_COOKIE_SAMESITE, domain=SESSION_COOKIE_DOMAIN, path=REFRESH_COOKIE_PATH,
        max_age=max(0, int((session.expires_at - utc_now()).total_seconds())),
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH, domain=SESSION_COOKIE_DOMAIN,
        secure=SESSION_COOKIE_SECURE, httponly=True, samesite=SESSION_COOKIE_SAMESITE,
    )


def _remember_access_expiry(session: AuthRefreshSession) -> datetime:
    return min(
        datetime.now(timezone.utc) + timedelta(minutes=min(JWT_EXPIRE_MINUTES, REMEMBER_ACCESS_EXPIRE_MINUTES)),
        session.expires_at.replace(tzinfo=timezone.utc),
    )


def _auth_response(user, token: str, expires_at: datetime, *, session=None, refresh_raw=None) -> dict:
    return {
        "token": token, "user": _build_user_response(user),
        "csrf_token": create_csrf_token(token, _jwt_secret_key()),
        "expires_in": max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds())),
        "remember_me": session is not None,
        "refresh_expires_at": session.expires_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if session is not None else None,
        "refresh_csrf_token": create_csrf_token(refresh_raw, _jwt_secret_key()) if refresh_raw else None,
    }


def _refresh_error_response(error: refresh_tokens.RefreshTokenError) -> JSONResponse:
    response = JSONResponse({"detail": error.detail, "error_code": error.code}, status_code=error.status_code)
    _set_no_store(response)
    if error.status_code == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    if error.retry_after is not None:
        response.headers["Retry-After"] = str(error.retry_after)
    if error.status_code in (401, 403):
        _clear_refresh_cookie(response)
        _clear_session_cookie(response, cookie_name=IMPERSONATION_COOKIE_NAME)
        _clear_session_cookie(response)
    return response

# Pydantic Schemas
class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)
    login_as: Optional[Literal["admin", "mahasiswa"]] = None
    remember_me: bool = Field(
        default=True, strict=True, deprecated=True,
        description="Kompatibilitas klien lama. Setiap login berhasil menerbitkan sesi refresh.",
    )


class LoginModeRequest(BaseModel):
    login_as: Literal["admin", "mahasiswa"]


class ImpersonationRequest(BaseModel):
    nim: str = Field(min_length=1, max_length=100)
    impersonation_password: SecretStr = Field(min_length=1, max_length=256)

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
    available_login_modes: List[Literal["admin", "mahasiswa"]] = Field(default_factory=list)
    unlimited_daily_checks: bool = False

    class Config:
        from_attributes = True

def _build_user_response(user: User) -> dict:
    """Helper: konversi ORM User ke dictionary flat tanpa nested null."""
    can_choose_mode = can_choose_login_mode(user)
    res = {
        "id": user.id,
        "identifier": user.identifier,
        "role": user.role,
        "nama_lengkap": user.name,
        "name": user.name,
        "available_login_modes": (["admin", "mahasiswa"] if can_choose_mode
                                  else ["admin"] if user.role in ("admin", "super_admin") else ["mahasiswa"]),
        "unlimited_daily_checks": can_choose_mode,
    }
    if user.role == "mahasiswa":
        res["program_studi"] = user.program_studi
        res["fakultas"] = user.fakultas
    return res

class LoginResponse(BaseModel):
    token: str
    user: UserResponse
    csrf_token: str
    expires_in: int | None = None
    remember_me: bool = False
    refresh_expires_at: datetime | None = None
    refresh_csrf_token: str | None = None

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
    session_mode: str | None = None,
    refresh_session_id: str | None = None,
) -> str:
    if isinstance(user, LoginModeUser):
        if user.session_mode == "mahasiswa" and session_mode == "admin":
            raise ValueError("A student session cannot mint an administrator session")
        session_mode = session_mode or user.session_mode
        user = account_user(user)
    if session_mode is not None and (
        session_mode not in ("admin", "mahasiswa") or not can_choose_login_mode(user) or impersonation_id is not None
    ):
        raise ValueError("Login mode is unavailable for this account/session")
    secret = _jwt_secret_key()
    expires_at = expires_at or datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),
        "identifier": user.identifier,
        "role": user_for_login_mode(user, session_mode).role,
        "sv": user.session_version,
        "sads_courses_verified": sads_courses_verified,
        "exp": expires_at,
        "token_type": "access",
        "jti": secrets.token_hex(16),
    }
    if impersonation_id is not None:
        payload["impersonation_id"] = impersonation_id
    if session_mode is not None:
        payload["session_mode"] = session_mode
    if refresh_session_id is not None:
        payload["refresh_session_id"] = refresh_session_id
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
    session_mode: str | None = None
    refresh_session_id: str | None = None

    @property
    def session_identity(self) -> tuple:
        return self.user_id, self.session_version, self.sads_courses_verified, self.impersonation_id, self.session_mode, self.refresh_session_id


def _decode_access_token(token: str, *, allow_expired: bool = False) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            _jwt_secret_key(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "sub", "sv"], "verify_exp": not allow_expired},
        )
    except jwt.PyJWTError as exc:
        raise _auth_exception("Token autentikasi tidak valid atau telah kedaluwarsa.") from exc

    subject = payload.get("sub")
    if payload.get("token_type", "access") != "access":
        raise _auth_exception("Jenis token autentikasi tidak valid.")
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
    session_mode = payload.get("session_mode")
    refresh_session_id = payload.get("refresh_session_id")
    if "refresh_session_id" in payload and (
        not isinstance(refresh_session_id, str) or len(refresh_session_id) != 32
        or any(c not in "0123456789abcdef" for c in refresh_session_id)
        or impersonation_id is not None
    ):
        raise _auth_exception("Sesi tersimpan tidak valid.")
    if "session_mode" in payload and (
        not isinstance(session_mode, str) or session_mode not in ("admin", "mahasiswa")
        or impersonation_id is not None
        or (session_mode == "mahasiswa" and payload.get("role") != "mahasiswa")
        or (session_mode == "admin" and payload.get("role") not in ("admin", "super_admin"))
    ):
        raise _auth_exception("Mode sesi tidak valid.")
    try:
        expires_at = datetime.fromtimestamp(int(payload["exp"]), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise _auth_exception("Token autentikasi tidak valid.") from None
    return AccessTokenClaims(
        user_id, session_version, payload.get("sads_courses_verified") is True, expires_at, impersonation_id, session_mode, refresh_session_id,
    )


def _rotate_session_version(db: Session, user: User, *, commit: bool = True) -> None:
    """Invalidate every token minted before this point for one account."""
    user = account_user(user)
    db.execute(
        update(User)
        .where(User.id == user.id)
        .values(session_version=func.coalesce(User.session_version, 0) + 1)
    )
    refresh_tokens.revoke_user_sessions(db, user.id, "session_changed")
    db.flush()
    db.refresh(user)
    if commit:
        db.commit()


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
    if claims.refresh_session_id is not None:
        remembered = db.get(AuthRefreshSession, claims.refresh_session_id)
        if (remembered is None or remembered.revoked_at is not None or remembered.expires_at <= utc_now()
                or remembered.user_id != user.id or remembered.session_version != user.session_version
                or remembered.session_mode != claims.session_mode
                or remembered.auth_context_hash != refresh_tokens.auth_context_hash(user)):
            raise _auth_exception("Sesi tersimpan tidak berlaku. Silakan login kembali.")
    if claims.session_mode is not None and not can_choose_login_mode(user):
        raise _auth_exception("Mode sesi sudah tidak berlaku. Silakan login kembali.")

    impersonation = None
    if claims.impersonation_id is not None:
        if not claims.sads_courses_verified:
            raise _auth_exception("Sesi belum memverifikasi mata kuliah aktif.")
        impersonation = impersonation_service.resolve_impersonation(db, claims.impersonation_id, user)
    elif user.role == "mahasiswa" and claims.session_mode is None:
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
    return user_for_login_mode(user, claims.session_mode)


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
    try:
        user = get_current_user(
            authorization=authorization, plagiarism_session=plagiarism_session, db=db,
            impersonation_session=None, http_request=http_request,
        )
    except HTTPException as error:
        # Explicit stop can recover a remembered parent after access expiry.
        # A malformed/stale Bearer must never select a different credential.
        if error.status_code != 401 or authorization is not None:
            raise
        try:
            grant = refresh_tokens.resolve_grant(db, http_request.cookies.get(REFRESH_COOKIE_NAME))
            _check_refresh_access_cookie(http_request, grant)
        except refresh_tokens.RefreshTokenError as refresh_error:
            raise _auth_exception(refresh_error.detail) from None
        user = user_for_login_mode(grant.user, grant.session.session_mode)
        if user.role not in ("admin", "super_admin") or grant.session.impersonation_session_id is None:
            raise _auth_exception("Sesi admin asli diperlukan untuk kembali dari impersonasi.", status_code=403)
        http_request.state.refresh_recovery_grant = grant
        http_request.state.access_claims = AccessTokenClaims(
            grant.user.id, grant.user.session_version, grant.session.sads_courses_verified,
            _remember_access_expiry(grant.session), session_mode=grant.session.session_mode,
            refresh_session_id=grant.session.id,
        )
        http_request.state.access_token = None
    if user.role not in ("admin", "super_admin") or http_request.state.access_claims.impersonation_id:
        raise _auth_exception("Sesi admin asli diperlukan untuk kembali dari impersonasi.", status_code=403)
    return user


def _check_refresh_access_cookie(http_request: Request, grant: refresh_tokens.RefreshGrant) -> None:
    """An expired parent cookie is fine; a different principal/mode is not."""
    cookie = http_request.cookies.get(SESSION_COOKIE_NAME)
    if cookie is None:
        return
    try:
        claims = _decode_access_token(cookie, allow_expired=True)
    except HTTPException:
        raise refresh_tokens.RefreshTokenError() from None
    if (claims.user_id != grant.user.id or claims.session_version != grant.session.session_version
            or claims.refresh_session_id != grant.session.id or claims.session_mode != grant.session.session_mode
            or claims.impersonation_id is not None):
        raise refresh_tokens.RefreshTokenError()


def _check_refresh_eligibility(db: Session, grant: refresh_tokens.RefreshGrant) -> None:
    user, session = grant.user, grant.session
    if session.session_mode is not None:
        if not can_choose_login_mode(user):
            refresh_tokens.revoke_grant(db, grant, "mode_unavailable")
            raise refresh_tokens.RefreshTokenError()
        return
    if user.role != "mahasiswa":
        return
    settings = StudentAuthSettings.from_environment()
    if settings.provider == "sads" or user.auth_provider == "sads":
        if settings.provider != "sads" or user.auth_provider != "sads" or not session.sads_courses_verified:
            refresh_tokens.revoke_grant(db, grant, "provider_changed")
            raise refresh_tokens.RefreshTokenError()
        try:
            SadsActiveCoursesService().require_eligible_student(user.identifier)
        except HTTPException as error:
            if error.status_code == 403:
                refresh_tokens.revoke_grant(db, grant, "courses_ineligible")
                raise refresh_tokens.RefreshTokenError("refresh_access_denied", error.detail, status_code=403) from None
            # Temporary SADS errors do not consume/revoke the current token.
            raise


@router.post("/refresh", response_model=LoginResponse)
def refresh_session(http_request: Request, response: Response, db: Session = Depends(get_db)):
    """Renew a remembered login using its HttpOnly cookie and rotate once."""
    _set_no_store(response)
    if http_request.headers.get("Authorization") is not None:
        raise _auth_exception("Refresh memakai cookie sesi tersimpan; jangan sertakan header Authorization.")
    try:
        grant = refresh_tokens.resolve_grant(db, http_request.cookies.get(REFRESH_COOKIE_NAME))
        _check_refresh_access_cookie(http_request, grant)
        if (IMPERSONATION_COOKIE_NAME in http_request.cookies or grant.session.impersonation_session_id is not None):
            db.rollback()
            return JSONResponse({"detail": "Akhiri impersonasi secara eksplisit sebelum memperbarui sesi admin.",
                                 "error_code": "impersonation_active"}, status_code=403,
                                headers={"Cache-Control": AUTH_NO_STORE_VALUE, "Pragma": "no-cache"})
        interval = min(60, min(JWT_EXPIRE_MINUTES, REMEMBER_ACCESS_EXPIRE_MINUTES) * 30)
        refresh_tokens.enforce_rotation_interval(grant, interval)
        _check_refresh_eligibility(db, grant)
        raw = refresh_tokens.rotate_grant(db, grant, minimum_interval_seconds=interval)
        expires_at = _remember_access_expiry(grant.session)
        token = create_access_token(
            grant.user, session_mode=grant.session.session_mode,
            sads_courses_verified=grant.session.sads_courses_verified,
            refresh_session_id=grant.session.id, expires_at=expires_at,
        )
        db.commit()
        _set_refresh_cookie(response, raw, grant.session)
        _set_session_cookie(response, token, max_age=max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds())))
        effective = user_for_login_mode(grant.user, grant.session.session_mode)
        return _auth_response(effective, token, expires_at, session=grant.session, refresh_raw=raw)
    except refresh_tokens.RefreshTokenError as error:
        db.rollback()
        return _refresh_error_response(error)
    except HTTPException:
        db.rollback()
        raise


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

    refresh_tokens.purge_expired_sessions(db)
    sads_courses_verified = False
    if sads_profile is not None:
        # Enrollment is an authorization check, separate from password failures.
        # Verify before provisioning a profile or issuing/rotating a session.
        if not is_sads_tester(sads_profile.username):
            try:
                SadsActiveCoursesService().require_eligible_student(sads_profile.username)
            except HTTPException as error:
                if error.status_code == 403 and user is not None and user.auth_provider == "sads":
                    # A definitive loss of eligibility also revokes a previous grant.
                    _rotate_session_version(db, user)
                raise
            sads_courses_verified = True
        user = SadsAuthService.provision_student(db, sads_profile, password_hash.hash)

    if request.login_as is not None:
        expected_mode = "admin" if user.role in ("admin", "super_admin") else "mahasiswa"
        if request.login_as != expected_mode and not can_choose_login_mode(user):
            raise _auth_exception("Pilihan mode login tidak tersedia untuk akun ini.", status_code=403)
    session_mode = (request.login_as or "admin") if can_choose_login_mode(user) else None

    # Login is a session boundary: revoke all old Bearer and cookie JWTs for
    # this account before minting the new pair.
    _rotate_session_version(db, user, commit=False)
    remembered, raw = refresh_tokens.issue_session(
        db, user, session_mode=session_mode, sads_courses_verified=sads_courses_verified,
    )
    expires_at = _remember_access_expiry(remembered)
    token = create_access_token(user, sads_courses_verified=sads_courses_verified, session_mode=session_mode,
                                expires_at=expires_at, refresh_session_id=remembered.id)
    db.commit()
    _clear_session_cookie(response, cookie_name=IMPERSONATION_COOKIE_NAME)
    _set_refresh_cookie(response, raw, remembered)
    _set_session_cookie(response, token, max_age=max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds())))

    effective = user_for_login_mode(user, session_mode)
    return _auth_response(effective, token, expires_at, session=remembered, refresh_raw=raw)


@router.post("/session-mode", response_model=LoginResponse)
def select_login_mode(
    request: LoginModeRequest, http_request: Request, response: Response,
    admin: User = Depends(require_admin), db: Session = Depends(get_db),
):
    """Choose a mode after admin login; returning from student mode requires login again."""
    _set_no_store(response)
    if not can_choose_login_mode(admin):
        raise _auth_exception("Pilihan mode login tidak tersedia untuk akun ini.", status_code=403)
    expires_at = http_request.state.access_claims.expires_at
    previous = db.get(AuthRefreshSession, http_request.state.access_claims.refresh_session_id) if http_request.state.access_claims.refresh_session_id else None
    _rotate_session_version(db, admin, commit=False)
    remembered, raw = None, None
    if previous is not None:
        remembered, raw = refresh_tokens.issue_session(db, account_user(admin), expires_at=previous.expires_at,
                                                      session_mode=request.login_as, sads_courses_verified=False)
        expires_at = min(expires_at, remembered.expires_at.replace(tzinfo=timezone.utc))
    token = create_access_token(admin, session_mode=request.login_as, expires_at=expires_at,
                                refresh_session_id=remembered.id if remembered else None)
    db.commit()
    remaining = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    _clear_session_cookie(response, cookie_name=IMPERSONATION_COOKIE_NAME)
    if remembered:
        _set_refresh_cookie(response, raw, remembered)
    else:
        _clear_refresh_cookie(response)
    _set_session_cookie(response, token, max_age=remaining)
    effective = user_for_login_mode(admin, request.login_as)
    return _auth_response(effective, token, expires_at, session=remembered, refresh_raw=raw)


@router.post("/impersonation")
def start_admin_impersonation(
    request: ImpersonationRequest,
    http_request: Request,
    response: Response,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _set_no_store(response)
    impersonation_service.verify_start_password(
        db, admin, request.impersonation_password.get_secret_value(), http_request,
        verify_password=password_hash.verify,
    )
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
        "csrf_token": create_csrf_token(token, _jwt_secret_key()),
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
    recovery = getattr(http_request.state, "refresh_recovery_grant", None)
    remembered = db.get(AuthRefreshSession, claims.refresh_session_id) if claims.refresh_session_id else None
    if (remembered is not None and remembered.impersonation_session_id is not None
            and remembered.impersonation_session_id != request.session_id):
        raise _auth_exception("Akhiri sesi impersonasi yang sedang aktif.", status_code=403)
    impersonation_service.stop_impersonation(
        db, admin, request.session_id, session_version=claims.session_version, session_mode=claims.session_mode,
        commit=recovery is None,
    )
    token = http_request.state.access_token
    expires_at = claims.expires_at
    raw = http_request.cookies.get(REFRESH_COOKIE_NAME)
    if recovery is not None:
        raw = refresh_tokens.rotate_grant(db, recovery)
        expires_at = _remember_access_expiry(recovery.session)
        token = create_access_token(admin, session_mode=recovery.session.session_mode,
                                    refresh_session_id=recovery.session.id, expires_at=expires_at)
        db.commit()
        _set_refresh_cookie(response, raw, recovery.session)
    # Legacy parent sessions are returned without extending their expiry.
    remaining = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    _clear_session_cookie(response, cookie_name=IMPERSONATION_COOKIE_NAME)
    _set_session_cookie(response, token, max_age=remaining)
    return {**_auth_response(admin, token, expires_at, session=remembered, refresh_raw=raw if remembered else None),
            "impersonation": {"active": False}}


@router.get("/csrf")
def get_csrf_token(http_request: Request, response: Response):
    """Bootstrap protection, including an expired overlay when returning to admin.

    This endpoint does not authenticate or renew a session. Normal route
    dependencies still validate JWTs; stopping impersonation validates the
    original admin credential independently of the overlay.
    """
    _set_no_store(response)
    response.headers["X-Content-Type-Options"] = "nosniff"
    cookies = http_request.cookies
    token = cookies.get(IMPERSONATION_COOKIE_NAME) if IMPERSONATION_COOKIE_NAME in cookies else cookies.get(SESSION_COOKIE_NAME)
    result = {"csrf_token": create_csrf_token(token, _jwt_secret_key()) if token is not None else None}
    refresh = cookies.get(REFRESH_COOKIE_NAME)
    if refresh is not None:
        result["refresh_csrf_token"] = create_csrf_token(refresh, _jwt_secret_key())
    return result


@router.get("/me", response_model=UserResponse)
def get_me(response: Response, current_user: User = Depends(get_current_user)):
    _set_no_store(response)
    return _build_user_response(current_user)


@router.post("/logout")
def logout(
    response: Response,
    http_request: Request,
    db: Session = Depends(get_db),
):
    _set_no_store(response)
    try:
        current_user = get_current_user(
            authorization=http_request.headers.get("Authorization"),
            plagiarism_session=http_request.cookies.get(SESSION_COOKIE_NAME), db=db,
            impersonation_session=http_request.cookies.get(IMPERSONATION_COOKIE_NAME), http_request=http_request,
        )
    except HTTPException as error:
        if error.status_code != 401 or http_request.headers.get("Authorization") is not None:
            raise
        if REFRESH_COOKIE_NAME not in http_request.cookies:
            raise
        try:
            grant = refresh_tokens.resolve_grant(db, http_request.cookies[REFRESH_COOKIE_NAME], allow_used=True)
            _check_refresh_access_cookie(http_request, grant)
            current_user = grant.user
        except refresh_tokens.RefreshTokenError as refresh_error:
            db.rollback()
            return _refresh_error_response(refresh_error)
    session = getattr(http_request.state, "impersonation", None)
    if session is not None:
        impersonation_service.end_impersonation(db, session.id, "logout")
        _clear_session_cookie(response, cookie_name=IMPERSONATION_COOKIE_NAME)
        return {"message": "Sesi impersonasi dihentikan."}
    _rotate_session_version(db, current_user)
    _clear_refresh_cookie(response)
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
        refresh_tokens.revoke_user_sessions(db, target_user.id, "password_changed")
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
