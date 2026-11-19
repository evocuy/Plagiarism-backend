"""Exercise the candidate SQL and actual pgvector index in an isolated schema."""

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.services.candidate_index_service import hnsw_index_name, hnsw_index_state, supports_iterative_hnsw
from app.services.embedding_service import EmbeddingService
from app.services.repository_candidate_service import RepositoryCandidateService
from app.workers.build_candidate_index import build_index
from test_repository_import_postgresql import postgres


MODEL = "synthetic-model's-v1"


@pytest.fixture
def vectors(postgres, monkeypatch):
    monkeypatch.setenv("CANDIDATE_HNSW_EF_SEARCH", "160")
    with postgres.begin() as conn:
        available = conn.execute(text("SELECT 1 FROM pg_available_extensions WHERE name='vector'")).scalar()
        if not available:
            pytest.skip("pgvector is unavailable in the isolated PostgreSQL installation")
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        if not supports_iterative_hnsw(conn) or conn.execute(text("SELECT to_regtype('vector')")).scalar() is None:
            pytest.skip("pgvector 0.8+ must be visible in this test schema")
        conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding vector"))
        conn.execute(text("""
            INSERT INTO documents (id, title, file_path, document_type, source_origin, repository_status)
            VALUES (1,'first','first.pdf','proposal','alumni','published'),
                   (2,'second','second.pdf','proposal','alumni','published'),
                   (3,'other','other.pdf','skripsi','alumni','published')
        """))
        conn.execute(text("""
            INSERT INTO document_chunks (document_id, chunk_type, chunk_index, chapter, raw_text,
                cleaned_text, embedding, embedding_model, embedding_error)
            VALUES
                (1,'sentence',1,'bab_1','expected','expected','[1,0,0]',:model,NULL),
                (1,'sentence',2,'bab_2','second chapter','second chapter','[1,0,0]',:model,NULL),
                (2,'sentence',1,'bab_1','wrong model','wrong model','[1,0,0]','previous-model',NULL),
                (2,'sentence',2,'bab_1','wrong width','wrong width','[1,0,0,0]',:model,NULL),
                (2,'sentence',3,'bab_1','zero','zero','[0,0,0]',:model,NULL),
                (2,'sentence',4,'bab_1','failed','failed','[1,0,0]',:model,'failed'),
                (2,'sentence',5,'bab_1','blank',' ','[1,0,0]',:model,NULL),
                (3,'sentence',1,'bab_1','outside scope','outside scope','[1,0,0]',:model,NULL)
        """), {"model": MODEL})
        conn.execute(text("""
            INSERT INTO document_chunks (document_id, chunk_type, chunk_index, chapter, raw_text,
                cleaned_text, embedding, embedding_model)
            SELECT (i % 2) + 1, 'sentence', i + 20, 'bab_1', 'synthetic', 'synthetic',
                CAST(ARRAY[cos(i * 0.77::float8), sin(i * 0.93::float8), cos(i * 0.49::float8)] AS vector), :model
            FROM generate_series(1, 6000) i
        """), {"model": MODEL})
        conn.execute(text("ANALYZE document_chunks"))
    return postgres, RepositoryCandidateService(embedding_service=EmbeddingService(model=MODEL))


def test_candidate_search_uses_index_and_respects_scope_model_dimension_and_chapter(vectors):
    engine, service = vectors
    name = hnsw_index_name(MODEL, 3)
    with Session(engine) as db:
        assert service._prepare_hnsw_search(db, 3, 20) is False
        baseline = service._query_semantic_matches([1, 2], "[1,0,0]", db, 20, "bab_1")
    # The fixture intentionally has mixed dimensions under one model.
    assert build_index(engine, MODEL, 3) == {"status": "created", "index": name, "dimensions": 3}
    assert build_index(engine, MODEL, 3)["status"] == "already_exists"

    queries = []
    with Session(engine) as db:
        assert hnsw_index_state(db, name)["valid"]
        assert service._prepare_hnsw_search(db, 3, 20) is True
        assert db.execute(text("SELECT current_setting('hnsw.ef_search')")).scalar() == "160"

        def capture(_conn, _cursor, sql, params, _context, _executemany):
            if "ORDER BY (CAST(embedding AS vector(" in sql:
                queries.append((sql, params))

        connection = db.connection()
        event.listen(connection, "before_cursor_execute", capture)
        try:
            matches = service._query_semantic_matches([1, 2], "[1,0,0]", db, 20, "bab_1", use_hnsw=True)
        finally:
            event.remove(connection, "before_cursor_execute", capture)
        plan = connection.exec_driver_sql("EXPLAIN (COSTS OFF) " + queries[0][0], queries[0][1]).scalars().all()
        assert any(name in line for line in plan), plan
        assert len(matches) == 20
        assert baseline[0] == matches[0]
        assert matches[0]["raw_text"] == "expected"
        assert {row["raw_text"] for row in matches} == {"expected", "synthetic"}
        assert all(row["document_id"] in {1, 2} and row["chapter"] == "bab_1" for row in matches)
        assert service._count_repository_embedding_chunks([1, 2], db, 3) == 6002


def test_sparse_ann_results_trigger_exact_search_in_the_same_chapter(vectors):
    engine, service = vectors
    with engine.begin() as conn:
        conn.execute(text("UPDATE document_chunks SET embedding_model = 'different-width-model' WHERE vector_dims(embedding) = 4"))
    assert build_index(engine, MODEL)["dimensions"] == 3
    orders = []
    with Session(engine) as db:
        assert service._prepare_hnsw_search(db, 3, 20)

        def capture(_conn, _cursor, sql, _params, _context, _executemany):
            if "<=>" in sql:
                orders.append(sql.split("ORDER BY")[1])

        connection = db.connection()
        event.listen(connection, "before_cursor_execute", capture)
        try:
            matches = service._query_semantic_matches([1, 2], "[1,0,0]", db, 20, "bab_2", use_hnsw=True)
        finally:
            event.remove(connection, "before_cursor_execute", capture)
        assert len(orders) == 2 and "+ 0.0" in orders[1]
        assert [row["raw_text"] for row in matches] == ["second chapter"]

        service.hnsw_ef_search = 0
        assert service._prepare_hnsw_search(db, 3, 20) is False
