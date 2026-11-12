"""Downloading is read-only and never manufactures an incomplete report."""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, PlagiarismCheck, User, utc_now
from app.routes import plagiarism


@pytest.fixture
def context(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        owner = User(identifier="owner", password="unused", role="mahasiswa")
        outsider = User(identifier="outsider", password="unused", role="mahasiswa")
        admin = User(identifier="admin", password="unused", role="admin")
        db.add_all([owner, outsider, admin])
        db.flush()
        source = tmp_path / "source.pdf"
        source.write_bytes(b"synthetic source")
        artifact = tmp_path / "highlighted.pdf"
        artifact.write_bytes(b"synthetic existing artifact")
        document = Document(user_id=owner.id, title="source.pdf", file_path=str(source))
        db.add(document)
        db.flush()
        check = PlagiarismCheck(document_id=document.id, user_id=owner.id, overall_similarity=0.0,
                                status="processing", highlighted_file_path=str(artifact))
        db.add(check)
        db.commit()
        monkeypatch.setattr(plagiarism.pdf_service, "generate_highlighted_pdf", lambda **_kwargs: pytest.fail("GET must never render a PDF"))
        yield db, owner, outsider, admin, document, check, artifact
    engine.dispose()


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
def test_unfinished_checks_cannot_serve_even_a_preexisting_pdf(context, status):
    db, owner, _, admin, _, check, _ = context
    check.status = status
    db.commit()
    for user in [owner, admin]:
        with pytest.raises(HTTPException) as error:
            plagiarism.download_highlighted_pdf(check.id, current_user=user, db=db)
        assert error.value.status_code == 409
        assert error.value.headers["Cache-Control"] == "no-store"
        assert ("Retry-After" in error.value.headers) == (status in {"pending", "processing"})
    assert plagiarism._serialize_check_progress(check)["highlighted_pdf_available"] is False
    history = plagiarism.get_check_history(current_user=owner, db=db)
    assert history[0]["highlighted_pdf_available"] is False
    assert history[0]["highlighted_pdf_url"] is None


@pytest.mark.parametrize("missing", ["unset", "missing", "directory"])
def test_missing_completed_artifact_does_not_generate_an_empty_fallback(context, missing):
    db, owner, _, _, document, check, artifact = context
    check.status = "completed"
    check.highlighted_file_path = None if missing == "unset" else (str(artifact.parent) if missing == "directory" else str(artifact.parent / "missing.pdf"))
    db.commit()
    original_path = check.highlighted_file_path
    with pytest.raises(HTTPException) as error:
        plagiarism.download_highlighted_pdf(check.id, current_user=owner, db=db)
    assert error.value.status_code == 404
    assert check.highlighted_file_path == original_path
    assert plagiarism._serialize_check_progress(check)["highlighted_pdf_available"] is False


def test_completed_artifact_is_served_without_rewriting_it(context):
    db, owner, _, admin, _, check, artifact = context
    check.status = "completed"
    db.commit()
    before = artifact.read_bytes()
    for user in [owner, admin]:
        response = plagiarism.download_highlighted_pdf(check.id, current_user=user, db=db)
        assert response.path == str(artifact)
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert artifact.read_bytes() == before
    assert plagiarism._serialize_check_progress(check)["highlighted_pdf_available"] is True


def test_ownership_is_checked_before_revealing_readiness_and_deleted_document_stays_hidden(context):
    db, owner, outsider, _, document, check, _ = context
    with pytest.raises(HTTPException) as error:
        plagiarism.download_highlighted_pdf(check.id, current_user=outsider, db=db)
    assert error.value.status_code == 403
    document.deleted_at = utc_now()
    db.commit()
    with pytest.raises(HTTPException) as error:
        plagiarism.download_highlighted_pdf(check.id, current_user=owner, db=db)
    assert error.value.status_code == 404
