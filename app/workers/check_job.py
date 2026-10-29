"""Execute exactly one persisted check inside a deadline-controlled process."""

import json
import logging
import sys

from app.services.repository_check_service import (
    run_direct_check_in_background,
    run_repository_check_in_background,
)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    job = json.loads(sys.argv[1])
    reference_document_id = job.pop("reference_document_id", None)
    if reference_document_id is None:
        run_repository_check_in_background(**job)
    else:
        run_direct_check_in_background(**job, reference_document_id=reference_document_id)
