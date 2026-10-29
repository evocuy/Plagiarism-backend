from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.session import Base
from app.models.schemas import Document, PlagiarismCheck, User, utc_now
from app.services.check_queue_service import CheckQueueService, CheckQueueSettings
from app.services.check_queue_worker import CheckQueueWorker


def _make_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine), engine


def _add_document(db, user, title="proposal.pdf"):
    document = Document(
        user_id=user.id,
        title=title,
        document_type="proposal",
        file_path=title,
    )
    db.add(document)
    db.flush()
    return document


def _add_check(db, document, user, *, status="completed", created_at=None, **kwargs):
    check = PlagiarismCheck(
        document_id=document.id,
        user_id=user.id,
        overall_similarity=0.0,
        status=status,
        created_at=created_at or utc_now(),
        updated_at=created_at or utc_now(),
        **kwargs,
    )
    db.add(check)
    db.flush()
    return check


def test_daily_limit_is_three_and_returns_retry_after():
    factory, engine = _make_session_factory()
    now = utc_now().replace(hour=10, minute=0, second=0, microsecond=0)
    db = factory()
    user = User(identifier="student", password="hash", role="mahasiswa")
    db.add(user)
    db.flush()
    document = _add_document(db, user)
    for _ in range(3):
        _add_check(db, document, user, created_at=now)
    db.commit()

    service = CheckQueueService(
        settings=CheckQueueSettings(daily_limit=3, quota_timezone="UTC"),
        now_provider=lambda: now,
    )
    with pytest.raises(HTTPException) as error:
        service.assert_can_enqueue(db, user.id)

    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == str(14 * 60 * 60)
    assert "3 pemeriksaan" in error.value.detail
    db.close()
    engine.dispose()


def test_daily_quota_uses_wita_calendar_day_even_when_database_is_utc():
    service = CheckQueueService(
        settings=CheckQueueSettings(quota_timezone="Asia/Makassar")
    )

    # 00:30 WITA on 27 October is still 16:30 UTC on 26 October.
    day_start, next_day = service._quota_day_window(datetime(2026, 10, 26, 16, 30))

    assert day_start == datetime(2026, 10, 26, 16, 0)
    assert next_day == datetime(2026, 10, 27, 16, 0)


def test_one_active_check_blocks_second_submission_before_upload():
    factory, engine = _make_session_factory()
    db = factory()
    user = User(identifier="student", password="hash", role="mahasiswa")
    db.add(user)
    db.flush()
    document = _add_document(db, user)
    _add_check(db, document, user, status="pending", queued_at=utc_now())
    db.commit()

    service = CheckQueueService(settings=CheckQueueSettings(max_active_per_student=1))
    with pytest.raises(HTTPException) as error:
        service.assert_can_enqueue(db, user.id)

    assert error.value.status_code == 409
    assert "sedang mengantre atau diproses" in error.value.detail
    db.close()
    engine.dispose()


def test_enqueue_rechecks_active_limit_at_the_authoritative_admission_point():
    factory, engine = _make_session_factory()
    db = factory()
    user = User(identifier="student", password="hash", role="mahasiswa")
    db.add(user)
    db.flush()
    document = _add_document(db, user)
    _add_check(db, document, user, status="pending", queued_at=utc_now())
    db.commit()

    service = CheckQueueService(settings=CheckQueueSettings(max_active_per_student=1))
    with pytest.raises(HTTPException) as error:
        service.enqueue(
            db=db,
            document_id=document.id,
            user_id=user.id,
            candidate_limit=20,
            auto_index_embeddings=True,
            auto_index_repository_limit=50,
        )

    assert error.value.status_code == 409
    assert db.query(PlagiarismCheck).count() == 1
    db.close()
    engine.dispose()


