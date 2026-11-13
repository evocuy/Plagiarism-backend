"""Short-lived admin-to-student sessions with independent revocation."""
import os
from datetime import timedelta, timezone
from uuid import uuid4

from pwdlib.exceptions import UnknownHashError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.schemas import ImpersonationSession, User, utc_now
from app.services.login_throttle_service import LoginThrottleService
from app.services.sads_auth_service import SadsAuthService, auth_error
from app.services.sads_course_service import SadsActiveCoursesService


ADMIN_ROLES = ("admin", "super_admin")


def verify_start_password(db: Session, admin: User, password: str, request, *, verify_password) -> None:
    """Check a separate private credential before contacting SADS or changing sessions."""
    configured_hash = os.getenv("IMPERSONATION_PASSWORD_HASH", "").strip()
    if not configured_hash:
        raise auth_error(503, "Password impersonasi belum dikonfigurasi. Hubungi pengelola backend.")

    def verify() -> bool:
        try:
            return verify_password(password, configured_hash)
        except (UnknownHashError, ValueError):
            raise auth_error(503, "Konfigurasi password impersonasi tidak valid. Hubungi pengelola backend.") from None

    throttle = LoginThrottleService()
    if not throttle.verify(db, str(admin.id), throttle.client_ip(request), verify, purpose="impersonation"):
        raise auth_error(403, "Password impersonasi tidak valid.")


def _student_is_available(user: User | None) -> None:
    if user is not None and (user.role != "mahasiswa" or not user.is_active):
        raise auth_error(403, "Impersonasi hanya tersedia untuk akun mahasiswa aktif.")


def _lock_actor(db: Session, actor_id: int, session_version: int) -> User:
    actor = db.query(User).populate_existing().filter(User.id == actor_id).with_for_update().first()
    if (
        actor is None or not actor.is_active or actor.role not in ADMIN_ROLES
        or actor.session_version != session_version
    ):
        raise auth_error(401, "Sesi admin sudah tidak berlaku. Silakan login kembali.")
    return actor


def _commit(db: Session) -> None:
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise auth_error(503, "Impersonasi sementara tidak tersedia. Silakan coba kembali.") from None


def start_impersonation(db: Session, admin: User, nim: str, *, actor_claims, minutes: int, hash_password):
    target = db.query(User).filter(User.identifier == nim).first()
    _student_is_available(target)
    # The admin supplies no student password. Campus identity and eligibility
    # come from the active-courses API, even for an existing local account.
    profile = SadsActiveCoursesService().get_eligible_student_profile(nim)
    actor = _lock_actor(db, admin.id, actor_claims.session_version)
    now = utc_now()
    parent_expiry = actor_claims.expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    if parent_expiry <= now:
        raise auth_error(401, "Sesi admin sudah kedaluwarsa. Silakan login kembali.")
    target = db.query(User).populate_existing().filter(User.identifier == nim).with_for_update().first()
    _student_is_available(target)
    if target is None:
        # Only first-time students are provisioned; existing passwords, profile,
        # provider and session version remain untouched by support sessions.
        target = SadsAuthService.provision_student(db, profile, hash_password)
    db.query(ImpersonationSession).filter(
        ImpersonationSession.actor_user_id == actor.id,
        ImpersonationSession.ended_at.is_(None),
    ).update({"ended_at": now, "end_reason": "replaced"}, synchronize_session=False)
    session = ImpersonationSession(
        id=uuid4().hex, actor_user_id=actor.id, target_user_id=target.id,
        actor_identifier=actor.identifier, target_identifier=target.identifier,
        actor_session_version=actor.session_version, created_at=now,
        expires_at=min(now + timedelta(minutes=minutes), parent_expiry),
    )
    db.add(session)
    _commit(db)
    return target, session


def resolve_impersonation(db: Session, session_id: str, target: User) -> ImpersonationSession:
    session = db.get(ImpersonationSession, session_id)
    if (
        session is None or session.ended_at is not None or session.expires_at <= utc_now()
        or session.target_user_id != target.id or target.role != "mahasiswa"
    ):
        raise auth_error(401, "Sesi impersonasi sudah tidak berlaku. Kembali ke akun admin.")
    actor = db.get(User, session.actor_user_id) if session.actor_user_id is not None else None
    if (
        actor is None or not actor.is_active or actor.role not in ADMIN_ROLES
        or actor.session_version != session.actor_session_version
    ):
        raise auth_error(401, "Sesi admin pemilik impersonasi sudah tidak berlaku.")
    return session


def stop_impersonation(db: Session, admin: User, session_id: str, *, session_version: int):
    actor = _lock_actor(db, admin.id, session_version)
    session = db.query(ImpersonationSession).filter(
        ImpersonationSession.id == session_id,
        ImpersonationSession.actor_user_id == actor.id,
        ImpersonationSession.actor_session_version == session_version,
    ).with_for_update().first()
    if session is None:
        raise auth_error(404, "Sesi impersonasi tidak ditemukan.")
    if session.ended_at is None:
        session.ended_at = utc_now()
        session.end_reason = "expired" if session.expires_at <= session.ended_at else "stopped"
        _commit(db)
    return session


def end_impersonation(db: Session, session_id: str, reason: str) -> None:
    session = db.get(ImpersonationSession, session_id)
    if session is not None and session.ended_at is None:
        session.ended_at = utc_now()
        session.end_reason = reason
        _commit(db)


def impersonation_data(session: ImpersonationSession) -> dict:
    return {
        "active": True,
        "session_id": session.id,
        "actor": {"id": session.actor_user_id, "identifier": session.actor_identifier},
        "target": {"id": session.target_user_id, "identifier": session.target_identifier},
        "started_at": session.created_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at": session.expires_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
    }
