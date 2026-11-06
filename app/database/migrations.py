from sqlalchemy import text

from app.database.session import engine


def migrate_alumni_repository_workflow(conn) -> None:
    """Archive retired workflow metadata, then apply the alumni-only corpus.

    The caller owns one PostgreSQL transaction. Any unexpected dependency or
    failed statement rolls back the archive and schema/data changes together.
    Account/document/check identities and score history are never deleted.
    """
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS legacy_workflow_archive (
            entity_type VARCHAR(40) NOT NULL,
            entity_id INTEGER NOT NULL,
            payload JSONB NOT NULL,
            archived_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (entity_type, entity_id)
        )
    """))
    conn.execute(text("""
        DO $$ BEGIN
            IF to_regclass('dosen') IS NOT NULL THEN
                INSERT INTO legacy_workflow_archive (entity_type, entity_id, payload)
                SELECT 'lecturer_profile', id, to_jsonb(d) FROM dosen AS d
                ON CONFLICT DO NOTHING;
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'mahasiswa'
                  AND column_name = 'dosen_pembimbing_id'
            ) THEN
                INSERT INTO legacy_workflow_archive (entity_type, entity_id, payload)
                SELECT 'student_supervisor', id,
                    jsonb_build_object('supervisor_id', dosen_pembimbing_id)
                FROM mahasiswa WHERE dosen_pembimbing_id IS NOT NULL
                ON CONFLICT DO NOTHING;
            END IF;
        END $$
    """))
    # JSON access also works on older databases with only some review columns.
    conn.execute(text("""
        INSERT INTO legacy_workflow_archive (entity_type, entity_id, payload)
        SELECT 'check_review', id, jsonb_build_object(
            'approval_status', to_jsonb(c)->'approval_status',
            'reviewed_at', to_jsonb(c)->'reviewed_at',
            'reviewer_note', to_jsonb(c)->'reviewer_note'
        )
        FROM plagiarism_checks AS c
        WHERE to_jsonb(c) ?| ARRAY['approval_status', 'reviewed_at', 'reviewer_note']
        ON CONFLICT DO NOTHING
    """))
    conn.execute(text("""
        UPDATE users SET is_active = FALSE,
            session_version = COALESCE(session_version, 0) + 1
        WHERE role = 'dosen' AND is_active IS DISTINCT FROM FALSE
    """))
    conn.execute(text("ALTER TABLE mahasiswa DROP COLUMN IF EXISTS dosen_pembimbing_id"))
    # No CASCADE: an unanticipated dependency must fail instead of deleting it.
    conn.execute(text("DROP TABLE IF EXISTS dosen"))
    conn.execute(text("ALTER TABLE plagiarism_checks DROP COLUMN IF EXISTS approval_status"))
    conn.execute(text("ALTER TABLE plagiarism_checks DROP COLUMN IF EXISTS reviewed_at"))
    conn.execute(text("ALTER TABLE plagiarism_checks DROP COLUMN IF EXISTS reviewer_note"))

    # A nullable add distinguishes legacy rows from future rows. The default
    # is installed only after classification; restarts never reclassify a new
    # ownerless student upload as alumni material.
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_origin VARCHAR(20)"))
    conn.execute(text("""
        UPDATE documents SET source_origin = CASE
            WHEN user_id IS NULL THEN 'alumni' ELSE 'student' END
        WHERE source_origin IS NULL
    """))
    conn.execute(text("ALTER TABLE documents ALTER COLUMN source_origin SET DEFAULT 'student'"))
    conn.execute(text("ALTER TABLE documents ALTER COLUMN source_origin SET NOT NULL"))
    conn.execute(text("""
        UPDATE documents SET source_origin = 'student', repository_status = 'draft',
            repository_published_at = NULL
        WHERE user_id IS NOT NULL
          AND (source_origin <> 'student' OR repository_status <> 'draft'
               OR repository_published_at IS NOT NULL)
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_documents_source_origin ON documents(source_origin)"))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_documents_alumni_candidates ON documents(document_type)
        WHERE deleted_at IS NULL AND repository_status = 'published'
          AND source_origin = 'alumni' AND user_id IS NULL
    """))
    conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS comparison_status VARCHAR(30)"))
    conn.execute(text("""
        UPDATE plagiarism_checks AS c SET comparison_status = CASE
            WHEN status = 'completed' AND COALESCE(overall_similarity, 0) = 0 AND NOT EXISTS (
                SELECT 1 FROM similarity_results AS r WHERE r.check_id = c.id
            ) THEN 'no_comparison_sources' ELSE 'available' END
        WHERE comparison_status IS NULL
    """))
    conn.execute(text("ALTER TABLE plagiarism_checks ALTER COLUMN comparison_status SET DEFAULT 'available'"))
    conn.execute(text("ALTER TABLE plagiarism_checks ALTER COLUMN comparison_status SET NOT NULL"))


def ensure_runtime_columns() -> None:
    with engine.connect() as conn:
        # Existing databases need this before JWTs with a session version are
        # accepted. The default safely invalidates pre-versioned JWTs.
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider VARCHAR(20) NOT NULL DEFAULT 'local'"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS progress INTEGER NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS processing_stage VARCHAR(64)"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS processing_message TEXT"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS error_message TEXT"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS candidate_limit INTEGER NOT NULL DEFAULT 20"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS auto_index_embeddings BOOLEAN NOT NULL DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS auto_index_repository_limit INTEGER NOT NULL DEFAULT 50"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS reference_document_id INTEGER"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS queued_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS started_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_plagiarism_checks_queue_status ON plagiarism_checks(status, queued_at, created_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_plagiarism_checks_user_queue_status ON plagiarism_checks(user_id, status, created_at)"))
        conn.execute(text("UPDATE plagiarism_checks SET candidate_limit = 20 WHERE candidate_limit IS NULL OR candidate_limit < 1"))
        conn.execute(text("UPDATE plagiarism_checks SET auto_index_repository_limit = 50 WHERE auto_index_repository_limit IS NULL OR auto_index_repository_limit < 0"))
        conn.execute(text("UPDATE plagiarism_checks SET attempt_count = 0 WHERE attempt_count IS NULL"))
        conn.execute(text("UPDATE plagiarism_checks SET queued_at = created_at WHERE status = 'pending' AND queued_at IS NULL"))
        conn.execute(text("""
            UPDATE plagiarism_checks
            SET progress = 100,
                processing_stage = COALESCE(processing_stage, 'completed'),
                processing_message = COALESCE(processing_message, 'Pengecekan kemiripan selesai.'),
                completed_at = COALESCE(completed_at, created_at),
                updated_at = COALESCE(updated_at, created_at)
            WHERE status = 'completed' AND progress < 100
        """))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS extracted_text TEXT"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS cleaned_text TEXT"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS extraction_metadata JSONB"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS text_extracted_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS text_extraction_error TEXT"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_documents_deleted_at ON documents(deleted_at)"))
        # Comparison corpus lifecycle: uploads stay out of the repository
        # until explicitly published. Server defaults also protect direct SQL
        # inserts that do not go through the ORM.
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS repository_status VARCHAR(20) NOT NULL DEFAULT 'draft'"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS repository_published_at TIMESTAMP"))
        conn.execute(text("UPDATE documents SET repository_status = 'draft' WHERE repository_status IS NULL"))
        # Uploads have used canonical lowercase values for some time, but
        # normalize older spellings too so the type-isolated corpus does not
        # silently exclude legacy Skripsi/Proposal/Sempro records.
        conn.execute(text("""
            UPDATE documents
            SET document_type = CASE LOWER(BTRIM(document_type))
                WHEN 'sempro' THEN 'proposal'
                WHEN 'proposal' THEN 'proposal'
                WHEN 'skripsi' THEN 'skripsi'
                ELSE document_type
            END
            WHERE LOWER(BTRIM(document_type)) IN ('sempro', 'proposal', 'skripsi')
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_documents_repository_status ON documents(repository_status)"))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_documents_published_candidates
            ON documents(document_type)
            WHERE deleted_at IS NULL AND repository_status = 'published'
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS document_chunks (
                id SERIAL PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                chunk_type VARCHAR(50) NOT NULL DEFAULT 'sentence',
                chapter VARCHAR(50),
                page_number INTEGER,
                chunk_index INTEGER NOT NULL,
                raw_text TEXT NOT NULL,
                cleaned_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_document_chunks_document_id ON document_chunks(document_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_document_chunks_chunk_type ON document_chunks(chunk_type)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_document_chunks_chapter ON document_chunks(chapter)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_document_chunks_page_number ON document_chunks(page_number)"))
        conn.execute(text("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS page_start_position INTEGER"))
        conn.execute(text("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS page_end_position INTEGER"))
        conn.execute(text("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(255)"))
        conn.execute(text("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_generated_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_error TEXT"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_model ON document_chunks(embedding_model)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS similarity_results (
                id SERIAL PRIMARY KEY,
                check_id INTEGER NOT NULL REFERENCES plagiarism_checks(id) ON DELETE CASCADE,
                source_document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                chapter VARCHAR(50),
                similarity_score DOUBLE PRECISION NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_similarity_results_check_id ON similarity_results(check_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_similarity_results_source_document_id ON similarity_results(source_document_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_similarity_results_chapter ON similarity_results(chapter)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS similarity_matches (
                id SERIAL PRIMARY KEY,
                result_id INTEGER NOT NULL REFERENCES similarity_results(id) ON DELETE CASCADE,
                source_text TEXT NOT NULL,
                submitted_text TEXT NOT NULL,
                similarity_score DOUBLE PRECISION NOT NULL,
                page_number INTEGER,
                start_position INTEGER,
                end_position INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_similarity_matches_result_id ON similarity_matches(result_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_similarity_matches_page_number ON similarity_matches(page_number)"))
        conn.execute(text("ALTER TABLE similarity_matches ADD COLUMN IF NOT EXISTS page_start_position INTEGER"))
        conn.execute(text("ALTER TABLE similarity_matches ADD COLUMN IF NOT EXISTS page_end_position INTEGER"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id SERIAL PRIMARY KEY,
                owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                owner_name VARCHAR(255) NOT NULL,
                owner_identifier VARCHAR(100) NOT NULL,
                category VARCHAR(100) NOT NULL,
                subject VARCHAR(200) NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'new'
                    CONSTRAINT ck_support_ticket_status CHECK (status IN ('new', 'in_progress', 'waiting_student', 'resolved')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_support_tickets_owner_created_at ON support_tickets(owner_id, created_at)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id SERIAL PRIMARY KEY,
                ticket_id INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
                author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                author_name VARCHAR(255) NOT NULL,
                author_role VARCHAR(30) NOT NULL
                    CONSTRAINT ck_support_message_role CHECK (author_role IN ('mahasiswa', 'admin')),
                message TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_support_messages_author_created_at ON support_messages(author_id, created_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_support_messages_ticket_created_at ON support_messages(ticket_id, created_at)"))
        migrate_alumni_repository_workflow(conn)
        conn.commit()

    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding vector"))
            conn.commit()
    except Exception:
        # pgvector is optional at startup. Embedding endpoints will surface storage errors
        # if the extension is unavailable.
        pass
