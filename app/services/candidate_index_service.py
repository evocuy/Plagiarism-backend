"""Shared PostgreSQL expressions for building and using candidate indexes."""

import hashlib
import re

from sqlalchemy import text


MAX_HNSW_DIMENSIONS = 2000


def hnsw_index_name(model: str, dimensions: int) -> str:
    model_id = hashlib.sha256(model.encode()).hexdigest()[:16]
    return f"ix_document_chunks_hnsw_{dimensions}_{model_id}"


def embedding_predicate(dimensions: int) -> str:
    # The dimension comes from vector_dims or an integer argument. PostgreSQL
    # must see this literal to prove the partial-index predicate matches.
    if type(dimensions) is not int or not 1 <= dimensions <= 16000:
        raise ValueError("Dimensi embedding tidak valid.")
    return f"""chunk_type = 'sentence'
        AND embedding IS NOT NULL
        AND embedding_model = :embedding_model
        AND embedding_error IS NULL
        AND vector_dims(embedding) = {dimensions}
        AND vector_norm(embedding) > 0
        AND NULLIF(BTRIM(cleaned_text), '') IS NOT NULL"""


def hnsw_index_state(db, index_name: str):
    return db.execute(text("""
        SELECT i.indisvalid AS valid, n.nspname AS schema_name
        FROM pg_index i
        JOIN pg_class idx ON idx.oid = i.indexrelid
        JOIN pg_namespace n ON n.oid = idx.relnamespace
        JOIN pg_am am ON am.oid = idx.relam
        WHERE i.indrelid = to_regclass('document_chunks')
          AND idx.relname = :index_name AND am.amname = 'hnsw'
    """), {"index_name": index_name}).mappings().one_or_none()


def supports_iterative_hnsw(db) -> bool:
    version = db.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar_one_or_none()
    match = re.match(r"^(\d+)\.(\d+)", version or "")
    return bool(match and tuple(map(int, match.groups())) >= (0, 8))