def test_global_queue_capacity_rejects_new_work():
    factory, engine = _make_session_factory()
    db = factory()
    first_user = User(identifier="first", password="hash", role="mahasiswa")
    second_user = User(identifier="second", password="hash", role="mahasiswa")
    third_user = User(identifier="third", password="hash", role="mahasiswa")
    db.add_all([first_user, second_user, third_user])
    db.flush()
    first_document = _add_document(db, first_user, "first.pdf")
    second_document = _add_document(db, second_user, "second.pdf")
    _add_check(db, first_document, first_user, status="processing")
    _add_check(db, second_document, second_user, status="pending", queued_at=utc_now())
    db.commit()

    service = CheckQueueService(settings=CheckQueueSettings(max_queued_checks=2))
    with pytest.raises(HTTPException) as error:
        service.assert_can_enqueue(db, third_user.id)

    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "5"
    db.close()
    engine.dispose()


def test_enqueue_returns_fifo_queue_position_and_persists_effective_parameters():
    factory, engine = _make_session_factory()
    db = factory()
    first_user = User(identifier="first-student", password="hash", role="mahasiswa")
    user = User(identifier="student", password="hash", role="mahasiswa")
    db.add_all([first_user, user])
    db.flush()
    first_document = _add_document(db, first_user, "first.pdf")
    second_document = _add_document(db, user, "second.pdf")
    now = utc_now()
    _add_check(db, first_document, first_user, status="processing", created_at=now)
    db.commit()

    service = CheckQueueService(now_provider=lambda: now)
    check, position = service.enqueue(
        db=db,
        document_id=second_document.id,
        user_id=user.id,
        candidate_limit=20,
        auto_index_embeddings=True,
        auto_index_repository_limit=50,
    )

    assert position == 2
    assert check.status == "pending"
    assert check.candidate_limit == 20
    assert check.auto_index_embeddings is True
    assert check.auto_index_repository_limit == 50
    assert check.queued_at == now
    db.close()
    engine.dispose()


def test_worker_claims_oldest_pending_check_and_runs_once():
    factory, engine = _make_session_factory()
    db = factory()
    user = User(identifier="student", password="hash", role="mahasiswa")
    db.add(user)
    db.flush()
    first_document = _add_document(db, user, "first.pdf")
    second_document = _add_document(db, user, "second.pdf")
    now = utc_now()
    first = _add_check(
        db,
        first_document,
        user,
        status="pending",
        queued_at=now - timedelta(seconds=1),
    )
    second = _add_check(db, second_document, user, status="pending", queued_at=now)
    db.commit()
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)

    worker = CheckQueueWorker(
        session_factory=factory,
        settings=CheckQueueSettings(),
        runner=runner,
        now_provider=lambda: now,
    )

    assert worker.process_next() is True
    db.refresh(first)
    db.refresh(second)
    assert first.status == "processing"
    assert first.attempt_count == 1
    assert second.status == "pending"
    assert calls[0]["check_id"] == first.id
    assert calls[0]["candidate_limit"] == 20
    assert worker.process_next() is True
    db.refresh(second)
    assert second.status == "processing"
    assert calls[1]["check_id"] == second.id
    db.close()
    engine.dispose()


def test_worker_dispatches_direct_check_to_the_same_bounded_worker():
    factory, engine = _make_session_factory()
    db = factory()
    user = User(identifier="student", password="hash", role="mahasiswa")
    db.add(user)
    db.flush()
    target = _add_document(db, user, "target.pdf")
    reference = _add_document(db, user, "reference.pdf")
    direct_check = _add_check(
        db,
        target,
        user,
        status="pending",
        queued_at=utc_now(),
        reference_document_id=reference.id,
    )
    db.commit()
    repository_calls = []
    direct_calls = []

    worker = CheckQueueWorker(
        session_factory=factory,
        settings=CheckQueueSettings(),
        runner=lambda **kwargs: repository_calls.append(kwargs),
        direct_runner=lambda **kwargs: direct_calls.append(kwargs),
    )

    assert worker.process_next() is True
    assert repository_calls == []
    assert direct_calls[0]["check_id"] == direct_check.id
    assert direct_calls[0]["document_id"] == target.id
    assert direct_calls[0]["reference_document_id"] == reference.id
    db.close()
    engine.dispose()


