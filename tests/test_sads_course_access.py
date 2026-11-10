"""Current-course authorization with synthetic campus responses only."""
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError, URLError

import jwt
import pytest
from fastapi import HTTPException

from app.models.schemas import LoginThrottle, User
from app.routes import auth
from app.services import sads_course_service as courses
from test_auth_session import request
from test_sads_auth import NIM, PASSWORD, PRIVATE_VALUE, environment, login


@pytest.mark.parametrize("course_names", [["Seminar"], ["Skripsi"], ["  SEMINAR  "], ["Seminar", "Skripsi"]])
def test_only_current_seminar_or_skripsi_grants_a_student_session(environment, course_names):
    app, _, opener = environment
    opener.courses.payload["jadwal_matakuliah"] = [{"matkul": name} for name in course_names]

    code, headers, body = login(app)

    assert code == 200
    assert headers["cache-control"] == "no-store"
    outgoing, timeout = opener.courses.requests[0]
    assert outgoing.method == "GET"
    assert outgoing.data is None
    assert outgoing.full_url == f"https://courses.example.invalid/current/{NIM}"
    assert outgoing.get_header("User-agent") == "INSTIKI-SimilarityChecker/1.0"
    assert outgoing.get_header("X-api-key") is None  # Login integration headers are not forwarded.
    assert outgoing.get_header("Secret") == PRIVATE_VALUE
    assert timeout == 10
    claims = jwt.decode(body["token"], auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    assert claims["sads_courses_verified"] is True
    assert "jadwal_matakuliah" not in claims
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {body['token']}")[0] == 200


@pytest.mark.parametrize("names", [[], ["Seminar Proposal"], ["Seminar Nasional"], ["Pra Skripsi"], ["Skripsi Lanjutan"], ["Basis Data Lanjut"]])
def test_unenrolled_student_cannot_obtain_a_token_or_bypass_using_client_fields(environment, names):
    app, db, opener = environment
    opener.courses.payload["jadwal_matakuliah"] = [{"matkul": name} for name in names]

    code, headers, body = request(app, "POST", "/api/auth/login", body={
        "identifier": NIM, "password": PASSWORD, "sads_courses_verified": True,
        "jadwal_matakuliah": [{"matkul": "Seminar"}],
    })

    assert code == 403
    assert "Seminar atau Skripsi" in body["detail"]
    assert headers["cache-control"] == "no-store"
    assert "set-cookie" not in headers
    assert "token" not in body
    assert db.query(User).count() == 0
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("account:%")).one().attempt_count == 0


def test_course_denial_preserves_existing_local_account(environment):
    app, db, opener = environment
    user = User(identifier=NIM, password=auth.password_hash.hash("local-test-password"), role="mahasiswa")
    db.add(user)
    db.commit()
    original_state = (user.id, user.password, user.auth_provider, user.session_version)
    opener.courses.payload["jadwal_matakuliah"] = []

    assert login(app)[0] == 403

    db.refresh(user)
    assert (user.id, user.password, user.auth_provider, user.session_version) == original_state
    assert user.mahasiswa is None


@pytest.mark.parametrize("error", [
    URLError(f"private connection {PRIVATE_VALUE}"),
    TimeoutError(f"private timeout {PRIVATE_VALUE}"),
    HTTPError(f"https://private.invalid/{PRIVATE_VALUE}", 401, PRIVATE_VALUE, {}, io.BytesIO(PRIVATE_VALUE.encode())),
    HTTPError(f"https://private.invalid/{PRIVATE_VALUE}", 429, PRIVATE_VALUE, {}, io.BytesIO(PRIVATE_VALUE.encode())),
    HTTPError(f"https://private.invalid/{PRIVATE_VALUE}", 500, PRIVATE_VALUE, {}, io.BytesIO(PRIVATE_VALUE.encode())),
])
def test_course_api_outage_blocks_new_login_without_counting_a_bad_password(environment, caplog, error):
    app, db, opener = environment
    opener.courses.error = error

    code, headers, body = login(app)

    assert code == 503
    assert "mata kuliah aktif" in body["detail"]
    assert "set-cookie" not in headers
    assert db.query(User).count() == 0
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("ip:%")).one().attempt_count == 1
    assert db.query(LoginThrottle).filter(LoginThrottle.key.like("account:%")).one().attempt_count == 0
    assert PRIVATE_VALUE not in json.dumps(body) + caplog.text
    assert "private.invalid" not in json.dumps(body) + caplog.text


@pytest.mark.parametrize("kind", ["invalid_json", "html", "oversized", "missing_courses", "null_courses", "wrong_nim", "missing_period", "invalid_course", "blank_name"])
def test_malformed_or_other_students_course_response_cannot_grant_access(environment, kind):
    app, db, opener = environment
    if kind == "invalid_json":
        opener.courses.raw = b'{"mahasiswa":'
    elif kind == "html":
        opener.courses.raw = b"<html>gateway error</html>"
    elif kind == "oversized":
        opener.courses.raw = b" " * (courses.SadsActiveCoursesService.max_response_bytes + 1)
    elif kind == "missing_courses":
        del opener.courses.payload["jadwal_matakuliah"]
    elif kind == "null_courses":
        opener.courses.payload["jadwal_matakuliah"] = None
    elif kind == "wrong_nim":
        opener.courses.use_requested_nim = False
        opener.courses.payload["mahasiswa"]["nim"] = "2400000099"
    elif kind == "missing_period":
        del opener.courses.payload["periode"]
    elif kind == "invalid_course":
        opener.courses.payload["jadwal_matakuliah"].append(None)
    else:
        opener.courses.payload["jadwal_matakuliah"].append({"matkul": " "})

    code, headers, _ = login(app)

    assert code == 502
    assert "set-cookie" not in headers
    assert db.query(User).count() == 0


