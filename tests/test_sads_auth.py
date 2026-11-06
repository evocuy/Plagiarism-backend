"""Exercise SADS authentication without campus credentials or campus requests."""

import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError, URLError

import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.session import Base, get_db
from app.models.schemas import Document, LoginThrottle, Mahasiswa, PlagiarismCheck, User
from app.routes import auth
from app.services import sads_auth_service as sads
from test_auth_session import request


NIM = "2400000001"
PASSWORD = "synthetic-campus-password"
PRIVATE_VALUE = "synthetic-private-integration-value"


def student_payload():
    return {
        "id1": "opaque-example-one",
        "id2": "opaque-example-two",
        "nama": "Mahasiswa Uji",
        "jurusan": "IF-DEMO",
        "periode_aktif": "Contoh periode",
        "role": {"name": "Mahasiswa", "menus": [{"name": "irrelevant menu"}]},
        "roles": [{"name": "Mahasiswa"}],
        "sbt": PRIVATE_VALUE,
    }


class FakeResponse(io.BytesIO):
    status = 200


class FakeOpener:
    def __init__(self):
        self.payload = student_payload()
        self.error = None
        self.raw = None
        self.requests = []

    def open(self, outgoing, *, timeout):
        self.requests.append((outgoing, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.raw if self.raw is not None else json.dumps(self.payload).encode())


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setenv("STUDENT_AUTH_PROVIDER", "sads")
    monkeypatch.setenv("SADS_LOGIN_URL", "https://campus.example.invalid/private-login")
    monkeypatch.setenv("SADS_LOGIN_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("SADS_LOGIN_HEADERS_JSON", json.dumps({"X-API-Key": PRIVATE_VALUE}))
    monkeypatch.delenv("SADS_TEST_ALLOWED_USERNAMES", raising=False)
    monkeypatch.delenv("SADS_TEST_ACCESS_EXPIRES_AT", raising=False)
    monkeypatch.setenv("LOGIN_ACCOUNT_MAX_FAILURES", "5")
    monkeypatch.setenv("LOGIN_IP_MAX_ATTEMPTS", "30")
    monkeypatch.setattr(auth, "JWT_SECRET_KEY", "sads-tests-only-secret-which-is-more-than-32-characters")
    monkeypatch.setattr(auth, "JWT_EXPIRE_MINUTES", 60)
    monkeypatch.setattr(auth, "SESSION_COOKIE_SECURE", True)
    opener = FakeOpener()
    monkeypatch.setattr(sads, "build_opener", lambda *handlers: opener)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    yield app, db, opener
    db.close()
    engine.dispose()


def login(app, username=NIM, password=PASSWORD):
    return request(app, "POST", "/api/auth/login", body={"identifier": username, "password": password})


def test_login_uses_post_json_and_local_session_without_sads_secrets(environment):
    app, db, opener = environment
    code, headers, body = login(app)
    assert code == 200
    assert headers["cache-control"] == "no-store"
    assert "httponly" in headers["set-cookie"].lower()
    outgoing, timeout = opener.requests[0]
    assert outgoing.method == "POST"
    assert json.loads(outgoing.data) == {"username": NIM, "password": PASSWORD}
    assert outgoing.get_header("Content-type") == "application/json"
    assert outgoing.get_header("User-agent") == "INSTIKI-SimilarityChecker/1.0"
    assert outgoing.get_header("X-api-key") == PRIVATE_VALUE
    assert timeout == 10
    assert body["user"]["identifier"] == NIM
    assert body["user"]["role"] == "mahasiswa"
    assert body["user"]["program_studi"] == "IF-DEMO"
    assert body["user"]["fakultas"] == ""
    assert PRIVATE_VALUE not in json.dumps(body)
    assert PASSWORD not in json.dumps(body)
    assert "sbt" not in json.dumps(body)
    user = db.query(User).one()
    assert user.auth_provider == "sads"
    assert user.password != PASSWORD
    assert not auth.password_hash.verify(PASSWORD, user.password)
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {body['token']}")[0] == 200
    assert request(app, "POST", "/api/auth/logout", authorization=f"Bearer {body['token']}")[0] == 200
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {body['token']}")[0] == 401


@pytest.mark.parametrize("legacy_settings", ["missing", "other_nim_only", "empty", "expired", "invalid"])
def test_sads_student_login_has_no_manual_nim_or_enrolment_gate(environment, monkeypatch, legacy_settings):
    app, db, opener = environment
    username = "2400000002"
    if legacy_settings == "other_nim_only":
        monkeypatch.setenv("SADS_TEST_ALLOWED_USERNAMES", NIM)
        monkeypatch.setenv("SADS_TEST_ACCESS_EXPIRES_AT", "2099-01-01T00:00:00Z")
    elif legacy_settings == "empty":
        monkeypatch.setenv("SADS_TEST_ALLOWED_USERNAMES", "")
        monkeypatch.setenv("SADS_TEST_ACCESS_EXPIRES_AT", "")
    elif legacy_settings == "expired":
        monkeypatch.setenv("SADS_TEST_ALLOWED_USERNAMES", username)
        monkeypatch.setenv("SADS_TEST_ACCESS_EXPIRES_AT", "2020-01-01T00:00:00Z")
    elif legacy_settings == "invalid":
        monkeypatch.setenv("SADS_TEST_ALLOWED_USERNAMES", "retired-setting")
        monkeypatch.setenv("SADS_TEST_ACCESS_EXPIRES_AT", "retired-setting")
    code, headers, body = login(app, username)
    assert code == 200
    assert headers["cache-control"] == "no-store"
    assert body["user"]["identifier"] == username
    assert body["user"]["role"] == "mahasiswa"
    assert len(opener.requests) == 1
    assert json.loads(opener.requests[0][0].data)["username"] == username
    assert db.query(User).count() == 1
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {body['token']}")[0] == 200
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("ip:%")).one().attempt_count == 1
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("account:%")).one().attempt_count == 0


