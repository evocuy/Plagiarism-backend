import asyncio
import json

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.session import Base, get_db
from app.models.schemas import Document, User
from app.routes import auth, documents


TEST_JWT_SECRET = "document-list-route-test-secret-at-least-32-chars"


def request(app, path, *, query_string=b"", token=None):
    headers = []
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
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
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(event):
        events.append(event)

    asyncio.run(app(scope, receive, send))
    start = next(event for event in events if event["type"] == "http.response.start")
    content = b"".join(event.get("body", b"") for event in events if event["type"] == "http.response.body")
    response_headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    return start["status"], response_headers, json.loads(content)


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET_KEY", TEST_JWT_SECRET)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    admin = User(identifier="admin", password="hash", role="admin")
    db.add_all([
        admin,
        Document(
            title="published.pdf",
            document_type="skripsi",
            file_path="published.pdf",
            repository_status="published",
        ),
        Document(
            title="draft.pdf",
            document_type="skripsi",
            file_path="draft.pdf",
            repository_status="draft",
        ),
    ])
    db.commit()

    app = FastAPI()
    app.include_router(documents.router, prefix="/api/documents")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    yield app, auth.create_access_token(admin)
    db.close()
    engine.dispose()


def test_document_list_supports_both_url_forms_without_redirect(environment):
    app, token = environment
    results = []

    for path in ("/api/documents", "/api/documents/"):
        status, headers, body = request(
            app,
            path,
            query_string=b"repository_only=true",
            token=token,
        )
        assert status == 200
        assert "location" not in headers
        assert [document["title"] for document in body] == ["published.pdf"]
        results.append(body)

    assert results[0] == results[1]


def test_slashless_document_list_keeps_authentication_required(environment):
    app, _ = environment

    status, headers, body = request(app, "/api/documents")

    assert status == 401
    assert "location" not in headers
    assert body["detail"] == "Autentikasi diperlukan. Silakan login terlebih dahulu."
