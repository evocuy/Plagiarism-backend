import logging
import os
from pathlib import Path
from typing import Optional, List
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, UploadFile, File, Form, HTTPException, Depends, Body, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel

from app.database.session import get_db
from app.models.schemas import Document, PlagiarismCheck, SimilarityResult, User, Mahasiswa, utc_now
from app.routes.auth import get_current_user, get_optional_current_user, require_admin
from app.services.document_chunk_service import DocumentChunkService
from app.services.document_embedding_service import DocumentEmbeddingService
from app.services.document_index_service import DocumentIndexService
from app.services.document_structure_service import DocumentStructureService
from app.services.document_text_service import DocumentTextService
from app.services.repository_check_service import run_repository_check_in_background

router = APIRouter()
logger = logging.getLogger(__name__)
document_chunk_service = DocumentChunkService()
document_text_service = DocumentTextService()
document_index_service = DocumentIndexService(
    document_text_service=document_text_service,
    document_chunk_service=document_chunk_service,
)
document_embedding_service = DocumentEmbeddingService(document_chunk_service=document_chunk_service)
document_structure_service = DocumentStructureService()
UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50")) * 1024 * 1024
ALLOWED_PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "application/acrobat",
    "applications/vnd.pdf",
    "text/pdf",
}
ALLOWED_FILENAME_STEM_PUNCTUATION = {" ", "_", "-", ".", "(", ")"}


class ReindexDocumentsRequest(BaseModel):
    document_ids: Optional[List[int]] = None


class EmbedDocumentsRequest(BaseModel):
    document_ids: Optional[List[int]] = None
    refresh_chunks: bool = False
    refresh_embeddings: bool = False


def _can_access_document(document: Document, user: Optional[User], db: Session) -> bool:
    if not user:
        return True
    if user.role in ("admin", "super_admin"):
        return True
    if user.role == "mahasiswa":
        return document.user_id == user.id
    if user.role == "dosen":
        if not document.user_id:
            return True
        mhs = db.query(Mahasiswa).filter(Mahasiswa.id == document.user_id).first()
        return bool(mhs and mhs.dosen_pembimbing_id == user.id)
    return False


def _visible_documents_query(current_user: Optional[User], db: Session):
    query = db.query(Document)
    if current_user and current_user.role == "mahasiswa":
        query = query.filter(Document.user_id == current_user.id)
    elif current_user and current_user.role == "dosen" and current_user.dosen:
        dosen_id = current_user.id
        bimbingan_user_ids = (
            db.query(Mahasiswa.id)
            .filter(Mahasiswa.dosen_pembimbing_id == dosen_id)
            .all()
        )
        bimbingan_user_ids = [uid for (uid,) in bimbingan_user_ids]
        query = query.filter(Document.user_id.in_(bimbingan_user_ids))
    return query


def _effective_upload_user_id(current_user: Optional[User], user_id: Optional[int]) -> Optional[int]:
    if current_user:
        return current_user.id
    return user_id


