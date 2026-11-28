"""Restore missing highlight PDFs for explicitly selected checks."""
from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Buat ulang PDF highlight dari hasil cek yang sudah tersimpan.")
    parser.add_argument("check_ids", type=int, nargs="+", metavar="CHECK_ID")
    args = parser.parse_args(argv)
    if any(check_id < 1 for check_id in args.check_ids):
        parser.error("CHECK_ID harus positif.")

    from app.database.session import SessionLocal
    from app.services.error_service import CHECK_ERROR, report_error
    from app.services.repository_check_service import RepositoryCheckService

    service = RepositoryCheckService()
    failed = False
    for check_id in dict.fromkeys(args.check_ids):
        with SessionLocal() as db:
            try:
                status = service.repair_highlighted_pdf(check_id, db)
                row = {"check_id": check_id, "status": status}
            except Exception as exc:
                db.rollback()
                error = report_error(exc, CHECK_ERROR, operation="repair_highlighted_pdf")
                row = {"check_id": check_id, "status": "failed", "error_id": error.error_id}
            print(json.dumps(row), flush=True)
            failed = failed or row["status"] not in {"repaired", "already_available"}
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
