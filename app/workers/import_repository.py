"""Import alumni PDFs from a local folder. Start with --dry-run; never imports app.main."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pymupdf
from dotenv import load_dotenv

from app.services.document_structure_service import DocumentStructureService
from app.services.pdf_limits import positive_int_setting
from app.services.pdf_service import UPLOAD_DIR
from app.services.repository_import_files import ImportInputError, discover_pdfs, ensure_separate_directories, pdf_identity, validate_pdf


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Impor massal dokumen alumni (proposal/skripsi), dengan opsi embedding dan resume otomatis.")
    command.add_argument("--root", type=Path, required=True, help="Folder berisi proposal/ dan skripsi/, atau folder satu jenis dokumen.")
    command.add_argument("--document-type", choices=("proposal", "skripsi"), help="Jenis untuk seluruh PDF bila folder tidak dibagi per jenis.")
    command.add_argument("--admin", default="cihuy", help="Identifier admin aktif yang menjalankan impor.")
    command.add_argument("--dry-run", action="store_true", help="Baca/validasi input tanpa database, salinan file, embedding, atau report di disk.")
    command.add_argument("--with-embeddings", action="store_true", help="Indeks embedding sekaligus; mengulang command mencoba lagi chunks yang gagal.")
    command.add_argument("--embedding-batch-size", type=int, default=32, help="Jumlah chunks per request embedding, 1–128 (default 32).")
    command.add_argument("--limit", type=int, help="Proses N file pertama dalam urutan nama, misalnya 10 untuk uji awal.")
    command.add_argument("--min-free-mb", type=int, default=512, help="Cadangan ruang disk minimal sebelum menyalin/indexing (default 512 MiB).")
    command.add_argument("--report", type=Path, help="Report .jsonl baru; default uploads/import-reports/. File yang sudah ada tidak ditimpa.")
    return command


def _print(event: dict) -> None:
    # JSON escapes control characters in external filenames; never echo raw errors.
    print(json.dumps(event, ensure_ascii=True), flush=True)


def preview(files, max_bytes: int) -> int:
    structure = DocumentStructureService()
    seen, failed, valid, duplicate, total_bytes = set(), 0, 0, 0, 0
    for position, candidate in enumerate(files, start=1):
        row = {"event": "preview_file", "position": position, "total": len(files),
               "file": candidate.relative_path, "document_type": candidate.document_type}
        try:
            if candidate.document_type is None:
                raise ImportInputError("unknown_document_type", "Gunakan folder proposal/skripsi atau isi --document-type.")
            digest, size = pdf_identity(candidate.path, max_bytes)
            row.update(sha256=digest, size_bytes=size)
            key = (digest, candidate.document_type)
            if key in seen:
                duplicate += 1
                row["status"] = "duplicate_in_input"
            else:
                validate_pdf(candidate, structure)
                seen.add(key)
                valid += 1
                total_bytes += size
                row["status"] = "valid"
        except Exception as exc:
            failed += 1
            row.update(status="failed", error_code=exc.code if isinstance(exc, ImportInputError) else "file_unreadable",
                       error=str(exc) if isinstance(exc, ImportInputError) else "PDF input tidak dapat dibaca.")
        _print(row)
    _print({"event": "preview_summary", "total": len(files), "valid_unique": valid, "duplicate_in_input": duplicate,
            "failed": failed, "unique_input_bytes": total_bytes, "database_checked": False,
            "embedding_executed": False, "files_written": False})
    return 1 if failed else 0


def execute(args) -> int:
    load_dotenv()
    ensure_separate_directories(args.root, UPLOAD_DIR)
    files = discover_pdfs(args.root, args.document_type, args.limit)
    if not files:
        raise ImportInputError("no_pdf_files", "Tidak ada PDF biasa yang ditemukan pada folder input.")
    max_bytes = positive_int_setting("MAX_UPLOAD_SIZE_MB", 10) * 1024 * 1024
    if args.dry_run:
        return preview(files, max_bytes)

    # Lazy imports keep --help/--dry-run independent of database configuration.
    from sqlalchemy import inspect, text
    from app.database.session import SessionLocal, engine
    from app.services.repository_import_service import RepositoryImportService

    if engine.dialect.name != "postgresql":
        raise ImportInputError("postgresql_required", "Impor sungguhan harus menggunakan PostgreSQL.")
    if "content_sha256" not in {column["name"] for column in inspect(engine).get_columns("documents")}:
        raise ImportInputError("schema_not_ready", "Jalankan/restart backend versi ini untuk migrasi sebelum melakukan impor.")
    if args.with_embeddings:
        with engine.connect() as connection:
            ready = connection.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_attribute
                    WHERE attrelid = 'document_chunks'::regclass
                      AND attname = 'embedding' AND NOT attisdropped
                      AND format_type(atttypid, atttypmod) LIKE 'vector%'
                )
            """)).scalar()
        if not ready:
            raise ImportInputError("embedding_storage_unavailable", "Kolom embedding pgvector belum tersedia. Siapkan database/migrasi backend sebelum menghubungi layanan embedding.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.report or UPLOAD_DIR / "import-reports" / f"import-{stamp}-{uuid4().hex[:8]}.jsonl"
    if report_path.suffix.lower() != ".jsonl":
        raise ImportInputError("invalid_report", "Report harus menggunakan ekstensi .jsonl.")
    if report_path.exists():
        raise ImportInputError("report_exists", "File report sudah ada. Pilih nama report baru atau gunakan lokasi default.")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as report, SessionLocal() as db:
        report_path.chmod(0o640)

        def emit(event):
            _print(event)
            report.write(json.dumps(event, ensure_ascii=True) + "\n")
            report.flush()

        emit({"event": "run", "total": len(files), "with_embeddings": args.with_embeddings,
              "report": str(report_path), "resume": "content_sha256_and_document_type"})
        importer = RepositoryImportService(db, UPLOAD_DIR, admin_identifier=args.admin, max_bytes=max_bytes,
                                           min_free_bytes=args.min_free_mb * 1024 * 1024)
        importer.embeddings.batch_size = args.embedding_batch_size
        result = importer.run(files, with_embeddings=args.with_embeddings, on_event=emit)
        return 1 if result["failed"] or result["not_attempted"] else 0


def main(argv=None) -> int:
    # Live extraction also runs in this CLI after isolated validation. Keep
    # recoverable native messages (which may contain PDF data) out of JSONL.
    pymupdf.set_messages(path=os.devnull)
    pymupdf.set_log(path=os.devnull)
    command = parser()
    args = command.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        command.error("--limit harus positif")
    if not 1 <= args.embedding_batch_size <= 128:
        command.error("--embedding-batch-size harus 1–128")
    if args.min_free_mb < 0:
        command.error("--min-free-mb tidak boleh negatif")
    if args.dry_run and args.report is not None:
        command.error("--dry-run tidak menulis report; gunakan output terminal")
    try:
        return execute(args)
    except KeyboardInterrupt:
        _print({"event": "interrupted", "message": "Impor dihentikan. Jalankan kembali command yang sama untuk melanjutkan."})
        return 130
    except ImportInputError as exc:
        _print({"event": "error", "error_code": exc.code, "error": str(exc)})
        return 2
    except Exception as exc:
        from app.services.error_service import REINDEX_ERROR, report_error
        error = report_error(exc, REINDEX_ERROR, operation="repository_import_cli")
        _print({"event": "error", "error_code": "import_unavailable", "error": error.text, "error_id": error.error_id})
        return 2


if __name__ == "__main__":
    sys.exit(main())
