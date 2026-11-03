import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from app.database.session import Base
from app.models.schemas import Document, Mahasiswa, PlagiarismCheck, User
from app.routes.dashboard import get_dashboard_summary


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_dashboard_data(db):
    admin = User(identifier="admin", password="secret", role="admin")
    student = User(identifier="student", password="secret", role="mahasiswa")
    other = User(identifier="other", password="secret", role="mahasiswa")
    db.add_all([admin, student, other])
    db.flush()
    db.add(Mahasiswa(id=student.id, nama_lengkap="Mahasiswa", program_studi="Informatika", fakultas="Teknik"))
    own = Document(user_id=student.id, title="Own.pdf", document_type="proposal", file_path="own.pdf")
    elsewhere = Document(user_id=other.id, title="Other.pdf", document_type="skripsi", file_path="other.pdf")
    db.add_all([own, elsewhere])
    db.flush()
    db.add_all([
        PlagiarismCheck(document_id=own.id, user_id=student.id, overall_similarity=.31, status="completed"),
        PlagiarismCheck(document_id=own.id, user_id=student.id, overall_similarity=.18, status="completed"),
        PlagiarismCheck(document_id=elsewhere.id, user_id=other.id, overall_similarity=.42, status="completed"),
        PlagiarismCheck(document_id=own.id, user_id=student.id, overall_similarity=0, status="processing", progress=55),
    ])
    db.commit()
    return admin, student


def test_dashboard_summary_scopes_counts_by_role_without_review_fields():
    db = _make_session()
    admin, student = _seed_dashboard_data(db)
    student_summary = get_dashboard_summary(current_user=student, db=db)
    admin_summary = get_dashboard_summary(current_user=admin, db=db)
    assert student_summary["document_count"] == 1
    assert student_summary["total_check_count"] == 2
    assert student_summary["latest_check_count"] == 1
    assert student_summary["average_similarity"] == .18
    assert student_summary["processing_check_count"] == 1
    assert admin_summary["document_count"] == 2
    assert admin_summary["total_check_count"] == 3
    assert admin_summary["latest_check_count"] == 2
    assert admin_summary["average_similarity"] == .3
    assert admin_summary["processing_check_count"] == 1
    assert not {"approved_count", "pending_review_count", "revision_count", "supervised_student_count"} & admin_summary.keys()
    assert "approval_status" not in admin_summary["latest_check"]


@pytest.mark.parametrize("role", ["dosen", "unsupported"])
def test_dashboard_rejects_retired_and_unsupported_roles(role):
    db = _make_session()
    removed_user = User(identifier="old-user", password="secret", role=role)
    with pytest.raises(HTTPException) as error:
        get_dashboard_summary(current_user=removed_user, db=db)
    assert error.value.status_code == 403


def test_empty_corpus_check_does_not_become_zero_percent_similarity():
    db = _make_session()
    admin, student = _seed_dashboard_data(db)
    own = db.query(Document).filter(Document.user_id == student.id).one()
    db.add(PlagiarismCheck(document_id=own.id, user_id=student.id, overall_similarity=0,
                          status="completed", comparison_status="no_comparison_sources"))
    db.commit()
    student_summary = get_dashboard_summary(current_user=student, db=db)
    assert student_summary["average_similarity"] is None
    assert student_summary["latest_check"]["overall_similarity"] is None
    assert student_summary["latest_check"]["comparison_status"] == "no_comparison_sources"
    # Unavailable latest result does not dilute other students' mean.
    assert get_dashboard_summary(current_user=admin, db=db)["average_similarity"] == .42
