import logging
import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload, selectinload
from pydantic import BaseModel

from app.database.session import get_db
from app.models.schemas import Document, PlagiarismCheck, SimilarityResult, User, Mahasiswa, utc_now
from app.services.pdf_service import PDFService
from app.services.document_chunk_service import DocumentChunkService
from app.services.document_embedding_service import DocumentEmbeddingService
from app.services.document_text_service import DocumentTextService
from app.services.repository_candidate_service import RepositoryCandidateService
from app.services.repository_check_service import RepositoryCheckService
from app.services.similarity_result_service import SimilarityResultService
from app.services.similarity_service import SimilarityService
from app.routes.auth import get_optional_current_user, get_current_user

router = APIRouter()
pdf_service = PDFService()
similarity_service = SimilarityService()
similarity_result_service = SimilarityResultService()
document_chunk_service = DocumentChunkService(pdf_service=pdf_service, similarity_service=similarity_service)
document_text_service = DocumentTextService(pdf_service=pdf_service, similarity_service=similarity_service)
repository_candidate_service = RepositoryCandidateService(document_chunk_service=document_chunk_service)
document_embedding_service = DocumentEmbeddingService(document_chunk_service=document_chunk_service)
repository_check_service = RepositoryCheckService(
    pdf_service=pdf_service,
    similarity_service=similarity_service,
    similarity_result_service=similarity_result_service,
    document_chunk_service=document_chunk_service,
    document_text_service=document_text_service,
    repository_candidate_service=repository_candidate_service,
    document_embedding_service=document_embedding_service,
)
logger = logging.getLogger(__name__)

class SingleCheckRequest(BaseModel):
    document_id: int
    reference_document_id: int