@pytest.mark.parametrize("change", ["removed", "expired", "invalid", "disabled"])
def test_student_session_uses_auth_provider_and_ignores_retired_pilot_limits(environment, monkeypatch, change):
    app, _, _ = environment
    _, _, body = login(app)
    token = body["token"]
    if change == "removed":
        monkeypatch.setenv("SADS_TEST_ALLOWED_USERNAMES", "")
    elif change == "expired":
        monkeypatch.setenv("SADS_TEST_ACCESS_EXPIRES_AT", "2020-01-01T00:00:00Z")
    elif change == "invalid":
        monkeypatch.setenv("SADS_TEST_ALLOWED_USERNAMES", "retired-setting")
        monkeypatch.setenv("SADS_TEST_ACCESS_EXPIRES_AT", "retired-setting")
    else:
        monkeypatch.setenv("STUDENT_AUTH_PROVIDER", "local")
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {token}")[0] == (401 if change == "disabled" else 200)


def test_two_sads_students_keep_separate_accounts_and_valid_sessions(environment):
    app, db, _ = environment
    first_code, _, first = login(app)
    second_code, _, second = login(app, "2400000002")
    assert first_code == second_code == 200
    assert first["user"]["id"] != second["user"]["id"]
    assert db.query(User).count() == 2
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {first['token']}")[2]["identifier"] == NIM
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {second['token']}")[2]["identifier"] == "2400000002"


@pytest.mark.parametrize("active_role", ["Admin", "Dosen"])
def test_active_role_must_be_mahasiswa_even_if_role_list_contains_it(environment, active_role):
    app, db, opener = environment
    opener.payload["role"]["name"] = active_role
    code, _, body = login(app)
    assert code == 403
    assert db.query(User).count() == 0
    assert PRIVATE_VALUE not in json.dumps(body)


@pytest.mark.parametrize("kind", ["html", "invalid_json", "missing_role", "missing_id", "missing_name", "oversized"])
def test_malformed_success_responses_never_create_a_session(environment, kind):
    app, db, opener = environment
    if kind == "html":
        opener.raw = b"<html>sign in</html>"
    elif kind == "invalid_json":
        opener.raw = b'{"role":'
    elif kind == "missing_role":
        del opener.payload["role"]
    elif kind == "missing_id":
        del opener.payload["id1"]
    elif kind == "missing_name":
        opener.payload["nama"] = ""
    else:
        opener.raw = b" " * (sads.SadsAuthService.max_response_bytes + 1)
    code, headers, body = login(app)
    assert code == 502
    assert "set-cookie" not in headers
    assert "token" not in body
    assert db.query(User).count() == 0


