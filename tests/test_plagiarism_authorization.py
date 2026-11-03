import inspect
import pytest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, Mahasiswa, PlagiarismCheck, User
from app.routes import plagiarism as plagiarism_routes
from app.routes.auth import get_current_user


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _add_student(db, identifier):
    student = User(identifier=identifier, password="secret", role="mahasiswa")
    db.add(student)
    db.flush()
    db.add(
        Mahasiswa(
            id=student.id,
            nama_lengkap=identifier,
            program_studi="Informatika",
            fakultas="Teknik",
        )
    )
    return student


def _add_document_and_check(
    db,
    owner,
    *,
    status="completed",
    repository_status="draft",
    file_path=None,
):
    document = Document(
        user_id=owner.id,
        title=f"{owner.identifier}.pdf",
        document_type="skripsi",
        file_path=file_path or f"{owner.identifier}.pdf",
        repository_status=repository_status,
    )
    db.add(document)
    db.flush()
    check = PlagiarismCheck(
        document_id=document.id,
        user_id=owner.id,
        overall_similarity=0.42,
        status=status,
        progress=100 if status == "completed" else 50,
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
        plagiarism_routes.get_comparison_source_pdf,
    )

    for endpoint in protected_routes:
        dependency = inspect.signature(endpoint).parameters["current_user"].default
        assert dependency.dependency is get_current_user


def test_history_rejects_the_retired_lecturer_role():
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
        raise AssertionError("Expected the retired lecturer role to be denied")




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








def test_manual_check_rejects_draft_wrong_type_and_same_owner_sources():
    db = _make_session()
    owner = _add_student(db, "pemilik")
    other_owner = _add_student(db, "mahasiswa-lain")
    db.flush()
    target = Document(
        user_id=owner.id,
        title="target.pdf",
        document_type="skripsi",
        file_path="target.pdf",
        repository_status="draft",
    )
    draft_source = Document(
        user_id=other_owner.id,
        title="draft-source.pdf",
        document_type="skripsi",
        file_path="draft-source.pdf",
        repository_status="draft",
    )
    wrong_type_source = Document(
        source_origin="alumni",
        title="proposal-source.pdf",
        document_type="proposal",
        file_path="proposal-source.pdf",
        repository_status="published",
    )
    same_owner_source = Document(
        user_id=owner.id,
        title="same-owner-source.pdf",
        document_type="skripsi",
        file_path="same-owner-source.pdf",
        repository_status="published",
    )
    db.add_all([target, draft_source, wrong_type_source, same_owner_source])
    db.commit()

    cases = [
        (draft_source, "repositori alumni"),
        (wrong_type_source, "jenis dokumen yang sama"),
        (same_owner_source, "repositori alumni"),
    ]
    for source, expected_detail in cases:
        try:
            plagiarism_routes.check_similarity(
                plagiarism_routes.SingleCheckRequest(
                    document_id=target.id,
                    reference_document_id=source.id,
                ),
                current_user=owner,
                db=db,
            )
        except HTTPException as exc:
            assert exc.status_code == 409
            assert expected_detail in exc.detail
        else:
            raise AssertionError("Expected incompatible repository source to be rejected")


def test_manual_check_is_enqueued_instead_of_running_inside_api_request():
    db = _make_session()
    owner = _add_student(db, "pemilik")
    other_owner = _add_student(db, "mahasiswa-lain")
    db.flush()
    target = Document(
        user_id=owner.id,
        title="target.pdf",
        document_type="skripsi",
        file_path="target.pdf",
        repository_status="draft",
    )
    source = Document(
        source_origin="alumni",
        title="source.pdf",
        document_type="skripsi",
        file_path="source.pdf",
        repository_status="published",
    )
    db.add_all([target, source])
    db.commit()

    payload = plagiarism_routes.check_similarity(
        plagiarism_routes.SingleCheckRequest(
            document_id=target.id,
            reference_document_id=source.id,
        ),
        current_user=owner,
        db=db,
    )

    check = db.query(PlagiarismCheck).filter(PlagiarismCheck.id == payload["check_id"]).first()
    assert payload["status"] == "pending"
    assert payload["reference_document_id"] == source.id
    assert payload["queue_position"] == 1
    assert check.reference_document_id == source.id
    assert check.status == "pending"


@pytest.mark.parametrize("student_owned,source_origin", [(True, "alumni"), (False, "student")])
def test_manual_check_rejects_published_documents_without_trusted_alumni_origin(student_owned, source_origin):
    db = _make_session()
    owner = _add_student(db, "owner")
    other = _add_student(db, "other")
    db.flush()
    target = Document(user_id=owner.id, title="target.pdf", file_path="target.pdf", document_type="proposal")
    source = Document(user_id=other.id if student_owned else None, title="source.pdf",
                      file_path="source.pdf", document_type="proposal",
                      repository_status="published", source_origin=source_origin)
    db.add_all([target, source])
    db.commit()
    with pytest.raises(HTTPException) as error:
        plagiarism_routes.check_similarity(
            plagiarism_routes.SingleCheckRequest(document_id=target.id, reference_document_id=source.id),
            current_user=owner, db=db,
        )
    assert error.value.status_code == 409
    assert db.query(PlagiarismCheck).count() == 0


def test_removed_approval_route_is_not_registered():
    assert not any("approval" in route.path for route in plagiarism_routes.router.routes)

