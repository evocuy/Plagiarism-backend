"""Single-consumer PostgreSQL worker for queued plagiarism checks."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Callable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.models.schemas import PlagiarismCheck, utc_now
from app.services.check_queue_service import (
    PENDING_STATUS,
    PROCESSING_STATUS,
    CheckQueueSettings,
)
from app.services.repository_check_service import (
    run_direct_check_in_background,
    run_repository_check_in_background,
)


logger = logging.getLogger(__name__)


class CheckQueueWorker:
    """Claim and execute one persisted check at a time.

    The worker deliberately holds no database transaction while the expensive
    PDF/NLP work runs.  ``FOR UPDATE SKIP LOCKED`` protects the claim step if
    a second worker is added later, while one Compose worker is the default.
    """

    def __init__(
        self,
        *,
        session_factory=SessionLocal,
        settings: Optional[CheckQueueSettings] = None,
        runner: Callable = run_repository_check_in_background,
        direct_runner: Callable = run_direct_check_in_background,
        now_provider=utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session_factory = session_factory
        self.settings = settings or CheckQueueSettings.from_environment()
        self.runner = runner
        self.direct_runner = direct_runner
        self.now_provider = now_provider
        self.sleep = sleep

    def recover_stale_checks(self, db: Session, *, force: bool = False) -> int:
        """Requeue interrupted work or fail it after the retry budget.

        ``force`` is used only during worker startup. Deployment runs exactly
        one worker, so a check left in ``processing`` at that moment belongs
        to the process that was stopped and can be recovered immediately
        instead of making students wait for the stale timeout.
        """
        cutoff = self.now_provider() - timedelta(seconds=self.settings.stale_seconds)
        query = db.query(PlagiarismCheck).filter(
            PlagiarismCheck.status == PROCESSING_STATUS,
        )
        if not force:
            query = query.filter(
                func.coalesce(
                    PlagiarismCheck.heartbeat_at,
                    PlagiarismCheck.updated_at,
                    PlagiarismCheck.claimed_at,
                    PlagiarismCheck.started_at,
                    PlagiarismCheck.created_at,
                ) < cutoff,
            )
        stale_checks = query.all()
        if not stale_checks:
            return 0

        now = self.now_provider()
        for check_record in stale_checks:
            if int(check_record.attempt_count or 0) < self.settings.max_attempts:
                check_record.status = PENDING_STATUS
                check_record.processing_stage = "queued"
                check_record.processing_message = (
                    "Pemeriksaan dikembalikan ke antrean setelah worker sebelumnya terhenti."
                )
                check_record.claimed_at = None
                check_record.heartbeat_at = None
                check_record.updated_at = now
            else:
                check_record.status = "failed"
                check_record.processing_stage = "failed"
                check_record.processing_message = "Pemeriksaan berhenti terlalu lama dan tidak dapat dilanjutkan."
                check_record.error_message = check_record.processing_message
                check_record.completed_at = now
                check_record.heartbeat_at = now
                check_record.updated_at = now
        db.commit()
        return len(stale_checks)

    def claim_next(self, db: Session):
        self.recover_stale_checks(db)
        check_record = (
            db.query(PlagiarismCheck)
            .filter(PlagiarismCheck.status == PENDING_STATUS)
            .order_by(
                func.coalesce(PlagiarismCheck.queued_at, PlagiarismCheck.created_at).asc(),
                PlagiarismCheck.id.asc(),
            )
            .with_for_update(skip_locked=True)
            .first()
        )
        if not check_record:
            return None

        now = self.now_provider()
        check_record.status = PROCESSING_STATUS
        check_record.progress = 0
        check_record.processing_stage = "queued"
        check_record.processing_message = "Pemeriksaan sedang dimulai oleh worker."
        check_record.claimed_at = now
        check_record.heartbeat_at = now
        check_record.started_at = check_record.started_at or now
        check_record.updated_at = now
        check_record.attempt_count = int(check_record.attempt_count or 0) + 1
        db.commit()
        db.refresh(check_record)
        return {
            "document_id": check_record.document_id,
            "check_id": check_record.id,
            "requester_user_id": check_record.user_id,
            "candidate_limit": check_record.candidate_limit,
            "auto_index_embeddings": check_record.auto_index_embeddings,
            "auto_index_repository_limit": check_record.auto_index_repository_limit,
            "reference_document_id": check_record.reference_document_id,
        }

    def process_next(self) -> bool:
        with self.session_factory() as db:
            job = self.claim_next(db)
        if not job:
            return False

        reference_document_id = job.pop("reference_document_id", None)
        if reference_document_id is None:
            self.runner(
                **job,
                session_factory=self.session_factory,
            )
        else:
            self.direct_runner(
                **job,
                reference_document_id=reference_document_id,
                session_factory=self.session_factory,
            )
        return True

    def run_forever(self) -> None:
        logger.info(
            "Check queue worker started (poll=%ss, stale=%ss, max_attempts=%s).",
            self.settings.worker_poll_seconds,
            self.settings.stale_seconds,
            self.settings.max_attempts,
        )
        try:
            with self.session_factory() as db:
                recovered = self.recover_stale_checks(db, force=True)
            if recovered:
                logger.warning("Recovered %s interrupted plagiarism check(s) at startup.", recovered)
        except Exception:
            logger.exception("Check queue worker gagal memulihkan antrean saat startup.")
        while True:
            try:
                processed = self.process_next()
            except Exception:
                logger.exception("Check queue worker gagal mengambil atau memproses antrean.")
                processed = False
            if not processed:
                self.sleep(self.settings.worker_poll_seconds)
