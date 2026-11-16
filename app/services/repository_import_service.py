"""Sequential, resumable alumni imports using the existing PDF/NLP services."""
from __future__ import annotations

import shutil
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.schemas import Document, User, utc_now
from app.services.document_chunk_service import DocumentChunkService
from app.services.document_embedding_service import DocumentEmbeddingService
from app.services.document_structure_service import DocumentStructureService
from app.services.document_text_service import DocumentTextService
from app.services.error_service import EMBEDDING_ERROR, REINDEX_ERROR, report_error
from app.services.pdf_service import PDF_EXTRACTION_VERSION
from app.services.repository_import_files import ImportFile, ImportInputError, copy_pdf_atomically, pdf_identity, validate_pdf


IMPORT_LOCK = 71430751203844622


@contextmanager
def repository_import_lock(db: Session):
    """A dedicated connection retains the lock across per-document commits."""
    if db.get_bind().dialect.name != "postgresql":
        yield  # Isolated unit tests only; the CLI requires PostgreSQL.
        return
    connection = db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired = False
    try:
        acquired = bool(connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": IMPORT_LOCK}).scalar())
        if not acquired:
            raise ImportInputError("import_already_running", "Impor repositori lain masih berjalan. Tunggu hingga selesai.")
        yield
    finally:
        if acquired:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": IMPORT_LOCK})
            except Exception:
                connection.invalidate()
        connection.close()


class RepositoryImportService:
    def __init__(
        self, db: Session, upload_dir: Path, *, admin_identifier: str = "cihuy",
        max_bytes: int = 10 * 1024 * 1024, min_free_bytes: int = 512 * 1024 * 1024,
        structure_service=None, text_service=None, chunk_service=None, embedding_service=None,
    ):
        self.db, self.upload_dir = db, upload_dir.resolve()
        self.admin_identifier = admin_identifier
        self.max_bytes, self.min_free_bytes = max_bytes, min_free_bytes
        self.structure = structure_service or DocumentStructureService()
        self.texts = text_service or DocumentTextService()
        self.chunks = chunk_service or DocumentChunkService()
        self.embeddings = embedding_service or DocumentEmbeddingService(document_chunk_service=self.chunks)
        self.lookup = {}
        self.actor = {}
        self.emit = lambda event: None

    def _checkpoint(self, document: Document, stage: str, **details) -> None:
        document.extraction_metadata = {
            **(document.extraction_metadata or {}),
            "repository_import": {
                **((document.extraction_metadata or {}).get("repository_import") or {}),
                **self.actor, **details, "stage": stage, "updated_at": utc_now().isoformat(),
            },
        }
        self.db.add(document)
        self.db.commit()

    def _managed_path(self, stored: str) -> Path | None:
        candidate = Path(stored)
        candidates = [candidate if candidate.is_absolute() else self.upload_dir.parent / candidate,
                      self.upload_dir / candidate.name]
        for path in candidates:
            resolved = path.resolve()
            if resolved.is_relative_to(self.upload_dir) and resolved.is_file():
                return resolved
        return None

    def _load_existing(self) -> None:
        # Lightweight columns only: never load the whole repository's NLP text.
        rows = self.db.query(Document.id, Document.file_path, Document.content_sha256,
                             Document.document_type, Document.deleted_at).filter(
            Document.source_origin == "alumni", Document.user_id.is_(None),
        ).order_by(Document.id).all()
        backfilled, unavailable = 0, 0
        for identifier, stored, digest, kind, deleted_at in rows:
            if not digest:
                path = self._managed_path(stored)
                try:
                    digest = pdf_identity(path, self.max_bytes)[0] if path else None
                except (OSError, ImportInputError):
                    digest = None
                if not digest:
                    unavailable += 1
                    continue
                self.db.query(Document).filter(Document.id == identifier).update(
                    {"content_sha256": digest}, synchronize_session=False,
                )
                self.db.commit()
                backfilled += 1
            key = (digest, kind)
            previous = self.lookup.get(key)
            # Keep historical duplicates, preferring an active row for reuse.
            if previous is None or (previous[1] and deleted_at is None):
                self.lookup[key] = (identifier, deleted_at is not None)
        self.emit({"event": "existing_repository", "checksums_backfilled": backfilled,
                   "historical_files_unavailable": unavailable})

    def _reserve_space(self, size: int) -> None:
        if shutil.disk_usage(self.upload_dir).free < self.min_free_bytes + size:
            raise ImportInputError("disk_space_low", "Ruang disk tidak mencukupi. Impor dihentikan; dokumen sebelumnya tetap tersimpan.")

    def _process(self, candidate: ImportFile, *, with_embeddings: bool, position: int, total: int) -> dict:
        row = {"event": "file", "position": position, "total": total, "file": candidate.relative_path,
               "document_type": candidate.document_type, "document_id": None,
               "action": "none", "status": "failed", "embedding_status": "not_requested", "bytes_stored": 0}
        document = None
        try:
            if candidate.document_type not in {"proposal", "skripsi"}:
                raise ImportInputError("unknown_document_type", "Gunakan folder proposal/skripsi atau isi --document-type.")
            digest, size = pdf_identity(candidate.path, self.max_bytes)
            row["sha256"], row["size_bytes"] = digest, size
            key = (digest, candidate.document_type)
            if key in self.seen:
                return {**row, "status": "skipped", "action": "duplicate_in_input"}
            self.seen.add(key)
            existing = self.lookup.get(key)
            if existing and existing[1]:
                return {**row, "status": "skipped", "action": "soft_deleted", "document_id": existing[0]}
            if existing:
                document = self.db.get(Document, existing[0])
                if document.deleted_at is not None:
                    return {**row, "status": "skipped", "action": "soft_deleted", "document_id": document.id}
                row["document_id"] = document.id
                row["action"] = "existing" if document.repository_status == "published" else "resumed"
            self.emit({**row, "event": "progress", "status": "processing", "stage": "validating"})
            # Skip redundant parsing only for our own completed, current cache.
            metadata = (document.extraction_metadata or {}) if document is not None else {}
            if not (metadata.get("repository_import", {}).get("stage") == "ready"
                    and metadata.get("text_extraction_version") == PDF_EXTRACTION_VERSION
                    and metadata.get("chunk_extraction_version") == PDF_EXTRACTION_VERSION):
                validate_pdf(candidate, self.structure)
            if document is None:
                self._reserve_space(size)
                target = (self.upload_dir / "repository" / candidate.document_type / f"{digest}.pdf").resolve()
                if not target.is_relative_to(self.upload_dir):
                    raise ImportInputError("unsafe_storage_path", "Direktori penyimpanan repositori tidak valid.")
                row["bytes_stored"] = copy_pdf_atomically(candidate.path, target, digest, self.max_bytes)
                document = Document(title=candidate.path.name, document_type=candidate.document_type,
                                    file_path=str(target), content_sha256=digest, source_origin="alumni",
                                    user_id=None, repository_status="draft")
                self.db.add(document)
                self.db.commit()
                row.update(document_id=document.id, action="imported")
                self.lookup[key] = (document.id, False)
                self._checkpoint(document, "stored", input_file=candidate.relative_path)
            elif self._managed_path(document.file_path) is None:
                # Restore a missing copy only inside managed storage, with identical bytes.
                target = Path(document.file_path).resolve()
                if not target.is_relative_to(self.upload_dir):
                    raise ImportInputError("unsafe_storage_path", "Path dokumen lama perlu diperiksa pengelola.")
                self._reserve_space(size)
                row["bytes_stored"] = copy_pdf_atomically(candidate.path, target, digest, self.max_bytes)
            self.emit({**row, "event": "progress", "status": "processing", "stage": "extracting_and_chunking"})
            self.texts.get_or_extract_text(document, self.db)
            chunks = self.chunks.get_or_build_sentence_chunks(document, self.db)
            if not chunks:
                raise ImportInputError("no_comparison_text", "PDF tidak memiliki potongan teks isi yang dapat dibandingkan.")
            document.repository_status = "published"
            document.repository_published_at = document.repository_published_at or utc_now()
            self._checkpoint(document, "ready", error=None)
            row.update(status="completed", chunk_count=len(chunks))
            if with_embeddings:
                self._reserve_space(0)
                row["embedding_attempted"] = True
                self.emit({**row, "event": "progress", "status": "processing", "stage": "embedding"})
                result = self.embeddings.ensure_document_embeddings(document, self.db, retry_failed=True)
                complete = (result.get("status") == "completed" or result.get("reason") == "up_to_date")
                row.update(embedding_status="completed" if complete else result.get("status", "failed"),
                           indexed_chunks=result.get("total_indexed_chunks", 0),
                           failed_chunks=result.get("total_failed_chunks", 0),
                           pending_chunks=result.get("total_pending_chunks", 0))
                if not complete:
                    row.update(status="failed", error_code="embedding_incomplete", error=EMBEDDING_ERROR)
                self._checkpoint(document, "ready", embedding_status=row["embedding_status"])
            elif row["action"] == "existing":
                row["status"] = "skipped"
            return row
        except Exception as exc:
            self.db.rollback()
            error = report_error(exc, REINDEX_ERROR, operation="repository_import")
            row.update(status="failed", error_code=exc.code if isinstance(exc, ImportInputError) else "import_failed",
                       error=str(exc) if isinstance(exc, ImportInputError) else error.text, error_id=error.error_id)
            if row.get("embedding_attempted"):
                row["embedding_status"] = "failed"
            if document is not None and row["document_id"] is not None:
                try:
                    self._checkpoint(document, "failed", error=error.text)
                except Exception as persist_error:
                    self.db.rollback()
                    report_error(persist_error, REINDEX_ERROR, operation="persist_import_failure")
            return row

    def run(self, files: list[ImportFile], *, with_embeddings: bool = False, on_event: Callable[[dict], None] | None = None) -> dict:
        self.emit = on_event or (lambda event: None)
        admin = self.db.query(User).filter(User.identifier == self.admin_identifier, User.is_active.is_(True),
                                          User.role.in_(("admin", "super_admin"))).first()
        if admin is None:
            raise ImportInputError("admin_required", "Akun --admin harus merupakan administrator aktif yang sudah ada.")
        self.actor = {"admin_id": admin.id, "admin_identifier": admin.identifier}
        if with_embeddings and not self.embeddings.embedding_service.is_configured():
            raise ImportInputError("embedding_not_configured", "Konfigurasi embedding belum lengkap. Isi konfigurasi backend atau gunakan mode tanpa --with-embeddings.")
        counts, self.seen, self.lookup = Counter(), set(), {}
        with repository_import_lock(self.db):
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            self._load_existing()
            for position, candidate in enumerate(files, start=1):
                row = self._process(candidate, with_embeddings=with_embeddings, position=position, total=len(files))
                counts["processed"] += 1
                counts[row["status"]] += 1
                counts[row["action"]] += 1
                counts["bytes_stored"] += row["bytes_stored"]
                counts["embedding_completed"] += row["embedding_status"] == "completed"
                counts["embedding_failed"] += bool(row.get("embedding_attempted") and row["status"] == "failed")
                self.emit(row)
                self.db.expunge_all()
                if row.get("error_code") == "disk_space_low":
                    counts["stopped_for_storage"] = 1
                    break
        summary = {"event": "summary", "total": len(files),
                   **{key: counts[key] for key in ("processed", "completed", "skipped", "failed", "imported", "resumed",
                                                   "existing", "duplicate_in_input", "soft_deleted", "bytes_stored",
                                                   "embedding_completed", "embedding_failed", "stopped_for_storage")},
                   "not_attempted": len(files) - counts["processed"]}
        self.emit(summary)
        return summary
