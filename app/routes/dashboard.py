from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.schemas import Document, Mahasiswa, PlagiarismCheck, User
from app.routes.auth import get_current_user

router = APIRouter()


def _assert_dashboard_access(current_user: User) -> None:
    """Reject malformed or unsupported roles before building any aggregate."""
    if current_user.role in ("admin", "super_admin", "mahasiswa"):
        return
    if current_user.role == "dosen" and current_user.dosen:
        return
    if current_user.role == "dosen":
        raise HTTPException(
            status_code=403,
            detail="Profil dosen tidak ditemukan. Hubungi administrator.",
        )
    raise HTTPException(status_code=403, detail="Akses ditolak.")


def _visible_documents_query(current_user: User, db: Session):
    query = db.query(Document).filter(Document.deleted_at.is_(None))

    if current_user.role in ("admin", "super_admin"):
        return query

    if current_user.role == "mahasiswa":
        return query.filter(Document.user_id == current_user.id)

    if current_user.role == "dosen":
        supervised_student_ids = (
            db.query(Mahasiswa.id)
            .filter(Mahasiswa.dosen_pembimbing_id == current_user.id)
        )
        return query.filter(Document.user_id.in_(supervised_student_ids))

    # `_assert_dashboard_access` is called by the public endpoint. Retain a
    # fail-closed fallback in case this helper is reused elsewhere.
    return query.filter(Document.id.is_(None))


def _visible_checks_query(current_user: User, db: Session):
    query = (
        db.query(PlagiarismCheck)
        .join(Document, PlagiarismCheck.document_id == Document.id)
        .filter(Document.deleted_at.is_(None))
    )

    if current_user.role in ("admin", "super_admin"):
        return query

    if current_user.role == "mahasiswa":
        return query.filter(
            (PlagiarismCheck.user_id == current_user.id) | (Document.user_id == current_user.id)
        )

    if current_user.role == "dosen":
        supervised_student_ids = (
            db.query(Mahasiswa.id)
            .filter(Mahasiswa.dosen_pembimbing_id == current_user.id)
        )
        return query.filter(Document.user_id.in_(supervised_student_ids))

    return query.filter(PlagiarismCheck.id.is_(None))


def _latest_checks_per_document(checks: List[PlagiarismCheck]) -> List[PlagiarismCheck]:
    latest_checks = []
    document_ids = set()

    for check in checks:
        if check.document_id in document_ids:
            continue
        document_ids.add(check.document_id)
        latest_checks.append(check)

    return latest_checks


def _serialize_check(check: PlagiarismCheck):
    document = check.document
    return {
        "id": check.id,
        "document_id": check.document_id,
        "title": document.title if document else "Dokumen tidak ditemukan",
        "document_type": document.document_type if document else None,
        "overall_similarity": check.overall_similarity,
        "approval_status": check.approval_status or "belum disetujui",
        "status": check.status,
        "created_at": check.created_at.isoformat() if check.created_at else None,
    }


@router.get("/summary")
def get_dashboard_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return role-scoped dashboard counters calculated from database records."""
    _assert_dashboard_access(current_user)
    document_count = _visible_documents_query(current_user, db).count()
    checks = (
        _visible_checks_query(current_user, db)
        .order_by(PlagiarismCheck.created_at.desc(), PlagiarismCheck.id.desc())
        .all()
    )
    completed_checks = [check for check in checks if check.status == "completed"]
    latest_checks = _latest_checks_per_document(completed_checks)
    processing_check_count = sum(
        1 for check in checks if check.status in {"pending", "processing"}
    )

    approval_counts = {
        "belum disetujui": 0,
        "disetujui": 0,
        "revisi": 0,
    }
    for check in latest_checks:
        approval_status = (check.approval_status or "belum disetujui").lower()
        approval_counts[approval_status] = approval_counts.get(approval_status, 0) + 1

    average_similarity = (
        sum(float(check.overall_similarity or 0.0) for check in latest_checks)
        / len(latest_checks)
        if latest_checks
        else 0.0
    )
    start_of_today = datetime.now(timezone.utc).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=None,
    )
    today_check_count = sum(
        1
        for check in completed_checks
        if check.created_at and check.created_at >= start_of_today
    )

    supervised_student_count = 0
    if current_user.role == "dosen":
        supervised_student_count = (
            db.query(Mahasiswa)
            .filter(Mahasiswa.dosen_pembimbing_id == current_user.id)
            .count()
        )

    return {
        "role": current_user.role,
        "document_count": document_count,
        "total_check_count": len(completed_checks),
        "processing_check_count": processing_check_count,
        "latest_check_count": len(latest_checks),
        "today_check_count": today_check_count,
        "average_similarity": round(average_similarity, 4),
        "pending_review_count": approval_counts["belum disetujui"],
        "approved_count": approval_counts["disetujui"],
        "revision_count": approval_counts["revisi"],
        "supervised_student_count": supervised_student_count,
        "latest_check": _serialize_check(latest_checks[0]) if latest_checks else None,
    }
