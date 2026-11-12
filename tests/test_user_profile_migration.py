"""Exercise the actual profile migration in an isolated PostgreSQL schema."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.database import migrations
from app.database.session import Base
from app.models.schemas import Document, PlagiarismCheck, User


@pytest.fixture
def migration_db():
    configured_url = os.getenv("PROFILE_MIGRATION_TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("isolated PostgreSQL profile migration test URL not configured")
    url = make_url(configured_url)
    if url.get_backend_name() != "postgresql" or "test" not in (url.database or "").lower():
        pytest.fail("PROFILE_MIGRATION_TEST_DATABASE_URL must designate a PostgreSQL test database")
    schema = "profile_test_" + uuid.uuid4().hex
    admin_engine = create_engine(url)
    test_engine = None
    try:
        with admin_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        test_engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        with test_engine.connect() as conn:
            yield conn
    finally:
        if test_engine is not None:
            test_engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def create_legacy_data(conn):
    conn.execute(text("""
        CREATE TABLE users (
            id SERIAL PRIMARY KEY, identifier VARCHAR(100) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL, role VARCHAR(30) NOT NULL,
            auth_provider VARCHAR(20) NOT NULL DEFAULT 'local',
            is_active BOOLEAN NOT NULL DEFAULT TRUE, session_version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE mahasiswa (
            id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            nama_lengkap VARCHAR(255) NOT NULL, program_studi VARCHAR(255) NOT NULL,
            fakultas VARCHAR(255) NOT NULL
        );
    """))
    Base.metadata.create_all(bind=conn)
    conn.execute(User.__table__.insert(), [
        {"id": 1, "identifier": "admin", "password": "admin-hash", "role": "admin", "is_active": True, "session_version": 1},
        {"id": 2, "identifier": "student", "password": "student-hash", "role": "mahasiswa", "is_active": True, "session_version": 7},
        {"id": 3, "identifier": "retired", "password": "old-hash", "role": "mahasiswa", "is_active": False, "session_version": 9},
        {"id": 4, "identifier": "without-profile", "password": "no-profile-hash", "role": "mahasiswa", "is_active": True, "session_version": 0},
    ])
    conn.execute(text("""
        INSERT INTO mahasiswa VALUES
            (2, 'Nama Mahasiswa', 'Prodi Uji', 'Fakultas Uji'),
            (3, 'Mahasiswa Nonaktif', 'Prodi Lama', '');
    """))
    conn.execute(Document.__table__.insert().values(id=11, user_id=2, title="Existing.pdf", file_path="existing.pdf"))
    conn.execute(PlagiarismCheck.__table__.insert().values(
        id=12, user_id=2, document_id=11, overall_similarity=.42, status="completed",
    ))


def account_snapshot(conn):
    return conn.execute(text("""
        SELECT id, identifier, password, role, auth_provider, is_active, session_version
        FROM users ORDER BY id
    """)).all()


def test_merge_archives_profiles_preserves_history_and_is_safe_to_repeat(migration_db):
    conn = migration_db
    with conn.begin():
        create_legacy_data(conn)
        accounts = account_snapshot(conn)
        history = conn.execute(text("SELECT row_to_json(c) FROM plagiarism_checks AS c")).scalar_one()
        migrations.migrate_student_profiles_to_users(conn)
        assert "mahasiswa" not in inspect(conn).get_table_names()
        assert account_snapshot(conn) == accounts
        assert conn.execute(text("SELECT nama_lengkap, program_studi, fakultas FROM users ORDER BY id")).all() == [
            (None, None, None), ("Nama Mahasiswa", "Prodi Uji", "Fakultas Uji"),
            ("Mahasiswa Nonaktif", "Prodi Lama", ""), (None, None, None),
        ]
        archive = conn.execute(text("SELECT entity_id, payload FROM legacy_workflow_archive ORDER BY entity_id")).all()
        assert archive[0].entity_id == 2
        assert archive[0].payload == {
            "id": 2, "nama_lengkap": "Nama Mahasiswa", "program_studi": "Prodi Uji", "fakultas": "Fakultas Uji",
        }
        assert archive[1].payload["fakultas"] == ""
        assert conn.execute(text("SELECT user_id FROM documents WHERE id=11")).scalar_one() == 2
        assert conn.execute(text("SELECT row_to_json(c) FROM plagiarism_checks AS c")).scalar_one() == history
        conn.execute(text("UPDATE users SET nama_lengkap='Nama Terbaru' WHERE id=2"))
        migrations.migrate_student_profiles_to_users(conn)
        assert conn.execute(text("SELECT nama_lengkap FROM users WHERE id=2")).scalar_one() == "Nama Terbaru"
        assert conn.execute(text("SELECT entity_id, payload FROM legacy_workflow_archive ORDER BY entity_id")).all() == archive


def test_partially_migrated_user_fields_are_not_overwritten(migration_db):
    with migration_db.begin():
        create_legacy_data(migration_db)
        migration_db.execute(text("""
            ALTER TABLE users ADD COLUMN nama_lengkap VARCHAR(255);
            ALTER TABLE users ADD COLUMN program_studi VARCHAR(255);
            UPDATE users SET nama_lengkap='Already Updated', program_studi='' WHERE id=2;
        """))
        migrations.migrate_student_profiles_to_users(migration_db)
        assert migration_db.execute(text("SELECT nama_lengkap, program_studi, fakultas FROM users WHERE id=2")).one() == (
            "Already Updated", "", "Fakultas Uji",
        )
        assert migration_db.execute(text("""
            SELECT payload->>'nama_lengkap' FROM legacy_workflow_archive
            WHERE entity_type='student_profile' AND entity_id=2
        """)).scalar_one() == "Nama Mahasiswa"


@pytest.mark.parametrize("failure", ["orphan", "unexpected_dependency"])
def test_migration_failure_rolls_back_profile_copy_archive_and_schema_changes(migration_db, failure):
    conn = migration_db
    with conn.begin():
        create_legacy_data(conn)
        if failure == "orphan":
            conn.execute(text("ALTER TABLE mahasiswa DROP CONSTRAINT mahasiswa_id_fkey"))
            conn.execute(text("INSERT INTO mahasiswa VALUES (99, 'Orphan', 'Uji', '')"))
        else:
            conn.execute(text("CREATE TABLE unexpected_dependency (student_id INTEGER REFERENCES mahasiswa(id))"))
        accounts = account_snapshot(conn)
        profiles = conn.execute(text("SELECT * FROM mahasiswa ORDER BY id")).all()
    with pytest.raises(DBAPIError):
        with conn.begin():
            migrations.migrate_student_profiles_to_users(conn)
    with conn.begin():
        assert account_snapshot(conn) == accounts
        assert conn.execute(text("SELECT * FROM mahasiswa ORDER BY id")).all() == profiles
        assert "legacy_workflow_archive" not in inspect(conn).get_table_names()
        assert "nama_lengkap" not in {column["name"] for column in inspect(conn).get_columns("users")}


@pytest.mark.parametrize("legacy", [False, True])
def test_runtime_startup_handles_fresh_and_legacy_databases_and_concurrent_restarts(migration_db, monkeypatch, legacy):
    with migration_db.begin():
        if legacy:
            create_legacy_data(migration_db)
        else:
            Base.metadata.create_all(bind=migration_db)
    monkeypatch.setattr(migrations, "engine", migration_db.engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: migrations.ensure_runtime_columns(), range(2)))
    with migration_db.begin():
        assert "mahasiswa" not in inspect(migration_db).get_table_names()
        assert {"nama_lengkap", "program_studi", "fakultas"} <= {
            column["name"] for column in inspect(migration_db).get_columns("users")
        }
        if legacy:
            assert migration_db.execute(text("SELECT nama_lengkap FROM users WHERE id=2")).scalar_one() == "Nama Mahasiswa"
            assert migration_db.execute(text("SELECT overall_similarity FROM plagiarism_checks WHERE id=12")).scalar_one() == .42
            assert migration_db.execute(text("SELECT COUNT(*) FROM legacy_workflow_archive WHERE entity_type='student_profile'")).scalar_one() == 2
