import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload
from pydantic import BaseModel

from app.database.session import get_db
from app.models.schemas import (
    Document,
    PlagiarismCheck,
    REPOSITORY_STATUS_DRAFT,
    REPOSITORY_STATUS_PUBLISHED,
    SimilarityResult,
    User,
    Mahasiswa,
    utc_now,
)
from app.services.pdf_service import PDFService
from app.services.check_queue_service import CheckQueueService
from app.services.similarity_result_service import SimilarityResultService
from app.routes.auth import get_current_user

router = APIRouter()
pdf_service = PDFService()
similarity_result_service = SimilarityResultService()

class SingleCheckRequest(BaseModel):
    document_id: int
    reference_document_id: int


@router.post("/check", status_code=status.HTTP_202_ACCEPTED)
def check_similarity(
    request: SingleCheckRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Enqueue one direct comparison instead of running it in the API process."""
    doc_a = (
        db.query(Document)
        .filter(Document.id == request.document_id, Document.deleted_at.is_(None))
        .first()
    )
    doc_b = (
        db.query(Document)
        .filter(Document.id == request.reference_document_id, Document.deleted_at.is_(None))
        .first()
    )

    if not doc_a or not doc_b:
        raise HTTPException(status_code=404, detail="Salah satu atau kedua dokumen tidak ditemukan di database.")

    # Only the document owner, their supervising lecturer, or an administrator
    # may initiate a check that creates a result for this target document.
    if not _can_access_document(doc_a, current_user, db):
        raise HTTPException(
            status_code=403,
            detail="Anda tidak memiliki izin untuk mengecek dokumen ini.",
        )

    # A manual comparison must use the same corpus policy as the repository
    # workflow: only supervisor-approved/admin-imported sources of the same
    # document type may be used, never another revision of the same student.
    if doc_a.id == doc_b.id:
        raise HTTPException(
            status_code=409,
            detail="Dokumen tidak dapat dibandingkan dengan dirinya sendiri.",
        )
    if doc_b.repository_status != REPOSITORY_STATUS_PUBLISHED:
        raise HTTPException(
            status_code=409,
            detail="Dokumen referensi belum diterbitkan ke repositori pembanding.",
        )
    if doc_b.document_type != doc_a.document_type:
        raise HTTPException(
            status_code=409,
            detail="Dokumen referensi harus memiliki jenis dokumen yang sama.",
        )

    effective_user_id = doc_a.user_id or current_user.id
    if doc_b.user_id is not None and doc_b.user_id == effective_user_id:
        raise HTTPException(
            status_code=409,
            detail="Dokumen tidak dapat dibandingkan dengan dokumen milik mahasiswa yang sama.",
        )

    queue_service = CheckQueueService()
    check_record, queue_position = queue_service.enqueue(
        db=db,
        document_id=doc_a.id,
        user_id=effective_user_id,
        candidate_limit=1,
        auto_index_embeddings=False,
        auto_index_repository_limit=0,
        reference_document_id=doc_b.id,
    )
    return {
        "check_id": check_record.id,
        "document_id": doc_a.id,
        "reference_document_id": doc_b.id,
        "status": check_record.status,
        "progress": check_record.progress,
        "stage": check_record.processing_stage,
        "message": check_record.processing_message,
        "queue_position": queue_position,
        "status_url": f"/api/plagiarism/check/{check_record.id}/status",
        "result_url": f"/api/plagiarism/check/{check_record.id}/matches",
        "poll_after_ms": queue_service.settings.poll_after_ms,
    }

@router.post("/check-repository/{document_id}", status_code=status.HTTP_202_ACCEPTED)
def check_against_repository(
    document_id: int,
    candidate_limit: int = Query(20, ge=1, le=20),
    auto_index_embeddings: bool = Query(True),
    auto_index_repository_limit: int = Query(50, ge=0, le=50),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Enqueue an existing document for the same bounded repository workflow."""
    target_document = (
        db.query(Document)
        .filter(Document.id == document_id, Document.deleted_at.is_(None))
        .first()
    )
    if not target_document:
        raise HTTPException(status_code=404, detail="Dokumen target tidak ditemukan.")
    if not _can_access_document(target_document, current_user, db):
        raise HTTPException(
            status_code=403,
            detail="Anda tidak memiliki izin untuk mengecek dokumen ini.",
        )

    queue_service = CheckQueueService()
    settings = queue_service.settings
    if candidate_limit > settings.max_candidate_limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"candidate_limit harus berada di antara 1 dan "
                f"{settings.max_candidate_limit}."
            ),
        )
    if auto_index_repository_limit > settings.max_auto_index_repository_limit:
        raise HTTPException(
            status_code=400,
            detail=(
                "auto_index_repository_limit harus berada di antara 0 dan "
                f"{settings.max_auto_index_repository_limit}."
            ),
        )

    effective_user_id = target_document.user_id or current_user.id
    check_record, queue_position = queue_service.enqueue(
        db=db,
        document_id=target_document.id,
        user_id=effective_user_id,
        candidate_limit=candidate_limit,
        auto_index_embeddings=(auto_index_embeddings and settings.auto_index_embeddings),
        auto_index_repository_limit=auto_index_repository_limit,
    )
    return {
        "check_id": check_record.id,
        "document_id": target_document.id,
        "status": check_record.status,
        "progress": check_record.progress,
        "stage": check_record.processing_stage,
        "message": check_record.processing_message,
        "queue_position": queue_position,
        "status_url": f"/api/plagiarism/check/{check_record.id}/status",
        "result_url": f"/api/plagiarism/check/{check_record.id}/matches",
        "poll_after_ms": settings.poll_after_ms,
    }

