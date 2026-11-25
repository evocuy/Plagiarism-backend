"""Copy legacy PDFs into MinIO and switch verified database references."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from app.services import pdf_storage
from app.services.pdf_service import UPLOAD_DIR


@dataclass(frozen=True)
class LegacyPDF:
    kind: str
    record_id: int | None
    reference: str
    destination: str
    sha256: str | None = None

    def object_key(self, digest: str) -> str:
        if self.kind == "watermark":
            return self.destination
        return f"{self.destination}{digest}.pdf"


def _batches(query, primary_key):
    last_id = 0
    while rows := query.filter(primary_key > last_id).order_by(primary_key).limit(100).all():
        yield from rows
        last_id = rows[-1][0]


def _references(db, upload_dir: Path):
    from app.models.schemas import Document, PlagiarismCheck

    documents = db.query(Document.id, Document.file_path, Document.source_origin,
                         Document.document_type, Document.content_sha256)
    for document_id, path, origin, document_type, digest in _batches(documents, Document.id):
        if origin == "alumni":
            folder = document_type if document_type in {"proposal", "skripsi"} else "legacy"
            destination = f"repository/{folder}/"
        else:
            destination = f"student-uploads/{document_id}-"
        yield LegacyPDF("document", document_id, path, destination, digest)

    highlights = db.query(PlagiarismCheck.id, PlagiarismCheck.highlighted_file_path).filter(
        PlagiarismCheck.highlighted_file_path.is_not(None),
    )
    for check_id, path in _batches(highlights, PlagiarismCheck.id):
        yield LegacyPDF("highlight", check_id, path, f"highlights/{check_id}-")

    for path in sorted((upload_dir / "watermarked").glob("*.pdf")):
        if re.fullmatch(r"[0-9a-f]{64}\.pdf", path.name):
            yield LegacyPDF("watermark", None, str(path), f"watermark-cache/{path.name}")


def _record_column(kind):
    from app.models.schemas import Document, PlagiarismCheck

    if kind == "document":
        return Document, Document.file_path
    if kind == "highlight":
        return PlagiarismCheck, PlagiarismCheck.highlighted_file_path
    raise ValueError("Jenis referensi PDF tidak valid.")


def _copy_pdf(entry, source, size, digest, storage, report, db, root) -> str:
    reference = storage.reference(entry.object_key(digest))
    if not storage.exists(reference):
        storage.upload(reference, source)
    if not storage.verify(reference, size, digest):
        return "verification_failed"

    # Write the verified copy to the private journal before changing its
    # reference. Cleanup checks the committed reference again after a retry.
    _print({"kind": entry.kind, "id": entry.record_id, "source": source.relative_to(root).as_posix(),
            "reference": reference, "size": size, "sha256": digest}, stream=report)
    os.fsync(report.fileno())
    if entry.kind != "watermark":
        model, column = _record_column(entry.kind)
        fields = {column: reference}
        if entry.kind == "document" and not entry.sha256:
            fields[model.content_sha256] = digest
        changed = db.query(model).filter(
            model.id == entry.record_id, column == entry.reference,
        ).update(fields, synchronize_session=False)
        db.commit()
        if not changed:
            return "reference_changed"
    return "copied"


def migrate(db, upload_dir: Path, *, storage=None, report=None, emit=None) -> Counter:
    root = upload_dir.resolve()
    counts = Counter()
    for entry in _references(db, root):
        status, size = "already_in_minio", 0
        if pdf_storage.is_minio_reference(entry.reference):
            if storage is not None:
                storage.object_key(entry.reference)
        else:
            try:
                source = pdf_storage.managed_local_pdf(entry.reference, root)
                if source is None or source.suffix.lower() != ".pdf":
                    status = "local_missing"
                else:
                    size, digest = pdf_storage.file_identity(source)
                    if size == 0 or (entry.sha256 and entry.sha256 != digest):
                        status = "checksum_mismatch"
                    elif storage is None:
                        status = "ready_to_copy"
                    else:
                        status = _copy_pdf(entry, source, size, digest, storage, report, db, root)
            except pdf_storage.PDFStorageError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                status = "copy_failed"
                if emit:
                    emit({"event": "error", "kind": entry.kind, "id": entry.record_id,
                          "error_type": type(exc).__name__})
        counts[status] += 1
        counts["bytes"] += size if status in {"copied", "ready_to_copy"} else 0
        if emit and status not in {"already_in_minio", "ready_to_copy"}:
            emit({"event": "file", "kind": entry.kind, "id": entry.record_id, "status": status})
    return counts


def _cleanup_file(record, db, root, referenced_paths, storage) -> str:
    source = (root / record["source"]).resolve()
    reference, kind = record["reference"], record["kind"]
    if not source.is_relative_to(root) or source.suffix.lower() != ".pdf":
        return "unsafe_path"
    if source in referenced_paths:
        return "still_referenced"
    if not source.is_file():
        return "already_removed"
    if kind == "watermark":
        if (source.parent != root / "watermarked"
                or reference != storage.reference(f"watermark-cache/{source.name}")):
            return "reference_changed"
    else:
        model, column = _record_column(kind)
        if db.query(column).filter(model.id == record["id"]).scalar() != reference:
            return "reference_changed"
    size, digest = pdf_storage.file_identity(source)
    if (size, digest) != (record["size"], record["sha256"]):
        return "checksum_mismatch"
    if not storage.verify(reference, size, digest):
        return "verification_failed"
    source.unlink()
    return "removed"


def cleanup_local(db, upload_dir: Path, storage, report, *, emit=None) -> Counter:
    root = upload_dir.resolve()
    # A legacy duplicate can share the same physical file with a migrated row.
    referenced = {
        path for entry in _references(db, root) if entry.kind != "watermark"
        if (path := pdf_storage.managed_local_pdf(entry.reference, root)) is not None
    }
    counts = Counter()
    for line_number, line in enumerate(report, start=1):
        try:
            record = json.loads(line)
            outcome = _cleanup_file(record, db, root, referenced, storage)
        except Exception as exc:
            db.rollback()
            outcome = "cleanup_failed"
            if emit:
                emit({"event": "error", "line": line_number, "error_type": type(exc).__name__})
        counts[outcome] += 1
        if emit and outcome not in {"removed", "already_removed"}:
            emit({"event": "cleanup", "line": line_number, "status": outcome})
    return counts


def _print(data, *, stream=None):
    print(json.dumps(data, ensure_ascii=True), file=stream or sys.stdout, flush=True)


def _open_report(path: Path):
    return path.open("x", encoding="utf-8") if os.name == "nt" else open(
        path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pindahkan PDF lama ke MinIO tanpa mengubah ID atau skor.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Inventaris PDF lokal tanpa menulis file atau database.")
    mode.add_argument("--apply", action="store_true", help="Salin, verifikasi, lalu alihkan referensi.")
    mode.add_argument("--cleanup-local", type=Path, metavar="REPORT",
                      help="Bersihkan salinan lokal terverifikasi dari report migrasi.")
    parser.add_argument("--report", type=Path, help="Lokasi report baru untuk --apply.")
    args = parser.parse_args(argv)
    if args.report and not args.apply:
        parser.error("--report digunakan dengan --apply")
    load_dotenv()

    try:
        from app.database.session import SessionLocal, engine
        from app.services.repository_import_service import repository_import_lock

        if engine.dialect.name != "postgresql":
            raise RuntimeError("Migrasi memerlukan PostgreSQL.")
        with SessionLocal() as db:
            if args.dry_run:
                counts = migrate(db, UPLOAD_DIR, emit=_print)
                success = {"ready_to_copy", "already_in_minio"}
            else:
                storage = pdf_storage.get_minio_storage()
                with repository_import_lock(db):
                    if args.cleanup_local:
                        with args.cleanup_local.open(encoding="utf-8") as report:
                            counts = cleanup_local(db, UPLOAD_DIR, storage, report, emit=_print)
                        success = {"removed", "already_removed"}
                    else:
                        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                        path = args.report or UPLOAD_DIR / "minio-migration" / f"{stamp}.jsonl"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with _open_report(path) as report:
                            _print({"event": "migration_started", "report": str(path)})
                            counts = migrate(db, UPLOAD_DIR, storage=storage, report=report, emit=_print)
                        success = {"copied", "already_in_minio"}
        _print({"event": "summary", **counts})
        return int(any(counts[key] for key in counts.keys() - success - {"bytes"}))
    except KeyboardInterrupt:
        _print({"event": "interrupted", "message": "Jalankan ulang untuk melanjutkan migrasi."})
        return 130
    except Exception as exc:
        detail = exc.diagnostic() if isinstance(exc, pdf_storage.PDFStorageError) else {}
        _print({"event": "error", "error_type": type(exc).__name__, **detail,
                "message": "Migrasi tidak dapat dijalankan. Periksa koneksi database, konfigurasi MinIO, dan report."})
        return 2


if __name__ == "__main__":
    sys.exit(main())
