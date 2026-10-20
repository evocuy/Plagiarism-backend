import inspect

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, Dosen, Mahasiswa, PlagiarismCheck, User
from app.routes import plagiarism as plagiarism_routes
from app.routes.auth import get_current_user


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _add_student(db, identifier, *, dosen_pembimbing_id=None):
    student = User(identifier=identifier, password="secret", role="mahasiswa")
    db.add(student)
    db.flush()
    db.add(
        Mahasiswa(
            id=student.id,
            nama_lengkap=identifier,
            program_studi="Informatika",
            fakultas="Teknik",
            dosen_pembimbing_id=dosen_pembimbing_id,
        )
    )
    return student


def _add_document_and_check(
    db,
    owner,
    *,
    status="completed",
    approval_status="belum disetujui",
    file_path=None,
):
    document = Document(
        user_id=owner.id,
        title=f"{owner.identifier}.pdf",
        document_type="skripsi",
        file_path=file_path or f"{owner.identifier}.pdf",
    )
    db.add(document)
    db.flush()
    check = PlagiarismCheck(
        document_id=document.id,
        user_id=owner.id,
        overall_similarity=0.42,
        status=status,
        progress=100 if status == "completed" else 50,
        approval_status=approval_status,
    )
    db.add(check)
    db.flush()
    return document, check


def test_sensitive_plagiarism_routes_use_required_authentication():
    protected_routes = (
        plagiarism_routes.check_similarity,
        plagiarism_routes.check_against_repository,
        plagiarism_routes.get_check_history,
        plagiarism_routes.get_check_processing_status,
        plagiarism_routes.get_check_matches,
        plagiarism_routes.get_check_top_matches,
        plagiarism_routes.download_highlighted_pdf,
    )

    for endpoint in protected_routes:
        dependency = inspect.signature(endpoint).parameters["current_user"].default
        assert dependency.dependency is get_current_user


def test_history_fails_closed_for_dosen_without_profile():
    db = _make_session()
    incomplete_dosen = User(identifier="dosen-tanpa-profil", password="secret", role="dosen")
    owner = _add_student(db, "mahasiswa")
    db.add(incomplete_dosen)
    db.flush()
    _add_document_and_check(db, owner)
    db.commit()

    try:
        plagiarism_routes.get_check_history(current_user=incomplete_dosen, db=db)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Expected a lecturer without a profile to be denied")


def test_history_for_dosen_contains_only_supervised_students():
    db = _make_session()
    dosen = User(identifier="dosen", password="secret", role="dosen")
    db.add(dosen)
    db.flush()
    db.add(Dosen(id=dosen.id, nama_lengkap="Dosen Pembimbing"))
    supervised = _add_student(db, "mahasiswa-bimbingan", dosen_pembimbing_id=dosen.id)
    other = _add_student(db, "mahasiswa-lain")
    db.flush()
    supervised_document, _ = _add_document_and_check(db, supervised)
    _add_document_and_check(db, other)
    db.commit()

    history = plagiarism_routes.get_check_history(current_user=dosen, db=db)

    assert [item["document_id"] for item in history] == [supervised_document.id]


def test_history_never_returns_the_server_side_file_path():
    db = _make_session()
    owner = _add_student(db, "pemilik")
    db.flush()
    _, check = _add_document_and_check(
        db,
        owner,
        file_path="/srv/plagiarism/uploads/internal-storage-name.pdf",
    )
    db.commit()

    history = plagiarism_routes.get_check_history(current_user=owner, db=db)

    assert history[0]["id"] == check.id
    assert history[0]["file_path"] == "pemilik.pdf"
    assert "/srv/" not in history[0]["file_path"]


def test_matches_reject_an_unrelated_student():
    db = _make_session()
    owner = _add_student(db, "pemilik")
    other = _add_student(db, "bukan-pemilik")
    db.flush()
    _, check = _add_document_and_check(db, owner)
    db.commit()

    try:
        plagiarism_routes.get_check_matches(check.id, current_user=other, db=db)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Expected an unrelated student to be denied")


def test_repository_check_rejects_an_unrelated_target_owner():
    db = _make_session()
    owner = _add_student(db, "pemilik")
    other = _add_student(db, "bukan-pemilik")
    db.flush()
    document, _ = _add_document_and_check(db, owner)
    db.commit()

    try:
        plagiarism_routes.check_against_repository(
            document.id,
            current_user=other,
            db=db,
        )
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Expected an unrelated student to be denied")


def test_approval_rejects_an_unfinished_check_and_preserves_state():
    db = _make_session()
    dosen = User(identifier="dosen", password="secret", role="dosen")
    db.add(dosen)
    db.flush()
    db.add(Dosen(id=dosen.id, nama_lengkap="Dosen Pembimbing"))
    student = _add_student(db, "mahasiswa", dosen_pembimbing_id=dosen.id)
    db.flush()
    _, check = _add_document_and_check(db, student, status="processing")
    db.commit()

    try:
        plagiarism_routes.update_approval_status(
            check.id,
            plagiarism_routes.ApprovalUpdateRequest(approval_status="disetujui"),
            current_user=dosen,
            db=db,
        )
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("Expected unfinished checks to reject approval")

    db.refresh(check)
    assert check.approval_status == "belum disetujui"
    assert check.reviewed_at is None


def test_completed_approval_returns_the_document_contract():
    db = _make_session()
    dosen = User(identifier="dosen", password="secret", role="dosen")
    db.add(dosen)
    db.flush()
    db.add(Dosen(id=dosen.id, nama_lengkap="Dosen Pembimbing"))
    student = _add_student(db, "mahasiswa", dosen_pembimbing_id=dosen.id)
    db.flush()
    document, check = _add_document_and_check(db, student)
    db.commit()

    payload = plagiarism_routes.update_approval_status(
        check.id,
        plagiarism_routes.ApprovalUpdateRequest(
            approval_status="disetujui",
            reviewer_note="Sudah sesuai.",
        ),
        current_user=dosen,
        db=db,
    )

    assert payload["id"] == check.id
    assert payload["document_id"] == document.id
    assert payload["title"] == document.title
    assert payload["document_type"] == document.document_type
    assert payload["status"] == "completed"
    assert payload["overall_similarity"] == 0.42
    assert payload["approval_status"] == "disetujui"
    assert payload["owner_name"] == "mahasiswa"
    assert payload["owner_identifier"] == "mahasiswa"
    assert payload["reviewed_at"] is not None