def test_sads_unavailability_is_redacted_and_keeps_ip_budget(environment, caplog):
    app, db, opener = environment
    opener.error = URLError(f"https://private.invalid?key={PRIVATE_VALUE}&password={PASSWORD}")
    code, headers, body = login(app)
    assert code == 503
    assert "set-cookie" not in headers
    assert db.query(User).count() == 0
    assert PRIVATE_VALUE not in json.dumps(body) + caplog.text
    assert PASSWORD not in json.dumps(body) + caplog.text
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("ip:%")).one().attempt_count == 1
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("account:%")).one().attempt_count == 0


@pytest.mark.parametrize("content_type", ["text/html", "text/plain", ""])
def test_non_json_forbidden_reports_upstream_block_without_counting_bad_passwords(environment, caplog, content_type):
    app, db, opener = environment
    opener.error = HTTPError(
        "https://private.invalid/login?secret=" + PRIVATE_VALUE,
        403, "Forbidden", {"Content-Type": content_type, "Server": "cloudflare"},
        io.BytesIO(("error code: 1010 " + PRIVATE_VALUE + PASSWORD).encode()),
    )
    code, headers, body = login(app)
    assert code == 503
    assert "HTTP 403" in body["detail"]
    assert "kata sandi tidak valid" not in body["detail"]
    assert "set-cookie" not in headers
    assert PRIVATE_VALUE not in json.dumps(body) + caplog.text
    assert PASSWORD not in json.dumps(body) + caplog.text
    assert "private.invalid" not in json.dumps(body) + caplog.text
    assert db.query(User).count() == 0
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("account:%")).one().attempt_count == 0
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("ip:%")).one().attempt_count == 1


@pytest.mark.parametrize("content_type", ["application/json", "application/problem+json; charset=utf-8"])
def test_json_forbidden_reports_upstream_denial_without_claiming_wrong_password(environment, content_type):
    app, db, opener = environment
    opener.error = HTTPError(
        "https://private.invalid/login", 403, "Forbidden", {"Content-Type": content_type},
        io.BytesIO(json.dumps({"private_error": PRIVATE_VALUE}).encode()),
    )
    code, headers, body = login(app)
    assert code == 403
    assert "HTTP 403" in body["detail"]
    assert PRIVATE_VALUE not in json.dumps(body)
    assert "set-cookie" not in headers
    assert db.query(User).count() == 0
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("account:%")).one().attempt_count == 1


def test_rejected_sads_authentication_labels_upstream_401_and_counts_failure(environment):
    app, db, opener = environment
    opener.error = HTTPError("https://private.invalid", 401, "Unauthorized", {}, io.BytesIO())
    code, _, body = login(app)
    assert code == 401
    assert "SADS" in body["detail"]
    assert "HTTP 401" in body["detail"]
    assert db.query(User).count() == 0
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("account:%")).one().attempt_count == 1


@pytest.mark.parametrize("header_name", ["User-Agent", "user-agent"])
def test_explicit_user_agent_header_can_override_application_identity(environment, monkeypatch, header_name):
    app, _, opener = environment
    monkeypatch.setenv("SADS_LOGIN_HEADERS_JSON", json.dumps({header_name: "CampusApprovedClient/1.0"}))
    assert login(app)[0] == 200
    assert opener.requests[0][0].get_header("User-agent") == "CampusApprovedClient/1.0"


@pytest.mark.parametrize("status", [302, 429, 500])
def test_upstream_failures_report_only_status_without_secrets(environment, status):
    app, db, opener = environment
    opener.error = HTTPError(
        "https://private.invalid/?token=" + PRIVATE_VALUE, status, "private reason " + PRIVATE_VALUE,
        {}, io.BytesIO(PRIVATE_VALUE.encode()),
    )
    code, headers, body = login(app)
    assert code in {502, 503}
    assert f"HTTP {status}" in body["detail"]
    assert "set-cookie" not in headers
    assert PRIVATE_VALUE not in json.dumps(body)
    assert "private.invalid" not in json.dumps(body)
    assert db.query(User).count() == 0
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("account:%")).one().attempt_count == 0


