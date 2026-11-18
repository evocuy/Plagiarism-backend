"""Remembered sessions use opaque cookies, rotating digests, and a fixed deadline."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

import jwt
import pytest

from app.models.schemas import AuthRefreshSession, AuthRefreshToken, User, utc_now
from app.routes import auth
from app.services.csrf_service import CsrfMiddleware
from test_auth_session import environment, request


@pytest.fixture
def context(environment, monkeypatch):
    monkeypatch.setenv("STUDENT_AUTH_PROVIDER", "local")
    monkeypatch.setattr(auth, "REMEMBER_ACCESS_EXPIRE_MINUTES", 15)
    monkeypatch.setattr(auth, "REFRESH_TOKEN_EXPIRE_DAYS", 30)
    app, db = environment
    app.add_middleware(CsrfMiddleware, secret_key=auth._jwt_secret_key,
                       session_cookie=auth.SESSION_COOKIE_NAME, impersonation_cookie=auth.IMPERSONATION_COOKIE_NAME)
    return app, db


def apply_cookies(headers, previous=None):
    cookies = dict(previous or {})
    for header in headers.get("set-cookie-all", []):
        parsed = SimpleCookie()
        parsed.load(header)
        for key, value in parsed.items():
            if value["max-age"] == "0":
                cookies.pop(key, None)
            else:
                cookies[key] = value.value
    return cookies


def login_remember(app, *, identifier="student", remember=True, login_as=None):
    payload = {"identifier": identifier, "password": "password123", "remember_me": remember}
    if login_as is not None:
        payload["login_as"] = login_as
    code, headers, body = request(app, "POST", "/api/auth/login", body=payload)
    assert code == 200, body
    return apply_cookies(headers), body, headers


def refresh(app, cookies, csrf=None, **kwargs):
    if csrf is None:
        csrf = request(app, "GET", "/api/auth/csrf", cookies=cookies)[2].get("refresh_csrf_token")
    return request(app, "POST", "/api/auth/refresh", cookies=cookies,
                   headers={"X-CSRF-Token": csrf} if csrf else {}, **kwargs)


def expired_access(cookies):
    cookies = dict(cookies)
    claims = jwt.decode(cookies[auth.SESSION_COOKIE_NAME], auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    claims["exp"] = int(datetime.now(timezone.utc).timestamp()) - 1
    cookies[auth.SESSION_COOKIE_NAME] = jwt.encode(claims, auth.JWT_SECRET_KEY, algorithm=auth.JWT_ALGORITHM)
    return cookies


def test_remember_issues_short_access_and_persistent_private_refresh_cookie(context):
    app, db = context
    cookies, body, headers = login_remember(app)
    raw = cookies[auth.REFRESH_COOKIE_NAME]
    assert body["remember_me"] is True
    assert 895 <= body["expires_in"] <= 900
    session = db.query(AuthRefreshSession).one()
    assert 2591995 <= (session.expires_at - session.created_at).total_seconds() <= 2592001
    assert db.query(AuthRefreshToken).one().token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in json.dumps(body)
    header = next(h for h in headers["set-cookie-all"] if h.startswith(auth.REFRESH_COOKIE_NAME + "="))
    assert "HttpOnly" in header and "Secure" in header and "SameSite=none" in header
    assert "Path=/api/auth" in header and "Domain=.example.test" in header
    assert headers["cache-control"] == "no-store"
    assert body["refresh_csrf_token"] != body["csrf_token"]


def test_login_without_remember_keeps_existing_duration_and_no_refresh_grant(context):
    app, db = context
    cookies, body, _ = login_remember(app, remember=False)
    assert auth.REFRESH_COOKIE_NAME not in cookies
    assert body["remember_me"] is False and body["refresh_expires_at"] is None
    assert 3595 <= body["expires_in"] <= 3600
    assert db.query(AuthRefreshSession).count() == 0


@pytest.mark.parametrize("missing_access", [False, True])
def test_refresh_works_after_access_expires_or_browser_discards_cookie(context, missing_access):
    app, db = context
    cookies, body, _ = login_remember(app)
    deadline = db.query(AuthRefreshSession).one().expires_at
    cookies = expired_access(cookies)
    if missing_access:
        cookies.pop(auth.SESSION_COOKIE_NAME)
    code, headers, renewed = refresh(app, cookies)
    assert code == 200, renewed
    updated = apply_cookies(headers, cookies)
    assert updated[auth.REFRESH_COOKIE_NAME] != cookies[auth.REFRESH_COOKIE_NAME]
    assert renewed["token"] != body["token"]
    assert renewed["refresh_expires_at"] == body["refresh_expires_at"]
    assert db.query(AuthRefreshSession).one().expires_at == deadline
    assert db.query(AuthRefreshToken).filter(AuthRefreshToken.used_at.isnot(None)).count() == 1
    assert request(app, "GET", "/api/auth/me", cookies=updated)[0] == 200


def test_reuse_revokes_the_entire_current_grant_and_its_access_tokens(context):
    app, db = context
    old, first, _ = login_remember(app)
    code, headers, second = refresh(app, old)
    assert code == 200
    current = apply_cookies(headers, old)
    assert refresh(app, old)[2]["error_code"] == "refresh_token_reused"
    assert db.query(AuthRefreshSession).one().revocation_reason == "token_reused"
    assert refresh(app, current)[0] == 401
    for token in (first["token"], second["token"]):
        assert request(app, "GET", "/api/auth/me", authorization="Bearer " + token)[0] == 401


def test_rapid_refresh_is_throttled_without_consuming_or_clearing_current_token(context):
    app, db = context
    cookies, _, _ = login_remember(app)
    code, headers, _ = refresh(app, cookies)
    assert code == 200
    cookies = apply_cookies(headers, cookies)
    count = db.query(AuthRefreshToken).count()
    code, headers, error = refresh(app, cookies)
    assert code == 429 and error["error_code"] == "refresh_too_frequent"
    assert 1 <= int(headers["retry-after"]) <= 60
    assert "set-cookie" not in headers
    assert db.query(AuthRefreshToken).count() == count
    session = db.query(AuthRefreshSession).one()
    session.last_rotated_at = utc_now() - timedelta(seconds=61)
    db.commit()
    assert refresh(app, cookies)[0] == 200


def test_stale_token_from_an_older_login_does_not_revoke_new_login(context):
    app, db = context
    old, _, _ = login_remember(app)
    current, _, _ = login_remember(app)
    version = db.query(User).filter_by(identifier="student").one().session_version
    assert refresh(app, old)[0] == 401
    assert db.query(User).filter_by(identifier="student").one().session_version == version
    assert refresh(app, current)[0] == 200


def test_remember_deadline_is_absolute_even_if_latest_access_is_not_expired(context):
    app, db = context
    cookies, _, _ = login_remember(app)
    session = db.query(AuthRefreshSession).one()
    session.expires_at = utc_now() - timedelta(seconds=1)
    db.commit()
    assert request(app, "GET", "/api/auth/me", cookies=cookies)[0] == 401
    assert refresh(app, cookies)[2]["error_code"] == "refresh_token_expired"


@pytest.mark.parametrize("access_state", ["valid", "expired", "missing"])
def test_logout_revokes_refresh_and_access_including_after_access_expiry(context, access_state):
    app, db = context
    cookies, body, _ = login_remember(app)
    if access_state == "expired":
        cookies = expired_access(cookies)
    elif access_state == "missing":
        cookies.pop(auth.SESSION_COOKIE_NAME)
    code, headers, _ = request(app, "POST", "/api/auth/logout", cookies=cookies,
                               headers={"X-CSRF-Token": body["refresh_csrf_token"]})
    assert code == 200
    assert apply_cookies(headers, cookies) == {}
    assert db.query(AuthRefreshSession).one().revoked_at is not None
    assert refresh(app, cookies)[0] == 401
    assert request(app, "GET", "/api/auth/me", authorization="Bearer " + body["token"])[0] == 401


def test_refresh_requires_csrf_even_when_only_refresh_cookie_survives(context):
    app, db = context
    cookies, body, _ = login_remember(app)
    cookies.pop(auth.SESSION_COOKIE_NAME)
    for csrf in (None, "bad", body["csrf_token"]):
        code, _, error = request(app, "POST", "/api/auth/refresh", cookies=cookies,
                                 headers={"X-CSRF-Token": csrf} if csrf else {})
        assert code == 403 and error["error_code"] == "csrf_token_invalid"
    assert db.query(AuthRefreshToken).one().used_at is None
    assert refresh(app, cookies)[0] == 200


def test_foreign_origin_cannot_refresh_or_read_refresh_csrf(context):
    app, _ = context
    cookies, body, _ = login_remember(app)
    for method, path in (("POST", "/api/auth/refresh"), ("GET", "/api/auth/csrf")):
        code, _, error = request(app, method, path, cookies=cookies,
            headers={"Origin": "https://untrusted.invalid", "X-CSRF-Token": body["refresh_csrf_token"]})
        assert code == 403 and error["error_code"] == "csrf_origin_rejected"


def test_refresh_refuses_bearer_and_cross_account_cookie_without_consuming_token(context):
    app, db = context
    cookies, body, _ = login_remember(app)
    assert refresh(app, cookies, authorization="Bearer " + body["token"])[0] == 401
    other, _, _ = login_remember(app, identifier="admin", remember=False)
    mixed = {**cookies, auth.SESSION_COOKIE_NAME: other[auth.SESSION_COOKIE_NAME]}
    assert refresh(app, mixed)[0] == 401
    assert db.query(AuthRefreshToken).one().used_at is None
    assert refresh(app, cookies)[0] == 200


@pytest.mark.parametrize("change", ["password", "inactive", "role", "identifier", "provider", "deleted"])
def test_account_changes_cannot_keep_a_remembered_grant(context, change):
    app, db = context
    cookies, _, _ = login_remember(app)
    user = db.query(User).filter_by(identifier="student").one()
    if change == "password":
        _, _, admin = request(app, "POST", "/api/auth/login", body={"identifier": "admin", "password": "password123"})
        assert request(app, "PUT", f"/api/auth/users/{user.id}", authorization="Bearer " + admin["token"],
                       body={"password": "new-local-password"})[0] == 200
    elif change == "deleted":
        db.delete(user)
    else:
        setattr(user, {"inactive": "is_active", "role": "role", "identifier": "identifier", "provider": "auth_provider"}[change],
                {"inactive": False, "role": "admin", "identifier": "new-identifier", "provider": "sads"}[change])
    db.commit()
    assert request(app, "GET", "/api/auth/me", cookies=cookies)[0] == 401
    assert refresh(app, cookies)[0] == 401


def test_mode_switch_preserves_deadline_and_refresh_cannot_upgrade_student_mode(context):
    app, db = context
    db.add(User(identifier="cihuy", password=auth.password_hash.hash("password123"), role="admin"))
    db.commit()
    old, original, _ = login_remember(app, identifier="cihuy")
    code, headers, student = request(app, "POST", "/api/auth/session-mode", cookies=old,
        headers={"X-CSRF-Token": original["csrf_token"]}, body={"login_as": "mahasiswa"})
    assert code == 200, student
    current = apply_cookies(headers, old)
    assert student["refresh_expires_at"] == original["refresh_expires_at"]
    assert refresh(app, old)[0] == 401
    code, headers, renewed = refresh(app, current, body={"login_as": "admin"})
    assert code == 200 and renewed["user"]["role"] == "mahasiswa"
    current = apply_cookies(headers, current)
    assert request(app, "GET", "/api/auth/users", cookies=current)[0] == 403
    assert db.query(User).filter_by(identifier="cihuy").one().role == "admin"


def test_disabling_remember_on_a_new_login_revokes_old_refresh(context):
    app, _ = context
    old, _, _ = login_remember(app)
    current, body, _ = login_remember(app, remember=False)
    assert body["remember_me"] is False
    assert auth.REFRESH_COOKIE_NAME not in current
    assert refresh(app, old)[0] == 401
    assert request(app, "GET", "/api/auth/me", cookies=current)[0] == 200


@pytest.mark.parametrize("value", ["true", "false", 1, None])
def test_remember_requires_a_real_json_boolean(context, value):
    app, db = context
    assert request(app, "POST", "/api/auth/login", body={
        "identifier": "student", "password": "password123", "remember_me": value,
    })[0] == 422
    assert db.query(AuthRefreshSession).count() == 0
