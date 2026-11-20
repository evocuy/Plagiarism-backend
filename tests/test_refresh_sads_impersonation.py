"""A remembered login rechecks enrollment and never silently restores admin."""
import json
from datetime import timedelta
from urllib.error import URLError

import pytest

from app.models.schemas import AuthRefreshSession, AuthRefreshToken, ImpersonationSession, utc_now
from app.routes import auth
from app.services.csrf_service import CsrfMiddleware
from test_auth_session import request
from test_impersonation import context as impersonation_context, IMPERSONATION_TEST_PASSWORD
from test_refresh_tokens import apply_cookies, expired_access, refresh
from test_sads_auth import environment, NIM, PASSWORD, PRIVATE_VALUE


def protect(app):
    app.add_middleware(CsrfMiddleware, secret_key=auth._jwt_secret_key,
                       session_cookie=auth.SESSION_COOKIE_NAME, impersonation_cookie=auth.IMPERSONATION_COOKIE_NAME)


def campus_login(app):
    code, headers, body = request(app, "POST", "/api/auth/login", body={
        "identifier": NIM, "password": PASSWORD,
    })
    assert code == 200, body
    return apply_cookies(headers), body


def test_campus_refresh_rechecks_active_courses_without_resending_password(environment):
    app, db, opener = environment
    protect(app)
    cookies, body = campus_login(app)
    assert len(opener.requests) == 1
    assert len(opener.courses.requests) == 1
    code, _, renewed = refresh(app, expired_access(cookies))
    assert code == 200, renewed
    assert len(opener.requests) == 1
    assert len(opener.courses.requests) == 2
    assert renewed["user"]["identifier"] == NIM
    assert PASSWORD not in json.dumps(renewed) and PRIVATE_VALUE not in json.dumps(renewed)


def test_sads_tester_can_switch_and_refresh_mode_until_removed(environment, monkeypatch):
    app, db, opener = environment
    monkeypatch.setenv("SADS_TESTER_NIMS", NIM)
    protect(app)
    cookies, parent = campus_login(app)
    assert parent["user"]["role"] == "admin"

    code, headers, child = request(app, "POST", "/api/auth/session-mode", cookies=cookies,
        headers={"X-CSRF-Token": parent["csrf_token"]}, body={"login_as": "mahasiswa"})
    assert code == 200, child
    assert child["user"]["role"] == "mahasiswa"
    assert request(app, "GET", "/api/auth/me", authorization="Bearer " + parent["token"])[0] == 401
    cookies = apply_cookies(headers, cookies)
    code, _, renewed = refresh(app, expired_access(cookies))
    assert code == 200, renewed
    assert renewed["user"]["role"] == "mahasiswa"
    assert len(opener.requests) == 1
    assert not opener.courses.requests

    monkeypatch.delenv("SADS_TESTER_NIMS")
    assert request(app, "GET", "/api/auth/me", authorization="Bearer " + renewed["token"])[0] == 401


def test_campus_loss_of_eligibility_revokes_refresh_and_previous_access(environment):
    app, db, opener = environment
    protect(app)
    cookies, body = campus_login(app)
    opener.courses.payload["jadwal_matakuliah"] = [{"kdmk": "DEMO-OTHER", "matkul": "Matematika"}]
    code, _, error = refresh(app, cookies)
    assert code == 403 and error["error_code"] == "refresh_access_denied"
    assert db.query(AuthRefreshSession).one().revocation_reason == "courses_ineligible"
    assert request(app, "GET", "/api/auth/me", authorization="Bearer " + body["token"])[0] == 401


def test_temporary_campus_failure_keeps_the_unconsumed_refresh_token(environment):
    app, db, opener = environment
    protect(app)
    cookies, _ = campus_login(app)
    opener.courses.error = URLError("synthetic-private-connection-detail")
    code, headers, error = refresh(app, cookies)
    assert code == 503
    assert "set-cookie" not in headers
    assert "synthetic-private" not in str(error)
    assert db.query(AuthRefreshToken).one().used_at is None
    assert db.query(AuthRefreshSession).one().revoked_at is None
    opener.courses.error = None
    assert refresh(app, cookies)[0] == 200