@pytest.mark.parametrize("error_body", [b"", json.dumps({"message": PRIVATE_VALUE, "errors": {"password": [PRIVATE_VALUE]}}).encode()])
def test_sads_validation_rejection_returns_readable_client_error(environment, caplog, error_body):
    app, db, opener = environment
    opener.error = HTTPError(
        "https://private.invalid/?secret=" + PRIVATE_VALUE, 422, "private reason " + PRIVATE_VALUE,
        {"Content-Type": "application/json"}, io.BytesIO(error_body),
    )
    code, headers, body = login(app)
    assert code == 422
    assert body == {"detail": "Data login tidak valid. Periksa NIM dan kata sandi Anda."}
    assert headers["cache-control"] == "no-store"
    assert "set-cookie" not in headers
    assert db.query(User).count() == 0
    assert len(opener.requests) == 1
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("account:%")).one().attempt_count == 1
    assert PRIVATE_VALUE not in json.dumps(body) + caplog.text
    assert "private.invalid" not in json.dumps(body) + caplog.text


def test_sads_validation_rejections_remain_subject_to_login_attempt_limit(environment):
    app, db, opener = environment
    for _ in range(5):
        opener.error = HTTPError("https://private.invalid", 422, "Rejected", {}, io.BytesIO())
        assert login(app)[0] == 422
    code, headers, _ = login(app)
    assert code == 429
    assert int(headers["retry-after"]) > 0
    assert len(opener.requests) == 5
    assert db.query(User).count() == 0


def test_sads_validation_rejection_preserves_existing_user_and_session(environment):
    app, db, opener = environment
    code, _, signed_in = login(app)
    assert code == 200
    user = db.query(User).one()
    original_state = (user.id, user.password, user.auth_provider, user.session_version)
    opener.error = HTTPError("https://private.invalid", 422, "Rejected", {}, io.BytesIO())
    code, headers, _ = login(app)
    assert code == 422
    assert "set-cookie" not in headers
    db.refresh(user)
    assert (user.id, user.password, user.auth_provider, user.session_version) == original_state
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {signed_in['token']}")[0] == 200


def test_rejected_passwords_remain_throttled_without_local_fallback(environment):
    app, db, opener = environment
    db.add(User(identifier=NIM, password=auth.password_hash.hash(PASSWORD), role="mahasiswa"))
    db.commit()
    # A valid old local password cannot bypass SADS rejection.
    for _ in range(5):
        opener.error = HTTPError("https://campus.example.invalid", 401, "Unauthorized", {}, io.BytesIO())
        assert login(app)[0] == 401
    code, headers, _ = login(app)
    assert code == 429
    assert int(headers["retry-after"]) > 0
    assert len(opener.requests) == 5
    assert db.query(User).one().auth_provider == "local"


def test_admin_login_stays_local_during_sads_outage(environment, monkeypatch):
    app, db, opener = environment
    db.add(User(identifier="admin", password=auth.password_hash.hash("local-admin-password"), role="admin"))
    db.commit()
    opener.error = URLError("campus unavailable")
    monkeypatch.setenv("SADS_TEST_ALLOWED_USERNAMES", "")
    code, _, body = login(app, "admin", "local-admin-password")
    assert code == 200
    assert body["user"]["role"] == "admin"
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {body['token']}")[0] == 200
    assert not opener.requests
    assert login(app, "admin", PASSWORD)[0] == 401
    assert not opener.requests


def test_linking_same_nim_preserves_documents_scores_and_account_identity(environment):
    app, db, _ = environment
    user = User(identifier=NIM, password=auth.password_hash.hash("old-local-password"), role="mahasiswa")
    db.add(user)
    db.flush()
    user.mahasiswa = Mahasiswa(nama_lengkap="Profil Lama", program_studi="Lama", fakultas="Fakultas Lama")
    document = Document(user_id=user.id, title="Existing.pdf", file_path="existing.pdf")
    db.add(document)
    db.flush()
    check = PlagiarismCheck(user_id=user.id, document_id=document.id, overall_similarity=0.42, status="completed")
    db.add(check)
    db.commit()
    user_id, document_id, check_id = user.id, document.id, check.id
    old_token = auth.create_access_token(user)
    # Enabling SADS already rejects local student sessions before linking.
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {old_token}")[0] == 401
    code, _, body = login(app)
    assert code == 200
    assert body["user"]["id"] == user_id
    assert db.query(User).count() == 1
    db.refresh(user)
    assert user.auth_provider == "sads"
    assert not auth.password_hash.verify("old-local-password", user.password)
    assert user.mahasiswa.nama_lengkap == "Mahasiswa Uji"
    assert user.mahasiswa.fakultas == "Fakultas Lama"
    assert db.get(Document, document_id).user_id == user_id
    assert db.get(PlagiarismCheck, check_id).overall_similarity == 0.42
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {old_token}")[0] == 401
    _, _, second = login(app)
    assert db.query(User).count() == 1
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {body['token']}")[0] == 401
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {second['token']}")[0] == 200


