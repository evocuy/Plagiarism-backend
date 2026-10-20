import asyncio
import inspect
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.session import Base, get_db
from app.models.schemas import Dosen, Mahasiswa, SupportMessage, SupportTicket, User
from app.routes import support
from app.routes.auth import create_access_token, get_current_user, require_admin
from app.services import support_service


def request(app, method, path, *, token=None, body=None):
    """Exercise FastAPI's ASGI HTTP interface without the optional httpx package."""
    body_bytes = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": headers, "client": ("test", 1), "server": ("test", 80),
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
    return start["status"], {key.decode().lower(): value.decode() for key, value in start["headers"]}, json.loads(content)


@pytest.fixture
def environment(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    student = User(identifier="1234", password="hash", role="mahasiswa")
    other = User(identifier="5678", password="hash", role="mahasiswa")
    admin = User(identifier="admin", password="hash", role="admin")
    lecturer = User(identifier="lecturer", password="hash", role="dosen")
    db.add_all([student, other, admin, lecturer])
    db.flush()
    db.add_all([
        Mahasiswa(id=student.id, nama_lengkap="Mahasiswa Pertama", program_studi="TI", fakultas="FT"),
        Mahasiswa(id=other.id, nama_lengkap="Mahasiswa Kedua", program_studi="TI", fakultas="FT"),
    ])
    db.commit()
    tokens = {name: create_access_token(user) for name, user in {
        "student": student, "other": other, "admin": admin, "lecturer": lecturer,
    }.items()}
    clock = [datetime(2026, 10, 19, 1, 0, 0)]  # UTC, intentionally naive like the database
    monkeypatch.setattr(support_service, "utc_now", lambda: clock[0])
    app = FastAPI()
    app.include_router(support.router, prefix="/api/support")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    yield app, db, factory, tokens, clock
    db.close()
    engine.dispose()


def create(app, token, subject="Gagal unggah", message="Tidak bisa mengunggah PDF"):
    return request(app, "POST", "/api/support/tickets", token=token, body={
        "category": "Unggah", "subject": subject, "message": message,
    })


def test_http_create_persists_ticket_and_initial_message_with_utc_times(environment):
    app, db, factory, tokens, _ = environment
    code, _, created = create(app, tokens["student"], "  Gagal unggah  ", "  PDF tidak bisa diunggah  ")
    assert code == 201
    assert created == {
        "id": 1, "ticket_number": "TKT-000001", "category": "Unggah", "subject": "Gagal unggah",
        "status": "new", "owner_name": "Mahasiswa Pertama", "owner_identifier": "1234",
        "created_at": "2026-10-19T01:00:00Z", "updated_at": "2026-10-19T01:00:00Z",
    }
    code, _, result = request(app, "GET", "/api/support/tickets", token=tokens["student"])
    assert code == 200
    assert result["tickets"] == [created]
    assert result["quota"] == {"limit": 3, "remaining": 2, "next_available_at": None, "cooldown_until": "2026-10-19T01:01:00Z"}
    code, _, result = request(app, "GET", "/api/support/tickets/1", token=tokens["student"])
    assert code == 200
    assert result["ticket"]["message"] == "PDF tidak bisa diunggah"
    assert result["messages"] == [{
        "id": 1, "author_name": "Mahasiswa Pertama", "author_role": "mahasiswa",
        "message": "PDF tidak bisa diunggah", "created_at": "2026-10-19T01:00:00Z",
    }]
    # A second session sees both records: a success was not returned before commit.
    with factory() as second:
        assert second.query(SupportTicket).count() == 1
        assert second.query(SupportMessage).count() == 1
    assert request(app, "DELETE", "/api/support/tickets/1", token=tokens["student"])[0] == 405


def test_http_auth_roles_ownership_and_validation(environment):
    app, _, _, tokens, _ = environment
    assert request(app, "GET", "/api/support/tickets")[0] == 401
    assert request(app, "GET", "/api/support/tickets", token=tokens["lecturer"])[0] == 403
    assert create(app, tokens["lecturer"])[0] == 403
    assert create(app, tokens["admin"])[0] == 403
    assert create(app, tokens["student"], subject="   ")[0] == 422
    assert create(app, tokens["student"], message="a" * 4001)[0] == 422
    assert create(app, tokens["student"])[0] == 201
    assert request(app, "GET", "/api/support/tickets/1", token=tokens["other"])[0] == 404
    assert request(app, "POST", "/api/support/tickets/1/messages", token=tokens["other"], body={"message": "Halo"})[0] == 404
    assert request(app, "PATCH", "/api/support/tickets/1", token=tokens["student"], body={"status": "resolved"})[0] == 403
    assert request(app, "PATCH", "/api/support/tickets/1", token=tokens["admin"], body={"status": "unknown"})[0] == 422
    assert request(app, "GET", "/api/support/tickets/999", token=tokens["admin"])[0] == 404
    code, _, admin_list = request(app, "GET", "/api/support/tickets", token=tokens["admin"])
    assert code == 200 and len(admin_list["tickets"]) == 1 and admin_list["quota"] is None
    code, _, other_list = request(app, "GET", "/api/support/tickets", token=tokens["other"])
    assert code == 200 and other_list["tickets"] == [] and other_list["quota"]["remaining"] == 3


def test_http_rolling_24_hour_limit_cooldown_and_independent_accounts(environment):
    app, _, _, tokens, clock = environment
    start = clock[0]
    assert create(app, tokens["student"])[0] == 201
    code, headers, data = create(app, tokens["student"])
    assert code == 429 and headers["retry-after"] == "60" and isinstance(data["detail"], str)
    assert create(app, tokens["other"])[0] == 201
    clock[0] = start + timedelta(minutes=1)
    assert create(app, tokens["student"])[0] == 201
    clock[0] = start + timedelta(minutes=2)
    assert create(app, tokens["student"])[0] == 201
    clock[0] = start + timedelta(minutes=10)
    code, headers, data = create(app, tokens["student"])
    assert code == 429 and headers["retry-after"] == str(24 * 60 * 60 - 10 * 60)
    assert "3 tiket" in data["detail"]
    code, _, result = request(app, "GET", "/api/support/tickets", token=tokens["student"])
    assert code == 200 and result["quota"] == {
        "limit": 3, "remaining": 0, "next_available_at": "2026-10-20T01:00:00Z", "cooldown_until": None,
    }
    clock[0] = start + timedelta(hours=24) - timedelta(microseconds=1)
    code, headers, _ = create(app, tokens["student"])
    assert code == 429 and headers["retry-after"] == "1"
    clock[0] = start + timedelta(hours=24)
    assert create(app, tokens["student"])[0] == 201
    code, _, result = request(app, "GET", "/api/support/tickets", token=tokens["student"])
    assert code == 200 and result["quota"]["next_available_at"] == "2026-10-20T01:01:00Z"


def test_http_student_reply_cooldown_is_global_admin_is_unrestricted_and_status_reopens(environment):
    app, _, _, tokens, clock = environment
    start = clock[0]
    assert create(app, tokens["student"])[0] == 201
    clock[0] = start + timedelta(seconds=9)
    code, headers, data = request(app, "POST", "/api/support/tickets/1/messages", token=tokens["student"], body={"message": "Tambahan"})
    assert code == 429 and headers["retry-after"] == "1" and isinstance(data["detail"], str)
    clock[0] = start + timedelta(seconds=10)
    assert request(app, "POST", "/api/support/tickets/1/messages", token=tokens["student"], body={"message": "Tambahan"})[0] == 200
    clock[0] = start + timedelta(seconds=61)
    assert create(app, tokens["student"], subject="Lain")[0] == 201
    clock[0] = start + timedelta(seconds=70)
    assert request(app, "POST", "/api/support/tickets/1/messages", token=tokens["student"], body={"message": "Tiket lain"})[0] == 429
    # Admin can reply to several tickets without the student's anti-spam delay.
    for ticket_id in (1, 2):
        code, _, detail = request(app, "POST", f"/api/support/tickets/{ticket_id}/messages", token=tokens["admin"], body={"message": "Sedang dicek"})
        assert code == 200 and detail["ticket"]["status"] == "in_progress"
        assert detail["messages"][-1]["author_role"] == "admin"
    code, _, detail = request(app, "PATCH", "/api/support/tickets/1", token=tokens["admin"], body={"status": "waiting_student"})
    assert code == 200 and detail["ticket"]["status"] == "waiting_student"
    clock[0] = start + timedelta(seconds=71)
    code, _, detail = request(app, "POST", "/api/support/tickets/1/messages", token=tokens["student"], body={"message": "Sudah dicoba"})
    assert code == 200 and detail["ticket"]["status"] == "in_progress"
    assert request(app, "PATCH", "/api/support/tickets/1", token=tokens["admin"], body={"status": "resolved"})[0] == 200
    clock[0] = start + timedelta(seconds=81)
    code, _, detail = request(app, "POST", "/api/support/tickets/1/messages", token=tokens["student"], body={"message": "Masih bermasalah"})
    assert code == 200 and detail["ticket"]["status"] == "in_progress"


def test_retry_after_accounts_for_quota_and_cooldown_together(environment):
    app, _, _, tokens, clock = environment
    start = clock[0]
    assert create(app, tokens["student"])[0] == 201
    clock[0] = start + timedelta(minutes=1)
    assert create(app, tokens["student"])[0] == 201
    clock[0] = start + timedelta(hours=24, seconds=-10)
    assert create(app, tokens["student"])[0] == 201
    clock[0] = start + timedelta(hours=24, seconds=-5)
    code, headers, _ = create(app, tokens["student"])
    # The first ticket expires in 5 seconds, but the last ticket's cooldown
    # remains for 55 seconds; a retry before then still could not succeed.
    assert code == 429 and headers["retry-after"] == "55"


def test_authorization_dependencies_and_postgresql_row_lock_are_used():
    for endpoint in (support.list_tickets, support.create_ticket, support.get_ticket, support.add_message):
        dependency = inspect.signature(endpoint).parameters["current_user"].default
        assert dependency.dependency is get_current_user
    assert inspect.signature(support.update_ticket).parameters["admin"].default.dependency is require_admin

    class SpyQuery:
        locked = False

        def populate_existing(self):
            return self

        def filter(self, *args):
            return self

        def with_for_update(self):
            self.locked = True
            return self

        def first(self):
            return student

    class SpySession:
        def query(self, model):
            assert model is User
            return query

    student = User(id=123, identifier="123", password="hash", role="mahasiswa")
    query = SpyQuery()
    assert support_service._lock_user(SpySession(), student) is student and query.locked
    assert "FOR UPDATE" in str(select(User).with_for_update().compile(dialect=postgresql.dialect()))


@pytest.mark.skipif(not os.environ.get("SUPPORT_TEST_DATABASE_URL"), reason="isolated PostgreSQL test URL not configured")
def test_postgresql_concurrent_requests_cannot_bypass_account_limits(monkeypatch):
    """Use a unique temporary schema in an explicitly designated test database."""
    url = make_url(os.environ["SUPPORT_TEST_DATABASE_URL"])
    if url.get_backend_name() != "postgresql" or "test" not in (url.database or "").lower():
        pytest.fail("SUPPORT_TEST_DATABASE_URL must designate a PostgreSQL database with 'test' in its name")
    schema_name = "support_test_" + uuid.uuid4().hex
    admin_engine = create_engine(url)
    schema_created = False
    engine = None
    try:
        # The schema name is locally generated, identifier-safe, and created
        # exclusively by this test; no pre-existing schema is modified.
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        schema_created = True
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema_name}"})
        Base.metadata.create_all(bind=engine, tables=[
            User.__table__, Dosen.__table__, Mahasiswa.__table__, SupportTicket.__table__, SupportMessage.__table__,
        ])
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        clock = [datetime(2026, 10, 19, 1, 0, 0)]
        monkeypatch.setattr(support_service, "utc_now", lambda: clock[0])
        with factory() as db:
            student = User(identifier="concurrent-student", password="hash", role="mahasiswa")
            db.add(student)
            db.flush()
            db.add(Mahasiswa(id=student.id, nama_lengkap="Student", program_studi="TI", fakultas="FT"))
            for age in (timedelta(minutes=2), timedelta(minutes=1)):
                timestamp = clock[0] - age
                ticket = SupportTicket(
                    owner_id=student.id, owner_name="Student", owner_identifier=student.identifier,
                    category="Unggah", subject="Sebelumnya", status="new", created_at=timestamp, updated_at=timestamp,
                )
                db.add(ticket)
                db.flush()
                db.add(SupportMessage(
                    ticket_id=ticket.id, author_id=student.id, author_name="Student", author_role="mahasiswa",
                    message="Pesan awal", created_at=timestamp,
                ))
            db.commit()
            token = create_access_token(student)
            user_id = student.id

        app = FastAPI()
        app.include_router(support.router, prefix="/api/support")

        def override_db():
            with factory() as db:
                yield db

        app.dependency_overrides[get_db] = override_db

        def parallel_requests(operation):
            barrier = threading.Barrier(4)

            def run(_):
                barrier.wait(timeout=10)
                return operation()

            with ThreadPoolExecutor(max_workers=4) as executor:
                return list(executor.map(run, range(4)))

        responses = parallel_requests(lambda: create(app, token))
        assert sorted(item[0] for item in responses) == [201, 429, 429, 429]
        with factory() as db:
            tickets = db.query(SupportTicket).filter(SupportTicket.owner_id == user_id).all()
            assert len(tickets) == 3
            new_ticket_id = max(ticket.id for ticket in tickets)

        clock[0] += timedelta(seconds=10)
        responses = parallel_requests(lambda: request(
            app, "POST", f"/api/support/tickets/{new_ticket_id}/messages", token=token, body={"message": "Tambahan"},
        ))
        assert sorted(item[0] for item in responses) == [200, 429, 429, 429]
        with factory() as db:
            assert db.query(SupportMessage).filter(SupportMessage.author_id == user_id).count() == 4
    finally:
        if engine is not None:
            engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        admin_engine.dispose()
