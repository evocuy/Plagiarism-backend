"""Verify support sessions, ownership and parent-credential recovery end to end."""
import io
import json
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import jwt
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models.schemas import Document, ImpersonationSession, Mahasiswa, PlagiarismCheck, User, utc_now
from app.routes import auth, documents, plagiarism
from test_auth_session import request
from test_sads_auth import NIM, PRIVATE_VALUE, environment


@pytest.fixture
def context(environment, monkeypatch):
    app, db, opener = environment
    monkeypatch.setattr(auth, "IMPERSONATION_EXPIRE_MINUTES", 15)
    app.include_router(documents.router, prefix="/api/documents")
    app.include_router(plagiarism.router, prefix="/api/plagiarism")
    admin = User(identifier="admin-support", password="unused-admin-hash", role="admin")
    student = User(identifier=NIM, password="unchanged-student-hash", role="mahasiswa", auth_provider="sads", session_version=7)
    other = User(identifier="2400000002", password="other-student-hash", role="mahasiswa", auth_provider="sads")
    db.add_all([admin, student, other])
    db.flush()
    student.mahasiswa = Mahasiswa(nama_lengkap="Existing Student", program_studi="Existing Program", fakultas="Existing Faculty")
    target_doc = Document(user_id=student.id, title="target.pdf", file_path="target.pdf", document_type="proposal")
    other_doc = Document(user_id=other.id, title="other.pdf", file_path="other.pdf", document_type="proposal")
    db.add_all([target_doc, other_doc])
    db.commit()
    yield SimpleNamespace(
        app=app, db=db, opener=opener, admin=admin, student=student, other=other,
        target_doc=target_doc, other_doc=other_doc,
        admin_token=auth.create_access_token(admin),
        student_token=auth.create_access_token(student, sads_courses_verified=True),
    )


def start(context, nim=NIM, *, token=None, cookies=None):
    return request(
        context.app, "POST", "/api/auth/impersonation",
        authorization=f"Bearer {token or context.admin_token}", cookies=cookies, body={"nim": nim},
    )


def overlay_cookies(context, token):
    return {auth.SESSION_COOKIE_NAME: context.admin_token, auth.IMPERSONATION_COOKIE_NAME: token}


def me(context, token=None, cookies=None):
    return request(context.app, "GET", "/api/auth/me", authorization=f"Bearer {token}" if token else None, cookies=cookies)


def stop(context, result, *, token=None, cookies=None):
    return request(
        context.app, "POST", "/api/auth/impersonation/stop",
        authorization=f"Bearer {token}" if token else None, cookies=cookies,
        body={"session_id": result["impersonation"]["session_id"]},
    )


