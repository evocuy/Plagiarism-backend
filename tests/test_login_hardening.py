import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.session import Base
from app.models.schemas import Dosen, LoginThrottle, User
from app.routes import auth
from app.services.login_throttle_service import LoginThrottleService, LoginThrottleSettings


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def make_request(peer="198.51.100.10", **headers):
    return Request({
        "type": "http", "method": "POST", "scheme": "https", "path": "/api/auth/login",
        "query_string": b"", "client": (peer, 12345), "server": ("test", 443),
        "headers": [(key.replace("_", "-").encode(), value.encode()) for key, value in headers.items()],
    })


def test_account_limit_survives_service_recreation_and_expires(db):
    current = [datetime(2026, 10, 29, 1, 0, 0)]
    settings = LoginThrottleSettings(account_max_failures=2, ip_max_attempts=30, window_seconds=900)
    service = LoginThrottleService(settings, now_provider=lambda: current[0])
    assert service.verify(db, "student", "198.51.100.1", lambda: False) is False
    assert service.verify(db, "student", "198.51.100.2", lambda: False) is False
    restarted = LoginThrottleService(settings, now_provider=lambda: current[0])
    with pytest.raises(HTTPException) as error:
        restarted.verify(db, "student", "198.51.100.3", lambda: pytest.fail("Locked account must not verify"))
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "900"
    current[0] += timedelta(seconds=901)
    assert restarted.verify(db, "student", "198.51.100.3", lambda: True) is True


def test_ip_budget_counts_success_and_password_spraying_across_accounts(db):
    service = LoginThrottleService(LoginThrottleSettings(account_max_failures=5, ip_max_attempts=2))
    assert service.verify(db, "student-a", "198.51.100.1", lambda: True)
    assert not service.verify(db, "student-b", "198.51.100.1", lambda: False)
    with pytest.raises(HTTPException) as error:
        service.verify(db, "student-c", "198.51.100.1", lambda: pytest.fail("IP budget must be enforced"))
    assert error.value.status_code == 429
    assert int(error.value.headers["Retry-After"]) > 0


def test_success_clears_only_account_failures_and_keys_do_not_store_identifiers(db):
    service = LoginThrottleService(LoginThrottleSettings(account_max_failures=2, ip_max_attempts=10))
    service.verify(db, "student@example.test", "198.51.100.1", lambda: False)
    service.verify(db, "student@example.test", "198.51.100.1", lambda: True)
    service.verify(db, "student@example.test", "198.51.100.1", lambda: False)
    buckets = db.query(LoginThrottle).all()
    assert sorted(bucket.attempt_count for bucket in buckets) == [1, 3]
    assert all("student" not in bucket.key and "198.51.100.1" not in bucket.key for bucket in buckets)
    assert len(buckets) == 2


def test_database_failure_denies_login_without_exposing_internal_error(db, monkeypatch):
    service = LoginThrottleService()

    def broken_query(*_args, **_kwargs):
        raise RuntimeError("internal database detail must not escape")

    monkeypatch.setattr(db, "query", broken_query)
    with pytest.raises(HTTPException) as error:
        service.verify(db, "student", "198.51.100.1", lambda: pytest.fail("Database failure must deny login"))
    assert error.value.status_code == 503
    assert "internal database" not in error.value.detail


def test_untrusted_peer_cannot_spoof_client_ip():
    service = LoginThrottleService(LoginThrottleSettings(trusted_proxy_cidrs=("172.18.0.1/32",)))
    request = make_request(cf_connecting_ip="203.0.113.5", x_forwarded_for="203.0.113.6")
    assert service.client_ip(request) == "198.51.100.10"


def test_explicit_tunnel_peer_can_supply_cloudflare_client_ip():
    service = LoginThrottleService(LoginThrottleSettings(trusted_proxy_cidrs=("172.18.0.1/32",)))
    request = make_request("172.18.0.1", cf_connecting_ip="203.0.113.5", x_forwarded_for="203.0.113.6")
    assert service.client_ip(request) == "203.0.113.5"


def test_forwarded_chain_stops_at_first_untrusted_peer():
    service = LoginThrottleService(LoginThrottleSettings(trusted_proxy_cidrs=("172.18.0.1/32",)))
    assert service.client_ip(make_request("172.18.0.1", x_forwarded_for="192.0.2.99, 203.0.113.5")) == "203.0.113.5"
    assert service.client_ip(make_request("172.18.0.1", x_forwarded_for="invalid")) == "172.18.0.1"


