import asyncio
import json
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import session as database_session
from app.database.session import Base, get_db
from app.models.schemas import User
from app.routes import auth


TEST_JWT_SECRET = "auth-session-test-secret-that-is-long-enough-1234567890"


def request(app, method, path, *, authorization=None, cookie=None, body=None):
    body_bytes = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if cookie is not None:
        headers.append((b"cookie", f"{auth.SESSION_COOKIE_NAME}={cookie}".encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("test", 1),
        "server": ("test", 443),
    }
    events = []
    sent_request = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(event):
        events.append(event)

    asyncio.run(app(scope, receive, send))
    start = next(event for event in events if event["type"] == "http.response.start")
    content = b"".join(event.get("body", b"") for event in events if event["type"] == "http.response.body")
    response_headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    return start["status"], response_headers, json.loads(content)


def token_from_set_cookie(set_cookie: str) -> str:
    parsed = SimpleCookie()
    parsed.load(set_cookie)
    return parsed[auth.SESSION_COOKIE_NAME].value


def login(app, identifier: str, password: str = "password123"):
    return request(
        app,
        "POST",
        "/api/auth/login",
        body={"identifier": identifier, "password": password},
    )


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET_KEY", TEST_JWT_SECRET)
    monkeypatch.setattr(auth, "JWT_EXPIRE_MINUTES", 60)
    monkeypatch.setattr(auth, "SESSION_COOKIE_SECURE", True)
    monkeypatch.setattr(auth, "SESSION_COOKIE_SAMESITE", "none")
    monkeypatch.setattr(auth, "SESSION_COOKIE_DOMAIN", ".example.test")
    monkeypatch.setattr(auth, "SESSION_COOKIE_PATH", "/api")

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add_all([
        User(identifier="student", password=auth.password_hash.hash("password123"), role="mahasiswa"),
        User(identifier="lecturer", password=auth.password_hash.hash("password123"), role="dosen"),
    ])
    db.commit()

    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    yield app, db
    db.close()
    engine.dispose()


def test_login_cookie_only_me_logout_and_no_store(environment):
    app, _ = environment

    code, login_headers, login_body = login(app, "student")
    assert code == 200
    assert login_headers["cache-control"] == "no-store"
    assert login_headers["pragma"] == "no-cache"
    assert login_body["user"]["identifier"] == "student"
    assert "httponly" in login_headers["set-cookie"].lower()
    assert "secure" in login_headers["set-cookie"].lower()
    assert "samesite=none" in login_headers["set-cookie"].lower()
    assert "domain=.example.test" in login_headers["set-cookie"].lower()
    assert "path=/api" in login_headers["set-cookie"].lower()
    cookie_token = token_from_set_cookie(login_headers["set-cookie"])

    code, me_headers, me_body = request(app, "GET", "/api/auth/me", cookie=cookie_token)
    assert code == 200
    assert me_headers["cache-control"] == "no-store"
    assert me_body["identifier"] == "student"

    code, logout_headers, _ = request(app, "POST", "/api/auth/logout", cookie=cookie_token)
    assert code == 200
    assert logout_headers["cache-control"] == "no-store"
    assert "max-age=0" in logout_headers["set-cookie"].lower()
    assert "httponly" in logout_headers["set-cookie"].lower()
    assert "secure" in logout_headers["set-cookie"].lower()
    assert "samesite=none" in logout_headers["set-cookie"].lower()
    assert "domain=.example.test" in logout_headers["set-cookie"].lower()
    assert "path=/api" in logout_headers["set-cookie"].lower()

    # The token used to log out cannot authenticate again, whether sent as a
    # cookie or as an Authorization header.
    assert request(app, "GET", "/api/auth/me", cookie=cookie_token)[0] == 401
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {cookie_token}")[0] == 401


def test_bearer_and_cookie_from_different_accounts_are_rejected(environment):
    app, _ = environment
    _, student_headers, student_login = login(app, "student")
    _, lecturer_headers, _ = login(app, "lecturer")

    code, headers, body = request(
        app,
        "GET",
        "/api/auth/me",
        authorization=f"Bearer {student_login['token']}",
        cookie=token_from_set_cookie(lecturer_headers["set-cookie"]),
    )

    assert code == 401
    assert headers["cache-control"] == "no-store"
    assert "akun yang berbeda" in body["detail"]
    # The first login's cookie is deliberately not used as an implicit fallback.
    assert student_headers["set-cookie"]


def test_invalid_bearer_does_not_fall_back_to_valid_cookie(environment):
    app, _ = environment
    _, login_headers, _ = login(app, "student")
    valid_cookie = token_from_set_cookie(login_headers["set-cookie"])

    code, headers, body = request(
        app,
        "GET",
        "/api/auth/me",
        authorization="Bearer definitely-not-a-valid-jwt",
        cookie=valid_cookie,
    )

    assert code == 401
    assert headers["cache-control"] == "no-store"
    assert body["detail"] == "Token autentikasi tidak valid atau telah kedaluwarsa."


def test_new_login_invalidates_previous_token_server_side(environment):
    app, _ = environment
    _, first_headers, first_login = login(app, "student")
    _, second_headers, second_login = login(app, "student")

    first_token = first_login["token"]
    second_token = second_login["token"]
    assert first_token != second_token
    assert token_from_set_cookie(first_headers["set-cookie"]) == first_token
    assert token_from_set_cookie(second_headers["set-cookie"]) == second_token

    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {first_token}")[0] == 401
    assert request(app, "GET", "/api/auth/me", cookie=first_token)[0] == 401
    assert request(app, "GET", "/api/auth/me", authorization=f"Bearer {second_token}")[0] == 200


def test_missing_or_placeholder_jwt_secret_fails_closed(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET_KEY", "change-this-secret")
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        auth.validate_auth_configuration()


def test_database_url_requires_explicit_configuration(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        database_session.get_database_url()
