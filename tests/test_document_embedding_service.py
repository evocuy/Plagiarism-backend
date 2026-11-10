"""Exercise indexing/retry state against isolated persisted chunk metadata."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, DocumentChunk, utc_now
from app.services.document_embedding_service import DocumentEmbeddingService
from app.services.embedding_service import EmbeddingService
from app.services.pdf_service import PDF_EXTRACTION_VERSION


class ControlledEmbeddingService(EmbeddingService):
    def __init__(self):
        super().__init__(base_url="https://example.test/v1", api_key="test-key", model="test-model")
        self.failures_remaining = 0
        self.calls = 0

    def _post_json(self, url, payload):
        self.calls += 1
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("Embedding service temporarily unavailable")
        # Exercise the real response parser with deliberately reordered results.
        return {"data": [
            {"index": index, "embedding": [float(index + 1), 0.5]}
            for index in reversed(range(len(payload["input"])))
        ]}


class MetadataDocumentEmbeddingService(DocumentEmbeddingService):
    """Replace only pgvector storage, which SQLite cannot execute."""
    def __init__(self, embedding_service):
        super().__init__(embedding_service=embedding_service, batch_size=1)
        self.stored_vectors = {}
        self.fail_chunk_index = None

    def _store_chunk_embedding(self, chunk, vector, db):
        if chunk.chunk_index == self.fail_chunk_index:
            raise RuntimeError("Vector storage temporarily unavailable")
        self.stored_vectors[chunk.chunk_index] = vector
        chunk.embedding_model = self.embedding_service.model
        chunk.embedding_generated_at = utc_now()
        chunk.embedding_error = None
        db.add(chunk)
        db.commit()


@pytest.fixture
def embedding_document():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        document = Document(
            title="synthetic.pdf", file_path="synthetic.pdf", document_type="proposal",
            extraction_metadata={"chunk_extraction_version": PDF_EXTRACTION_VERSION},
        )
        db.add(document)
        db.flush()
        db.add_all([
            DocumentChunk(
                document_id=document.id, chunk_type="sentence", chunk_index=index,
                raw_text=sentence, cleaned_text=sentence.lower(),
            )
            for index, sentence in enumerate([
                "Sistem informasi akademik mengelola jadwal mahasiswa.",
                "Aplikasi perpustakaan mendukung peminjaman buku.",
            ])
        ])
        db.commit()
        provider = ControlledEmbeddingService()
        service = MetadataDocumentEmbeddingService(provider)
        yield db, document, service, provider
    engine.dispose()


def test_retry_never_reports_completed_while_previous_chunk_failed(embedding_document):
    db, document, service, provider = embedding_document
    provider.failures_remaining = 1

    first = service.ensure_document_embeddings(document, db)
    retry = service.ensure_document_embeddings(document, db)

    assert first["status"] == "failed"
    assert first["total_failed_chunks"] == 1
    assert first["total_pending_chunks"] == 1
    assert retry["status"] == "partial"
    assert retry["total_indexed_chunks"] == 1
    assert retry["total_failed_chunks"] == 1
    assert retry["total_pending_chunks"] == 0
    assert retry["index_result"]["total_failed"] == 0

    # A normal call preserves the existing error; explicit refresh retries it.
    calls_before_skip = provider.calls
    skipped = service.ensure_document_embeddings(document, db)
    assert skipped["reason"] == "previous_embedding_errors"
    assert provider.calls == calls_before_skip

    refreshed = service.ensure_document_embeddings(document, db, refresh_embeddings=True)
    assert refreshed["status"] == "completed"
    assert refreshed["total_indexed_chunks"] == 2
    assert refreshed["total_failed_chunks"] == 0
    assert refreshed["total_pending_chunks"] == 0


def test_indexing_returns_current_counts_and_vectors_stay_with_their_chunks(embedding_document):
    db, document, service, provider = embedding_document
    service.batch_size = 2

    result = service.ensure_document_embeddings(document, db)

    assert result["status"] == "completed"
    assert result["total_indexed_chunks"] == 2
    assert result["total_pending_chunks"] == 0
    assert result["total_failed_chunks"] == 0
    assert service.stored_vectors == {0: [1.0, 0.5], 1: [2.0, 0.5]}
    assert provider.calls == 1


def test_partial_status_counts_previously_indexed_chunks(embedding_document):
    db, document, service, provider = embedding_document
    service.ensure_document_embeddings(document, db)
    chunk = db.query(DocumentChunk).filter_by(document_id=document.id, chunk_index=1).one()
    chunk.embedding_generated_at = None
    db.commit()
    provider.failures_remaining = 1

    result = service.ensure_document_embeddings(document, db)

    assert result["status"] == "partial"
    assert result["total_indexed_chunks"] == 1
    assert result["total_failed_chunks"] == 1
    assert result["index_result"]["total_indexed"] == 0


def test_storage_error_remains_partial_until_explicit_refresh(embedding_document):
    db, document, service, _ = embedding_document
    service.fail_chunk_index = 0

    result = service.ensure_document_embeddings(document, db)

    assert result["status"] == "partial"
    assert result["total_indexed_chunks"] == 1
    assert result["total_failed_chunks"] == 1
    assert result["total_pending_chunks"] == 0

    service.fail_chunk_index = None
    refreshed = service.ensure_document_embeddings(document, db, refresh_embeddings=True)
    assert refreshed["status"] == "completed"
    assert refreshed["total_indexed_chunks"] == 2
    assert refreshed["total_failed_chunks"] == 0
