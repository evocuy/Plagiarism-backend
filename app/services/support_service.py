"""Ticket operations, access control, and atomic per-account rate limits."""

import math
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.schemas import SupportMessage, SupportTicket, User, utc_now

TICKET_LIMIT = 3
TICKET_WINDOW = timedelta(hours=24)
TICKET_COOLDOWN = timedelta(minutes=1)
MESSAGE_COOLDOWN = timedelta(seconds=10)
ADMIN_ROLES = ("admin", "super_admin")


def _utc_string(value: datetime | None) -> str | None:
    if value is None:
        return None
    # The project's database stores naive UTC timestamps; mark them explicitly
    # so browsers do not interpret them in the local timezone.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ticket_data(ticket: SupportTicket) -> dict:
    return {
        "id": ticket.id,
        "ticket_number": f"TKT-{ticket.id:06d}",
        "category": ticket.category,
        "subject": ticket.subject,
        "status": ticket.status,
        "owner_name": ticket.owner_name,
        "owner_identifier": ticket.owner_identifier,
        "created_at": _utc_string(ticket.created_at),
        "updated_at": _utc_string(ticket.updated_at),
    }


def _message_data(message: SupportMessage) -> dict:
    return {
        "id": message.id,
        "author_name": message.author_name,
        "author_role": message.author_role,
        "message": message.message,
        "created_at": _utc_string(message.created_at),
    }


def _detail(db: Session, ticket: SupportTicket) -> dict:
    messages = (
        db.query(SupportMessage)
        .filter(SupportMessage.ticket_id == ticket.id)
        .order_by(SupportMessage.created_at.asc(), SupportMessage.id.asc())
        .all()
    )
    return {"ticket": _ticket_data(ticket), "messages": [_message_data(message) for message in messages]}


def _require_supported_user(user: User) -> None:
    if user.role != "mahasiswa" and user.role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Pusat bantuan hanya tersedia untuk mahasiswa dan admin.")


def _lock_user(db: Session, user: User) -> User:
    # PostgreSQL holds this row lock until commit/rollback. Requests from the
    # same account therefore check the quota and write as one atomic operation,
    # even when they reach different application workers simultaneously.
    locked = (
        db.query(User)
        .populate_existing()
        .filter(User.id == user.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        raise HTTPException(status_code=401, detail="Pengguna tidak ditemukan. Silakan masuk kembali.")
    return locked


def _visible_ticket(db: Session, user: User, ticket_id: int, *, lock: bool = False) -> SupportTicket:
    _require_supported_user(user)
    query = db.query(SupportTicket).filter(SupportTicket.id == ticket_id)
    if user.role == "mahasiswa":
        query = query.filter(SupportTicket.owner_id == user.id)
    if lock:
        query = query.with_for_update()
    ticket = query.first()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Tiket bantuan tidak ditemukan.")
    return ticket


def _quota(db: Session, user_id: int, now: datetime) -> dict:
    timestamps = [
        row[0]
        for row in (
            db.query(SupportTicket.created_at)
            .filter(SupportTicket.owner_id == user_id, SupportTicket.created_at > now - TICKET_WINDOW)
            .order_by(SupportTicket.created_at.asc(), SupportTicket.id.asc())
            .all()
        )
    ]
    remaining = max(0, TICKET_LIMIT - len(timestamps))
    # If historical data exceeds the limit, wait until enough tickets have
    # expired from the window, not merely until the first one expires.
    next_available = timestamps[len(timestamps) - TICKET_LIMIT] + TICKET_WINDOW if not remaining else None
    cooldown = timestamps[-1] + TICKET_COOLDOWN if timestamps else None
    if cooldown is not None and cooldown <= now:
        cooldown = None
    return {
        "limit": TICKET_LIMIT,
        "remaining": remaining,
        "next_available_at": next_available,
        "cooldown_until": cooldown,
    }


def _quota_data(quota: dict) -> dict:
    return {
        **quota,
        "next_available_at": _utc_string(quota["next_available_at"]),
        "cooldown_until": _utc_string(quota["cooldown_until"]),
    }


def _rate_limit(until: datetime, now: datetime, detail: str) -> None:
    retry_after = max(1, math.ceil((until - now).total_seconds()))
    raise HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(retry_after)})


