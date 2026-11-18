"""Both highlight URLs require the same authentication, owner and ready artifact."""
from types import SimpleNamespace

import pymupdf
import pytest

from app.models.schemas import Document, PlagiarismCheck, User, utc_now
from app.routes import auth, plagiarism
from app.services.account_mode_service import student_mode_user
from test_auth_session import environment, request


URL_SUFFIXES = ("download-highlighted", "highlighted-pdf")


@pytest.fixture
def context(environment, tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENT_AUTH_PROVIDER", "local")
    app, db = environment
    app.include_router(plagiarism.router, prefix="/api/plagiarism")
    owner = db.query(User).filter_by(identifier="student").one()
    admin = db.query(User).filter_by(identifier="admin").one()
    outsider = User(identifier="other-student", password="unused", role="mahasiswa")
    cihuy = User(identifier="cihuy", password="unused", role="admin")
    db.add_all([outsider, cihuy])
    db.flush()
    artifact = tmp_path / "synthetic-highlight.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Synthetic highlighted report for route verification.")
        page.add_highlight_annot(page.search_for("highlighted"))
        pdf.save(artifact)
    document = Document(user_id=owner.id, title="synthetic.pdf", file_path=str(artifact))
    db.add(document)
    db.flush()
    check = PlagiarismCheck(document_id=document.id, user_id=owner.id, status="completed", overall_similarity=.2,
                            highlighted_file_path=str(artifact))
    db.add(check)
    db.commit()
    return SimpleNamespace(app=app, db=db, owner=owner, admin=admin, outsider=outsider, cihuy=cihuy,
                           document=document, check=check, artifact=artifact)


def get_pdf(context, suffix, *, user=None, via_cookie=False, headers=None):
    token = auth.create_access_token(user) if user is not None else None
    return request(context.app, "GET", f"/api/plagiarism/check/{context.check.id}/{suffix}",
                   cookie=token if via_cookie else None, authorization="Bearer " + token if token and not via_cookie else None,
                   headers=headers, raw_response=True)


@pytest.mark.parametrize("suffix", URL_SUFFIXES)
@pytest.mark.parametrize("via_cookie", [False, True])
def test_student_owner_receives_real_pdf_through_either_url(context, suffix, via_cookie):
    code, headers, content = get_pdf(context, suffix, user=context.owner, via_cookie=via_cookie)
    assert code == 200
    assert content == context.artifact.read_bytes() and content.startswith(b"%PDF-")
    assert headers["content-type"] == "application/pdf"
    assert headers["content-disposition"].startswith("inline;")
    assert headers["cache-control"] == "private, no-store"
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("suffix", URL_SUFFIXES)
def test_admin_can_read_but_other_student_and_anonymous_cannot(context, suffix):
    assert get_pdf(context, suffix, user=context.admin)[0] == 200
    assert get_pdf(context, suffix, user=context.outsider)[0] == 403
    assert get_pdf(context, suffix)[0] == 401


@pytest.mark.parametrize("suffix", URL_SUFFIXES)
def test_cihuy_student_mode_only_opens_its_own_pdf(context, suffix):
    student_view = student_mode_user(context.cihuy)
    assert get_pdf(context, suffix, user=student_view)[0] == 403
    context.document.user_id = context.cihuy.id
    context.check.user_id = context.cihuy.id
    context.db.commit()
    assert get_pdf(context, suffix, user=student_view)[0] == 200


@pytest.mark.parametrize("suffix", URL_SUFFIXES)
def test_alias_retains_readiness_and_soft_delete_gates(context, suffix):
    context.check.status = "processing"
    context.db.commit()
    code, headers, content = get_pdf(context, suffix, user=context.owner)
    assert code == 409 and headers["retry-after"] == "5"
    context.check.status = "completed"
    context.document.deleted_at = utc_now()
    context.db.commit()
    assert get_pdf(context, suffix, user=context.owner)[0] == 404


@pytest.mark.parametrize("suffix", URL_SUFFIXES)
def test_browser_pdf_range_request_is_served_for_owner(context, suffix):
    code, headers, content = get_pdf(context, suffix, user=context.owner, headers={"Range": "bytes=0-255"})
    assert code == 206
    assert headers["content-range"].startswith("bytes 0-255/")
    assert content == context.artifact.read_bytes()[:256]
