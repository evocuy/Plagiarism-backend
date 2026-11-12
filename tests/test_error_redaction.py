"""Use synthetic sensitive values to verify responses, persistence and logs."""
import asyncio
import io
import json
import re
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.models.schemas import Document, DocumentChunk, PlagiarismCheck, User
from app.routes import auth, documents, plagiarism
from app.services import embedding_service as embedding_module
from app.services.document_text_service import DocumentTextService
from app.services.embedding_service import EmbeddingService
from app.services.error_service import (
    CHECK_ERROR, TEXT_ERROR, ErrorHandlingMiddleware, public_error_message,
    sanitized_validation_error,
)
from app.services.pdf_service import PDF_EXTRACTION_VERSION
from app.services.repository_candidate_service import RepositoryCandidateService
from test_auth_session import environment, request
from test_document_embedding_service import embedding_document
from test_document_text_service import FakeDb, FakeDocument


PRIVATE = "synthetic-sensitive-value-that-must-not-leak"
FRONTEND = "https://frontend.example.test"


@pytest.fixture
def context(environment, monkeypatch):
    monkeypatch.setenv("STUDENT_AUTH_PROVIDER", "local")
    app, db = environment
    app.include_router(documents.router, prefix="/api/documents")
    app.include_router(plagiarism.router, prefix="/api/plagiarism")

    @app.get("/error-probe")
    def fail():
        raise RuntimeError(PRIVATE)

    app.add_middleware(ErrorHandlingMiddleware)
    app.add_exception_handler(RequestValidationError, sanitized_validation_error)
    app.add_middleware(CORSMiddleware, allow_origins=[FRONTEND], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Error-ID"])
    student = db.query(User).filter_by(identifier="student").one()
    admin = db.query(User).filter_by(identifier="admin").one()
    document = Document(user_id=student.id, title="synthetic.pdf", file_path="synthetic.pdf", text_extraction_error=PRIVATE)
    db.add(document)
    db.flush()
    check = PlagiarismCheck(document_id=document.id, user_id=student.id, overall_similarity=0,
                            status="failed", error_message=PRIVATE, processing_message=PRIVATE)
    db.add(check)
    db.commit()
    return app, db, student, admin, document, check


def test_unhandled_error_is_redacted_and_has_a_reference_and_cors(context, caplog):
    app, _, _, _, _, _ = context
    code, headers, body = request(app, "GET", "/error-probe", headers={"Origin": FRONTEND})
    assert code == 500
    assert re.fullmatch(r"[0-9a-f]{32}", body["error_id"])
    assert headers["x-error-id"] == body["error_id"]
    assert headers["cache-control"] == "no-store"
    assert headers["access-control-allow-origin"] == FRONTEND
    assert PRIVATE not in json.dumps(body) + caplog.text


def test_validation_response_never_echoes_a_password(context):
    app, _, _, _, _, _ = context
    code, _, body = request(app, "POST", "/api/auth/login", body={"identifier": "student", "password": PRIVATE * 20})
    assert code == 422
    assert body["detail"][0]["loc"] == ["body", "password"]
    assert "input" not in body["detail"][0]
    assert "ctx" not in body["detail"][0]
    assert PRIVATE not in json.dumps(body)


def test_historical_document_and_check_errors_are_redacted_when_read(context):
    app, _, student, _, _, check = context
    bearer = "Bearer " + auth.create_access_token(student)
    for path in ["/api/documents", "/api/plagiarism/history", f"/api/plagiarism/check/{check.id}/status"]:
        code, _, body = request(app, "GET", path, authorization=bearer)
        assert code == 200
        assert PRIVATE not in json.dumps(body)
    assert request(app, "GET", "/api/documents", authorization=bearer)[2][0]["text_extraction_error"] == TEXT_ERROR


@pytest.mark.parametrize("operation,batch", [("reindex", False), ("reindex", True), ("embeddings", False), ("embeddings", True)])
def test_indexing_errors_use_public_messages_and_references(context, monkeypatch, caplog, operation, batch):
    app, _, _, admin, document, _ = context

    def fail(*_args, **_kwargs):
        raise RuntimeError(PRIVATE)

    if operation == "reindex":
        monkeypatch.setattr(documents.document_index_service, "reindex_document", fail)
    else:
        monkeypatch.setattr(documents.document_embedding_service.embedding_service, "is_configured", lambda: True)
        monkeypatch.setattr(documents.document_embedding_service, "index_document_embeddings", fail)
    path = f"/api/documents/{operation}" if batch else f"/api/documents/{document.id}/{operation}"
    code, headers, body = request(app, "POST", path, authorization="Bearer " + auth.create_access_token(admin),
                                body={"document_ids": [document.id]} if batch else {})
    assert code == (200 if batch else 500)
    error_id = body["failed"][0]["error_id"] if batch else headers["x-error-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", error_id)
    assert error_id in json.dumps(body)
    assert PRIVATE not in json.dumps(body) + caplog.text


def test_text_extraction_stores_only_a_safe_error_and_preserves_internal_exception_type(caplog):
    class BrokenPdf:
        def extract_text_from_pdf(self, *_args, **_kwargs):
            raise ValueError(PRIVATE)

    document = FakeDocument()
    with pytest.raises(ValueError):
        DocumentTextService(pdf_service=BrokenPdf()).get_or_extract_text(document, FakeDb())
    assert document.text_extraction_error.startswith(TEXT_ERROR + " (ID error: ")
    assert public_error_message(document.text_extraction_error, TEXT_ERROR) == document.text_extraction_error
    assert PRIVATE not in document.text_extraction_error + caplog.text


@pytest.mark.parametrize("kind", ["http", "connection"])
def test_embedding_provider_does_not_expose_response_body_or_connection_detail(monkeypatch, caplog, kind):
    def fail(*_args, **_kwargs):
        if kind == "http":
            raise HTTPError("https://private.invalid/" + PRIVATE, 502, PRIVATE, {}, io.BytesIO(PRIVATE.encode()))
        raise URLError(PRIVATE)

    monkeypatch.setattr(embedding_module, "urlopen", fail)
    service = EmbeddingService(base_url="https://embedding.example.test/v1", api_key="synthetic-key", model="synthetic-model")
    with pytest.raises(RuntimeError) as error:
        service.embed_texts(["synthetic text"])
    assert "ID error:" in str(error.value)
    assert PRIVATE not in str(error.value) + caplog.text


def test_failed_chunk_errors_are_safe_in_both_storage_and_response(embedding_document, monkeypatch, caplog):
    db, document, service, provider = embedding_document

    def fail(*_args, **_kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(provider, "embed_texts", fail)
    result = service.index_document_embeddings(document, db)
    assert result["total_failed"] == 1
    stored = db.query(DocumentChunk).filter(DocumentChunk.embedding_error.is_not(None)).one().embedding_error
    assert "ID error:" in stored
    assert PRIVATE not in stored + json.dumps(result) + caplog.text


def test_candidate_fallback_reason_does_not_contain_database_errors(monkeypatch, caplog):
    service = RepositoryCandidateService()
    service.embedding_service.model = "synthetic-model"

    def fail(*_args, **_kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(service, "_fetch_embedded_target_chunks", fail)
    target = SimpleNamespace(id=1, extraction_metadata={"chunk_extraction_version": PDF_EXTRACTION_VERSION})
    source = SimpleNamespace(id=2, extraction_metadata={"chunk_extraction_version": PDF_EXTRACTION_VERSION})
    result = service._rank_semantic_candidates(target, [source], {2: source}, SimpleNamespace(rollback=lambda: None), 20, 0, 1)
    assert result["reason"] == "semantic_query_failed"
    assert PRIVATE not in json.dumps(result) + caplog.text


def test_stream_failure_does_not_send_a_second_response_or_propagate_private_details(caplog):
    events = []

    async def broken(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError(PRIVATE)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        events.append(event)

    with pytest.raises(RuntimeError, match="Response interrupted") as error:
        asyncio.run(ErrorHandlingMiddleware(broken)({"type": "http"}, receive, send))
    assert len(events) == 1
    assert PRIVATE not in str(error.value) + caplog.text