def _validate_uploaded_filename(filename: Optional[str]) -> str:
    """Return a safe display/storage filename or reject unsupported characters."""
    raw_filename = filename or ""
    if "/" in raw_filename or "\\" in raw_filename:
        raise HTTPException(status_code=400, detail="Nama file tidak valid.")

    original_filename = Path(raw_filename).name
    if not original_filename or Path(original_filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Hanya file PDF yang diizinkan.")

    stem = Path(original_filename).stem
    has_letter_or_number = any(character.isalnum() for character in stem)
    has_unsupported_character = any(
        not (character.isalnum() or character in ALLOWED_FILENAME_STEM_PUNCTUATION)
        for character in stem
    )
    if not has_letter_or_number or has_unsupported_character:
        raise HTTPException(
            status_code=400,
            detail=(
                "Nama file hanya boleh berisi huruf, angka, spasi, tanda kurung, "
                "titik, garis bawah, atau tanda hubung."
            ),
        )
    return original_filename


def _delete_managed_upload_file(file_path: Optional[str]) -> bool:
    """Delete an artifact only when it belongs to this backend's upload directory."""
    if not file_path:
        return False

    try:
        upload_root = UPLOAD_DIR.resolve()
        candidate = Path(file_path).resolve()
        if not candidate.is_relative_to(upload_root):
            logger.warning("Melewati penghapusan file di luar upload directory: %s", candidate)
            return False
        if not candidate.is_file():
            return False
        candidate.unlink()
        return True
    except (OSError, RuntimeError, ValueError) as error:
        logger.warning("Gagal menghapus file dokumen %s: %s", file_path, error)
        return False


def _is_file_referenced_elsewhere(
    file_path: str,
    document_id: int,
    check_ids: List[int],
    db: Session,
) -> bool:
    if (
        db.query(Document.id)
        .filter(Document.id != document_id, Document.file_path == file_path)
        .first()
    ):
        return True

    highlighted_query = db.query(PlagiarismCheck.id).filter(
        PlagiarismCheck.highlighted_file_path == file_path
    )
    if check_ids:
        highlighted_query = highlighted_query.filter(~PlagiarismCheck.id.in_(check_ids))
    return highlighted_query.first() is not None


async def _store_uploaded_document(
    *,
    file: UploadFile,
    document_type: str,
    user_id: Optional[int],
    db: Session,
) -> Document:
    """Validate, store, and persist one PDF without using the raw filename as a path."""
    original_filename = _validate_uploaded_filename(file.filename)

    content_type = (file.content_type or "").lower()
    if content_type and content_type not in ALLOWED_PDF_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Tipe konten file harus berupa PDF.")

    try:
        clean_type = document_structure_service.normalize_document_type(document_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stored_filename = f"{uuid4().hex}_{original_filename}"
    file_path = UPLOAD_DIR / stored_filename
    total_bytes = 0

    try:
        with file_path.open("wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Ukuran file melebihi batas {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                    )
                buffer.write(chunk)

        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="File PDF tidak boleh kosong.")

        structure_validation = document_structure_service.validate_pdf_structure(
            file_path,
            clean_type,
        )
        if not structure_validation["is_valid"]:
            logger.info(
                "Menolak upload %s karena struktur %s: %s",
                original_filename,
                structure_validation["error_code"],
                structure_validation["message"],
            )
            raise HTTPException(
                status_code=422,
                detail=structure_validation["message"],
            )

        db_doc = Document(
            user_id=user_id,
            title=original_filename,
            document_type=clean_type,
            file_path=str(file_path),
        )
        db.add(db_doc)
        db.commit()
        db.refresh(db_doc)
        return db_doc
    except Exception:
        db.rollback()
        try:
            if file_path.exists():
                file_path.unlink()
        except OSError as cleanup_error:
            logger.warning("Gagal menghapus file upload yang dibatalkan %s: %s", file_path, cleanup_error)
        raise

@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    document_type: str = Form(...),
    user_id: Optional[int] = Form(None),
    auto_index_embeddings: bool = Form(False),
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    db_doc = await _store_uploaded_document(
        file=file,
        document_type=document_type,
        user_id=_effective_upload_user_id(current_user, user_id),
        db=db,
    )

    text_cache_status = "completed"
    text_cache_error = None
    chunk_cache_status = "completed"
    chunk_count = 0
    chunk_cache_error = None
    embedding_auto_index = {
        "enabled": auto_index_embeddings,
        "configured": document_embedding_service.embedding_service.is_configured(),
        "embedding_model": document_embedding_service.embedding_service.model,
        "status": "skipped",
        "reason": "disabled_by_request",
    }
    try:
        document_text_service.get_or_extract_text(db_doc, db)
    except Exception as err:
        text_cache_status = "failed"
        text_cache_error = str(err)
        logger.warning(f"Gagal membuat cache teks untuk dokumen {db_doc.id}: {err}")

    try:
        chunks = document_chunk_service.get_or_build_sentence_chunks(db_doc, db)
        chunk_count = len(chunks)
    except Exception as err:
        chunk_cache_status = "failed"
        chunk_cache_error = str(err)
        logger.warning(f"Gagal membuat cache chunk untuk dokumen {db_doc.id}: {err}")

    if auto_index_embeddings:
        try:
            result = document_embedding_service.ensure_document_embeddings(db_doc, db)
            index_result = result.get("index_result") or {}
            embedding_auto_index = {
                "enabled": True,
                "configured": document_embedding_service.embedding_service.is_configured(),
                "embedding_model": document_embedding_service.embedding_service.model,
                "status": result.get("status"),
                "reason": result.get("reason"),
                "total_chunks": result.get("total_chunks"),
                "total_pending_chunks": result.get("total_pending_chunks"),
                "total_indexed": index_result.get("total_indexed", 0),
                "total_failed": index_result.get("total_failed", 0),
            }
        except Exception as err:
            db.rollback()
            embedding_auto_index = {
                "enabled": True,
                "configured": document_embedding_service.embedding_service.is_configured(),
                "embedding_model": document_embedding_service.embedding_service.model,
                "status": "failed",
                "reason": str(err),
            }
            logger.warning(f"Gagal auto-index embedding dokumen {db_doc.id}: {err}")

    return {
        "id": db_doc.id,
        "user_id": db_doc.user_id,
        "filename": db_doc.title,
        "document_type": db_doc.document_type,
        "file_path": db_doc.file_path,
        "text_cache_status": text_cache_status,
        "text_cache_error": text_cache_error,
        "chunk_cache_status": chunk_cache_status,
        "chunk_count": chunk_count,
        "chunk_cache_error": chunk_cache_error,
        "embedding_auto_index": embedding_auto_index,
        "message": f"File berhasil diunggah sebagai dokumen {db_doc.document_type.upper()} dan tersimpan di database.",
    }


@router.post("/upload-and-check", status_code=status.HTTP_202_ACCEPTED)
async def upload_and_check_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    document_type: str = Form(...),
    user_id: Optional[int] = Form(None),
    candidate_limit: int = Form(20),
    auto_index_embeddings: bool = Form(True),
    auto_index_repository_limit: int = Form(50),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a PDF and start its repository check without holding the request open."""
    if not 1 <= candidate_limit <= 100:
        raise HTTPException(status_code=400, detail="candidate_limit harus berada di antara 1 dan 100.")
    if not 0 <= auto_index_repository_limit <= 500:
        raise HTTPException(
            status_code=400,
            detail="auto_index_repository_limit harus berada di antara 0 dan 500.",
        )

    # For this asynchronous flow the authenticated uploader owns the job.
    # ``user_id`` is kept in the form contract for client compatibility.
    effective_user_id = current_user.id
    db_doc = await _store_uploaded_document(
        file=file,
        document_type=document_type,
        user_id=effective_user_id,
        db=db,
    )

    check_record = PlagiarismCheck(
        document_id=db_doc.id,
        user_id=effective_user_id,
        overall_similarity=0.0,
        status="pending",
        progress=0,
        processing_stage="queued",
        processing_message="File diterima dan menunggu proses pengecekan.",
        updated_at=utc_now(),
    )
    try:
        db.add(check_record)
        db.commit()
        db.refresh(check_record)
    except Exception:
        stored_file_path = Path(db_doc.file_path)
        db.rollback()
        try:
            db.delete(db_doc)
            db.commit()
        finally:
            try:
                if stored_file_path.exists():
                    stored_file_path.unlink()
            except OSError as cleanup_error:
                logger.warning(
                    "Gagal menghapus file upload untuk check yang batal %s: %s",
                    stored_file_path,
                    cleanup_error,
                )
        raise

    background_tasks.add_task(
        run_repository_check_in_background,
        document_id=db_doc.id,
        check_id=check_record.id,
        requester_user_id=effective_user_id,
        candidate_limit=candidate_limit,
        auto_index_embeddings=auto_index_embeddings,
        auto_index_repository_limit=auto_index_repository_limit,
    )

    return {
        "check_id": check_record.id,
        "document_id": db_doc.id,
        "filename": db_doc.title,
        "document_type": db_doc.document_type,
        "status": check_record.status,
        "progress": check_record.progress,
        "stage": check_record.processing_stage,
        "message": check_record.processing_message,
        "status_url": f"/api/plagiarism/check/{check_record.id}/status",
        "result_url": f"/api/plagiarism/check/{check_record.id}/matches",
        "poll_after_ms": 1000,
    }

@router.get("/")
def get_all_documents(
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    - Mahasiswa: hanya dokumen miliknya.
    - Dosen: dokumen milik mahasiswa bimbingannya.
    - Admin / tanpa login: seluruh dokumen.
    """
    query = _visible_documents_query(current_user, db)
    docs = query.order_by(Document.created_at.desc()).all()
    results = []
    for doc in docs:
        results.append({
            "id": doc.id,
            "user_id": doc.user_id,
            "owner_name": doc.user.nama_lengkap if doc.user else "Anonim",
            "owner_identifier": doc.user.identifier if doc.user else "-",
            "title": doc.title,
            "document_type": doc.document_type,
            "file_path": doc.file_path,
            "text_cache_status": "completed" if doc.text_extracted_at else "pending",
            "text_extraction_error": doc.text_extraction_error,
            "chunk_count": len(doc.chunks),
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
        })
    return results


@router.post("/reindex")
def reindex_documents(
    request: Optional[ReindexDocumentsRequest] = Body(default=None),
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    document_ids = request.document_ids if request else None
    query = _visible_documents_query(current_user, db)
    if document_ids:
        query = query.filter(Document.id.in_(document_ids))

    documents = query.order_by(Document.id.asc()).all()
    results = []
    failed = []

    for document in documents:
        try:
            results.append(document_index_service.reindex_document(document, db))
        except Exception as err:
            failed.append({
                "document_id": document.id,
                "title": document.title,
                "error": str(err),
            })
            logger.warning(f"Gagal reindex dokumen {document.id}: {err}")

    return {
        "total_requested": len(document_ids) if document_ids else len(documents),
        "total_reindexed": len(results),
        "total_failed": len(failed),
        "results": results,
        "failed": failed,
    }


@router.post("/embeddings")
def index_document_embeddings(
    request: Optional[EmbedDocumentsRequest] = Body(default=None),
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    if not document_embedding_service.embedding_service.is_configured():
        raise HTTPException(
            status_code=500,
            detail="Embedding config belum lengkap. Pastikan EMBED_URL, EMBED_API_KEY, dan EMBED_MODEL tersedia.",
        )

    document_ids = request.document_ids if request else None
    refresh_chunks = request.refresh_chunks if request else False
    refresh_embeddings = request.refresh_embeddings if request else False

    query = _visible_documents_query(current_user, db)
    if document_ids:
        query = query.filter(Document.id.in_(document_ids))

    documents = query.order_by(Document.id.asc()).all()
    results = []
    failed = []

    for document in documents:
        try:
            results.append(
                document_embedding_service.index_document_embeddings(
                    document=document,
                    db=db,
                    refresh_chunks=refresh_chunks,
                    refresh_embeddings=refresh_embeddings,
                )
            )
        except Exception as err:
            failed.append({
                "document_id": document.id,
                "title": document.title,
                "error": str(err),
            })
            logger.warning(f"Gagal index embedding dokumen {document.id}: {err}")

    return {
        "embedding_model": document_embedding_service.embedding_service.model,
        "total_requested": len(document_ids) if document_ids else len(documents),
        "total_processed": len(results),
        "total_failed": len(failed),
        "results": results,
        "failed": failed,
    }


@router.post("/{document_id}/reindex")
def reindex_document(
    document_id: int,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan.")

    if not _can_access_document(document, current_user, db):
        raise HTTPException(status_code=403, detail="Anda tidak memiliki izin untuk reindex dokumen ini.")

    try:
        result = document_index_service.reindex_document(document, db)
    except Exception as err:
        logger.warning(f"Gagal reindex dokumen {document.id}: {err}")
        raise HTTPException(status_code=500, detail=f"Gagal reindex dokumen: {str(err)}")

    return result


@router.post("/{document_id}/embeddings")
def index_single_document_embeddings(
    document_id: int,
    request: Optional[EmbedDocumentsRequest] = Body(default=None),
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    if not document_embedding_service.embedding_service.is_configured():
        raise HTTPException(
            status_code=500,
            detail="Embedding config belum lengkap. Pastikan EMBED_URL, EMBED_API_KEY, dan EMBED_MODEL tersedia.",
        )

    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan.")

    if not _can_access_document(document, current_user, db):
        raise HTTPException(status_code=403, detail="Anda tidak memiliki izin untuk index embedding dokumen ini.")

    try:
        return document_embedding_service.index_document_embeddings(
            document=document,
            db=db,
            refresh_chunks=request.refresh_chunks if request else False,
            refresh_embeddings=request.refresh_embeddings if request else False,
        )
    except Exception as err:
        logger.warning(f"Gagal index embedding dokumen {document.id}: {err}")
        raise HTTPException(status_code=500, detail=f"Gagal index embedding dokumen: {str(err)}")

@router.delete("/clear")
def clear_all_documents(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    db.execute(text("TRUNCATE TABLE documents, plagiarism_checks RESTART IDENTITY CASCADE;"))
    db.commit()

    for file_path in UPLOAD_DIR.glob("*.pdf"):
        try:
            file_path.unlink()
        except Exception:
            pass

    return {"message": "Seluruh isi tabel database dan file di folder uploads berhasil dibersihkan, ID telah di-reset ke 1."}


@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin-only deletion of one document and its local processing artifacts."""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan.")

    check_records = (
        db.query(PlagiarismCheck)
        .filter(PlagiarismCheck.document_id == document_id)
        .all()
    )
    if any(check.status in {"pending", "processing"} for check in check_records):
        raise HTTPException(
            status_code=409,
            detail="Dokumen tidak dapat dihapus saat pengecekan masih diproses.",
        )
    check_ids = [check.id for check in check_records]
    candidate_file_paths = {
        path
        for path in [
            document.file_path,
            *(check.highlighted_file_path for check in check_records),
        ]
        if path
    }
    file_paths_to_remove = [
        path
        for path in candidate_file_paths
        if not _is_file_referenced_elsewhere(path, document_id, check_ids, db)
    ]

    try:
        # A document may be the source of another user's check.  Delete those
        # result rows first, because source_document_id is non-nullable.
        source_results = (
            db.query(SimilarityResult)
            .filter(SimilarityResult.source_document_id == document_id)
            .all()
        )
        for result in source_results:
            db.delete(result)
        db.flush()

        document_title = document.title
        deleted_check_count = len(check_records)
        db.delete(document)
        db.commit()
    except Exception as error:
        db.rollback()
        logger.exception("Gagal menghapus dokumen %s: %s", document_id, error)
        raise HTTPException(status_code=500, detail="Gagal menghapus dokumen.") from error

    deleted_file_count = sum(
        _delete_managed_upload_file(file_path)
        for file_path in file_paths_to_remove
    )
    return {
        "document_id": document_id,
        "title": document_title,
        "deleted_check_count": deleted_check_count,
        "deleted_file_count": deleted_file_count,
        "message": f"Dokumen '{document_title}' berhasil dihapus.",
    }