def list_tickets(db: Session, current_user: User) -> dict:
    _require_supported_user(current_user)
    query = db.query(SupportTicket)
    if current_user.role == "mahasiswa":
        query = query.filter(SupportTicket.owner_id == current_user.id)
        quota = _quota_data(_quota(db, current_user.id, utc_now()))
    else:
        quota = None
    tickets = query.order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc()).all()
    return {"tickets": [_ticket_data(ticket) for ticket in tickets], "quota": quota}


def create_ticket(db: Session, current_user: User, category: str, subject: str, message: str) -> dict:
    user = _lock_user(db, current_user)
    if user.role != "mahasiswa":
        raise HTTPException(status_code=403, detail="Hanya mahasiswa yang dapat membuat tiket bantuan.")
    # Read the clock *after* acquiring the lock so waiting requests cannot use
    # a stale time to bypass the cooldown or produce misleading retry times.
    now = utc_now()
    quota = _quota(db, user.id, now)
    if quota["next_available_at"] is not None:
        # When both limits apply, wait until both have expired before retrying.
        retry_at = max(quota["next_available_at"], quota["cooldown_until"] or now)
        _rate_limit(
            retry_at, now,
            "Batas 3 tiket dalam 24 jam sudah tercapai. Silakan lanjutkan percakapan di tiket yang ada.",
        )
    if quota["cooldown_until"] is not None:
        _rate_limit(quota["cooldown_until"], now, "Tunggu 1 menit sebelum membuat tiket baru lagi.")

    ticket = SupportTicket(
        owner_id=user.id,
        owner_name=user.nama_lengkap,
        owner_identifier=user.identifier,
        category=category,
        subject=subject,
        status="new",
        created_at=now,
        updated_at=now,
    )
    db.add(ticket)
    db.flush()
    db.add(SupportMessage(
        ticket_id=ticket.id,
        author_id=user.id,
        author_name=user.nama_lengkap,
        author_role="mahasiswa",
        message=message,
        created_at=now,
    ))
    db.commit()
    db.refresh(ticket)
    return _ticket_data(ticket)


def get_ticket(db: Session, current_user: User, ticket_id: int) -> dict:
    return _detail(db, _visible_ticket(db, current_user, ticket_id))


def add_message(db: Session, current_user: User, ticket_id: int, message: str) -> dict:
    user = _lock_user(db, current_user)
    ticket = _visible_ticket(db, user, ticket_id, lock=True)
    now = utc_now()
    is_admin = user.role in ADMIN_ROLES
    if not is_admin:
        latest = (
            db.query(SupportMessage.created_at)
            .filter(SupportMessage.author_id == user.id)
            .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
            .first()
        )
        if latest and latest[0] + MESSAGE_COOLDOWN > now:
            _rate_limit(latest[0] + MESSAGE_COOLDOWN, now, "Tunggu 10 detik sebelum mengirim pesan lagi.")

    db.add(SupportMessage(
        ticket_id=ticket.id,
        author_id=user.id,
        author_name=user.nama_lengkap,
        author_role="admin" if is_admin else "mahasiswa",
        message=message,
        created_at=now,
    ))
    if is_admin or ticket.status in ("waiting_student", "resolved"):
        ticket.status = "in_progress"
    ticket.updated_at = now
    db.commit()
    db.refresh(ticket)
    return _detail(db, ticket)


def update_ticket(db: Session, admin: User, ticket_id: int, ticket_status: str) -> dict:
    ticket = _visible_ticket(db, admin, ticket_id, lock=True)
    if ticket.status != ticket_status:
        ticket.status = ticket_status
        ticket.updated_at = utc_now()
    db.commit()
    db.refresh(ticket)
    return _detail(db, ticket)
