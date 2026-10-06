import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database.session import get_db
from app.models.schemas import Document, PlagiarismCheck, SimilarityResult, User, Mahasiswa, utc_now
from app.services.pdf_service import PDFService
from app.services.document_chunk_service import DocumentChunkService
from app.services.document_embedding_service import DocumentEmbeddingService
from app.services.document_text_service import DocumentTextService
from app.services.repository_candidate_service import RepositoryCandidateService
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
logger = logging.getLogger(__name__)

class SingleCheckRequest(BaseModel):
    document_id: int
    reference_document_id: int


def _merge_highlight_matches(*match_groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged = []
    seen = set()

    for group in match_groups:
        for match in group or []:
            key = (
                match.get("page"),
                (match.get("sentence") or "").strip().lower(),
                match.get("source_document_id"),
                (match.get("reference_sentence") or "").strip().lower(),
                match.get("match_type"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(match)

    return merged


def _summarize_embedding_index_result(result):
    index_result = result.get("index_result") or {}
    return {
        "document_id": result.get("document_id"),
        "title": result.get("title"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "total_chunks": result.get("total_chunks"),
        "total_indexed_chunks": result.get("total_indexed_chunks"),
        "total_pending_chunks": result.get("total_pending_chunks"),
        "total_failed_chunks": result.get("total_failed_chunks"),
        "total_indexed": index_result.get("total_indexed", 0),
        "total_failed": index_result.get("total_failed", 0),
    }


def _auto_index_embeddings_for_check(
    target_doc: Document,
    repo_docs: Sequence[Document],
    db: Session,
    enabled: bool,
    repository_limit: int,
):
    status = {
        "enabled": enabled,
        "configured": document_embedding_service.embedding_service.is_configured(),
        "embedding_model": document_embedding_service.embedding_service.model,
        "repository_limit": repository_limit,
        "target": None,
        "repository": [],
        "total_repository_considered": 0,
        "total_failed": 0,
        "reason": None,
    }
    if not enabled:
        status["reason"] = "disabled_by_request"
        return status
    if not status["configured"]:
        status["reason"] = "embedding_config_not_configured"
        return status

    try:
        target_result = document_embedding_service.ensure_document_embeddings(target_doc, db)
        status["target"] = _summarize_embedding_index_result(target_result)
        if target_result.get("status") == "failed":
            status["total_failed"] += 1
        target_indexed_chunks = target_result.get("total_indexed_chunks") or 0
        target_newly_indexed = (target_result.get("index_result") or {}).get("total_indexed") or 0
        if target_result.get("status") == "failed" or (
            target_indexed_chunks == 0
            and target_newly_indexed == 0
            and target_result.get("reason") in {"previous_embedding_errors", "embedding_config_not_configured"}
        ):
            status["reason"] = "target_embedding_unavailable_tfidf_fallback"
            return status
    except Exception as err:
        db.rollback()
        status["target"] = {
            "document_id": target_doc.id,
            "title": target_doc.title,
            "status": "failed",
            "reason": str(err),
        }
        status["total_failed"] += 1
        status["reason"] = "target_embedding_failed_tfidf_fallback"
        logger.warning(f"Gagal auto-index embedding target {target_doc.id}: {err}")
        return status

    repository_docs_to_index = list(repo_docs[:repository_limit]) if repository_limit else []
    status["total_repository_considered"] = len(repository_docs_to_index)
    if not repository_docs_to_index:
        status["reason"] = status["reason"] or "repository_auto_index_empty"
        return status

    for document in repository_docs_to_index:
        try:
            result = document_embedding_service.ensure_document_embeddings(document, db)
            status["repository"].append(_summarize_embedding_index_result(result))
            if result.get("status") == "failed":
                status["total_failed"] += 1
        except Exception as err:
            db.rollback()
            status["repository"].append({
                "document_id": document.id,
                "title": document.title,
                "status": "failed",
                "reason": str(err),
            })
            status["total_failed"] += 1
            logger.warning(f"Gagal auto-index embedding repository {document.id}: {err}")

    status["reason"] = status["reason"] or "best_effort_completed"
    return status

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
    try:
        _auto_index_embeddings_for_check(
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
        semantic_matches = repository_candidate_service.find_semantic_sentence_matches(
            target_document=doc_a,
            repository_documents=[doc_b],
            db=db,
        )
        plagiarized_sentences = _merge_highlight_matches(lexical_matches, semantic_matches)
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
    """Membandingkan 1 dokumen mahasiswa terhadap SELURUH dokumen di repositori kampus dan menghasilkan PDF kuning stabilo"""
    target_doc = db.query(Document).filter(Document.id == document_id).first()
    if not target_doc:
        raise HTTPException(status_code=404, detail="Dokumen target tidak ditemukan.")

    # Kecualikan dokumen target dan seluruh dokumen milik user yang sama (agar file revisi tidak terdeteksi plagiat terhadap file sendiri)
    target_owner_id = target_doc.user_id or (current_user.id if current_user else None)
    repo_query = db.query(Document).filter(Document.id != document_id)
    if target_owner_id is not None:
        repo_query = repo_query.filter(Document.user_id != target_owner_id)

    repo_docs = repo_query.all()
    effective_user_id = target_owner_id
    embedding_auto_index = (
        _auto_index_embeddings_for_check(
            target_doc=target_doc,
            repo_docs=repo_docs,
            db=db,
            enabled=auto_index_embeddings,
            repository_limit=auto_index_repository_limit,
        )
        if repo_docs else {
            "enabled": auto_index_embeddings,
            "configured": document_embedding_service.embedding_service.is_configured(),
            "embedding_model": document_embedding_service.embedding_service.model,
            "repository_limit": auto_index_repository_limit,
            "target": None,
            "repository": [],
            "total_repository_considered": 0,
            "total_failed": 0,
            "reason": "repository_empty",
        }
    )

    # Jika repositori belum memiliki dokumen lain
    if not repo_docs:
        check_record = PlagiarismCheck(
            document_id=target_doc.id,
            user_id=effective_user_id,
            overall_similarity=0.0,
            status="completed",
        )
        db.add(check_record)
        db.commit()
        db.refresh(check_record)

        # Buat copy dokumen sebagai highlighted default jika perlu
        output_filename = f"highlighted_check_{check_record.id}_{Path(target_doc.file_path).stem}.pdf"
        highlighted_file = pdf_service.generate_highlighted_pdf(
            source_pdf_path=target_doc.file_path,
            plagiarized_sentences=[],
            output_filename=output_filename,
        )
        check_record.highlighted_file_path = highlighted_file
        db.commit()
        db.refresh(check_record)

        target_validation = None
        try:
            target_data_init = document_text_service.get_or_extract_text(target_doc, db)
            target_validation = target_data_init.get("chapter_validation")
        except Exception:
            pass

        return {
            "check_id": check_record.id,
            "target_document": target_doc.title,
            "highest_similarity_percentage": "0.0%",
            "total_repository_checked": 0,
            "total_repository_available": 0,
            "total_repository_chunks": 0,
            "total_target_embedding_chunks": 0,
            "total_repository_embedding_chunks": 0,
            "candidate_limit": candidate_limit,
            "candidate_strategy": "none",
            "candidate_strategy_reason": "repository_empty",
            "semantic_search_used": False,
            "semantic_fallback_reason": None,
            "embedding_auto_index": embedding_auto_index,
            "chapter_aware": False,
            "target_chapters": [],
            "matches": [],
            "message": "Dokumen berhasil disimpan. Belum ada dokumen lain di repositori kampus untuk dibandingkan.",
            "highlighted_pdf_available": True,
            "chapter_validation": target_validation,
        }

    try:
        target_data = document_text_service.get_or_extract_text(target_doc, db)
        clean_target = target_data["cleaned_text"]
    except Exception as e:
        logger.error(f"Gagal mengekstrak teks dari dokumen target {target_doc.id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Gagal membaca teks dokumen target '{target_doc.title}': {str(e)}",
        )

    results = []
    max_score = 0.0

    candidate_result = repository_candidate_service.find_candidates(
        target_document=target_doc,
        repository_documents=repo_docs,
        db=db,
        top_k=candidate_limit,
    )
    candidate_docs = [candidate["document"] for candidate in candidate_result["candidates"]]
    repo_sentences_all = candidate_result["reference_corpus"]

    for repo_doc in candidate_docs:
        try:
            repo_data = document_text_service.get_or_extract_text(repo_doc, db)
            clean_repo = repo_data["cleaned_text"]
            score = similarity_service.calculate_clean_text_similarity(clean_target, clean_repo)

            if score > max_score:
                max_score = score

            results.append({
                "repository_document_id": repo_doc.id,
                "title": repo_doc.title,
                "similarity_score": round(score, 4),
                "similarity_percentage": f"{round(score * 100, 2)}%",
            })

        except Exception as repo_err:
            logger.warning(f"Melewati dokumen repositori ID {repo_doc.id} karena error: {repo_err}")
            continue

    results.sort(key=lambda x: x["similarity_score"], reverse=True)

    check_record = PlagiarismCheck(
        document_id=target_doc.id,
        user_id=effective_user_id,
        overall_similarity=max_score,
        status="completed",
    )
    db.add(check_record)
    db.commit()
    db.refresh(check_record)

    result_records_by_doc_id = {}
    for item in results:
        result_record = similarity_result_service.create_result(
            db=db,
            check_id=check_record.id,
            source_document_id=item["repository_document_id"],
            similarity_score=item["similarity_score"],
        )
        result_records_by_doc_id[item["repository_document_id"]] = result_record

    # Deteksi kalimat plagiat & buat Highlight Kuning Stabilo di PDF
    highlighted_file = None
    plagiarized_sentences = []
    try:
        sentences_target = document_chunk_service.get_or_build_sentence_chunks(target_doc, db)
        lexical_matches = similarity_service.find_sentence_matches_against_references(
            target_sentences=sentences_target,
            reference_corpus=repo_sentences_all,
            threshold=0.70,
        )
        semantic_matches = repository_candidate_service.find_semantic_sentence_matches(
            target_document=target_doc,
            repository_documents=candidate_docs,
            db=db,
        )
        plagiarized_sentences = _merge_highlight_matches(lexical_matches, semantic_matches)
        matches_by_source_document_id = {}
        for match in plagiarized_sentences:
            source_document_id = match.get("source_document_id")
            if source_document_id is None:
                continue
            matches_by_source_document_id.setdefault(source_document_id, []).append(match)

        for source_document_id, matches in matches_by_source_document_id.items():
            result_record = result_records_by_doc_id.get(source_document_id)
            if result_record:
                similarity_result_service.create_matches(
                    db=db,
                    result_id=result_record.id,
                    matches=matches,
                )

        output_filename = f"highlighted_check_{check_record.id}_{Path(target_doc.file_path).stem}.pdf"
        highlighted_file = pdf_service.generate_highlighted_pdf(
            source_pdf_path=target_doc.file_path,
            plagiarized_sentences=plagiarized_sentences,
            output_filename=output_filename,
        )
        check_record.highlighted_file_path = highlighted_file
        db.commit()
        db.refresh(check_record)
    except Exception as hl_err:
        logger.warning(f"Gagal generate highlighted PDF untuk check {check_record.id}: {hl_err}")
        try:
            output_filename = f"highlighted_check_{check_record.id}_{Path(target_doc.file_path).stem}.pdf"
            highlighted_file = pdf_service.generate_highlighted_pdf(
                source_pdf_path=target_doc.file_path,
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
        "target_document": target_doc.title,
        "highest_similarity_percentage": f"{round(max_score * 100, 2)}%",
        "total_repository_checked": len(results),
        "total_repository_available": candidate_result["total_repository_documents"],
        "total_repository_chunks": candidate_result["total_repository_chunks"],
        "total_target_embedding_chunks": candidate_result["total_target_embedding_chunks"],
        "total_repository_embedding_chunks": candidate_result["total_repository_embedding_chunks"],
        "candidate_limit": candidate_limit,
        "candidate_strategy": candidate_result["candidate_strategy"],
        "candidate_strategy_reason": candidate_result["candidate_strategy_reason"],
        "semantic_search_used": candidate_result["semantic_search_used"],
        "semantic_fallback_reason": candidate_result["semantic_fallback_reason"],
        "embedding_auto_index": embedding_auto_index,
        "chapter_aware": candidate_result["chapter_aware"],
        "target_chapters": candidate_result["target_chapters"],
        "matches": results,
        "total_plagiarized_sentences": len(plagiarized_sentences),
        "highlighted_pdf_available": bool(highlighted_file and os.path.exists(highlighted_file)),
        "chapter_validation": target_data.get("chapter_validation"),
    }

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
            "approval_status": check.approval_status or "belum disetujui",
            "reviewed_at": check.reviewed_at.isoformat() if check.reviewed_at else None,
            "reviewer_note": check.reviewer_note,
            "highlighted_pdf_available": has_highlighted,
            "highlighted_pdf_url": f"/api/plagiarism/check/{check.id}/download-highlighted" if has_highlighted else None,
            "created_at": check.created_at.isoformat() if check.created_at else None,
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

    stored_results = (
        db.query(SimilarityResult)
        .filter(SimilarityResult.check_id == check_id)
        .order_by(SimilarityResult.similarity_score.desc())
        .all()
    )

    return {
        "check_id": check.id,
        "document_id": check.document_id,
        "overall_similarity": check.overall_similarity,
        "similarity_percentage": f"{round(check.overall_similarity * 100, 2)}%",
        "results": [
            {
                "result_id": result.id,
                "source_document_id": result.source_document_id,
                "source_document_title": result.source_document.title if result.source_document else None,
                "chapter": result.chapter,
                "similarity_score": result.similarity_score,
                "similarity_percentage": f"{round(result.similarity_score * 100, 2)}%",
                "matches": [
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
                    for match in result.matches
                ],
            }
            for result in stored_results
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
