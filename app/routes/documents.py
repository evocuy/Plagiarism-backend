import shutil
from pathlib import Path
from typing import Optional, List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database.session import get_db
from app.models.schemas import Document, User, Mahasiswa
from app.routes.auth import get_optional_current_user, get_current_user

router = APIRouter()
UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    document_type: str = Form(...),
    user_id: Optional[int] = Form(None),
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

    return {
        "id": db_doc.id,
        "user_id": db_doc.user_id,
        "filename": db_doc.title,
        "document_type": db_doc.document_type,
        "file_path": db_doc.file_path,
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
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
        })
    return results

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