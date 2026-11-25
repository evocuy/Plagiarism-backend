"""PostgreSQL worker for queued plagiarism checks."""

from __future__ import annotations

import logging
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.models.schemas import PlagiarismCheck, utc_now
from app.services.check_job_lock import CheckJobBusy, hold_check_job, try_recover_check
from app.services.pdf_limits import positive_int_setting
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

    Row locks protect claiming. A PostgreSQL advisory lock protects the job
    during processing, without keeping the claim transaction open.
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
        # Injected runners are used by unit tests/in-process integrations. The
        # deployment defaults always execute the expensive work in a child.
        self.isolate_jobs = (
            runner is run_repository_check_in_background
            and direct_runner is run_direct_check_in_background
        )
        self.job_timeout = positive_int_setting("CHECK_JOB_TIMEOUT_SECONDS", 1200)
        self.now_provider = now_provider
        self.sleep = sleep

    def recover_stale_checks(self, db: Session, *, force: bool = False) -> int:
        """Requeue interrupted work or fail it after the retry budget.

        Startup can review fresh claims as well as stale ones. Both paths
        must verify that the worker and its subprocess have released the job.
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
        stale_checks = query.populate_existing().with_for_update(skip_locked=True).all()
        if not stale_checks:
            return 0

        now = self.now_provider()
        recovered = 0
        for check_record in stale_checks:
            if not try_recover_check(db, check_record.id):
                continue
            recovered += 1
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
        return recovered

    def _claim_next(self, db: Session, locks: ExitStack):
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

        try:
            owner = locks.enter_context(hold_check_job(db.get_bind(), check_record.id))
        except CheckJobBusy:
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
        job = {
            "document_id": check_record.document_id,
            "check_id": check_record.id,
            "attempt_count": check_record.attempt_count,
            "requester_user_id": check_record.user_id,
            "candidate_limit": check_record.candidate_limit,
            "auto_index_embeddings": check_record.auto_index_embeddings,
            "auto_index_repository_limit": check_record.auto_index_repository_limit,
            "reference_document_id": check_record.reference_document_id,
        }
        db.commit()
        return job, owner

    def process_next(self) -> bool:
        with ExitStack() as locks:
            with self.session_factory() as db:
                claim = self._claim_next(db, locks)
            if claim is None:
                return False

            job, owner = claim
            if self.isolate_jobs:
                self._run_isolated_job(job)
            else:
                job.pop("attempt_count")
                reference_document_id = job.pop("reference_document_id")
                factory = partial(self.session_factory, bind=owner) if owner is not None else self.session_factory
                if reference_document_id is None:
                    self.runner(**job, session_factory=factory)
                else:
                    self.direct_runner(**job, reference_document_id=reference_document_id, session_factory=factory)
        return True

    def _run_isolated_job(self, job: dict) -> None:
        """Enforce a real wall-clock deadline, even for stuck native PDF code."""
        try:
            with TemporaryDirectory(prefix="check-job-") as directory:
                subprocess.run(
                    [sys.executable, "-m", "app.workers.check_job", json.dumps(job)],
                    stdin=subprocess.DEVNULL,
                    timeout=self.job_timeout,
                    check=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                         "TMPDIR": directory, "TEMP": directory, "TMP": directory},
                    cwd=str(Path(__file__).resolve().parents[2]),
                )
            message = "Proses pemeriksaan terhenti sebelum hasil selesai disimpan. Silakan coba lagi."
        except subprocess.TimeoutExpired:
            logger.warning("Check %s dihentikan setelah melewati batas waktu.", job["check_id"])
            message = "Pemeriksaan melewati batas waktu pemrosesan. Coba sederhanakan dokumen."
        except (subprocess.SubprocessError, OSError):
            logger.exception("Proses pemeriksaan %s gagal dijalankan.", job["check_id"])
            message = "Proses pemeriksaan terhenti. Silakan coba lagi."

        # The child normally completes or fails the record itself. A timeout,
        # native crash or early process exit must also release the user's slot.
        with self.session_factory() as db:
            check_record = db.query(PlagiarismCheck).filter(
                PlagiarismCheck.id == job["check_id"],
                PlagiarismCheck.status == PROCESSING_STATUS,
                PlagiarismCheck.attempt_count == job["attempt_count"],
            ).with_for_update().first()
            if check_record is not None:
                now = self.now_provider()
                check_record.status = "failed"
                check_record.processing_stage = "failed"
                check_record.processing_message = message
                check_record.error_message = message
                check_record.completed_at = now
                check_record.heartbeat_at = now
                check_record.updated_at = now
                db.commit()

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