def test_worker_requeues_one_stale_attempt_then_marks_second_failure():
    factory, engine = _make_session_factory()
    db = factory()
    user = User(identifier="student", password="hash", role="mahasiswa")
    db.add(user)
    db.flush()
    document = _add_document(db, user)
    now = utc_now()
    stale = _add_check(
        db,
        document,
        user,
        status="processing",
        heartbeat_at=now - timedelta(minutes=46),
        attempt_count=1,
    )
    db.commit()
    worker = CheckQueueWorker(
        session_factory=factory,
        settings=CheckQueueSettings(stale_seconds=45 * 60, max_attempts=2),
        now_provider=lambda: now,
    )

    assert worker.recover_stale_checks(db) == 1
    db.refresh(stale)
    assert stale.status == "pending"
    assert stale.claimed_at is None

    stale.status = "processing"
    stale.heartbeat_at = now - timedelta(minutes=46)
    stale.attempt_count = 2
    db.commit()
    assert worker.recover_stale_checks(db) == 1
    db.refresh(stale)
    assert stale.status == "failed"
    assert stale.processing_stage == "failed"
    db.close()
    engine.dispose()


def test_worker_startup_recovers_processing_check_without_waiting_for_timeout():
    factory, engine = _make_session_factory()
    db = factory()
    user = User(identifier="student", password="hash", role="mahasiswa")
    db.add(user)
    db.flush()
    document = _add_document(db, user)
    now = utc_now()
    interrupted = _add_check(
        db,
        document,
        user,
        status="processing",
        heartbeat_at=now,
        attempt_count=1,
    )
    db.commit()
    worker = CheckQueueWorker(
        session_factory=factory,
        settings=CheckQueueSettings(stale_seconds=45 * 60, max_attempts=2),
        now_provider=lambda: now,
    )

    assert worker.recover_stale_checks(db, force=True) == 1
    db.refresh(interrupted)
    assert interrupted.status == "pending"
    assert interrupted.processing_stage == "queued"
    db.close()
    engine.dispose()


def test_worker_deadline_marks_failed_and_allows_next_job(monkeypatch):
    import json
    import subprocess
    from app.services import check_queue_worker as worker_module

    factory, engine = _make_session_factory()
    with factory() as db:
        user = User(identifier="deadline-student", password="hash", role="mahasiswa")
        db.add(user)
        db.flush()
        first_document = _add_document(db, user, "slow.pdf")
        second_document = _add_document(db, user, "next.pdf")
        first = _add_check(db, first_document, user, status="pending", queued_at=utc_now() - timedelta(seconds=1))
        second = _add_check(db, second_document, user, status="pending", queued_at=utc_now())
        db.commit()
        first_id, second_id = first.id, second.id

    invocations = []

    def run_child(command, **kwargs):
        invocations.append(kwargs)
        if len(invocations) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        job = json.loads(command[-1])
        with factory() as db:
            record = db.get(PlagiarismCheck, job["check_id"])
            record.status = "completed"
            db.commit()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(worker_module.subprocess, "run", run_child)
    monkeypatch.setenv("CHECK_JOB_TIMEOUT_SECONDS", "30")
    worker = CheckQueueWorker(session_factory=factory)

    assert worker.process_next() is True
    with factory() as db:
        first = db.get(PlagiarismCheck, first_id)
        assert first.status == "failed"
        assert first.completed_at is not None
        assert "batas waktu" in first.error_message
        assert db.get(PlagiarismCheck, second_id).status == "pending"

    assert worker.process_next() is True
    with factory() as db:
        assert db.get(PlagiarismCheck, second_id).status == "completed"
    assert [invocation["timeout"] for invocation in invocations] == [30, 30]
    assert worker.process_next() is False
    engine.dispose()
