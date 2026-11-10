from unittest.mock import Mock

import pytest

from app.services.document_embedding_service import DocumentEmbeddingService
from app.services.embedding_service import EmbeddingService


class FakeEmbeddingService(EmbeddingService):
    def __init__(self, response=None):
        super().__init__(
            base_url="https://example.test/v1",
            api_key="test-key",
            model="test-model",
        )
        self.last_url = None
        self.last_payload = None
        self.response = response

    def _post_json(self, url, payload):
        self.last_url = url
        self.last_payload = payload
        if self.response is not None:
            return self.response
        return {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
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


def test_embedding_service_matches_vectors_to_input_indices_when_response_is_reordered():
    service = FakeEmbeddingService(response={
        "data": [
            {"index": 1, "embedding": [0.3, 0.4]},
            {"index": 0, "embedding": [0.1, 0.2]},
        ],
    })

    assert service.embed_texts(["teks satu", "teks dua"]) == [[0.1, 0.2], [0.3, 0.4]]


@pytest.mark.parametrize("indices", [
    [0, 0], [0, 2], [-1, 0], [0, None], [0, "1"], [0, True],
])
def test_embedding_service_rejects_invalid_or_duplicate_indices(indices):
    service = FakeEmbeddingService(response={
        "data": [{"index": index, "embedding": [0.1, 0.2]} for index in indices],
    })

    with pytest.raises(RuntimeError, match="Indeks embedding"):
        service.embed_texts(["teks satu", "teks dua"])


def test_embedding_service_rejects_missing_indices():
    service = FakeEmbeddingService(response={"data": [{"embedding": [0.1, 0.2]}]})

    with pytest.raises(RuntimeError, match="Indeks embedding"):
        service.embed_texts(["teks satu"])


def test_embedding_service_rejects_incomplete_batch_response():
    service = FakeEmbeddingService(response={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    with pytest.raises(RuntimeError, match="Jumlah embedding tidak sesuai"):
        service.embed_texts(["teks satu", "teks dua"])


@pytest.mark.parametrize("data", [None, {}, "invalid", [None]])
def test_embedding_service_rejects_malformed_response_data(data):
    service = FakeEmbeddingService(response={"data": data})

    with pytest.raises(RuntimeError, match="Format respons embedding"):
        service.embed_texts(["teks satu"])


@pytest.mark.parametrize("vector", [None, [], "invalid", ["invalid"], [float("nan")], [float("inf")]])
def test_embedding_service_rejects_invalid_vectors(vector):
    service = FakeEmbeddingService(response={"data": [{"index": 0, "embedding": vector}]})

    with pytest.raises(RuntimeError, match="Vektor embedding"):
        service.embed_texts(["teks satu"])


def test_embedding_service_rejects_inconsistent_vector_dimensions():
    service = FakeEmbeddingService(response={"data": [
        {"index": 0, "embedding": [0.1, 0.2]},
        {"index": 1, "embedding": [0.3]},
    ]})

    with pytest.raises(RuntimeError, match="Dimensi embedding"):
        service.embed_texts(["teks satu", "teks dua"])


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
    initial_summary = {
        "document_id": FakeDocument.id,
        "title": FakeDocument.title,
        "embedding_model": "test-model",
        "total_chunks": 2,
        "total_embeddable_chunks": 2,
        "total_indexed_chunks": 0,
        "total_pending_chunks": 2,
        "total_failed_chunks": 0,
    }
    service.summarize_document_embeddings = Mock(side_effect=[
        initial_summary,
        {**initial_summary, "total_indexed_chunks": 2, "total_pending_chunks": 0},
    ])
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
    assert result["total_indexed_chunks"] == 2
    assert result["total_pending_chunks"] == 0


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