@router.get("/history")
def get_check_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Mengambil riwayat pengecekan.
    - Mahasiswa: hanya riwayat naskahnya sendiri.
    - Dosen: riwayat naskah mahasiswa bimbingannya.
    - Admin: seluruh riwayat.
    """
    query = (
        db.query(PlagiarismCheck)
        .join(Document, PlagiarismCheck.document_id == Document.id)
        .filter(Document.deleted_at.is_(None))
    )

    if current_user.role == "mahasiswa":
        query = query.filter(
            (PlagiarismCheck.user_id == current_user.id) | (Document.user_id == current_user.id)
        )
    elif current_user.role == "dosen":
        # A role value alone must not grant repository-wide access.  A broken
        # lecturer account without its Dosen profile fails closed instead.
        if not current_user.dosen:
            raise HTTPException(
                status_code=403,
                detail="Profil dosen tidak ditemukan. Hubungi administrator.",
            )
        # Hanya tampilkan riwayat mahasiswa bimbingan dosen ini
        dosen_id = current_user.id
        bimbingan_user_ids = (
            db.query(Mahasiswa.id)
            .filter(Mahasiswa.dosen_pembimbing_id == dosen_id)
            .all()
        )
        bimbingan_user_ids = [uid for (uid,) in bimbingan_user_ids]
        query = query.filter(Document.user_id.in_(bimbingan_user_ids))
    elif current_user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Akses ditolak.")

    checks = query.order_by(PlagiarismCheck.created_at.desc()).all()

    results = []
    for check in checks:
        doc = check.document
        doc_user = doc.user if doc and doc.user else (check.user if check.user else None)
        has_highlighted = bool(check.highlighted_file_path and os.path.exists(check.highlighted_file_path))
        results.append({
            "id": check.id,
            "document_id": check.document_id,
            "user_id": check.user_id or (doc.user_id if doc else None),
            "owner_name": doc_user.nama_lengkap if doc_user else "Anonim",
            "owner_identifier": doc_user.identifier if doc_user else "-",
            "title": doc.title if doc else "Dokumen tidak ditemukan",
            "document_type": doc.document_type if doc else "Unknown",
            # Kept under the legacy key for current clients, but never expose
            # the server-side upload path outside the backend.
            "file_path": doc.title if doc else "",
            "overall_similarity": check.overall_similarity,
            "similarity_percentage": f"{round(check.overall_similarity * 100, 2)}%",
            "status": check.status,
            "progress": 100 if check.status == "completed" else int(check.progress or 0),
            "processing_stage": check.processing_stage,
            "processing_message": check.processing_message,
            "error_message": check.error_message if check.status == "failed" else None,
            "approval_status": check.approval_status or "belum disetujui",
            "reviewed_at": check.reviewed_at.isoformat() if check.reviewed_at else None,
            "reviewer_note": check.reviewer_note,
            "repository_status": doc.repository_status if doc else None,
            "repository_published_at": (
                doc.repository_published_at.isoformat()
                if doc and doc.repository_published_at
                else None
            ),
            "is_repository_published": bool(
                doc and doc.repository_status == REPOSITORY_STATUS_PUBLISHED
            ),
            "highlighted_pdf_available": has_highlighted,
            "highlighted_pdf_url": f"/api/plagiarism/check/{check.id}/download-highlighted" if has_highlighted else None,
            "result_url": f"/api/plagiarism/check/{check.id}/matches" if check.status == "completed" else None,
            "top_matches_url": (
                f"/api/plagiarism/check/{check.id}/top-matches"
                if check.status == "completed"
                else None
            ),
            "created_at": check.created_at.isoformat() if check.created_at else None,
            "completed_at": check.completed_at.isoformat() if check.completed_at else None,
        })
    return results

def _can_access_document(document: Document, user: User, db: Session) -> bool:
    """Return whether an authenticated user may operate on a target document."""
    if user.role in ("admin", "super_admin"):
        return True
    if user.role == "mahasiswa":
        return document.user_id == user.id

    if user.role == "dosen":
        if not user.dosen or not document.user_id:
            return False
        mhs = db.query(Mahasiswa).filter(Mahasiswa.id == document.user_id).first()
        return bool(mhs and mhs.dosen_pembimbing_id == user.id)

    return False


def _can_access_check(check: PlagiarismCheck, user: User, db: Session) -> bool:
    """Helper untuk validasi apakah user berhak melihat hasil check ini."""
    if user.role in ("admin", "super_admin"):
        return True

    document = check.document
    if document:
        return _can_access_document(document, user, db)

    # _get_active_check joins the document table, but keep this fallback safe
    # if an in-memory/stale ORM object is ever passed by a caller.
    return user.role == "mahasiswa" and check.user_id == user.id


def _get_active_check(check_id: int, db: Session) -> Optional[PlagiarismCheck]:
    """Return a check only while its target document remains active."""
    return (
        db.query(PlagiarismCheck)
        .join(Document, PlagiarismCheck.document_id == Document.id)
        .filter(PlagiarismCheck.id == check_id, Document.deleted_at.is_(None))
        .first()
    )


def _serialize_check_progress(check: PlagiarismCheck) -> dict:
    is_completed = check.status == "completed"
    has_highlighted_pdf = bool(
        check.highlighted_file_path and os.path.exists(check.highlighted_file_path)
    )
    progress = 100 if is_completed else int(check.progress or 0)
    return {
        "check_id": check.id,
        "document_id": check.document_id,
        "status": check.status,
        "progress": progress,
        "stage": check.processing_stage or ("completed" if is_completed else "queued"),
        "message": check.processing_message,
        "error": check.error_message if check.status == "failed" else None,
        "is_finished": check.status in {"completed", "failed"},
        "overall_similarity": check.overall_similarity if is_completed else None,
        "similarity_percentage": (
            f"{round(check.overall_similarity * 100, 2)}%" if is_completed else None
        ),
        "result_url": f"/api/plagiarism/check/{check.id}/matches" if is_completed else None,
        "highlighted_pdf_available": has_highlighted_pdf,
        "highlighted_pdf_url": (
            f"/api/plagiarism/check/{check.id}/download-highlighted"
            if has_highlighted_pdf
            else None
        ),
        "started_at": check.started_at.isoformat() if check.started_at else None,
        "updated_at": check.updated_at.isoformat() if check.updated_at else None,
        "completed_at": check.completed_at.isoformat() if check.completed_at else None,
    }


def _get_stored_check_results(check_id: int, db: Session, limit: Optional[int] = None):
    """Load persisted source scores and sentence matches for a completed check."""
    query = (
        db.query(SimilarityResult)
        .options(
            joinedload(SimilarityResult.source_document),
            selectinload(SimilarityResult.matches),
        )
        .filter(SimilarityResult.check_id == check_id)
        .order_by(SimilarityResult.similarity_score.desc(), SimilarityResult.id.asc())
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def _repository_available_count(check: PlagiarismCheck, db: Session) -> int:
    """Count sources currently eligible under this check's corpus policy."""
    target_document = check.document
    if not target_document:
        return 0

    query = db.query(Document).filter(
        Document.id != check.document_id,
        Document.deleted_at.is_(None),
        Document.repository_status == REPOSITORY_STATUS_PUBLISHED,
        Document.document_type == target_document.document_type,
    )
    target_owner_id = target_document.user_id or check.user_id
    if target_owner_id is not None:
        query = query.filter(
            or_(
                Document.user_id.is_(None),
                Document.user_id != target_owner_id,
            )
        )
    return query.count()


