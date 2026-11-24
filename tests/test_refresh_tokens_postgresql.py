"""Real PostgreSQL serialization and additive schema migration for remember-me."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from app.database import migrations
from app.models.schemas import AuthRefreshSession, AuthRefreshToken, Document, PlagiarismCheck, User, utc_now
from app.routes import auth
from app.services import refresh_token_service as tokens
from test_auth_session import TEST_JWT_SECRET
from test_repository_import_postgresql import postgres


def create_grant(engine):
    with sessionmaker(bind=engine)() as db:
        user = User(identifier="remember-student", password="synthetic-unused-hash", role="mahasiswa", session_version=7)
        db.add(user)
        db.flush()
        session, raw = tokens.issue_session(db, user, session_mode=None, sads_courses_verified=False)
        db.commit()
        return user.id, session.id, raw, session.expires_at


def test_postgresql_concurrent_reuse_revokes_the_winning_descendant(postgres):
    user_id, session_id, raw, deadline = create_grant(postgres)
    barrier = Barrier(2)

    def rotate():
        with sessionmaker(bind=postgres)() as db:
            barrier.wait(timeout=10)
            try:
                grant = tokens.resolve_grant(db, raw)
                next_token = tokens.rotate_grant(db, grant)
                db.commit()
                return "rotated", next_token
            except tokens.RefreshTokenError as error:
                db.rollback()
                return "error", error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(rotate) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(result[0] for result in results) == ["error", "rotated"]
    assert next(result[1] for result in results if result[0] == "error") == "refresh_token_reused"
    descendant = next(result[1] for result in results if result[0] == "rotated")
    with sessionmaker(bind=postgres)() as db:
        assert db.get(User, user_id).session_version == 8
        session = db.get(AuthRefreshSession, session_id)
        assert session.revocation_reason == "token_reused" and session.expires_at == deadline
        with pytest.raises(tokens.RefreshTokenError):
            tokens.resolve_grant(db, descendant)


def test_postgresql_logout_racing_rotation_cannot_leave_a_live_descendant(postgres):
    user_id, session_id, raw, _ = create_grant(postgres)
    barrier = Barrier(2)

    def rotate():
        with sessionmaker(bind=postgres)() as db:
            barrier.wait(timeout=10)
            try:
                grant = tokens.resolve_grant(db, raw)
                descendant = tokens.rotate_grant(db, grant)
                db.commit()
                return descendant
            except tokens.RefreshTokenError:
                return None

    def logout():
        with sessionmaker(bind=postgres)() as db:
            barrier.wait(timeout=10)
            user = db.query(User).filter(User.id == user_id).with_for_update().one()
            user.session_version += 1
            tokens.revoke_user_sessions(db, user.id, "logout")
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        rotating, revoking = pool.submit(rotate), pool.submit(logout)
        descendant = rotating.result(timeout=20)
        revoking.result(timeout=20)
    with sessionmaker(bind=postgres)() as db:
        assert db.get(User, user_id).session_version == 8
        assert db.get(AuthRefreshSession, session_id).revoked_at is not None
        with pytest.raises(tokens.RefreshTokenError):
            tokens.resolve_grant(db, descendant or raw)


def test_postgresql_competing_admin_mode_switches_only_replace_the_session_once(postgres, monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET_KEY", TEST_JWT_SECRET)
    factory = sessionmaker(bind=postgres)
    with factory() as db:
        user = User(identifier="cihuy", password="unused", role="admin", session_version=7)
        db.add(user)
        db.flush()
        remembered, _ = tokens.issue_session(db, user, session_mode="mahasiswa", sads_courses_verified=False)
        token = auth.create_access_token(user, session_mode="mahasiswa", refresh_session_id=remembered.id)
        db.commit()
        user_id = user.id

    barrier = Barrier(2)

    def switch():
        with factory() as db:
            request = Request({"type": "http", "headers": []})
            current = auth.get_current_user(authorization="Bearer " + token, plagiarism_session=None,
                                            http_request=request, db=db)
            barrier.wait(timeout=10)
            try:
                result = auth.select_login_mode(auth.LoginModeRequest(login_as="admin"), request, Response(), current, db)
                assert result["user"]["role"] == "admin"
                return 200
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(switch), executor.submit(switch)
        assert sorted([first.result(timeout=20), second.result(timeout=20)]) == [200, 401]
    with factory() as db:
        assert db.get(User, user_id).session_version == 8
        assert db.query(AuthRefreshSession).filter_by(user_id=user_id, revoked_at=None).one().session_mode == "admin"


def test_runtime_migration_adds_refresh_tables_and_preserves_existing_history(postgres, monkeypatch):
    with sessionmaker(bind=postgres)() as db:
        user = User(identifier="historical-student", password="unchanged-hash", role="mahasiswa")
        db.add(user)
        db.flush()
        document = Document(title="synthetic-historical.pdf", file_path="synthetic.pdf", user_id=user.id)
        db.add(document)
        db.flush()
        check = PlagiarismCheck(document_id=document.id, user_id=user.id, overall_similarity=.31, status="completed")
        db.add(check)
        db.commit()
        identities = user.id, document.id, check.id
    with postgres.begin() as conn:
        conn.execute(text("DROP TABLE auth_refresh_tokens"))
        conn.execute(text("DROP TABLE auth_refresh_sessions"))
    monkeypatch.setattr(migrations, "engine", postgres)
    migrations.ensure_runtime_columns()
    migrations.ensure_runtime_columns()
    assert {"auth_refresh_sessions", "auth_refresh_tokens"} <= set(inspect(postgres).get_table_names())
    with sessionmaker(bind=postgres)() as db:
        assert db.get(User, identities[0]).password == "unchanged-hash"
        assert db.get(Document, identities[1]).title == "synthetic-historical.pdf"
        assert db.get(PlagiarismCheck, identities[2]).overall_similarity == .31


def test_runtime_migration_caps_existing_logins_from_the_first_grant(postgres, monkeypatch):
    now = utc_now()
    first_login_at = now - timedelta(days=4)
    later_login_at = now - timedelta(days=1)
    old_deadline = first_login_at + timedelta(days=30)
    shorter_deadline = now + timedelta(days=1)
    with sessionmaker(bind=postgres)() as db:
        tester = User(identifier="legacy-tester", password="unused-hash", role="admin")
        other = User(identifier="legacy-student", password="unused-hash", role="mahasiswa")
        db.add_all([tester, other])
        db.flush()
        legacy_grants = [
            (tester, "admin", first_login_at, old_deadline, first_login_at + timedelta(days=3)),
            (tester, "mahasiswa", later_login_at, old_deadline, first_login_at + timedelta(days=3)),
            (other, None, later_login_at, old_deadline, later_login_at + timedelta(days=3)),
            (tester, "admin", later_login_at, shorter_deadline, shorter_deadline),
        ]
        expected = {}
        for user, mode, created_at, previous_expiry, capped_expiry in legacy_grants:
            session, _ = tokens.issue_session(db, user, session_mode=mode, sads_courses_verified=False)
            session.created_at = created_at
            session.expires_at = previous_expiry
            if created_at == first_login_at:
                session.revoked_at = later_login_at
                session.revocation_reason = "session_changed"
            expected[session.id] = capped_expiry
        db.commit()

    monkeypatch.setattr(migrations, "engine", postgres)
    migrations.ensure_runtime_columns()
    migrations.ensure_runtime_columns()

    with sessionmaker(bind=postgres)() as db:
        assert {session.id: session.expires_at for session in db.query(AuthRefreshSession)} == expected
        assert db.query(AuthRefreshToken).count() == len(expected)


def test_postgresql_expiry_cleanup_and_user_deletion_cascade_token_history(postgres):
    user_id, session_id, raw, _ = create_grant(postgres)
    with sessionmaker(bind=postgres)() as db:
        grant = tokens.resolve_grant(db, raw)
        tokens.rotate_grant(db, grant)
        db.commit()
        assert db.query(AuthRefreshToken).count() == 2
        grant.session.expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
        db.query(AuthRefreshSession).filter_by(id=session_id).with_for_update().one()
        with sessionmaker(bind=postgres)() as cleanup:
            cleanup.execute(text("SET LOCAL lock_timeout = '1s'"))
            tokens.purge_expired_sessions(cleanup)
            assert cleanup.query(AuthRefreshSession).count() == 1
        db.rollback()
        tokens.purge_expired_sessions(db)
        assert db.query(AuthRefreshToken).count() == 0
        assert db.query(AuthRefreshSession).count() == 0
        user = db.get(User, user_id)
        tokens.issue_session(db, user, session_mode=None, sads_courses_verified=False)
        db.commit()
        db.delete(user)
        db.commit()
        assert db.query(AuthRefreshToken).count() == 0
        assert db.query(AuthRefreshSession).count() == 0