def remembered_admin(context):
    context.admin.password = auth.password_hash.hash("admin-remember-test-password")
    context.db.commit()
    protect(context.app)
    code, headers, body = request(context.app, "POST", "/api/auth/login", body={
        "identifier": context.admin.identifier, "password": "admin-remember-test-password",
    })
    assert code == 200
    cookies = apply_cookies(headers)
    code, headers, child = request(context.app, "POST", "/api/auth/impersonation", cookies=cookies,
        headers={"X-CSRF-Token": body["csrf_token"]},
        body={"nim": NIM, "impersonation_password": IMPERSONATION_TEST_PASSWORD})
    assert code == 200, child
    return apply_cookies(headers, cookies), body, child


@pytest.mark.parametrize("drop_overlay", [False, True])
def test_admin_refresh_does_not_silently_leave_impersonation(impersonation_context, drop_overlay):
    context = impersonation_context
    cookies, parent, child = remembered_admin(context)
    if drop_overlay:
        cookies.pop(auth.IMPERSONATION_COOKIE_NAME)
    code, headers, error = refresh(context.app, cookies)
    assert code == 403 and "impersonasi" in error["detail"]
    assert "set-cookie" not in headers
    assert context.db.query(AuthRefreshToken).one().used_at is None
    assert context.db.query(AuthRefreshSession).one().impersonation_session_id == child["impersonation"]["session_id"]


@pytest.mark.parametrize("drop_parent", [False, True])
def test_explicit_stop_recovers_original_admin_after_parent_access_expires(impersonation_context, drop_parent):
    context = impersonation_context
    cookies, parent, child = remembered_admin(context)
    deadline = context.db.query(AuthRefreshSession).one().expires_at
    cookies = expired_access(cookies)
    if drop_parent:
        cookies.pop(auth.SESSION_COOKIE_NAME)
    overlay = context.db.query(ImpersonationSession).one()
    overlay.expires_at = utc_now() - timedelta(seconds=1)
    context.db.commit()
    assert request(context.app, "GET", "/api/auth/me", cookies=cookies)[0] == 401
    code, headers, restored = request(context.app, "POST", "/api/auth/impersonation/stop", cookies=cookies,
        headers={"X-CSRF-Token": parent["refresh_csrf_token"]}, body={"session_id": overlay.id})
    assert code == 200, restored
    assert restored["user"]["role"] == "admin"
    assert restored["impersonation"] == {"active": False}
    assert restored["refresh_expires_at"] == parent["refresh_expires_at"]
    assert context.db.query(AuthRefreshSession).one().expires_at == deadline
    assert context.db.query(AuthRefreshSession).one().impersonation_session_id is None
    cookies = apply_cookies(headers, cookies)
    assert auth.IMPERSONATION_COOKIE_NAME not in cookies
    assert request(context.app, "GET", "/api/auth/me", cookies=cookies)[2]["role"] == "admin"
    assert request(context.app, "GET", "/api/auth/me", authorization="Bearer " + child["token"])[0] == 401
    assert refresh(context.app, cookies)[0] == 429  # Recovery just rotated the token.


def test_student_refresh_cannot_be_used_as_original_admin(impersonation_context):
    context = impersonation_context
    cookies, _, child = remembered_admin(context)
    student_cookies, student = campus_login(context.app)
    cookies[auth.REFRESH_COOKIE_NAME] = student_cookies[auth.REFRESH_COOKIE_NAME]
    cookies.pop(auth.SESSION_COOKIE_NAME)
    code, _, _ = request(context.app, "POST", "/api/auth/impersonation/stop", cookies=cookies,
        headers={"X-CSRF-Token": student["refresh_csrf_token"]},
        body={"session_id": child["impersonation"]["session_id"]})
    assert code == 403


def test_child_logout_ends_overlay_but_preserves_parent_remembered_session(impersonation_context):
    context = impersonation_context
    cookies, parent, child = remembered_admin(context)
    code, headers, _ = request(context.app, "POST", "/api/auth/logout", cookies=cookies,
                               headers={"X-CSRF-Token": child["csrf_token"]})
    assert code == 200
    cookies = apply_cookies(headers, cookies)
    assert auth.REFRESH_COOKIE_NAME in cookies
    assert context.db.query(AuthRefreshSession).one().revoked_at is None
    assert context.db.query(AuthRefreshSession).one().impersonation_session_id is None
    assert refresh(context.app, cookies)[0] == 200
