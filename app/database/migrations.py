from sqlalchemy import text

from app.database.session import engine


def ensure_runtime_columns() -> None:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS progress INTEGER NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS processing_stage VARCHAR(64)"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS processing_message TEXT"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS error_message TEXT"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS started_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"))
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
