"""Durable admission and queue helpers for plagiarism checks.

The web API only enqueues a check.  A dedicated worker claims the oldest
pending record, which prevents several PDF/NLP jobs from exhausting the API
process at the same time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, text
from sqlalchemy.orm import Session

from app.models.schemas import PlagiarismCheck, utc_now


PENDING_STATUS = "pending"
PROCESSING_STATUS = "processing"
ACTIVE_CHECK_STATUSES = (PENDING_STATUS, PROCESSING_STATUS)
# Serializes only the short admission transaction in PostgreSQL. It prevents
# two simultaneous uploads from both observing a free final quota slot before
# either creates its queue row.
CHECK_QUEUE_ADMISSION_LOCK_KEY = 7_419_842_331


def _read_int_setting(name: str, default: int, *, minimum: int = 0) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} harus berupa bilangan bulat.") from exc
    if value < minimum:
        raise RuntimeError(f"{name} harus bernilai minimal {minimum}.")
    return value


def _read_bool_setting(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} harus bernilai true atau false.")


@dataclass(frozen=True)
class CheckQueueSettings:
    daily_limit: int = 3
    quota_timezone: str = "Asia/Makassar"
    max_active_per_student: int = 1
    max_queued_checks: int = 25
    max_candidate_limit: int = 20
    max_auto_index_repository_limit: int = 50
    auto_index_embeddings: bool = True
    poll_after_ms: int = 5000
    worker_poll_seconds: int = 2
    stale_seconds: int = 45 * 60
    max_attempts: int = 2

    @classmethod
    def from_environment(cls) -> "CheckQueueSettings":
        return cls(
            daily_limit=_read_int_setting("CHECK_DAILY_LIMIT", 3, minimum=1),
            quota_timezone=(
                os.getenv("CHECK_QUOTA_TIMEZONE", "Asia/Makassar").strip()
                or "Asia/Makassar"
            ),
            max_active_per_student=_read_int_setting(
                "CHECK_MAX_ACTIVE_PER_STUDENT", 1, minimum=1
            ),
            max_queued_checks=_read_int_setting("CHECK_MAX_QUEUED", 25, minimum=1),
            max_candidate_limit=_read_int_setting("CHECK_MAX_CANDIDATES", 20, minimum=1),
            max_auto_index_repository_limit=_read_int_setting(
                "CHECK_MAX_AUTO_INDEX_REPOSITORY", 50, minimum=0
            ),
            auto_index_embeddings=_read_bool_setting("CHECK_AUTO_INDEX_EMBEDDINGS", True),
            poll_after_ms=_read_int_setting("CHECK_POLL_AFTER_MS", 5000, minimum=1000),
            worker_poll_seconds=_read_int_setting("CHECK_WORKER_POLL_SECONDS", 2, minimum=1),
            stale_seconds=_read_int_setting("CHECK_STALE_SECONDS", 45 * 60, minimum=60),
            max_attempts=_read_int_setting("CHECK_MAX_ATTEMPTS", 2, minimum=1),
        )


class CheckQueueService:
    def __init__(
        self,
        settings: Optional[CheckQueueSettings] = None,
        now_provider=utc_now,
    ):
        self.settings = settings or CheckQueueSettings.from_environment()
        self.now_provider = now_provider

    def assert_can_enqueue(self, db: Session, user_id: int) -> None:
        """Reject a submission before its PDF is stored when limits are reached."""
        now = self.now_provider()
        active_count = (
            db.query(func.count(PlagiarismCheck.id))
            .filter(
                PlagiarismCheck.user_id == user_id,
                PlagiarismCheck.status.in_(ACTIVE_CHECK_STATUSES),
            )
            .scalar()
            or 0
        )
        if active_count >= self.settings.max_active_per_student:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Anda masih memiliki pemeriksaan yang sedang mengantre atau diproses. "
                    "Tunggu pemeriksaan tersebut selesai sebelum mengirim naskah berikutnya."
                ),
            )

        day_start, next_day = self._quota_day_window(now)
        submitted_today = (
            db.query(func.count(PlagiarismCheck.id))
            .filter(
                PlagiarismCheck.user_id == user_id,
                PlagiarismCheck.created_at >= day_start,
            )
            .scalar()
            or 0
        )
        if submitted_today >= self.settings.daily_limit:
            retry_after = max(1, int((next_day - now).total_seconds()))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Batas {self.settings.daily_limit} pemeriksaan per hari sudah tercapai. "
                    "Silakan coba kembali besok."
                ),
                headers={"Retry-After": str(retry_after)},
            )

        queued_or_processing = (
            db.query(func.count(PlagiarismCheck.id))
            .filter(PlagiarismCheck.status.in_(ACTIVE_CHECK_STATUSES))
            .scalar()
            or 0
        )
        if queued_or_processing >= self.settings.max_queued_checks:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Antrean pemeriksaan sedang penuh. Silakan coba beberapa saat lagi."
                ),
                headers={"Retry-After": str(self.settings.poll_after_ms // 1000)},
            )

    @staticmethod
    def _acquire_admission_lock(db: Session) -> None:
        """Lock queue admission for one short PostgreSQL transaction.

        SQLite is used only by the test suite and has no advisory-lock
        primitive. Production PostgreSQL keeps the lock until ``commit`` or
        ``rollback`` in :meth:`enqueue`.
        """
        if db.get_bind().dialect.name != "postgresql":
            return
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": CHECK_QUEUE_ADMISSION_LOCK_KEY},
        )

    def _quota_day_window(self, now: datetime) -> Tuple[datetime, datetime]:
        """Return UTC-naive timestamps for the campus calendar-day window."""
        try:
            campus_timezone = ZoneInfo(self.settings.quota_timezone)
        except ZoneInfoNotFoundError as exc:
            # Minimal Windows Python environments can lack the IANA tzdata
            # database. Makassar is permanently UTC+08:00 (no DST), so retain
            # the configured campus-day semantics without adding a runtime
            # dependency merely for this fixed campus timezone.
            timezone_name = self.settings.quota_timezone.casefold()
            if timezone_name in {"utc", "etc/utc", "gmt"}:
                campus_timezone = timezone.utc
            elif timezone_name == "asia/makassar":
                campus_timezone = timezone(timedelta(hours=8), name="Asia/Makassar")
            else:
                raise RuntimeError(
                    f"CHECK_QUOTA_TIMEZONE tidak valid: {self.settings.quota_timezone}"
                ) from exc

        now_utc = (
            now.replace(tzinfo=timezone.utc)
            if now.tzinfo is None
            else now.astimezone(timezone.utc)
        )
        local_now = now_utc.astimezone(campus_timezone)
        local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_next_day = local_day_start + timedelta(days=1)
        return (
            local_day_start.astimezone(timezone.utc).replace(tzinfo=None),
            local_next_day.astimezone(timezone.utc).replace(tzinfo=None),
        )

    def enqueue(
        self,
        *,
        db: Session,
        document_id: int,
        user_id: int,
        candidate_limit: int,
        auto_index_embeddings: bool,
        auto_index_repository_limit: int,
        reference_document_id: Optional[int] = None,
    ) -> Tuple[PlagiarismCheck, int]:
        """Atomically recheck limits and persist one queue item.

        Routes may do an earlier best-effort check before storing a PDF, but
        this second check is the authoritative admission point. It is protected
        against concurrent requests on PostgreSQL.
        """
        try:
            self._acquire_admission_lock(db)
            self.assert_can_enqueue(db, user_id)
            now = self.now_provider()
            check_record = PlagiarismCheck(
                document_id=document_id,
                user_id=user_id,
                overall_similarity=0.0,
                status=PENDING_STATUS,
                progress=0,
                processing_stage="queued",
                processing_message="File diterima dan menunggu giliran pemeriksaan.",
                candidate_limit=candidate_limit,
                auto_index_embeddings=auto_index_embeddings,
                auto_index_repository_limit=auto_index_repository_limit,
                reference_document_id=reference_document_id,
                queued_at=now,
                updated_at=now,
                attempt_count=0,
            )
            db.add(check_record)
            db.commit()
            db.refresh(check_record)
        except Exception:
            db.rollback()
            raise
        return check_record, self.get_queue_position(db, check_record)

    def get_queue_position(self, db: Session, check_record: PlagiarismCheck) -> int:
        """Return a display-only position; the worker remains authoritative."""
        processing_count = (
            db.query(func.count(PlagiarismCheck.id))
            .filter(PlagiarismCheck.status == PROCESSING_STATUS)
            .scalar()
            or 0
        )
        queued_at = check_record.queued_at or check_record.created_at or self.now_provider()
        earlier_or_same_pending = (
            db.query(func.count(PlagiarismCheck.id))
            .filter(
                PlagiarismCheck.status == PENDING_STATUS,
                or_(
                    PlagiarismCheck.queued_at < queued_at,
                    and_(
                        PlagiarismCheck.queued_at == queued_at,
                        PlagiarismCheck.id <= check_record.id,
                    ),
                ),
            )
            .scalar()
            or 0
        )
        return int(processing_count) + int(earlier_or_same_pending)