def _serialize_check_final_result(
    check: PlagiarismCheck,
    stored_results: list[SimilarityResult],
    db: Session,
) -> dict:
    """Rebuild the final upload-result payload from persisted check data.

    The queue worker persists its result independently from the initial upload
    response, so the frontend fetches this representation after polling reaches
    completed.
    """
    target_document = check.document
    detailed_results = []
    source_matches = []
    persisted_highlight_matches = []

    for result in stored_results:
        source_title = result.source_document.title if result.source_document else None
        source_matches.append(
            {
                "repository_document_id": result.source_document_id,
                "title": source_title or "Dokumen sumber tidak ditemukan",
                "similarity_score": round(result.similarity_score, 4),
                "similarity_percentage": f"{round(result.similarity_score * 100, 2)}%",
            }
        )

        sentence_matches = []
        for match in sorted(result.matches, key=lambda item: item.id):
            sentence_matches.append(
                {
                    "id": match.id,
                    "source_text": match.source_text,
                    "submitted_text": match.submitted_text,
                    "similarity_score": match.similarity_score,
                    "similarity_percentage": f"{round(match.similarity_score * 100, 2)}%",
                    "page_number": match.page_number,
                    "start_position": match.start_position,
                    "end_position": match.end_position,
                }
            )
            persisted_highlight_matches.append(
                {
                    "sentence": match.submitted_text,
                    "matched_source": source_title or "Dokumen sumber tidak diketahui",
                    "source_document_id": result.source_document_id,
                    "page": match.page_number,
                    "start_position": match.start_position,
                    "end_position": match.end_position,
                }
            )

        detailed_results.append(
            {
                "result_id": result.id,
                "source_document_id": result.source_document_id,
                "source_document_title": source_title,
                "chapter": result.chapter,
                "similarity_score": result.similarity_score,
                "similarity_percentage": f"{round(result.similarity_score * 100, 2)}%",
                "matches": sentence_matches,
            }
        )

    extraction_metadata = (
        target_document.extraction_metadata
        if target_document and isinstance(target_document.extraction_metadata, dict)
        else {}
    )
    highlighted_pdf_available = bool(
        check.highlighted_file_path and os.path.exists(check.highlighted_file_path)
    )
    is_completed = check.status == "completed"

    return {
        "check_id": check.id,
        "document_id": check.document_id,
        "status": check.status,
        "progress": 100 if is_completed else int(check.progress or 0),
        "is_finished": check.status in {"completed", "failed"},
        "message": check.processing_message,
        "error": check.error_message if check.status == "failed" else None,
        "target_document": target_document.title if target_document else "Dokumen tidak ditemukan",
        "overall_similarity": check.overall_similarity,
        "highest_similarity_percentage": f"{round(check.overall_similarity * 100, 2)}%",
        "similarity_percentage": f"{round(check.overall_similarity * 100, 2)}%",
        "total_repository_checked": len(stored_results),
        "total_repository_available": _repository_available_count(check, db),
        "matches": source_matches,
        "results": detailed_results,
        "total_plagiarized_sentences": len(persisted_highlight_matches),
        "highlight_summary": similarity_result_service.summarize_highlight_matches(
            persisted_highlight_matches
        ),
        "highlighted_pdf_available": highlighted_pdf_available,
        "highlighted_pdf_url": (
            f"/api/plagiarism/check/{check.id}/download-highlighted"
            if highlighted_pdf_available
            else None
        ),
        "chapter_validation": extraction_metadata.get("chapter_validation"),
        "completed_at": check.completed_at.isoformat() if check.completed_at else None,
    }


