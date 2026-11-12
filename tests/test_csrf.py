"""Exercise cookie CSRF protection with real authentication dependencies."""
import pytest
from fastapi import Depends, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.models.schemas import User
from app.routes import auth
from app.services.csrf_service import CsrfMiddleware, create_csrf_token, trusted_csrf_origins
from app.services.request_limit_service import RequestLimitMiddleware
from test_auth_session import environment, login, request


FRONTEND = "https://frontend.example.test"


@pytest.fixture
def protected(environment, monkeypatch):
    monkeypatch.setenv("STUDENT_AUTH_PROVIDER", "local")
    app, db = environment

    @app.post("/multipart-probe")
    async def multipart(file: UploadFile = File(...), user: User = Depends(auth.get_current_user)):
        return {"bytes": len(await file.read()), "user_id": user.id}

    app.add_middleware(RequestLimitMiddleware)
    app.add_middleware(CsrfMiddleware, secret_key=auth._jwt_secret_key,
                       session_cookie=auth.SESSION_COOKIE_NAME, impersonation_cookie=auth.IMPERSONATION_COOKIE_NAME,
                       trusted_origins=[FRONTEND])
    app.add_middleware(CORSMiddleware, allow_origins=[FRONTEND], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    return app, db


def test_login_returns_session_bound_token_and_cookie_bootstrap_does_not_renew_session(protected):
    app, db = protected
    _, _, body = login(app, "student")
    cookie = body["token"]
    assert body["csrf_token"] == create_csrf_token(cookie, auth.JWT_SECRET_KEY)
    version = db.query(User).filter_by(identifier="student").one().session_version
    code, headers, bootstrap = request(app, "GET", "/api/auth/csrf", cookie=cookie, headers={"Origin": FRONTEND})
    assert code == 200
    assert bootstrap == {"csrf_token": body["csrf_token"]}
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in headers
    assert cookie not in str(bootstrap)
    assert db.query(User).filter_by(identifier="student").one().session_version == version


@pytest.mark.parametrize("token", [None, "", "wrong", "f" * 64, "é" * 64])
def test_invalid_csrf_cannot_log_out_a_cookie_session(protected, token):
    app, db = protected
    _, _, body = login(app, "student")
    headers = {"Origin": FRONTEND}
    if token is not None:
        headers["X-CSRF-Token"] = token
    code, response_headers, error = request(app, "POST", "/api/auth/logout", cookie=body["token"], headers=headers)
    assert code == 403
    assert error["error_code"] == "csrf_token_invalid"
    assert response_headers["cache-control"] == "no-store"
    assert response_headers["access-control-allow-origin"] == FRONTEND
    assert request(app, "GET", "/api/auth/me", cookie=body["token"])[0] == 200


@pytest.mark.parametrize("origin", [FRONTEND, None])
def test_valid_csrf_allows_cookie_logout_including_proxy_without_origin(protected, origin):
    app, _ = protected
    _, _, body = login(app, "student")
    headers = {"X-CSRF-Token": body["csrf_token"]}
    if origin:
        headers["Origin"] = origin
    assert request(app, "POST", "/api/auth/logout", cookie=body["token"], headers=headers)[0] == 200
    assert request(app, "GET", "/api/auth/me", cookie=body["token"])[0] == 401


@pytest.mark.parametrize("origin", ["https://evil.example", "null", FRONTEND + ".evil.test", FRONTEND + "/path", "https://frontend.example.test:8443", "https://frontend.example.test:0"])
def test_foreign_origin_cannot_write_or_bootstrap_even_with_a_valid_token(protected, origin):
    app, _ = protected
    _, _, body = login(app, "student")
    headers = {"Origin": origin, "X-CSRF-Token": body["csrf_token"]}
    for method, path in [("POST", "/api/auth/logout"), ("GET", "/api/auth/csrf")]:
        code, _, error = request(app, method, path, cookie=body["token"], headers=headers)
        assert code == 403
        assert error["error_code"] == "csrf_origin_rejected"
    assert request(app, "GET", "/api/auth/me", cookie=body["token"])[0] == 200


def test_bearer_only_clients_work_but_bearer_header_cannot_bypass_cookie_protection(protected):
    app, _ = protected
    _, _, body = login(app, "student")
    assert request(app, "POST", "/api/auth/logout", cookie=body["token"], authorization=f"Bearer {body['token']}")[0] == 403
    assert request(app, "POST", "/api/auth/logout", authorization=f"Bearer {body['token']}")[0] == 200


def test_token_from_another_account_or_older_login_cannot_authorize_cookie_actions(protected):
    app, _ = protected
    _, _, old = login(app, "student")
    _, _, admin = login(app, "admin")
    _, _, current = login(app, "student")
    for token in [old["csrf_token"], admin["csrf_token"]]:
        assert request(app, "POST", "/api/auth/logout", cookie=current["token"],
                       headers={"X-CSRF-Token": token})[0] == 403
    assert request(app, "GET", "/api/auth/me", cookie=current["token"])[0] == 200


@pytest.mark.parametrize("duplicate", ["origin", "x-csrf-token"])
def test_duplicate_security_headers_are_rejected(protected, duplicate):
    app, _ = protected
    _, _, body = login(app, "student")

    async def duplicated(scope, receive, send):
        scope = dict(scope)
        scope["headers"] = [*scope["headers"], (duplicate.encode(), (FRONTEND if duplicate == "origin" else body["csrf_token"]).encode())]
        await app(scope, receive, send)

    assert request(duplicated, "POST", "/api/auth/logout", cookie=body["token"],
                   headers={"Origin": FRONTEND, "X-CSRF-Token": body["csrf_token"]})[0] == 403
    assert request(app, "GET", "/api/auth/me", cookie=body["token"])[0] == 200


def test_login_replaces_stale_cookies_but_rejects_foreign_origin(protected):
    app, _ = protected
    body = {"identifier": "student", "password": "password123"}
    assert request(app, "POST", "/api/auth/login", cookie="stale-cookie", body=body, headers={"Origin": FRONTEND})[0] == 200
    code, _, response = request(app, "POST", "/api/auth/login", body=body, headers={"Origin": "https://evil.example"})
    assert code == 403
    assert "token" not in response


def test_csrf_bootstrap_does_not_authenticate_an_invalid_cookie(protected):
    app, _ = protected
    assert request(app, "GET", "/api/auth/csrf")[2] == {"csrf_token": None}
    assert request(app, "GET", "/api/auth/csrf", cookie="not-a-jwt")[2]["csrf_token"]
    assert request(app, "GET", "/api/auth/me", cookie="not-a-jwt")[0] == 401


def test_multipart_upload_needs_csrf_and_trusted_preflight_is_allowed(protected):
    app, _ = protected
    _, _, body = login(app, "student")
    payload = b'--test-boundary\r\nContent-Disposition: form-data; name="file"; filename="probe.pdf"\r\nContent-Type: application/pdf\r\n\r\nprobe\r\n--test-boundary--\r\n'
    headers = {"Content-Type": "multipart/form-data; boundary=test-boundary", "Origin": FRONTEND}
    assert request(app, "POST", "/multipart-probe", cookie=body["token"], headers=headers, raw_body=payload)[0] == 403
    headers["X-CSRF-Token"] = body["csrf_token"]
    code, _, result = request(app, "POST", "/multipart-probe", cookie=body["token"], headers=headers, raw_body=payload)
    assert code == 200
    assert result["bytes"] == 5
    code, headers, _ = request(app, "OPTIONS", "/multipart-probe", headers={
        "Origin": FRONTEND, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-csrf-token,content-type",
    })
    assert code == 200
    assert headers["access-control-allow-credentials"] == "true"


def test_trusted_origin_configuration_is_exact_and_has_no_wildcards(monkeypatch):
    monkeypatch.setenv("CSRF_TRUSTED_ORIGINS", "https://EXTRA.example.test:443")
    assert trusted_csrf_origins([FRONTEND]) == ("https://extra.example.test", FRONTEND)
    for value in ["*", "https://*.example.test", "null", "https://user:password@example.test", "https://example.test/path", "https://example.test:0"]:
        monkeypatch.setenv("CSRF_TRUSTED_ORIGINS", value)
        with pytest.raises(RuntimeError, match="origin HTTP"):
            trusted_csrf_origins([])
