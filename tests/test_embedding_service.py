from app.services.document_embedding_service import DocumentEmbeddingService
from app.services.embedding_service import EmbeddingService


class FakeEmbeddingService(EmbeddingService):
    def __init__(self):
        super().__init__(
            base_url="https://example.test/v1",
            api_key="test-key",
            model="test-model",
        )
        self.last_url = None
        self.last_payload = None

    def _post_json(self, url, payload):
        self.last_url = url
        self.last_payload = payload
        return {
            "data": [
                {"embedding": [0.1, 0.2]},
                {"embedding": [0.3, 0.4]},
            ]
        }


def test_embedding_service_calls_openai_compatible_embeddings_endpoint():
    service = FakeEmbeddingService()

    embeddings = service.embed_texts(["teks satu", "teks dua"])

    assert service.last_url == "https://example.test/v1/embeddings"
    assert service.last_payload == {
        "model": "test-model",
        "input": ["teks satu", "teks dua"],
    }
    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]


def test_document_embedding_service_formats_pgvector_literal():
    service = DocumentEmbeddingService(embedding_service=FakeEmbeddingService())

    literal = service._format_vector_literal([0.1, 2, -3.5])

    assert literal == "[0.1,2.0,-3.5]"


class FakeDocument:
    id = 1
    title = "dokumen.pdf"


def test_ensure_document_embeddings_skips_when_embedding_config_missing():
    service = DocumentEmbeddingService(
        embedding_service=EmbeddingService(base_url="", api_key="", model="")
    )

    result = service.ensure_document_embeddings(FakeDocument(), db=None)

    assert result["status"] == "skipped"
    assert result["reason"] == "embedding_config_not_configured"


def test_ensure_document_embeddings_indexes_pending_chunks():
    service = DocumentEmbeddingService(embedding_service=FakeEmbeddingService())
    service.summarize_document_embeddings = lambda document, db: {
        "document_id": document.id,
        "title": document.title,
        "embedding_model": "test-model",
        "total_chunks": 2,
        "total_embeddable_chunks": 2,
        "total_indexed_chunks": 0,
        "total_pending_chunks": 2,
        "total_failed_chunks": 0,
    }
    service.index_document_embeddings = lambda **kwargs: {
        "document_id": kwargs["document"].id,
        "title": kwargs["document"].title,
        "embedding_model": "test-model",
        "total_chunks": 2,
        "total_pending": 2,
        "total_indexed": 2,
        "total_failed": 0,
        "failed": [],
    }

    result = service.ensure_document_embeddings(FakeDocument(), db=object())

    assert result["status"] == "completed"
    assert result["reason"] == "indexed"
    assert result["index_result"]["total_indexed"] == 2


def test_ensure_document_embeddings_skips_when_document_is_up_to_date():
    service = DocumentEmbeddingService(embedding_service=FakeEmbeddingService())
    service.summarize_document_embeddings = lambda document, db: {
        "document_id": document.id,
        "title": document.title,
        "embedding_model": "test-model",
        "total_chunks": 2,
        "total_embeddable_chunks": 2,
        "total_indexed_chunks": 2,
        "total_pending_chunks": 0,
        "total_failed_chunks": 0,
    }

    result = service.ensure_document_embeddings(FakeDocument(), db=object())

    assert result["status"] == "skipped"
    assert result["reason"] == "up_to_date"
    assert result["index_result"] is None