@router.post("/check")
def check_similarity(
    request: SingleCheckRequest,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """Membandingkan 2 dokumen secara spesifik (dokumen A vs dokumen B) serta memberi highlight pada kalimat terdeteksi plagiat"""
    doc_a = db.query(Document).filter(Document.id == request.document_id).first()
    doc_b = db.query(Document).filter(Document.id == request.reference_document_id).first()

    if not doc_a or not doc_b:
        raise HTTPException(status_code=404, detail="Salah satu atau kedua dokumen tidak ditemukan di database.")

    data_a = document_text_service.get_or_extract_text(doc_a, db)
    data_b = document_text_service.get_or_extract_text(doc_b, db)

    score = similarity_service.calculate_clean_text_similarity(data_a["cleaned_text"], data_b["cleaned_text"])

    effective_user_id = current_user.id if current_user else doc_a.user_id

    check_record = PlagiarismCheck(
        document_id=doc_a.id,
        user_id=effective_user_id,
        overall_similarity=score,
        status="completed",
        progress=100,
        processing_stage="completed",
        processing_message="Pengecekan kemiripan selesai.",
        started_at=utc_now(),
        completed_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(check_record)
    db.commit()
    db.refresh(check_record)

    result_record = similarity_result_service.create_result(
        db=db,
        check_id=check_record.id,
        source_document_id=doc_b.id,
        similarity_score=score,
    )

    # Deteksi kalimat mirip dan beri highlight kuning stabilo di file PDF
    highlighted_file = None
    plagiarized_sentences = []
    highlight_summary = similarity_result_service.summarize_highlight_matches([])
    try:
        repository_check_service._auto_index_embeddings_for_check(
            target_doc=doc_a,
            repo_docs=[doc_b],
            db=db,
            enabled=True,
            repository_limit=1,
        )
        sentences_a = document_chunk_service.get_or_build_sentence_chunks(doc_a, db)
        sentences_b = document_chunk_service.get_or_build_sentence_chunks(doc_b, db)

        lexical_matches = similarity_service.find_sentence_matches(
            target_sentences=sentences_a,
            reference_sentences=sentences_b,
            matched_source=doc_b.title,
            source_document_id=doc_b.id,
            threshold=0.70,
        )
        # Embedding dipakai untuk memilih kandidat, sedangkan stabilo hanya
        # menunjukkan teks yang benar-benar sama agar pemilik dokumen tahu
        # bagian konkret yang perlu direvisi.
        plagiarized_sentences = repository_check_service._merge_highlight_matches(lexical_matches)
        highlight_summary = similarity_result_service.summarize_highlight_matches(
            plagiarized_sentences
        )
        similarity_result_service.create_matches(
            db=db,
            result_id=result_record.id,
            matches=plagiarized_sentences,
        )

        output_filename = f"highlighted_check_{check_record.id}_{Path(doc_a.file_path).stem}.pdf"
        highlighted_file = pdf_service.generate_highlighted_pdf(
            source_pdf_path=doc_a.file_path,
            plagiarized_sentences=plagiarized_sentences,
            output_filename=output_filename,
        )
        check_record.highlighted_file_path = highlighted_file
        db.commit()
        db.refresh(check_record)
    except Exception as hl_err:
        logger.warning(f"Gagal generate highlighted PDF untuk check {check_record.id}: {hl_err}")
        try:
            output_filename = f"highlighted_check_{check_record.id}_{Path(doc_a.file_path).stem}.pdf"
            highlighted_file = pdf_service.generate_highlighted_pdf(
                source_pdf_path=doc_a.file_path,
                plagiarized_sentences=[],
                output_filename=output_filename,
            )
            check_record.highlighted_file_path = highlighted_file
            db.commit()
            db.refresh(check_record)
        except Exception as fallback_err:
            logger.warning(f"Gagal membuat fallback PDF untuk check {check_record.id}: {fallback_err}")

    return {
        "check_id": check_record.id,
        "target_document": doc_a.title,
        "reference_document": doc_b.title,
        "similarity_score": round(score, 4),
        "similarity_percentage": f"{round(score * 100, 2)}%",
        "status": check_record.status,
        "total_plagiarized_sentences": len(plagiarized_sentences),
        "highlight_summary": highlight_summary,
        "highlighted_pdf_available": bool(highlighted_file and os.path.exists(highlighted_file)),
        "chapter_validation": data_a.get("chapter_validation"),
    }

@router.post("/check-repository/{document_id}")
def check_against_repository(
    document_id: int,
    candidate_limit: int = Query(20, ge=1, le=100),
    auto_index_embeddings: bool = Query(True),
    auto_index_repository_limit: int = Query(50, ge=0, le=500),
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """Check one document against the internal repository synchronously."""
    return repository_check_service.run_repository_check(
        document_id=document_id,
        db=db,
        requester_user_id=current_user.id if current_user else None,
        candidate_limit=candidate_limit,
        auto_index_embeddings=auto_index_embeddings,
        auto_index_repository_limit=auto_index_repository_limit,
    )

@router.get("/history")
def get_check_history(
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    Mengambil riwayat pengecekan.
    - Mahasiswa: hanya riwayat naskahnya sendiri.
    - Dosen: riwayat naskah mahasiswa bimbingannya.
    - Admin: seluruh riwayat.
    """
    query = db.query(PlagiarismCheck)

    if current_user and current_user.role == "mahasiswa":
        query = query.join(Document, PlagiarismCheck.document_id == Document.id).filter(
            (PlagiarismCheck.user_id == current_user.id) | (Document.user_id == current_user.id)
        )
    elif current_user and current_user.role == "dosen" and current_user.dosen:
        # Hanya tampilkan riwayat mahasiswa bimbingan dosen ini
        dosen_id = current_user.id
        bimbingan_user_ids = (
            db.query(Mahasiswa.id)
            .filter(Mahasiswa.dosen_pembimbing_id == dosen_id)
            .all()
        )
        bimbingan_user_ids = [uid for (uid,) in bimbingan_user_ids]
        query = query.join(Document, PlagiarismCheck.document_id == Document.id).filter(
            Document.user_id.in_(bimbingan_user_ids)
        )

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
            "file_path": doc.file_path if doc else "",
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

def _can_access_check(check: PlagiarismCheck, user: Optional[User], db: Session) -> bool:
    """Helper untuk validasi apakah user berhak melihat hasil check ini."""
    if not user:
        return True  # Mengizinkan akses jika autentikasi opsional
    if user.role in ("admin", "super_admin"):
        return True

    doc = check.document
    doc_owner_id = doc.user_id if doc else check.user_id
    if user.role == "mahasiswa":
        return check.user_id == user.id or doc_owner_id == user.id

    if user.role == "dosen":
        if not doc_owner_id:
            return True
        mhs = db.query(Mahasiswa).filter(Mahasiswa.id == doc_owner_id).first()
        return bool(mhs and mhs.dosen_pembimbing_id == user.id)

    return False


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
    """Mirror the repository scope used when this check was started."""
    target_document = check.document
    if not target_document:
        return 0

    query = db.query(Document).filter(Document.id != check.document_id)
    target_owner_id = target_document.user_id or check.user_id
    if target_owner_id is not None:
        query = query.filter(Document.user_id != target_owner_id)
    return query.count()


def _serialize_check_final_result(
    check: PlagiarismCheck,
    stored_results: list[SimilarityResult],
    db: Session,
) -> dict:
    """Rebuild the final upload-result payload from persisted check data.

    BackgroundTasks discards the return value of RepositoryCheckService, so
    the frontend fetches this representation after polling reaches completed.
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
    check = db.query(PlagiarismCheck).filter(PlagiarismCheck.id == check_id).first()
    if not check:
        raise HTTPException(status_code=404, detail="Data pengecekan tidak ditemukan.")
    if not _can_access_check(check, current_user, db):
        raise HTTPException(status_code=403, detail="Anda tidak memiliki izin untuk melihat status ini.")
    return _serialize_check_progress(check)

@router.get("/check/{check_id}/matches")
def get_check_matches(
    check_id: int,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    check = db.query(PlagiarismCheck).filter(PlagiarismCheck.id == check_id).first()
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
    check = db.query(PlagiarismCheck).filter(PlagiarismCheck.id == check_id).first()
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
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    Mengunduh / membuka langsung PDF hasil pengecekan yang sudah distabilo kuning.
    Dapat diakses oleh Mahasiswa pemilik berkas dan Dosen pembimbingnya.
    """
    check = db.query(PlagiarismCheck).filter(PlagiarismCheck.id == check_id).first()
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

    check = db.query(PlagiarismCheck).filter(PlagiarismCheck.id == check_id).first()
    if not check:
        raise HTTPException(status_code=404, detail="Check record tidak ditemukan.")

    # Pastikan dosen hanya bisa update check milik mahasiswa bimbingannya
    if current_user.role == "dosen":
        doc = check.document
        if doc and doc.user_id:
            mhs = db.query(Mahasiswa).filter(Mahasiswa.id == doc.user_id).first()
            if not mhs or mhs.dosen_pembimbing_id != current_user.id:
                raise HTTPException(
                    status_code=403,
                    detail="Anda hanya dapat me-review dokumen mahasiswa bimbingan Anda.",
                )
    elif current_user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Akses ditolak.")

    check.approval_status = request.approval_status
    check.reviewed_at = utc_now()
    check.reviewer_note = request.reviewer_note
    db.commit()
    db.refresh(check)

    doc = check.document
    doc_user = doc.user if doc and doc.user else None

    return {
        "id": check.id,
        "document_id": check.document_id,
        "approval_status": check.approval_status,
        "reviewed_at": check.reviewed_at.isoformat() if check.reviewed_at else None,
        "reviewer_note": check.reviewer_note,
        "owner_name": doc_user.nama_lengkap if doc_user else "Anonim",
        "message": f"Status dokumen berhasil diubah menjadi '{check.approval_status}'.",
    }
