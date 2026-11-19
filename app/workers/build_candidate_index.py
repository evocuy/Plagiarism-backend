"""Build the cosine HNSW index outside API/worker startup."""

import argparse
import json
import os

from sqlalchemy import text

from app.services.candidate_index_service import (
    MAX_HNSW_DIMENSIONS,
    embedding_predicate,
    hnsw_index_name,
    hnsw_index_state,
    supports_iterative_hnsw,
)


class CandidateIndexError(RuntimeError):
    """Operator-facing error with no database or model details."""


def _repository_dimensions(conn, model: str) -> int:
    dimensions = conn.execute(text("""
        SELECT DISTINCT vector_dims(c.embedding)
        FROM document_chunks c JOIN documents d ON d.id = c.document_id
        WHERE d.deleted_at IS NULL AND d.source_origin = 'alumni'
          AND d.user_id IS NULL AND d.repository_status = 'published'
          AND c.chunk_type = 'sentence' AND c.embedding_model = :embedding_model
          AND c.embedding IS NOT NULL AND c.embedding_error IS NULL
          AND vector_norm(c.embedding) > 0
        LIMIT 2
    """), {"embedding_model": model}).scalars().all()
    if len(dimensions) != 1:
        raise CandidateIndexError(
            "Dimensi embedding repositori belum tunggal. Indeks embedding dahulu atau isi --dimensions."
        )
    return dimensions[0]


def build_index(engine, model: str, dimensions: int | None = None) -> dict:
    if engine.dialect.name != "postgresql" or not model:
        raise CandidateIndexError("Pembuatan indeks memerlukan PostgreSQL dan EMBED_MODEL.")

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        if not supports_iterative_hnsw(conn):
            raise CandidateIndexError("Indeks kandidat memerlukan pgvector 0.8 atau lebih baru.")
        if dimensions is None:
            dimensions = _repository_dimensions(conn, model)
        if not 1 <= dimensions <= MAX_HNSW_DIMENSIONS:
            raise CandidateIndexError(f"Dimensi HNSW yang didukung adalah 1 sampai {MAX_HNSW_DIMENSIONS}.")
        name = hnsw_index_name(model, dimensions)
        params = {"index_name": name, "embedding_model": model}
        locked = conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:index_name))"), params).scalar_one()
        if not locked:
            raise CandidateIndexError("Indeks kandidat ini sedang dibangun oleh proses lain.")
        try:
            state = hnsw_index_state(conn, name)
            if state and state["valid"]:
                return {"status": "already_exists", "index": name, "dimensions": dimensions}
            if state:
                # An interrupted concurrent build can leave an INVALID index.
                # Repair only the matching HNSW index on document_chunks.
                schema = conn.dialect.identifier_preparer.quote_identifier(state["schema_name"])
                conn.execute(text(f"DROP INDEX CONCURRENTLY {schema}.{name}"))

            conn.execute(text(f"""
                CREATE INDEX CONCURRENTLY {name} ON document_chunks
                USING hnsw ((CAST(embedding AS vector({dimensions}))) vector_cosine_ops)
                WHERE {embedding_predicate(dimensions)}
            """), params)
            return {"status": "created", "index": name, "dimensions": dimensions}
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(hashtext(:index_name))"), params)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dimensions", type=int, help="Dimensi model aktif jika tidak bisa dideteksi dari repositori.")
    args = parser.parse_args()
    try:
        # Load deployment environment, without starting the web application.
        from app.database.session import engine

        result = build_index(engine, os.environ.get("EMBED_MODEL", ""), args.dimensions)
    except CandidateIndexError as error:
        print(json.dumps({"status": "error", "message": str(error)}))
        return 1
    except Exception as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__,
                          "message": "Pembuatan indeks kandidat gagal. Periksa akses database dan ruang disk."}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
