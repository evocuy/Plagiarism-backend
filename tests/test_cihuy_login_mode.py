"""The cihuy student session has its own permissions, never a changed DB role."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import jwt
import pytest
from fastapi.exceptions import RequestValidationError
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.models.schemas import Document, PlagiarismCheck, SupportMessage, SupportTicket, User
from app.routes import auth, dashboard, documents, plagiarism, support
from app.services.csrf_service import CsrfMiddleware
from app.services.error_service import sanitized_validation_error
from app.services.account_mode_service import student_mode_user
from app.services.check_queue_service import CheckQueueService, CheckQueueSettings
from app.services import support_service
from test_auth_session import environment, request
from test_repository_import import make_pdf
from test_repository_import_postgresql import postgres


@pytest.fixture
def context(environment, monkeypatch, tmp_path):
    monkeypatch.setenv("STUDENT_AUTH_PROVIDER", "local")
    monkeypatch.setenv("CHECK_DAILY_LIMIT", "3")
    monkeypatch.setenv("CHECK_MAX_ACTIVE_PER_STUDENT", "1")
    monkeypatch.setenv("CHECK_MAX_QUEUED", "25")
    app, db = environment
    for router, prefix in [(documents.router, "/api/documents"), (plagiarism.router, "/api/plagiarism"),
                           (dashboard.router, "/api/dashboard"), (support.router, "/api/support")]:
        app.include_router(router, prefix=prefix)
    app.add_exception_handler(RequestValidationError, sanitized_validation_error)
    app.add_middleware(CsrfMiddleware, secret_key=auth._jwt_secret_key,
                       session_cookie=auth.SESSION_COOKIE_NAME, impersonation_cookie=auth.IMPERSONATION_COOKIE_NAME)
    cihuy = User(identifier="cihuy", password=auth.password_hash.hash("password123"), role="admin", nama_lengkap="Cihuy")
    db.add(cihuy)
    db.commit()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(documents, "UPLOAD_DIR", uploads)
    pdf = make_pdf(tmp_path / "input" / "synthetic.pdf").read_bytes()
    return SimpleNamespace(app=app, db=db, cihuy=cihuy, uploads=uploads, pdf=pdf)


def login(context, identifier="cihuy", mode=None):
    body = {"identifier": identifier, "password": "password123"}
    if mode is not None:
        body["login_as"] = mode
    return request(context.app, "POST", "/api/auth/login", body=body)


def call(context, body, method, path, payload=None, cookie=False):
    return request(context.app, method, path, body=payload,
                   authorization=None if cookie else "Bearer " + body["token"],
                   cookie=body["token"] if cookie else None,
                   headers={"X-CSRF-Token": body["csrf_token"]} if cookie else None)


def upload(context, body, *, owner=None, content=None):
    fields = {"document_type": "proposal", "auto_index_embeddings": "false"}
    if owner is not None:
        fields["user_id"] = str(owner)
    data = b""
    for name, value in fields.items():
        data += f'--test-boundary\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    data += b'--test-boundary\r\nContent-Disposition: form-data; name="file"; filename="synthetic.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
    data += content if content is not None else context.pdf
    data += b"\r\n--test-boundary--\r\n"
    return request(context.app, "POST", "/api/documents/upload-and-check", cookie=body["token"],
                   headers={"Content-Type": "multipart/form-data; boundary=test-boundary", "X-CSRF-Token": body["csrf_token"]}, raw_body=data)


def test_default_login_is_admin_and_exposes_both_choices(context):
    code, _, body = login(context)
    assert code == 200 and body["user"]["role"] == "admin"
    assert body["user"]["available_login_modes"] == ["admin", "mahasiswa"]
    assert body["user"]["unlimited_daily_checks"] is True
    assert call(context, body, "GET", "/api/auth/users")[0] == 200


def test_direct_student_login_uses_same_account_without_changing_role(context):
    code, _, body = login(context, mode="mahasiswa")
    assert code == 200 and body["user"]["role"] == "mahasiswa"
    assert body["user"]["id"] == context.cihuy.id
    assert body["user"]["unlimited_daily_checks"] is True
    claims = jwt.decode(body["token"], auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    assert claims["session_mode"] == "mahasiswa" and claims["role"] == "mahasiswa"
    assert claims.get("impersonation_id") is None
    assert call(context, body, "GET", "/api/auth/me", cookie=True)[2]["role"] == "mahasiswa"
    context.db.expire_all()
    assert context.db.query(User).filter_by(identifier="cihuy").one().role == "admin"


def test_choose_student_mode_after_admin_login_revokes_old_token_without_extending_expiry(context):
    _, _, parent = login(context)
    parent_claims = jwt.decode(parent["token"], auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    code, headers, child = call(context, parent, "POST", "/api/auth/session-mode", {"login_as": "mahasiswa"}, cookie=True)
    assert code == 200 and headers["cache-control"] == "no-store"
    claims = jwt.decode(child["token"], auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    assert claims["exp"] == parent_claims["exp"] and claims["sv"] == parent_claims["sv"] + 1
    assert child["csrf_token"] != parent["csrf_token"]
    assert call(context, parent, "GET", "/api/auth/me")[0] == 401
    assert call(context, child, "GET", "/api/auth/me")[2]["role"] == "mahasiswa"


def test_cookie_mode_switch_requires_csrf(context):
    _, _, parent = login(context)
    version = context.db.query(User).filter_by(identifier="cihuy").one().session_version
    code, _, body = request(context.app, "POST", "/api/auth/session-mode", cookie=parent["token"], body={"login_as": "mahasiswa"})
    assert code == 403 and body["error_code"] == "csrf_token_invalid"
    assert context.db.query(User).filter_by(identifier="cihuy").one().session_version == version


@pytest.mark.parametrize("identifier,mode", [("student", "admin"), ("admin", "mahasiswa")])
def test_other_accounts_cannot_choose_another_role_or_receive_unlimited_quota(context, identifier, mode):
    _, _, original = login(context, identifier)
    assert original["user"]["unlimited_daily_checks"] is False
    assert login(context, identifier, mode)[0] == 403
    assert call(context, original, "GET", "/api/auth/me")[0] == 200
    assert call(context, original, "POST", "/api/auth/session-mode", {"login_as": mode})[0] == 403


def test_student_mode_cannot_call_admin_operations_or_restore_admin_without_login(context):
    _, _, body = login(context, mode="mahasiswa")
    assert call(context, body, "GET", "/api/auth/users")[0] == 403
    assert call(context, body, "POST", "/api/auth/session-mode", {"login_as": "admin"})[0] == 403
    assert call(context, body, "POST", "/api/auth/impersonation", {"nim": "2400000001", "impersonation_password": "synthetic"})[0] == 403
    assert call(context, body, "POST", "/api/documents/reindex", {})[0] == 403
    assert login(context, mode="admin")[2]["user"]["role"] == "admin"


def test_student_mode_only_sees_own_documents_history_and_dashboard(context):
    owner = context.db.query(User).filter_by(identifier="student").one()
    own = Document(user_id=context.cihuy.id, title="own.pdf", file_path="own.pdf")
    other = Document(user_id=owner.id, title="other.pdf", file_path="other.pdf")
    context.db.add_all([own, other])
    context.db.flush()
    context.db.add_all([PlagiarismCheck(document_id=own.id, user_id=context.cihuy.id, overall_similarity=.1),
                        PlagiarismCheck(document_id=other.id, user_id=owner.id, overall_similarity=.2)])
    context.db.commit()
    own_id, other_id = own.id, other.id
    _, _, body = login(context, mode="mahasiswa")
    assert [row["id"] for row in call(context, body, "GET", "/api/documents")[2]] == [own_id]
    assert [row["document_id"] for row in call(context, body, "GET", "/api/plagiarism/history")[2]] == [own_id]
    assert call(context, body, "GET", "/api/dashboard/summary")[2]["document_count"] == 1
    assert call(context, body, "POST", f"/api/plagiarism/check-repository/{other_id}")[0] == 403


def test_four_real_uploads_are_private_owned_by_cihuy_and_not_limited_per_day(context):
    other_id = context.db.query(User).filter_by(identifier="student").one().id
    _, _, body = login(context, mode="mahasiswa")
    for _ in range(4):
        code, _, result = upload(context, body, owner=other_id)
        assert code == 202
        context.db.get(PlagiarismCheck, result["check_id"]).status = "completed"
        context.db.commit()
    assert context.db.query(Document).count() == 4
    for document in context.db.query(Document).all():
        assert document.user_id == context.cihuy.id
        assert document.source_origin == "student" and document.repository_status == "draft"
    assert context.db.query(User).filter_by(identifier="cihuy").one().role == "admin"


def test_daily_quota_still_applies_to_normal_students_even_with_client_flags(context):
    student = context.db.query(User).filter_by(identifier="student").one()
    document = Document(user_id=student.id, title="own.pdf", file_path="own.pdf")
    context.db.add(document)
    context.db.flush()
    context.db.add_all([PlagiarismCheck(document_id=document.id, user_id=student.id, overall_similarity=0) for _ in range(3)])
    context.db.commit()
    code, _, body = request(context.app, "POST", "/api/auth/login", body={"identifier": "student", "password": "password123",
                                                                             "login_as": "mahasiswa", "unlimited_daily_checks": True})
    assert code == 200 and body["user"]["unlimited_daily_checks"] is False
    assert upload(context, body)[0] == 429
    assert not list(context.uploads.glob("*.pdf"))


def test_unlimited_daily_quota_keeps_active_job_and_file_size_limits(context, monkeypatch):
    _, _, body = login(context, mode="mahasiswa")
    assert upload(context, body)[0] == 202
    assert upload(context, body)[0] == 409
    context.db.query(PlagiarismCheck).update({"status": "completed"})
    context.db.commit()
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 64)
    assert upload(context, body, content=b"%PDF-1.7" + b"x" * 100)[0] == 413
    assert context.db.query(Document).count() == 1


def test_unlimited_daily_quota_keeps_global_queue_capacity(context, monkeypatch):
    monkeypatch.setenv("CHECK_MAX_QUEUED", "1")
    student = context.db.query(User).filter_by(identifier="student").one()
    document = Document(user_id=student.id, title="other.pdf", file_path="other.pdf")
    context.db.add(document)
    context.db.flush()
    context.db.add(PlagiarismCheck(document_id=document.id, user_id=student.id, overall_similarity=0, status="pending"))
    context.db.commit()
    _, _, body = login(context, mode="mahasiswa")
    code, _, result = upload(context, body)
    assert code == 429 and "Antrean" in result["detail"]
    assert context.db.query(Document).count() == 1


def test_support_row_lock_keeps_student_permissions(context):
    student = context.db.query(User).filter_by(identifier="student").one()
    ticket = SupportTicket(owner_id=student.id, owner_name="Student", owner_identifier="student", category="Lainnya", subject="Private", status="new")
    context.db.add(ticket)
    context.db.commit()
    identifier = ticket.id
    _, _, body = login(context, mode="mahasiswa")
    code, _, created = call(context, body, "POST", "/api/support/tickets", {"category": "Lainnya", "subject": "Percobaan", "message": "Pesan percobaan dari akun sendiri."})
    assert code == 201
    assert context.db.query(SupportMessage).one().author_role == "mahasiswa"
    assert call(context, body, "GET", f"/api/support/tickets/{identifier}")[0] == 404
    assert call(context, body, "POST", f"/api/support/tickets/{identifier}/messages", {"message": "Cannot access private ticket"})[0] == 404


def test_logout_revokes_student_session_and_preserves_admin_database_role(context):
    _, _, body = login(context, mode="mahasiswa")
    assert call(context, body, "POST", "/api/auth/logout", cookie=True)[0] == 200
    assert call(context, body, "GET", "/api/auth/me")[0] == 401
    assert context.db.query(User).filter_by(identifier="cihuy").one().role == "admin"


def test_local_cihuy_student_mode_never_calls_campus_login_or_courses(context, monkeypatch):
    monkeypatch.setenv("STUDENT_AUTH_PROVIDER", "sads")
    monkeypatch.setenv("SADS_LOGIN_URL", "https://campus.example.test/login")
    monkeypatch.setenv("SADS_LOGIN_HEADERS_JSON", "{}")
    monkeypatch.setattr(auth.SadsAuthService, "authenticate", lambda *_args: pytest.fail("Local administrator must not call SADS login"))
    monkeypatch.setattr(auth.SadsActiveCoursesService, "require_eligible_student", lambda *_args: pytest.fail("Own test account has no campus NIM"))
    assert login(context, mode="mahasiswa")[0] == 200


@pytest.mark.parametrize("mode", [None, True, {}, "unsupported"])
def test_signed_invalid_session_mode_is_rejected(context, mode):
    token = auth.create_access_token(context.cihuy)
    claims = jwt.decode(token, auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    claims["session_mode"] = mode
    claims["role"] = "mahasiswa"
    forged = jwt.encode(claims, auth.JWT_SECRET_KEY, algorithm=auth.JWT_ALGORITHM)
    assert request(context.app, "GET", "/api/auth/me", authorization="Bearer " + forged)[0] == 401


@pytest.mark.parametrize("identifier", ["student", "admin"])
def test_signed_student_mode_on_other_accounts_is_rejected(context, identifier):
    user = context.db.query(User).filter_by(identifier=identifier).one()
    claims = jwt.decode(auth.create_access_token(user), auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    claims.update(session_mode="mahasiswa", role="mahasiswa")
    token = jwt.encode(claims, auth.JWT_SECRET_KEY, algorithm=auth.JWT_ALGORITHM)
    assert request(context.app, "GET", "/api/auth/me", authorization="Bearer " + token)[0] == 401


def test_cookie_and_bearer_cannot_mix_student_and_admin_modes(context):
    admin = auth.create_access_token(context.cihuy)
    student = auth.create_access_token(context.cihuy, session_mode="mahasiswa")
    assert request(context.app, "GET", "/api/auth/me", authorization="Bearer " + admin, cookie=student)[0] == 401


@pytest.mark.parametrize("change", [{"identifier": "renamed"}, {"role": "mahasiswa"}, {"is_active": False}, {"auth_provider": "sads"}])
def test_restricted_mode_stops_working_if_admin_identity_changes(context, change):
    _, _, body = login(context, mode="mahasiswa")
    context.db.query(User).filter_by(identifier="cihuy").update(change)
    context.db.commit()
    assert call(context, body, "GET", "/api/auth/me")[0] == 401


@pytest.mark.parametrize("changes", [{"role": "admin"}, {"impersonation_id": "a" * 32}])
def test_conflicting_role_and_impersonation_claims_are_rejected(context, changes):
    _, _, body = login(context, mode="mahasiswa")
    claims = jwt.decode(body["token"], auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    claims.update(changes)
    token = jwt.encode(claims, auth.JWT_SECRET_KEY, algorithm=auth.JWT_ALGORITHM)
    assert request(context.app, "GET", "/api/auth/me", authorization="Bearer " + token)[0] == 401


def test_postgresql_unlimited_daily_checks_still_serialize_active_job_admission(postgres):
    factory = sessionmaker(bind=postgres)
    settings = CheckQueueSettings(daily_limit=3, max_active_per_student=1, max_queued_checks=25)
    with factory() as db:
        cihuy = User(identifier="cihuy", password="unused", role="admin")
        regular = User(identifier="student", password="unused", role="mahasiswa")
        db.add_all([cihuy, regular])
        db.flush()
        own = Document(user_id=cihuy.id, title="own.pdf", file_path="own.pdf")
        other = Document(user_id=regular.id, title="other.pdf", file_path="other.pdf")
        db.add_all([own, other])
        db.flush()
        for owner, document in [(cihuy, own), (regular, other)]:
            db.add_all([PlagiarismCheck(document_id=document.id, user_id=owner.id, overall_similarity=0) for _ in range(3)])
        db.commit()
        user_id, document_id, regular_id = cihuy.id, own.id, regular.id

    def attempt(_index):
        with factory() as db:
            try:
                CheckQueueService(settings=settings).enqueue(db=db, document_id=document_id, user_id=user_id, candidate_limit=20,
                                            auto_index_embeddings=False, auto_index_repository_limit=0)
                return 202
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, [1, 2]))
    assert sorted(results) == [202, 409]
    with factory() as db:
        with pytest.raises(HTTPException) as error:
            CheckQueueService(settings=settings).assert_can_enqueue(db, regular_id)
        assert error.value.status_code == 429
        assert db.get(User, user_id).role == "admin"


def test_postgresql_support_row_lock_preserves_student_mode(postgres):
    with sessionmaker(bind=postgres)() as db:
        cihuy = User(identifier="cihuy", password="unused", role="admin")
        db.add(cihuy)
        db.commit()
        identifier = cihuy.id
        result = support_service.create_ticket(db, student_mode_user(cihuy), "Lainnya", "Percobaan", "Pesan percobaan.")
        assert db.get(SupportTicket, result["id"]).owner_id == identifier
        assert db.query(SupportMessage).one().author_role == "mahasiswa"
        assert db.get(User, identifier).role == "admin"