def test_invalid_trusted_proxy_setting_fails_configuration(monkeypatch):
    monkeypatch.setenv("LOGIN_TRUSTED_PROXY_CIDRS", "trust-anything")
    with pytest.raises(ValueError):
        LoginThrottleSettings.from_environment()


def test_unknown_wrong_inactive_and_plaintext_credentials_share_generic_error(db, monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET_KEY", "login-hardening-test-signing-key-at-least-32-characters")
    db.add_all([
        User(identifier="active", password=auth.password_hash.hash("correct-test-password"), role="mahasiswa"),
        User(identifier="inactive", password=auth.password_hash.hash("correct-test-password"), role="mahasiswa", is_active=False),
        User(identifier="legacy", password="legacy-test-password", role="mahasiswa"),
    ])
    db.commit()
    errors = []
    for identifier, password in (("missing", "wrong"), ("active", "wrong"), ("inactive", "correct-test-password"), ("legacy", "legacy-test-password")):
        with pytest.raises(HTTPException) as error:
            auth.login(auth.LoginRequest(identifier=identifier, password=password), Response(), db, make_request())
        errors.append((error.value.status_code, error.value.detail))
    assert errors == [(401, "Identifier atau kata sandi tidak valid.")] * 4


def test_deactivated_account_cannot_use_previously_issued_token(db, monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET_KEY", "login-hardening-test-signing-key-at-least-32-characters")
    user = User(identifier="retired", password=auth.password_hash.hash("test-password"), role="admin")
    db.add(user)
    db.commit()
    token = auth.create_access_token(user)
    user.is_active = False
    db.commit()
    with pytest.raises(HTTPException) as error:
        auth.get_current_user(authorization=f"Bearer {token}", plagiarism_session=None, db=db)
    assert error.value.status_code == 401


def test_bootstrap_is_opt_in_and_creates_only_cihuy(db, monkeypatch):
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    auth.seed_initial_users(db)
    assert db.query(User).count() == 0
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "bootstrap-test-password")
    auth.seed_initial_users(db)
    users = db.query(User).all()
    assert len(users) == 1
    assert users[0].identifier == "cihuy"
    assert users[0].role == "admin"
    assert auth.password_hash.verify("bootstrap-test-password", users[0].password)


def test_bootstrap_never_changes_existing_cihuy_or_other_users(db, monkeypatch):
    original_hash = auth.password_hash.hash("existing-test-password")
    db.add_all([
        User(identifier="cihuy", password=original_hash, role="admin", session_version=7),
        User(identifier="real-student", password=original_hash, role="mahasiswa"),
    ])
    db.commit()
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "different-bootstrap-test-password")
    auth.seed_initial_users(db)
    admin = db.query(User).filter(User.identifier == "cihuy").one()
    assert admin.password == original_hash
    assert admin.session_version == 7
    assert db.query(User).count() == 2


def test_retired_accounts_hidden_without_deleting_profiles(db):
    retired = User(identifier="retired-dosen", password="unused-hash", role="dosen", is_active=False)
    retired.dosen = Dosen(nama_lengkap="Retired Lecturer")
    active = User(identifier="cihuy", password="unused-hash", role="admin")
    db.add_all([retired, active])
    db.commit()
    assert [user["identifier"] for user in auth.list_users(active, db)] == ["cihuy"]
    assert auth.list_dosen(active, db) == []
    assert db.query(Dosen).count() == 1


@pytest.mark.skipif(not os.getenv("LOGIN_TEST_DATABASE_URL"), reason="isolated PostgreSQL test URL not configured")
def test_postgresql_concurrent_failures_cannot_exceed_account_budget():
    # A dedicated random schema keeps this check independent of application data.
    engine = create_engine(os.environ["LOGIN_TEST_DATABASE_URL"])
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL is required for advisory-lock verification")
    schema = "login_throttle_test_" + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    LoginThrottle.__table__.create(bind=isolated)
    factory = sessionmaker(bind=isolated)
    settings = LoginThrottleSettings(account_max_failures=5, ip_max_attempts=100)
    account = uuid.uuid4().hex

    def attempt(index):
        with factory() as session:
            try:
                LoginThrottleService(settings).verify(session, account, f"198.51.100.{index}", lambda: False)
                return 401
            except HTTPException as error:
                return error.status_code

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            codes = list(executor.map(attempt, range(1, 9)))
        assert codes.count(401) == 5
        assert codes.count(429) == 3
        with factory() as session:
            bucket = session.query(LoginThrottle).filter(LoginThrottle.key.like("account:%")).one()
            assert bucket.attempt_count == 5
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
