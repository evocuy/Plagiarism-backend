"""Execute exactly one persisted check inside a deadline-controlled process."""

import json
import logging
import sys
from functools import partial

from app.database.session import SessionLocal
from app.models.schemas import PlagiarismCheck
from app.services.check_job_lock import CheckJobBusy, hold_check_job
from app.services.check_queue_service import PROCESSING_STATUS
from app.services.repository_check_service import (
    run_direct_check_in_background,
    run_repository_check_in_background,
)


def run_claimed_job(job: dict, *, session_factory=SessionLocal) -> None:
    arguments = dict(job)
    attempt_count = arguments.pop("attempt_count")
    reference_document_id = arguments.pop("reference_document_id")
    with session_factory() as db:
        bind = db.get_bind()
    try:
        # Keep ownership independently of the supervisor. If it dies, the
        # subprocess can finish while its own connection still holds the lock.
        with hold_check_job(bind, job["check_id"]) as owner:
            factory = partial(session_factory, bind=owner) if owner is not None else session_factory
            with factory() as db:
                current = db.query(PlagiarismCheck.id).filter(
                    PlagiarismCheck.id == job["check_id"],
                    PlagiarismCheck.status == PROCESSING_STATUS,
                    PlagiarismCheck.attempt_count == attempt_count,
                ).first()
            if current is None:
                return
            if reference_document_id is None:
                run_repository_check_in_background(**arguments, session_factory=factory)
            else:
                run_direct_check_in_background(**arguments, reference_document_id=reference_document_id,
                                               session_factory=factory)
    except CheckJobBusy:
        # Recovery acquired the job before this subprocess started.
        return


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_claimed_job(json.loads(sys.argv[1]))