def test_inactive_student_is_not_reactivated(environment):
    app, db, opener = environment
    db.add(User(identifier=NIM, password=auth.password_hash.hash(PASSWORD), role="mahasiswa", is_active=False))
    db.commit()
    assert login(app)[0] == 401
    assert not opener.requests
    assert db.query(User).one().is_active is False


def test_sads_user_cannot_use_a_local_password_when_mode_is_disabled(environment, monkeypatch):
    app, db, opener = environment
    assert login(app)[0] == 200
    user = db.query(User).one()
    user.password = auth.password_hash.hash("known-local-password")
    db.commit()
    opener.requests.clear()
    monkeypatch.setenv("STUDENT_AUTH_PROVIDER", "local")
    assert login(app, NIM, "known-local-password")[0] == 401
    assert not opener.requests


def test_default_local_mode_preserves_local_student_login(environment, monkeypatch):
    app, db, opener = environment
    monkeypatch.delenv("STUDENT_AUTH_PROVIDER")
    monkeypatch.delenv("SADS_LOGIN_URL")
    db.add(User(identifier="local-student", password=auth.password_hash.hash(PASSWORD), role="mahasiswa"))
    db.commit()
    code, _, body = login(app, "local-student")
    assert code == 200
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {body['token']}")[0] == 200
    assert not opener.requests


@pytest.mark.parametrize("body", [{"identifier": "2400000002"}, {"password": "another-password"}])
def test_local_admin_cannot_change_sads_nim_or_password(environment, body):
    app, db, _ = environment
    _, _, student_login = login(app)
    db.add(User(identifier="admin", password=auth.password_hash.hash("local-admin-password"), role="admin"))
    db.commit()
    _, _, admin_login = login(app, "admin", "local-admin-password")
    status, _, _ = request(
        app, "PUT", f"/api/auth/users/{student_login['user']['id']}",
        authorization=f"Bearer {admin_login['token']}", body=body,
    )
    assert status == 400
    assert db.get(User, student_login["user"]["id"]).identifier == NIM


@pytest.mark.parametrize("url", ["", "http://private.invalid/login", "https://user:secret@private.invalid/login", "https://private.invalid/#token", "https://private.invalid:bad/login"])
def test_settings_require_https_without_embedded_credentials_or_fragment(environment, monkeypatch, url):
    monkeypatch.setenv("SADS_LOGIN_URL", url)
    with pytest.raises(RuntimeError, match="SADS_LOGIN_URL") as error:
        sads.SadsLoginSettings.from_environment()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("headers", ['[]', '{"Host":"other.invalid"}', '{"Authorization":"secret\\r\\ninjected"}', 'not-json'])
def test_invalid_extra_header_configuration_is_rejected(environment, monkeypatch, headers):
    monkeypatch.setenv("SADS_LOGIN_HEADERS_JSON", headers)
    with pytest.raises(RuntimeError, match="SADS_LOGIN_HEADERS_JSON"):
        sads.SadsLoginSettings.from_environment()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_transport_never_follows_login_redirects(code):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(code)
            self.send_header("Location", "/must-not-be-visited")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    # Only this isolated transport test supplies HTTP directly; environment
    # configuration always requires HTTPS for the real campus endpoint.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        service = sads.SadsAuthService(sads.SadsLoginSettings(f"http://127.0.0.1:{server.server_port}/login"))
        with pytest.raises(HTTPException) as error:
            service.authenticate(NIM, PASSWORD)
        assert error.value.status_code == 503
        assert received == ["/login"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