def _serialize_top_comparison(result: SimilarityResult, rank: int) -> dict:
    """Serialize one repository document for the history's top-three view."""
    source_title = result.source_document.title if result.source_document else None
    page_numbers = sorted(
        {
            match.page_number
            for match in result.matches
            if match.page_number is not None and match.page_number > 0
        }
    )
    return {
        "rank": rank,
        "repository_document_id": result.source_document_id,
        "title": source_title or "Dokumen sumber tidak ditemukan",
        "document_type": result.source_document.document_type if result.source_document else None,
        "similarity_score": round(result.similarity_score, 4),
        "similarity_percentage": f"{round(result.similarity_score * 100, 2)}%",
        "matched_sentence_count": len(result.matches),
        "matched_page_numbers": page_numbers,
        "matched_page_count": len(page_numbers),
    }


@router.get("/check/{check_id}/status")
def get_check_processing_status(
    check_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return persisted progress for a background repository check."""
    check = _get_active_check(check_id, db)
    if not check:
        raise HTTPException(status_code=404, detail="Data pengecekan tidak ditemukan.")
    if not _can_access_check(check, current_user, db):
        raise HTTPException(status_code=403, detail="Anda tidak memiliki izin untuk melihat status ini.")
    return _serialize_check_progress(check)

@router.get("/check/{check_id}/matches")
def get_check_matches(
    check_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    check = _get_active_check(check_id, db)
    if not check:
        raise HTTPException(status_code=404, detail="Data pengecekan tidak ditemukan.")

    if not _can_access_check(check, current_user, db):
        raise HTTPException(status_code=403, detail="Anda tidak memiliki izin untuk melihat hasil ini.")

    return _serialize_check_final_result(
        check,
        _get_stored_check_results(check_id, db),
        db,
    )


@router.get("/check/{check_id}/top-matches")
def get_check_top_matches(
    check_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the three highest-scoring source documents for a historic check."""
    check = _get_active_check(check_id, db)
    if not check:
        raise HTTPException(status_code=404, detail="Data pengecekan tidak ditemukan.")
    if not _can_access_check(check, current_user, db):
        raise HTTPException(
            status_code=403,
            detail="Anda tidak memiliki izin untuk melihat dokumen pembanding ini.",
        )
    if check.status != "completed":
        raise HTTPException(
            status_code=409,
            detail="Dokumen pembanding tersedia setelah pengecekan selesai.",
        )

    total_repository_checked = (
        db.query(SimilarityResult)
        .filter(SimilarityResult.check_id == check_id)
        .count()
    )
    top_results = _get_stored_check_results(check_id, db, limit=3)
    target_document = check.document
    return {
        "check_id": check.id,
        "document_id": check.document_id,
        "status": check.status,
        "target_document": target_document.title if target_document else "Dokumen tidak ditemukan",
        "total_repository_checked": total_repository_checked,
        "top_match_count": len(top_results),
        "top_matches": [
            _serialize_top_comparison(result, rank)
            for rank, result in enumerate(top_results, start=1)
        ],
    }

@router.get("/check/{check_id}/download-highlighted")
def download_highlighted_pdf(
    check_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Mengunduh / membuka langsung PDF hasil pengecekan yang sudah distabilo kuning.
    Dapat diakses oleh Mahasiswa pemilik berkas dan Dosen pembimbingnya.
    """
    check = _get_active_check(check_id, db)
    if not check:
        raise HTTPException(status_code=404, detail="Data pengecekan tidak ditemukan.")

    if not _can_access_check(check, current_user, db):
        raise HTTPException(status_code=403, detail="Anda tidak memiliki izin untuk melihat dokumen ini.")

    file_to_send = check.highlighted_file_path
    if not file_to_send or not os.path.exists(file_to_send):
        # Fallback jika belum di-generate atau file terhapus, generate sekarang dari dokumen asli
        if check.document and os.path.exists(check.document.file_path):
            output_filename = f"highlighted_check_{check.id}_{Path(check.document.file_path).stem}.pdf"
            file_to_send = pdf_service.generate_highlighted_pdf(
                source_pdf_path=check.document.file_path,
                plagiarized_sentences=[],
                output_filename=output_filename,
            )
            check.highlighted_file_path = file_to_send
            db.commit()
            db.refresh(check)
        else:
            raise HTTPException(status_code=404, detail="File PDF hasil stabilo tidak tersedia di server.")

    clean_filename = f"Hasil_Plagiasi_{Path(file_to_send).name}"
    return FileResponse(
        path=file_to_send,
        media_type="application/pdf",
        filename=clean_filename,
        headers={"Content-Disposition": f"inline; filename={clean_filename}"},
    )

class ApprovalUpdateRequest(BaseModel):
    approval_status: str  # 'disetujui' or 'revisi'
    reviewer_note: Optional[str] = None

@router.patch("/check/{check_id}/approval")
def update_approval_status(
    check_id: int,
    request: ApprovalUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Dosen pembimbing mengubah approval_status dari check milik mahasiswa bimbingannya.
    Nilai yang valid: 'disetujui', 'revisi'
    """
    if request.approval_status not in ("disetujui", "revisi"):
        raise HTTPException(
            status_code=400,
            detail="approval_status harus bernilai 'disetujui' atau 'revisi'.",
        )

    check = _get_active_check(check_id, db)
    if not check:
        raise HTTPException(status_code=404, detail="Check record tidak ditemukan.")

    # Pastikan dosen hanya bisa update check milik mahasiswa bimbingannya.
    if current_user.role == "dosen":
        if not current_user.dosen or not _can_access_check(check, current_user, db):
            raise HTTPException(
                status_code=403,
                detail="Anda hanya dapat me-review dokumen mahasiswa bimbingan Anda.",
            )
    elif current_user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Akses ditolak.")

    if check.status != "completed":
        raise HTTPException(
            status_code=409,
            detail="Persetujuan hanya dapat diberikan setelah pengecekan selesai.",
        )

    reviewed_at = utc_now()
    check.approval_status = request.approval_status
    check.reviewed_at = reviewed_at
    check.reviewer_note = request.reviewer_note
    doc = check.document
    if doc:
        if request.approval_status == "disetujui":
            doc.repository_status = REPOSITORY_STATUS_PUBLISHED
            doc.repository_published_at = reviewed_at
        else:
            doc.repository_status = REPOSITORY_STATUS_DRAFT
            doc.repository_published_at = None
    db.commit()
    db.refresh(check)

    if doc:
        db.refresh(doc)
    doc_user = doc.user if doc and doc.user else None
    repository_message = (
        "Dokumen disetujui dan diterbitkan ke repositori pembanding."
        if request.approval_status == "disetujui"
        else "Dokumen ditandai perlu revisi dan dikeluarkan dari repositori pembanding."
    )

    return {
        "id": check.id,
        "document_id": check.document_id,
        "title": doc.title if doc else "Dokumen tidak ditemukan",
        "document_type": doc.document_type if doc else None,
        "status": check.status,
        "overall_similarity": check.overall_similarity,
        "approval_status": check.approval_status,
        "reviewed_at": check.reviewed_at.isoformat() if check.reviewed_at else None,
        "reviewer_note": check.reviewer_note,
        "repository_status": doc.repository_status if doc else None,
        "repository_published_at": (
            doc.repository_published_at.isoformat()
            if doc and doc.repository_published_at
            else None
        ),
        "is_repository_published": bool(
            doc and doc.repository_status == REPOSITORY_STATUS_PUBLISHED
        ),
        "owner_name": doc_user.nama_lengkap if doc_user else "Anonim",
        "owner_identifier": doc_user.identifier if doc_user else "-",
        "message": repository_message,
    }