def test_failed_credentials_never_query_student_courses(environment):
    app, _, opener = environment
    opener.error = HTTPError("https://example.invalid/login", 401, "Unauthorized", {}, io.BytesIO())

    assert login(app)[0] == 401
    assert not opener.courses.requests


def test_missing_course_configuration_blocks_students_but_admin_stays_local(environment, monkeypatch):
    app, db, opener = environment
    monkeypatch.delenv("SADS_ACTIVE_COURSES_URL_TEMPLATE")

    code, headers, _ = login(app)
    assert code == 503
    assert "set-cookie" not in headers
    assert db.query(User).count() == 0

    db.add(User(identifier="admin", password=auth.password_hash.hash("local-admin-password"), role="admin"))
    db.commit()
    assert login(app, "admin", "local-admin-password")[0] == 200
    assert not opener.courses.requests
    assert len(opener.requests) == 1


@pytest.mark.parametrize("verified_claim", [None, False, "true", 1])
def test_previously_issued_or_unverified_student_tokens_require_login_again(environment, verified_claim):
    app, db, _ = environment
    _, _, body = login(app)
    user = db.query(User).one()
    claims = jwt.decode(auth.create_access_token(user), auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    if verified_claim is None:
        claims.pop("sads_courses_verified")
    else:
        claims["sads_courses_verified"] = verified_claim
    old_token = jwt.encode(claims, auth.JWT_SECRET_KEY, algorithm=auth.JWT_ALGORITHM)

    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {old_token}")[0] == 401
    assert request(app, "GET", "/api/auth/me", cookie=old_token)[0] == 401
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {body['token']}")[0] == 200


def test_each_student_login_rechecks_current_courses(environment):
    app, _, opener = environment
    code, _, previous = login(app)
    assert code == 200
    opener.courses.payload["jadwal_matakuliah"] = []

    code, headers, _ = login(app)

    assert code == 403
    assert "set-cookie" not in headers
    assert len(opener.courses.requests) == 2
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {previous['token']}")[0] == 401


@pytest.mark.parametrize("url", [
    "", "http://example.invalid/current/{nim}", "https://user:secret@example.invalid/{nim}",
    "https://example.invalid/current", "https://example.invalid/{nim}/{nim}",
    "https://example.invalid/{other}", "https://{nim}.invalid/current", "https://example.invalid/current?nim={nim}",
    "https://example.invalid/{nim}#private", "https://example.invalid:bad/{nim}",
])
def test_course_url_configuration_requires_https_and_a_single_nim_path_placeholder(environment, monkeypatch, url):
    monkeypatch.setenv("SADS_ACTIVE_COURSES_URL_TEMPLATE", url)

    with pytest.raises(RuntimeError, match="SADS_ACTIVE_COURSES_URL_TEMPLATE") as error:
        courses.SadsActiveCoursesSettings.from_environment()
    assert "secret" not in str(error.value)


def test_course_url_encodes_nim_and_uses_only_its_own_server_headers(environment, monkeypatch):
    _, _, opener = environment
    monkeypatch.setenv("SADS_ACTIVE_COURSES_HEADERS_JSON", json.dumps({"secret": PRIVATE_VALUE}))
    service = courses.SadsActiveCoursesService()

    service.require_eligible_student("student/name?query=value")

    outgoing, _ = opener.courses.requests[0]
    assert outgoing.full_url == "https://courses.example.invalid/current/student%2Fname%3Fquery%3Dvalue"
    assert outgoing.get_header("Secret") == PRIVATE_VALUE
    assert outgoing.get_header("Authorization") is None
    assert outgoing.data is None


@pytest.mark.parametrize("headers", ['{}', '{"secret":""}', '{"secret":" "}', '{"Secret":"key\\r\\ninjected"}', '[]'])
def test_required_course_secret_is_checked_without_exposing_its_value(environment, monkeypatch, headers):
    monkeypatch.setenv("SADS_ACTIVE_COURSES_HEADERS_JSON", headers)
    with pytest.raises(RuntimeError, match="SADS_ACTIVE_COURSES_HEADERS_JSON"):
        courses.SadsActiveCoursesSettings.from_environment()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_course_transport_never_follows_redirects(status):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(status)
            self.send_header("Location", "/must-not-be-visited")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        # Only isolated transport tests bypass environment HTTPS validation.
        service = courses.SadsActiveCoursesService(courses.SadsActiveCoursesSettings(
            f"http://127.0.0.1:{server.server_port}/current/{{nim}}",
        ))
        with pytest.raises(HTTPException) as error:
            service.require_eligible_student(NIM)
        assert error.value.status_code == 503
        assert received == [f"/current/{NIM}"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
