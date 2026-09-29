import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.database.session import get_db
from app.models.schemas import Document, PlagiarismCheck, User, Mahasiswa
from app.services.pdf_service import PDFService
from app.services.preprocessing_service import PreprocessingService
from app.routes.auth import get_optional_current_user, get_current_user

router = APIRouter()
pdf_service = PDFService()
preprocessor = PreprocessingService()
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

    data_a = pdf_service.extract_text_from_pdf(doc_a.file_path)
    data_b = pdf_service.extract_text_from_pdf(doc_b.file_path)

    clean_a = preprocessor.clean_text(data_a["full_text"])
    clean_b = preprocessor.clean_text(data_b["full_text"])

    if not clean_a.strip() or not clean_b.strip():
        score = 0.0
    else:
        vectorizer = TfidfVectorizer()
        tfidf_matrix = vectorizer.fit_transform([clean_a, clean_b])
        score = float(cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])[0][0])

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

    # Deteksi kalimat mirip dan beri highlight kuning stabilo di file PDF
    highlighted_file = None
    plagiarized_sentences = []
    try:
        sentences_a = pdf_service.extract_sentences_with_pages(doc_a.file_path)
        sentences_b = pdf_service.extract_sentences_with_pages(doc_b.file_path)

        clean_b_sentences = [preprocessor.clean_text(s["sentence"]) for s in sentences_b]
        valid_b_indices = [idx for idx, c in enumerate(clean_b_sentences) if len(c.strip()) > 0]

        if valid_b_indices and sentences_a:
            corpus_b = [clean_b_sentences[idx] for idx in valid_b_indices]
            s_vec = TfidfVectorizer().fit(corpus_b)
            mat_b = s_vec.transform(corpus_b)

            for item_a in sentences_a:
                clean_item_a = preprocessor.clean_text(item_a["sentence"])
                if not clean_item_a.strip():
                    continue
                mat_a = s_vec.transform([clean_item_a])
                sims = cosine_similarity(mat_a, mat_b)[0]
                max_s = float(sims.max()) if len(sims) > 0 else 0.0
                if max_s >= 0.70:
                    best_match_idx = int(sims.argmax())
                    ref_sentence = sentences_b[valid_b_indices[best_match_idx]]["sentence"]
                    plagiarized_sentences.append({
                        "page": item_a["page"],
                        "sentence": item_a["sentence"],
                        "similarity": round(max_s * 100, 2),
                        "matched_source": doc_b.title,
                        "reference_sentence": ref_sentence,
                    })

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

    return {
        "check_id": check_record.id,
        "target_document": doc_a.title,
        "reference_document": doc_b.title,
        "similarity_score": round(score, 4),
        "similarity_percentage": f"{round(score * 100, 2)}%",
        "status": check_record.status,
        "total_plagiarized_sentences": len(plagiarized_sentences),
        "highlighted_pdf_available": bool(highlighted_file and os.path.exists(highlighted_file)),
    }

@router.post("/check-repository/{document_id}")
def check_against_repository(
    document_id: int,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """Membandingkan 1 dokumen mahasiswa terhadap SELURUH dokumen di repositori kampus dan menghasilkan PDF kuning stabilo"""
    target_doc = db.query(Document).filter(Document.id == document_id).first()
    if not target_doc:
        raise HTTPException(status_code=404, detail="Dokumen target tidak ditemukan.")

    repo_docs = db.query(Document).filter(Document.id != document_id).all()
    effective_user_id = current_user.id if current_user else target_doc.user_id

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

        return {
            "check_id": check_record.id,
            "target_document": target_doc.title,
            "highest_similarity_percentage": "0.0%",
            "total_repository_checked": 0,
            "matches": [],
            "message": "Dokumen berhasil disimpan. Belum ada dokumen lain di repositori kampus untuk dibandingkan.",
            "highlighted_pdf_available": True,
        }

    try:
        target_data = pdf_service.extract_text_from_pdf(target_doc.file_path)
        clean_target = preprocessor.clean_text(target_data["full_text"])
    except Exception as e:
        logger.error(f"Gagal mengekstrak teks dari dokumen target {target_doc.id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Gagal membaca teks dokumen target '{target_doc.title}': {str(e)}",
        )

    results = []
    max_score = 0.0

    # Siapkan kalimat dari repositori untuk perbandingan tingkat kalimat
    repo_sentences_all = []

    for repo_doc in repo_docs:
        try:
            repo_data = pdf_service.extract_text_from_pdf(repo_doc.file_path)
            clean_repo = preprocessor.clean_text(repo_data["full_text"])

            if not clean_target.strip() or not clean_repo.strip():
                score = 0.0
            else:
                vectorizer = TfidfVectorizer()
                tfidf_matrix = vectorizer.fit_transform([clean_target, clean_repo])
                score = float(cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])[0][0])

            if score > max_score:
                max_score = score

            results.append({
                "repository_document_id": repo_doc.id,
                "title": repo_doc.title,
                "similarity_score": round(score, 4),
                "similarity_percentage": f"{round(score * 100, 2)}%",
            })

            # Ekstrak kalimat dari repo doc untuk deteksi highlight
            sentences_repo = pdf_service.extract_sentences_with_pages(repo_doc.file_path)
            for s in sentences_repo:
                c_text = preprocessor.clean_text(s["sentence"])
                if c_text.strip():
                    repo_sentences_all.append({
                        "clean": c_text,
                        "sentence": s["sentence"],
                        "doc_title": repo_doc.title,
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

    # Deteksi kalimat plagiat & buat Highlight Kuning Stabilo di PDF
    highlighted_file = None
    plagiarized_sentences = []
    try:
        sentences_target = pdf_service.extract_sentences_with_pages(target_doc.file_path)
        if repo_sentences_all and sentences_target:
            corpus_repo = [item["clean"] for item in repo_sentences_all]
            s_vec = TfidfVectorizer().fit(corpus_repo)
            mat_repo = s_vec.transform(corpus_repo)

            for s_target in sentences_target:
                clean_target_sentence = preprocessor.clean_text(s_target["sentence"])
                if not clean_target_sentence.strip():
                    continue
                mat_s = s_vec.transform([clean_target_sentence])
                sims = cosine_similarity(mat_s, mat_repo)[0]
                best_sim = float(sims.max()) if len(sims) > 0 else 0.0

                if best_sim >= 0.70:
                    best_match_idx = int(sims.argmax())
                    matched_info = repo_sentences_all[best_match_idx]
                    plagiarized_sentences.append({
                        "page": s_target["page"],
                        "sentence": s_target["sentence"],
                        "similarity": round(best_sim * 100, 2),
                        "matched_source": matched_info["doc_title"],
                        "reference_sentence": matched_info["sentence"],
                    })

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

    return {
        "check_id": check_record.id,
        "target_document": target_doc.title,
        "highest_similarity_percentage": f"{round(max_score * 100, 2)}%",
        "total_repository_checked": len(results),
        "matches": results,
        "total_plagiarized_sentences": len(plagiarized_sentences),
        "highlighted_pdf_available": bool(highlighted_file and os.path.exists(highlighted_file)),
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
    check.reviewed_at = datetime.utcnow()
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