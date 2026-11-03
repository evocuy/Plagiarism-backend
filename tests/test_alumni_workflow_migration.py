"""Run the retiring-workflow migration against an explicitly isolated test DB."""
import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.database.migrations import migrate_alumni_repository_workflow


@pytest.mark.skipif(
    not os.getenv("ALUMNI_MIGRATION_TEST_DATABASE_URL"),
    reason="isolated PostgreSQL migration test URL not configured",
)
def test_migration_archives_retired_workflow_and_preserves_scores_idempotently():
    url = make_url(os.environ["ALUMNI_MIGRATION_TEST_DATABASE_URL"])
    if url.get_backend_name() != "postgresql" or "test" not in (url.database or "").lower():
        pytest.fail("ALUMNI_MIGRATION_TEST_DATABASE_URL must designate a PostgreSQL test database")
    engine = create_engine(url)
    schema = "workflow_test_" + uuid.uuid4().hex
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text(f'SET search_path TO "{schema}"'))
            conn.execute(text("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY, identifier TEXT, role TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    session_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE dosen (id INTEGER PRIMARY KEY REFERENCES users(id), nama_lengkap TEXT);
                CREATE TABLE mahasiswa (
                    id INTEGER PRIMARY KEY REFERENCES users(id),
                    dosen_pembimbing_id INTEGER REFERENCES dosen(id)
                );
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id),
                    document_type TEXT, repository_status TEXT,
                    repository_published_at TIMESTAMP, deleted_at TIMESTAMP
                );
                CREATE TABLE plagiarism_checks (
                    id INTEGER PRIMARY KEY, document_id INTEGER REFERENCES documents(id),
                    overall_similarity FLOAT, status TEXT, approval_status TEXT,
                    reviewed_at TIMESTAMP, reviewer_note TEXT
                );
                CREATE INDEX ix_plagiarism_checks_approval_status ON plagiarism_checks(approval_status);
                CREATE TABLE similarity_results (
                    id INTEGER PRIMARY KEY, check_id INTEGER REFERENCES plagiarism_checks(id),
                    source_document_id INTEGER REFERENCES documents(id), similarity_score FLOAT
                );
                INSERT INTO users(id, identifier, role) VALUES
                    (1, 'admin', 'admin'), (2, 'student', 'mahasiswa'), (3, 'legacy', 'dosen');
                INSERT INTO dosen VALUES (3, 'Retained historical profile');
                INSERT INTO mahasiswa VALUES (2, 3);
                INSERT INTO documents VALUES
                    (1, 2, 'skripsi', 'published', CURRENT_TIMESTAMP, NULL),
                    (2, NULL, 'skripsi', 'published', CURRENT_TIMESTAMP, NULL),
                    (3, NULL, 'proposal', 'draft', NULL, NULL);
                INSERT INTO plagiarism_checks VALUES
                    (1, 1, 0.42, 'completed', 'disetujui', CURRENT_TIMESTAMP, 'Historical note'),
                    (2, 1, 0, 'completed', 'belum disetujui', NULL, NULL),
                    (3, 1, 0, 'processing', 'belum disetujui', NULL, NULL),
                    (4, 1, 0.82, 'completed', 'belum disetujui', NULL, NULL);
                INSERT INTO similarity_results VALUES (1, 1, 2, 0.42);
            """))
            migrate_alumni_repository_workflow(conn)
            inspector = inspect(conn)
            assert "dosen" not in inspector.get_table_names()
            assert "dosen_pembimbing_id" not in {c["name"] for c in inspector.get_columns("mahasiswa")}
            assert not {"approval_status", "reviewed_at", "reviewer_note"} & {
                c["name"] for c in inspector.get_columns("plagiarism_checks")
            }
            assert conn.execute(text("SELECT is_active, session_version FROM users WHERE id=3")).one() == (False, 1)
            assert conn.execute(text("SELECT source_origin, repository_status, repository_published_at FROM documents WHERE id=1")).one() == ("student", "draft", None)
            assert conn.execute(text("SELECT source_origin, repository_status FROM documents WHERE id=2")).one() == ("alumni", "published")
            assert conn.execute(text("SELECT source_origin, repository_status FROM documents WHERE id=3")).one() == ("alumni", "draft")
            assert conn.execute(text("SELECT comparison_status FROM plagiarism_checks ORDER BY id")).scalars().all() == [
                "available", "no_comparison_sources", "available", "available",
            ]
            assert conn.execute(text("SELECT overall_similarity FROM plagiarism_checks WHERE id=1")).scalar_one() == .42
            assert conn.execute(text("SELECT similarity_score FROM similarity_results WHERE id=1")).scalar_one() == .42
            archive = conn.execute(text("SELECT entity_type, entity_id, payload FROM legacy_workflow_archive ORDER BY entity_type, entity_id")).all()
            assert len(archive) == 6
            # Missing legacy source rows must not hide a stored nonzero score.
            assert conn.execute(text("SELECT overall_similarity FROM plagiarism_checks WHERE id=4")).scalar_one() == .82
            review = next(row.payload for row in archive if row.entity_type == "check_review" and row.entity_id == 1)
            assert review["approval_status"] == "disetujui"
            assert review["reviewer_note"] == "Historical note"
            assert next(row.payload for row in archive if row.entity_type == "student_supervisor")["supervisor_id"] == 3
            # Future ownerless student records must not be reclassified on restart.
            conn.execute(text("INSERT INTO documents (id, source_origin, document_type, repository_status) VALUES (4, 'student', 'proposal', 'draft')"))
            conn.execute(text("UPDATE plagiarism_checks SET comparison_status='no_comparison_sources' WHERE id=3"))
            migrate_alumni_repository_workflow(conn)
            assert conn.execute(text("SELECT source_origin FROM documents WHERE id=4")).scalar_one() == "student"
            assert conn.execute(text("SELECT comparison_status FROM plagiarism_checks WHERE id=3")).scalar_one() == "no_comparison_sources"
            assert conn.execute(text("SELECT session_version FROM users WHERE id=3")).scalar_one() == 1
            assert conn.execute(text("SELECT entity_type, entity_id, payload FROM legacy_workflow_archive ORDER BY entity_type, entity_id")).all() == archive
            assert conn.execute(text("SELECT COUNT(*) FROM documents")).scalar_one() == 4
            assert conn.execute(text("SELECT COUNT(*) FROM plagiarism_checks")).scalar_one() == 4
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
