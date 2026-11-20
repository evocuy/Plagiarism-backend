"""Opaque rotating refresh tokens stored only as digests in PostgreSQL."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.schemas import AuthRefreshSession, AuthRefreshToken, User, SUPPORTED_USER_ROLES, utc_now


REFRESH_SESSION_DAYS = 3


class RefreshTokenError(Exception):
    def __init__(self, code: str = "refresh_token_invalid", detail: str = "Sesi tersimpan tidak berlaku. Silakan login kembali.", *, status_code: int = 401, retry_after: int | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass
class RefreshGrant:
    session: AuthRefreshSession
    token: AuthRefreshToken
    user: User


def auth_context_hash(user: User) -> str:
    context = json.dumps([user.identifier, user.role, user.auth_provider, user.password], separators=(",", ":"))
    return hashlib.sha256(context.encode()).hexdigest()


def revoke_user_sessions(db: Session, user_id: int, reason: str) -> None:
    db.execute(update(AuthRefreshSession).where(
        AuthRefreshSession.user_id == user_id, AuthRefreshSession.revoked_at.is_(None),
    ).values(revoked_at=utc_now(), revocation_reason=reason))


def revoke_grant(db: Session, grant: RefreshGrant, reason: str) -> None:
    """Commit a definitive revocation before returning an authentication failure.

    A stale token from an earlier login must not log out a newer session.
    Callers hold the user row lock acquired by resolve_grant.
    """
    if grant.user.session_version == grant.session.session_version:
        grant.user.session_version += 1
        revoke_user_sessions(db, grant.user.id, reason)
    db.commit()


def purge_expired_sessions(db: Session, *, limit: int = 1000) -> None:
    """Bounded housekeeping before acquiring account locks; PostgreSQL cascades tokens."""
    identifiers = [row[0] for row in db.query(AuthRefreshSession.id).filter(
        AuthRefreshSession.expires_at <= utc_now(),
    ).order_by(AuthRefreshSession.expires_at).limit(limit).with_for_update(skip_locked=True).all()]
    if identifiers:
        # Explicit child removal also keeps isolated unit-test databases tidy.
        db.query(AuthRefreshToken).filter(AuthRefreshToken.session_id.in_(identifiers)).delete(synchronize_session=False)
        db.query(AuthRefreshSession).filter(AuthRefreshSession.id.in_(identifiers)).delete(synchronize_session=False)
    db.commit()


def _new_token(db: Session, session: AuthRefreshSession) -> str:
    raw = session.id + "." + secrets.token_urlsafe(32)
    db.add(AuthRefreshToken(token_hash=hashlib.sha256(raw.encode()).hexdigest(), session_id=session.id))
    db.flush()
    return raw


def issue_session(
    db: Session, user: User, *, session_mode: str | None, sads_courses_verified: bool,
    expires_at: datetime | None = None,
) -> tuple[AuthRefreshSession, str]:
    created_at = utc_now()
    deadline = created_at + timedelta(days=REFRESH_SESSION_DAYS)
    session = AuthRefreshSession(
        id=uuid4().hex, user_id=user.id, session_version=user.session_version,
        auth_context_hash=auth_context_hash(user), session_mode=session_mode,
        sads_courses_verified=sads_courses_verified, created_at=created_at,
        expires_at=min(expires_at, deadline) if expires_at is not None else deadline,
    )
    db.add(session)
    db.flush()
    return session, _new_token(db, session)


def resolve_grant(db: Session, raw: str | None, *, allow_used: bool = False) -> RefreshGrant:
    if not raw or not re.fullmatch(r"[0-9a-f]{32}\.[A-Za-z0-9_-]{43}", raw):
        raise RefreshTokenError()
    digest = hashlib.sha256(raw.encode()).hexdigest()
    # Verify the complete digest before trusting the public session selector.
    token = db.get(AuthRefreshToken, digest)
    if token is None or token.session_id != raw[:32]:
        raise RefreshTokenError()
    session = db.get(AuthRefreshSession, token.session_id)
    if session is None:
        raise RefreshTokenError()
    # Account first, then session and token: login/logout/rotation share this order.
    user = db.query(User).populate_existing().filter(User.id == session.user_id).with_for_update().first()
    session = db.query(AuthRefreshSession).populate_existing().filter(
        AuthRefreshSession.id == token.session_id,
    ).with_for_update().first()
    token = db.query(AuthRefreshToken).populate_existing().filter(
        AuthRefreshToken.token_hash == digest,
    ).with_for_update().first()
    if user is None or session is None or token is None:
        raise RefreshTokenError()
    grant = RefreshGrant(session, token, user)
    if session.revoked_at is not None or user.session_version != session.session_version:
        raise RefreshTokenError()
    if (not user.is_active or user.role not in SUPPORTED_USER_ROLES
            or session.auth_context_hash != auth_context_hash(user)):
        revoke_grant(db, grant, "account_changed")
        raise RefreshTokenError()
    if session.expires_at <= utc_now():
        revoke_grant(db, grant, "expired")
        raise RefreshTokenError("refresh_token_expired", "Sesi login sudah kedaluwarsa. Silakan login kembali.")
    if token.used_at is not None and not allow_used:
        revoke_grant(db, grant, "token_reused")
        raise RefreshTokenError("refresh_token_reused", "Sesi dicabut karena refresh token sudah pernah digunakan. Silakan login kembali.")
    return grant


def enforce_rotation_interval(grant: RefreshGrant, minimum_interval_seconds: int) -> None:
    now = utc_now()
    if grant.session.last_rotated_at is not None:
        remaining = minimum_interval_seconds - (now - grant.session.last_rotated_at).total_seconds()
        if remaining > 0:
            raise RefreshTokenError("refresh_too_frequent", "Sesi baru saja diperbarui. Coba lagi setelah jeda yang diberikan.",
                                    status_code=429, retry_after=math.ceil(remaining))


def rotate_grant(db: Session, grant: RefreshGrant, *, minimum_interval_seconds: int = 0) -> str:
    """Caller owns the account lock and commits new token plus access grant together."""
    if grant.token.used_at is not None:
        raise RefreshTokenError()
    enforce_rotation_interval(grant, minimum_interval_seconds)
    now = utc_now()
    grant.token.used_at = now
    grant.session.last_rotated_at = now
    return _new_token(db, grant.session)


def bind_impersonation(db: Session, user_id: int, version: int, session_id: str | None, *, previous_id: str | None = None) -> None:
    statement = update(AuthRefreshSession).where(
        AuthRefreshSession.user_id == user_id, AuthRefreshSession.session_version == version,
        AuthRefreshSession.revoked_at.is_(None),
    )
    if previous_id is not None:
        statement = statement.where(AuthRefreshSession.impersonation_session_id == previous_id)
    db.execute(statement.values(impersonation_session_id=session_id))
