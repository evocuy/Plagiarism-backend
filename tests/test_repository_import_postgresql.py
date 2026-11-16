"""Actual PostgreSQL locking, additive migration and CLI imports in a disposable schema."""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.database import migrations
from app.database.session import Base
from app.models.schemas import Document, PlagiarismCheck, User
from app.services.repository_import_files import ImportInputError
from app.services.repository_import_service import repository_import_lock
from test_repository_import import make_pdf


@pytest.fixture
def postgres():
    configured = os.getenv("IMPORT_TEST_DATABASE_URL")
    if not configured:
        pytest.skip("isolated PostgreSQL import test URL not configured")
    url = make_url(configured)
    if url.get_backend_name() != "postgresql" or "test" not in (url.database or "").lower():
        pytest.fail("IMPORT_TEST_DATABASE_URL must designate a PostgreSQL test database")
    schema = "repository_import_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    isolated = None
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        isolated = create_engine(url.update_query_dict({"options": f"-csearch_path={schema}"}))
        Base.metadata.create_all(isolated)
        yield isolated
    finally:
        if isolated is not None:
            isolated.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_global_import_lock_survives_commits_and_is_released(postgres):
    with sessionmaker(bind=postgres)() as first, sessionmaker(bind=postgres)() as second:
        with repository_import_lock(first):
            first.commit()
            with pytest.raises(ImportInputError, match="masih berjalan"):
                with repository_import_lock(second):
                    pytest.fail("Concurrent import must not acquire the lock")
        with repository_import_lock(second):
            second.commit()


def test_checksum_migration_is_nullable_indexed_and_preserves_history(postgres, monkeypatch):
    with sessionmaker(bind=postgres)() as db:
        document = Document(title="Historical PDF", file_path="historical.pdf", source_origin="alumni", repository_status="published")
        db.add(document)
        db.flush()
        check = PlagiarismCheck(document_id=document.id, overall_similarity=.42, status="completed")
        db.add(check)
        db.commit()
        identifier, check_id = document.id, check.id
    with postgres.begin() as conn:
        conn.execute(text("ALTER TABLE documents DROP COLUMN content_sha256"))
    monkeypatch.setattr(migrations, "engine", postgres)
    migrations.ensure_runtime_columns()
    migrations.ensure_runtime_columns()
    column = next(column for column in inspect(postgres).get_columns("documents") if column["name"] == "content_sha256")
    assert column["nullable"] is True
    assert "ix_documents_content_sha256" in {index["name"] for index in inspect(postgres).get_indexes("documents")}
    with sessionmaker(bind=postgres)() as db:
        assert db.get(Document, identifier).content_sha256 is None
        assert db.get(Document, identifier).title == "Historical PDF"
        assert db.get(PlagiarismCheck, check_id).overall_similarity == .42


def test_real_cli_imports_and_resumes_on_postgresql(postgres, tmp_path):
    with sessionmaker(bind=postgres)() as db:
        db.add(User(identifier="cihuy", password="unused", role="admin"))
        db.commit()
    root = tmp_path / "inbox"
    make_pdf(root / "proposal" / "real.pdf")
    environment = {**os.environ, "DATABASE_URL": postgres.url.render_as_string(hide_password=False),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    # Patch only the CLI's storage root and dotenv loading, not its importer/DB/parser.
    code = ("import sys; from pathlib import Path; from app.workers import import_repository as cli; "
            f"cli.UPLOAD_DIR = Path({str(tmp_path / 'uploads')!r}); "
            "cli.load_dotenv = lambda: None; raise SystemExit(cli.main())")
    summaries = []
    for iteration in range(2):
        report = tmp_path / f"run-{iteration}.jsonl"
        result = subprocess.run([sys.executable, "-c", code, "--root", str(root), "--min-free-mb", "0", "--report", str(report)],
                                env=environment, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        events = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
        summaries.append(events[-1])
    assert summaries[0]["imported"] == 1 and summaries[1]["existing"] == 1
    with sessionmaker(bind=postgres)() as db:
        document = db.query(Document).one()
        assert document.repository_status == "published" and document.source_origin == "alumni" and document.user_id is None
        assert document.content_sha256 and document.cleaned_text and document.chunks


def test_cli_without_vector_storage_stops_before_import_or_network(postgres, tmp_path):
    root = tmp_path / "inbox"
    make_pdf(root / "proposal" / "real.pdf")
    environment = {**os.environ, "DATABASE_URL": postgres.url.render_as_string(hide_password=False),
                   "PYTHONDONTWRITEBYTECODE": "1", "EMBED_URL": "https://synthetic.invalid/v1",
                   "EMBED_API_KEY": "synthetic-private-key", "EMBED_MODEL": "synthetic-model"}
    code = ("from pathlib import Path; from app.workers import import_repository as cli; "
            f"cli.UPLOAD_DIR = Path({str(tmp_path / 'uploads')!r}); "
            "cli.load_dotenv = lambda: None; raise SystemExit(cli.main())")
    result = subprocess.run([sys.executable, "-c", code, "--root", str(root), "--with-embeddings"],
                            env=environment, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=45)
    assert result.returncode == 2
    error = json.loads(result.stdout.splitlines()[-1])
    assert error["error_code"] == "embedding_storage_unavailable"
    assert "synthetic-private-key" not in result.stdout + result.stderr
    assert not (tmp_path / "uploads").exists()
    with sessionmaker(bind=postgres)() as db:
        assert db.query(Document).count() == 0
