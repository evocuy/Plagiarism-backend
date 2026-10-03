import shutil
import logging
from pathlib import Path
from typing import Optional, List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, Body
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel

from app.database.session import get_db
from app.models.schemas import Document, User, Mahasiswa
from app.routes.auth import get_optional_current_user, get_current_user
from app.services.document_chunk_service import DocumentChunkService
from app.services.document_embedding_service import DocumentEmbeddingService
from app.services.document_index_service import DocumentIndexService
from app.services.document_text_service import DocumentTextService

router = APIRouter()
logger = logging.getLogger(__name__)
document_chunk_service = DocumentChunkService()
document_text_service = DocumentTextService()
document_index_service = DocumentIndexService(
    document_text_service=document_text_service,
    document_chunk_service=document_chunk_service,
)
document_embedding_service = DocumentEmbeddingService(document_chunk_service=document_chunk_service)
UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


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

@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    document_type: str = Form(...),
    user_id: Optional[int] = Form(None),
    auto_index_embeddings: bool = Form(False),
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Hanya file PDF yang diizinkan.")

    clean_type = document_type.strip().lower()
    if not clean_type:
        raise HTTPException(status_code=400, detail="Tipe dokumen wajib diisi (skripsi/proposal).")

    file_path = UPLOAD_DIR / file.filename
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Kaitkan dokumen dengan user ID jika ada session / parameter
    effective_user_id = None
    if current_user:
        effective_user_id = current_user.id
    elif user_id:
        effective_user_id = user_id

    db_doc = Document(
        user_id=effective_user_id,
        title=file.filename,
        document_type=clean_type,
        file_path=str(file_path),
    )
    db.add(db_doc)
    db.commit()
    db.refresh(db_doc)

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
def clear_all_documents(db: Session = Depends(get_db)):
    db.execute(text("TRUNCATE TABLE documents, plagiarism_checks RESTART IDENTITY CASCADE;"))
    db.commit()

    for file_path in UPLOAD_DIR.glob("*.pdf"):
        try:
            file_path.unlink()
        except Exception:
            pass

    return {"message": "Seluruh isi tabel database dan file di folder uploads berhasil dibersihkan, ID telah di-reset ke 1."}
