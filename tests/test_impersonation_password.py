"""The shared impersonation password gates admin actions, without changing login."""
import json

import pytest

from app.models.schemas import ImpersonationSession, LoginThrottle, User
from app.routes import auth
from app.services.login_throttle_service import LoginThrottleService
from test_auth_session import request
from test_impersonation import IMPERSONATION_TEST_PASSWORD, context, me, overlay_cookies, start, stop
from test_sads_auth import NIM, environment


@pytest.mark.parametrize("password", ["incorrect-password", IMPERSONATION_TEST_PASSWORD.upper(),
                                      " " + IMPERSONATION_TEST_PASSWORD, "sandi-berbeda-🔒"])
def test_wrong_password_cannot_call_sads_or_issue_a_session(context, caplog, password):
    user_count = context.db.query(User).count()
    code, headers, body = start(context, nim="2400000099", password=password)
    assert code == 403
    assert body["detail"] == "Password impersonasi tidak valid."
    assert headers["cache-control"] == "no-store"
    assert "set-cookie" not in headers and "token" not in body
    assert password not in json.dumps(body) + caplog.text
    assert not context.opener.courses.requests
    assert context.db.query(User).count() == user_count
    assert context.db.query(ImpersonationSession).count() == 0
    assert me(context, context.admin_token)[0] == 200
    assert me(context, context.student_token)[0] == 200


@pytest.mark.parametrize("password", [None, "", "synthetic-private-value" * 20])
def test_password_is_required_and_bounded_without_echoing_it(context, password):
    code, _, body = start(context, password=password)
    assert code == 422
    assert all("input" not in error and "ctx" not in error for error in body["detail"])
    if password:
        assert password not in json.dumps(body)
    assert not context.opener.courses.requests
    assert context.db.query(ImpersonationSession).count() == 0


@pytest.mark.parametrize("configured_hash", [None, "", "malformed-synthetic-hash"])
def test_missing_or_invalid_configuration_disables_only_impersonation(context, monkeypatch, configured_hash):
    if configured_hash is None:
        monkeypatch.delenv("IMPERSONATION_PASSWORD_HASH", raising=False)
    else:
        monkeypatch.setenv("IMPERSONATION_PASSWORD_HASH", configured_hash)
    code, headers, body = start(context)
    assert code == 503
    assert headers["cache-control"] == "no-store"
    assert "token" not in body and "set-cookie" not in headers
    assert IMPERSONATION_TEST_PASSWORD not in json.dumps(body)
    assert not context.opener.courses.requests
    assert context.db.query(ImpersonationSession).count() == 0
    assert me(context, context.admin_token)[2]["role"] == "admin"


def test_failed_password_does_not_replace_an_existing_session(context):
    _, _, child = start(context)
    session_id = child["impersonation"]["session_id"]
    assert start(context, nim="2400000099", password="incorrect-password")[0] == 403
    assert context.db.get(ImpersonationSession, session_id).ended_at is None
    assert context.db.query(ImpersonationSession).count() == 1
    assert len(context.opener.courses.requests) == 1
    assert me(context, child["token"])[0] == 200


def test_wrong_password_attempts_are_limited_separately_from_login(context, monkeypatch):
    monkeypatch.setenv("LOGIN_ACCOUNT_MAX_FAILURES", "2")
    for _ in range(2):
        assert start(context, password="incorrect-password")[0] == 403
    code, headers, body = start(context)
    assert code == 429
    assert int(headers["retry-after"]) > 0
    assert "token" not in body
    assert not context.opener.courses.requests
    assert LoginThrottleService().verify(context.db, str(context.admin.id), "unknown", lambda: True)
    assert me(context, context.admin_token)[0] == 200
    keys = [row.key for row in context.db.query(LoginThrottle).all()]
    assert {key.split(":")[0] for key in keys} == {"imp_account", "imp_ip", "account", "ip"}
    assert all(len(key) <= 80 for key in keys)


def test_password_does_not_replace_admin_authentication(context):
    body = {"nim": NIM, "impersonation_password": IMPERSONATION_TEST_PASSWORD}
    assert request(context.app, "POST", "/api/auth/impersonation", body=body)[0] == 401
    assert start(context, token=context.student_token)[0] == 403
    assert not context.opener.courses.requests


def test_normal_login_lockout_does_not_block_impersonation(context):
    throttle = LoginThrottleService()
    for _ in range(throttle.settings.account_max_failures):
        assert not throttle.verify(context.db, str(context.admin.id), "unknown", lambda: False)
    assert start(context)[0] == 200


def test_returning_to_admin_does_not_require_the_shared_password(context, monkeypatch):
    _, _, child = start(context)
    monkeypatch.delenv("IMPERSONATION_PASSWORD_HASH")
    code, _, restored = stop(context, child, cookies=overlay_cookies(context, child["token"]))
    assert code == 200
    assert restored["user"]["role"] == "admin"
    assert me(context, child["token"])[0] == 401


def test_password_is_masked_in_request_representation():
    model = auth.ImpersonationRequest(nim=NIM, impersonation_password=IMPERSONATION_TEST_PASSWORD)
    assert IMPERSONATION_TEST_PASSWORD not in repr(model)
