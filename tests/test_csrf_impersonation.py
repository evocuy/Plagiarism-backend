"""Returning to admin must remain possible after the overlay JWT expires."""
from datetime import datetime, timezone

import jwt
import pytest

from app.routes import auth
from app.services.csrf_service import CsrfMiddleware
from test_auth_session import request
from test_impersonation import context, overlay_cookies
from test_sads_auth import NIM, environment


@pytest.mark.parametrize("overlay_state", ["valid", "expired", "malformed"])
def test_cookie_impersonation_can_recover_admin_with_csrf_for_effective_overlay(context, overlay_state):
    app = context.app
    app.add_middleware(CsrfMiddleware, secret_key=auth._jwt_secret_key,
                       session_cookie=auth.SESSION_COOKIE_NAME, impersonation_cookie=auth.IMPERSONATION_COOKIE_NAME)
    parent_cookies = {auth.SESSION_COOKIE_NAME: context.admin_token}
    parent_csrf = request(app, "GET", "/api/auth/csrf", cookies=parent_cookies)[2]["csrf_token"]
    code, _, child = request(app, "POST", "/api/auth/impersonation", cookies=parent_cookies,
                             headers={"X-CSRF-Token": parent_csrf}, body={"nim": NIM})
    assert code == 200
    overlay = child["token"]
    if overlay_state == "expired":
        claims = jwt.decode(overlay, auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
        claims["exp"] = int(datetime.now(timezone.utc).timestamp()) - 1
        overlay = jwt.encode(claims, auth.JWT_SECRET_KEY, algorithm=auth.JWT_ALGORITHM)
    elif overlay_state == "malformed":
        overlay = "malformed-overlay"
    cookies = overlay_cookies(context, overlay)
    csrf = request(app, "GET", "/api/auth/csrf", cookies=cookies)[2]["csrf_token"]
    if overlay_state == "valid":
        assert csrf == child["csrf_token"]
    body = {"session_id": child["impersonation"]["session_id"]}
    assert request(app, "POST", "/api/auth/impersonation/stop", cookies=cookies, body=body,
                   headers={"X-CSRF-Token": parent_csrf})[0] == 403
    code, _, restored = request(app, "POST", "/api/auth/impersonation/stop", cookies=cookies, body=body,
                                headers={"X-CSRF-Token": csrf})
    assert code == 200
    assert restored["user"]["role"] == "admin"
    assert restored["token"] == context.admin_token
    assert restored["csrf_token"] == parent_csrf


def test_csrf_token_never_replaces_original_admin_authentication(context):
    context.app.add_middleware(CsrfMiddleware, secret_key=auth._jwt_secret_key,
                              session_cookie=auth.SESSION_COOKIE_NAME, impersonation_cookie=auth.IMPERSONATION_COOKIE_NAME)
    code, _, child = request(context.app, "POST", "/api/auth/impersonation", authorization=f"Bearer {context.admin_token}", body={"nim": NIM})
    assert code == 200
    cookies = {auth.IMPERSONATION_COOKIE_NAME: child["token"]}
    code, _, _ = request(context.app, "POST", "/api/auth/impersonation/stop", cookies=cookies,
                         headers={"X-CSRF-Token": child["csrf_token"]}, body={"session_id": child["impersonation"]["session_id"]})
    assert code == 401