def test_start_is_a_separate_audited_student_session_without_kicking_the_owner(context):
    original_state = (context.student.password, context.student.auth_provider, context.student.session_version)

    code, headers, body = start(context)

    assert code == 200
    assert headers["cache-control"] == "no-store"
    assert body["user"]["id"] == context.student.id
    assert body["user"]["role"] == "mahasiswa"
    assert body["user"]["nama_lengkap"] == "Existing Student"
    assert body["impersonation"]["actor"]["id"] == context.admin.id
    row = context.db.query(ImpersonationSession).one()
    assert row.actor_user_id == context.admin.id
    assert row.target_user_id == context.student.id
    assert row.actor_session_version == context.admin.session_version
    assert row.ended_at is None
    assert 0 < (row.expires_at - row.created_at).total_seconds() <= 900
    assert PRIVATE_VALUE not in json.dumps(body)
    assert context.student.password not in json.dumps(body)
    claims = jwt.decode(body["token"], auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    assert claims["sub"] == str(context.student.id)
    assert claims["impersonation_id"] == row.id
    assert claims["sads_courses_verified"] is True
    cookie = SimpleCookie()
    cookie.load(headers["set-cookie"])
    assert cookie[auth.IMPERSONATION_COOKIE_NAME].value == body["token"]
    assert cookie[auth.IMPERSONATION_COOKIE_NAME]["httponly"]
    assert cookie[auth.IMPERSONATION_COOKIE_NAME]["secure"]
    assert auth.SESSION_COOKIE_NAME not in cookie
    assert (context.student.password, context.student.auth_provider, context.student.session_version) == original_state
    assert me(context, context.student_token)[0] == 200
    assert me(context, context.admin_token)[2]["role"] == "admin"
    assert me(context, body["token"])[2]["role"] == "mahasiswa"
    assert not context.opener.requests  # No student password or login call.
    assert len(context.opener.courses.requests) == 1


def test_first_time_nim_is_provisioned_from_verified_campus_identity(context):
    nim = "2400000003"

    code, _, body = start(context, nim)

    assert code == 200
    user = context.db.query(User).filter_by(identifier=nim).one()
    assert user.auth_provider == "sads"
    assert user.role == "mahasiswa"
    assert user.mahasiswa.nama_lengkap == "Mahasiswa Uji"
    assert user.mahasiswa.program_studi == "IF-DEMO"
    assert user.session_version == 0
    assert me(context, body["token"])[0] == 200
    assert not context.opener.requests


@pytest.mark.parametrize("name", ["Seminar", "Skripsi", "  SKRIPSI  "])
def test_course_requirement_is_also_enforced_for_admin_impersonation(context, name):
    context.opener.courses.payload["jadwal_matakuliah"] = [{"matkul": name}]
    assert start(context)[0] == 200


@pytest.mark.parametrize("names", [[], ["Seminar Nasional"], ["Pra Skripsi"]])
def test_missing_required_courses_cannot_be_bypassed_by_admin(context, names):
    context.opener.courses.payload["jadwal_matakuliah"] = [{"matkul": name} for name in names]

    code, headers, body = start(context, "2400000099")

    assert code == 403
    assert "Seminar atau Skripsi" in body["detail"]
    assert "token" not in body
    assert "set-cookie" not in headers
    assert context.db.query(User).filter_by(identifier="2400000099").first() is None
    assert context.db.query(ImpersonationSession).count() == 0
    assert me(context, context.admin_token)[0] == 200
    assert me(context, context.student_token)[0] == 200


@pytest.mark.parametrize("kind", ["outage", "http401", "invalid_json", "wrong_nim", "missing_name", "missing_program"])
def test_upstream_failures_do_not_create_accounts_or_disclose_private_data(context, caplog, kind):
    api = context.opener.courses
    expected = 502
    if kind == "outage":
        api.error = URLError(f"private {PRIVATE_VALUE}")
        expected = 503
    elif kind == "http401":
        api.error = HTTPError(f"https://private.invalid/{PRIVATE_VALUE}", 401, PRIVATE_VALUE, {}, io.BytesIO(PRIVATE_VALUE.encode()))
        expected = 503
    elif kind == "invalid_json":
        api.raw = PRIVATE_VALUE.encode()
    elif kind == "wrong_nim":
        api.use_requested_nim = False
    else:
        del api.payload["mahasiswa"]["nama" if kind == "missing_name" else "prodi_kode"]

    code, headers, body = start(context, "2400000099")

    assert code == expected
    assert "set-cookie" not in headers
    assert PRIVATE_VALUE not in json.dumps(body) + caplog.text
    assert context.db.query(User).filter_by(identifier="2400000099").first() is None
    assert context.db.query(ImpersonationSession).count() == 0


@pytest.mark.parametrize("target_kind", ["admin", "inactive", "lecturer"])
def test_admin_and_inactive_or_retired_roles_cannot_be_targets(context, target_kind):
    nim = NIM
    if target_kind == "admin":
        nim = context.admin.identifier
    elif target_kind == "inactive":
        context.student.is_active = False
    else:
        context.student.role = "dosen"
    context.db.commit()

    assert start(context, nim)[0] == 403
    assert not context.opener.courses.requests
    assert context.db.query(ImpersonationSession).count() == 0


def test_existing_local_student_credentials_are_not_relinked_or_reset(context):
    context.student.auth_provider = "local"
    context.db.commit()
    previous = (context.student.password, context.student.auth_provider, context.student.session_version)

    code, _, body = start(context)

    assert code == 200
    assert me(context, body["token"])[0] == 200
    assert (context.student.password, context.student.auth_provider, context.student.session_version) == previous


def test_anonymous_students_and_nested_impersonation_cannot_start_a_support_session(context):
    assert request(context.app, "POST", "/api/auth/impersonation", body={"nim": NIM})[0] == 401
    assert start(context, token=context.student_token)[0] == 403
    _, _, body = start(context)
    assert start(context, token=body["token"])[0] == 403
    assert start(context, cookies=overlay_cookies(context, body["token"]))[0] == 401


def test_overlay_cookie_controls_me_and_ownership_without_inheriting_admin_rights(context):
    _, _, body = start(context)
    cookies = overlay_cookies(context, body["token"])

    assert me(context, cookies=cookies)[2]["id"] == context.student.id
    code, headers, status = request(context.app, "GET", "/api/auth/impersonation", cookies=cookies)
    assert code == 200
    assert headers["cache-control"] == "no-store"
    assert status["active"] is True
    code, _, listed = request(context.app, "GET", "/api/documents", cookies=cookies)
    assert code == 200
    assert [doc["id"] for doc in listed] == [context.target_doc.id]
    assert request(context.app, "GET", "/api/auth/users", cookies=cookies)[0] == 403
    assert request(context.app, "POST", f"/api/plagiarism/check-repository/{context.other_doc.id}", cookies=cookies)[0] == 403
    code, _, result = request(context.app, "POST", f"/api/plagiarism/check-repository/{context.target_doc.id}", cookies=cookies)
    assert code == 202
    check = context.db.get(PlagiarismCheck, result["check_id"])
    assert check.user_id == context.student.id


def test_stop_requires_parent_credential_and_never_promotes_a_student_token(context):
    _, _, body = start(context)

    code, _, denied = stop(context, body, token=body["token"])
    assert code == 403
    assert "token" not in denied
    assert stop(context, body, token=context.student_token)[0] == 403
    assert stop(context, body)[0] == 401
    assert me(context, body["token"])[0] == 200

    code, headers, restored = stop(context, body, token=context.admin_token)
    assert code == 200
    assert restored["token"] == context.admin_token  # Original deadline, no renewal.
    assert restored["user"]["id"] == context.admin.id
    assert restored["impersonation"]["active"] is False
    assert any(auth.IMPERSONATION_COOKIE_NAME in cookie and "Max-Age=0" in cookie for cookie in headers["set-cookie-all"])
    assert me(context, body["token"])[0] == 401
    assert me(context, context.student_token)[0] == 200
    row = context.db.query(ImpersonationSession).one()
    assert row.ended_at is not None
    assert row.end_reason == "stopped"
    assert stop(context, body, token=context.admin_token)[0] == 200


def test_cookie_only_stop_restores_original_admin_without_reading_http_only_tokens(context):
    _, _, body = start(context)

    code, _, restored = stop(context, body, cookies=overlay_cookies(context, body["token"]))

    assert code == 200
    assert restored["token"] == context.admin_token
    assert restored["user"]["role"] == "admin"


def test_another_admin_cannot_stop_or_recover_the_original_actors_session(context):
    _, _, body = start(context)
    other_admin = User(identifier="another-admin", password="unused", role="admin")
    context.db.add(other_admin)
    context.db.commit()

    assert stop(context, body, token=auth.create_access_token(other_admin))[0] == 404
    assert me(context, body["token"])[0] == 200


def test_session_duration_is_capped_by_the_parent_admin_deadline(context):
    parent = auth.create_access_token(context.admin, expires_at=datetime.now(timezone.utc) + timedelta(minutes=3))
    code, _, body = start(context, token=parent)

    assert code == 200
    parent_claims = jwt.decode(parent, auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    child_claims = jwt.decode(body["token"], auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
    assert child_claims["exp"] == parent_claims["exp"]


@pytest.mark.parametrize("kind", ["expired_row", "expired_token", "malformed_token", "ordinary_token"])
def test_invalid_overlay_never_falls_back_to_the_admin_cookie(context, kind):
    _, _, body = start(context)
    child = body["token"]
    if kind == "expired_row":
        row = context.db.query(ImpersonationSession).one()
        row.expires_at = utc_now() - timedelta(seconds=1)
        context.db.commit()
    elif kind == "expired_token":
        claims = jwt.decode(child, auth.JWT_SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
        claims["exp"] = int(datetime.now(timezone.utc).timestamp()) - 1
        child = jwt.encode(claims, auth.JWT_SECRET_KEY, algorithm=auth.JWT_ALGORITHM)
    elif kind == "malformed_token":
        child = "not-a-jwt"
    else:
        child = context.student_token

    assert me(context, cookies=overlay_cookies(context, child))[0] == 401
    # Recovery uses the valid parent cookie and does not trust the overlay.
    assert stop(context, body, cookies=overlay_cookies(context, child))[0] == 200


@pytest.mark.parametrize("kind", ["actor_revoked", "actor_inactive", "actor_demoted", "actor_deleted", "student_revoked", "student_inactive", "student_promoted", "student_deleted"])
def test_account_or_session_changes_revoke_impersonation(context, kind):
    _, _, body = start(context)
    actor = kind.startswith("actor_")
    user = context.admin if actor else context.student
    if kind.endswith("revoked"):
        user.session_version += 1
    elif kind.endswith("inactive"):
        user.is_active = False
    elif kind.endswith("deleted"):
        context.db.delete(user)
    else:
        user.role = "mahasiswa" if actor else "admin"
    context.db.commit()

    assert me(context, body["token"])[0] == 401
    if not actor:
        assert stop(context, body, token=context.admin_token)[0] == 200
    assert context.db.query(ImpersonationSession).one().target_identifier == NIM


def test_logging_out_an_overlay_never_logs_out_the_actual_student(context):
    _, _, body = start(context)

    code, headers, _ = request(context.app, "POST", "/api/auth/logout", authorization=f"Bearer {body['token']}")

    assert code == 200
    assert "Max-Age=0" in headers["set-cookie"]
    assert auth.IMPERSONATION_COOKIE_NAME in headers["set-cookie"]
    assert context.db.query(ImpersonationSession).one().end_reason == "logout"
    assert me(context, body["token"])[0] == 401
    assert me(context, context.student_token)[0] == 200
    assert me(context, context.admin_token)[0] == 200


def test_new_successful_impersonation_replaces_previous_support_session(context):
    _, _, first = start(context)
    assert start(context, "2400000002")[0] == 200

    assert me(context, first["token"])[0] == 401
    previous = context.db.get(ImpersonationSession, first["impersonation"]["session_id"])
    assert previous.end_reason == "replaced"
    assert context.db.query(ImpersonationSession).filter_by(ended_at=None).count() == 1
    assert me(context, context.student_token)[0] == 200


def test_different_support_sessions_for_the_same_student_cannot_mix_credentials(context):
    _, _, first = start(context)
    another = User(identifier="second-admin", password="unused", role="admin")
    context.db.add(another)
    context.db.commit()
    _, _, second = start(context, token=auth.create_access_token(another))

    code, _, _ = me(context, second["token"], cookies=overlay_cookies(context, first["token"]))

    assert code == 401
    assert me(context, first["token"])[0] == 200
    assert me(context, second["token"])[0] == 200


def test_audit_commit_failure_rolls_back_new_account_and_preserves_the_previous_session(context, monkeypatch):
    _, _, first = start(context)

    def fail_commit():
        raise SQLAlchemyError("synthetic storage failure")

    monkeypatch.setattr(context.db, "commit", fail_commit)
    code, headers, _ = start(context, "2400000099")

    assert code == 503
    assert "set-cookie" not in headers
    assert context.db.query(User).filter_by(identifier="2400000099").first() is None
    assert context.db.query(ImpersonationSession).count() == 1
    assert me(context, first["token"])[0] == 200


def test_admin_revocation_during_campus_lookup_prevents_issuing_a_student_token(context, monkeypatch):
    original_open = context.opener.courses.open

    def revoke_before_response(outgoing, *, timeout):
        context.admin.session_version += 1
        context.db.commit()
        return original_open(outgoing, timeout=timeout)

    monkeypatch.setattr(context.opener.courses, "open", revoke_before_response)
    code, headers, body = start(context)

    assert code == 401
    assert "token" not in body
    assert "set-cookie" not in headers
    assert context.db.query(ImpersonationSession).count() == 0


@pytest.mark.parametrize("nim", ["", " ", "x" * 101])
def test_invalid_nim_is_rejected_before_the_campus_api_is_called(context, nim):
    assert start(context, nim)[0] == 422
    assert not context.opener.courses.requests
