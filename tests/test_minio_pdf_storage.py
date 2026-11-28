import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi import UploadFile
from minio.error import S3Error
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import Headers
from urllib3.exceptions import ProtocolError

from app.database.session import Base
from app.models.schemas import Document, PlagiarismCheck, SimilarityMatch, SimilarityResult, User, utc_now
from app.routes import documents, plagiarism
from app.services import pdf_service, pdf_storage
from app.services.repository_check_service import RepositoryCheckService
from app.services.repository_import_files import discover_pdfs
from app.services.repository_import_service import RepositoryImportService
from app.services.watermark_service import WatermarkService
from app.workers.migrate_pdf_storage import cleanup_local, migrate
from test_auth_session import environment  # noqa: F401
from test_highlight_pdf_routes import context as highlight_context, get_pdf
from test_repository_import import InlineStructure, make_pdf


class ObjectBody(BytesIO):
    def __init__(self, body, status, headers):
        super().__init__(body)
        self.status, self.headers = status, headers
        self.released = False

    def stream(self, amount):
        return iter(lambda: self.read(amount), b"")

    def release_conn(self):
        self.released = True


class MemoryS3:
    def __init__(self):
        self.files = {}
        self.last_response = None

    def fput_object(self, bucket, key, filename, **kwargs):
        self.files[bucket, key] = Path(filename).read_bytes()

    def remove_object(self, bucket, key):
        self.files.pop((bucket, key), None)

    def stat_object(self, bucket, key):
        if (bucket, key) not in self.files:
            raise S3Error(None, "NoSuchKey", "Missing", None, None, None)
        return SimpleNamespace(size=len(self.files[bucket, key]))

    def get_object(self, bucket, key, request_headers=None):
        self.stat_object(bucket, key)
        body = self.files[bucket, key]
        status, headers = 200, {}
        if request_headers:
            start, end = map(int, request_headers["Range"].removeprefix("bytes=").split("-"))
            headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
            body, status = body[start:end + 1], 206
        headers["Content-Length"] = str(len(body))
        self.last_response = ObjectBody(body, status, headers)
        return self.last_response


@pytest.fixture
def storage(monkeypatch):
    storage = pdf_storage.MinioPDFStorage(MemoryS3(), "test-private")
    monkeypatch.setenv("MINIO_ENABLED", "true")
    monkeypatch.setattr(pdf_storage, "get_minio_storage", lambda: storage)
    return storage


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize("highlight_upload", ["ok", "reset", "object_missing"])
def test_upload_import_check_and_watermark_storage(tmp_path, monkeypatch, storage, db, highlight_upload):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    source = make_pdf(tmp_path / "inbox" / "proposal" / "alumni.pdf")
    admin = User(identifier="cihuy", password="unused", role="admin")
    student = User(identifier="student", password="unused", role="mahasiswa")
    db.add_all([admin, student])
    db.commit()
    structure = InlineStructure()
    monkeypatch.setattr(documents, "document_structure_service", structure)
    monkeypatch.setattr(documents, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(pdf_service, "UPLOAD_DIR", uploads)
    fput_object = storage.client.fput_object

    def put(bucket, key, filename, **kwargs):
        if key.startswith("plagiarism/highlights/"):
            if highlight_upload == "reset":
                raise ProtocolError("synthetic transport failure", ConnectionResetError())
            if highlight_upload == "object_missing":
                return None
        return fput_object(bucket, key, filename, **kwargs)

    monkeypatch.setattr(storage.client, "fput_object", put)

    importer = RepositoryImportService(db, uploads, min_free_bytes=0, structure_service=structure)
    inputs = discover_pdfs(tmp_path / "inbox")
    assert importer.run(inputs)["imported"] == 1
    assert importer.run(inputs)["existing"] == 1
    alumni = db.query(Document).filter_by(source_origin="alumni").one()
    student = db.query(User).filter_by(identifier="student").one()
    assert "/repository/proposal/" in alumni.file_path
    upload = UploadFile(BytesIO(source.read_bytes()), filename="student.pdf",
                        headers=Headers({"content-type": "application/pdf"}))
    accepted = asyncio.run(documents.upload_and_check_document(
        file=upload, document_type="proposal", user_id=None, candidate_limit=20,
        auto_index_embeddings=False, auto_index_repository_limit=0, current_user=student, db=db,
    ))
    target = db.get(Document, accepted["document_id"])
    assert "/student-uploads/" in target.file_path
    result = RepositoryCheckService().run_repository_check(
        document_id=target.id, requester_user_id=student.id, existing_check_id=accepted["check_id"],
        db=db, auto_index_embeddings=False,
    )
    check = db.get(PlagiarismCheck, accepted["check_id"])
    assert check.status == "completed" and check.overall_similarity > 0.99
    assert result["highlighted_pdf_available"] is True
    history = plagiarism.get_check_history(current_user=student, db=db)
    assert history[0]["highlighted_pdf_available"] is True
    assert history[0]["highlighted_pdf_url"] == f"/api/plagiarism/check/{check.id}/download-highlighted"
    if highlight_upload == "ok":
        assert "/highlights/" in check.highlighted_file_path
        assert list(uploads.rglob("*.pdf")) == []
    else:
        highlight = Path(check.highlighted_file_path)
        assert highlight.parent == uploads / "highlighted"
        assert list(uploads.rglob("*.pdf")) == [highlight]
        assert list(highlight.parent.iterdir()) == [highlight]
        with pymupdf.open(highlight) as pdf:
            assert any(page.first_annot for page in pdf)
        response = plagiarism.download_highlighted_pdf(check.id, current_user=student, db=db)
        assert response.path == str(highlight)
    watermark = WatermarkService(uploads)
    watermarked_ref = watermark.get_pdf(alumni.file_path)
    assert "/watermark-cache/" in watermarked_ref
    assert watermark.get_pdf(alumni.file_path) == watermarked_ref
    with storage.materialize(watermarked_ref) as path, pymupdf.open(path) as marked:
        assert len(marked[0].get_images()) == 1


def test_repair_preserves_matches_and_restores_download(highlight_context, tmp_path, storage):
    context = highlight_context
    body = "Sistem informasi akademik mengelola catatan mahasiswa."
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 180), body)
        source = tmp_path / "unmarked.pdf"
        pdf.save(source)
    context.document.file_path = str(source)
    context.check.highlighted_file_path = None
    context.check.completed_at = utc_now()
    completed_at = context.check.completed_at
    result = SimilarityResult(check_id=context.check.id, source_document_id=context.document.id, similarity_score=0.2)
    context.db.add(result)
    context.db.flush()
    context.db.add(SimilarityMatch(result_id=result.id, source_text=body, submitted_text=body,
                                  similarity_score=1.0, page_number=1,
                                  page_start_position=0, page_end_position=len(body)))
    context.db.commit()

    service = RepositoryCheckService()
    assert get_pdf(context, "highlighted-pdf", user=context.owner)[0] == 404
    assert service.repair_highlighted_pdf(context.check.id, context.db) == "repaired"
    check = context.check
    assert check.status == "completed" and check.overall_similarity == 0.2
    assert check.completed_at == completed_at
    assert len(check.similarity_results[0].matches) == 1
    assert plagiarism.get_check_history(current_user=context.owner, db=context.db)[0]["highlighted_pdf_available"]
    status, _, data = get_pdf(context, "highlighted-pdf", user=context.owner)
    assert status == 200
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        assert pdf[0].first_annot is not None
    assert get_pdf(context, "highlighted-pdf", user=context.outsider)[0] == 403
    assert service.repair_highlighted_pdf(check.id, context.db) == "already_available"


def test_remote_highlight_keeps_owner_checks_and_range(highlight_context, storage):
    context = highlight_context
    reference = storage.reference("highlights/synthetic.pdf")
    storage.upload(reference, context.artifact)
    context.check.highlighted_file_path = reference
    context.db.commit()

    assert get_pdf(context, "highlighted-pdf", user=context.outsider)[0] == 403
    code, headers, body = get_pdf(context, "highlighted-pdf", user=context.owner,
                                  headers={"Range": "bytes=0-255"})
    assert code == 206 and headers["content-range"].startswith("bytes 0-255/")
    assert body == context.artifact.read_bytes()[:256]
    assert storage.client.last_response.closed and storage.client.last_response.released
    storage.delete(reference)
    assert get_pdf(context, "highlighted-pdf", user=context.owner)[0] == 404


def test_storage_error_keeps_safe_diagnostics_for_operators(tmp_path, storage, monkeypatch):
    def deny(*args, **kwargs):
        raise S3Error(SimpleNamespace(status=403), "AccessDenied", "synthetic internal detail",
                      "/private-object", "synthetic-request-id", None)

    monkeypatch.setattr(storage.client, "fput_object", deny)
    path = make_pdf(tmp_path / "source.pdf")
    with pytest.raises(pdf_storage.PDFStorageError) as error:
        storage.upload(storage.reference("repository/proposal/example.pdf"), path)
    assert error.value.diagnostic() == {
        "storage_operation": "put_object",
        "storage_error_type": "S3Error",
        "s3_code": "AccessDenied",
        "http_status": 403,
    }
    assert "synthetic internal detail" not in str(error.value)


def test_migration_verifies_references_before_cleaning_local_files(tmp_path, storage, db):
    source = make_pdf(tmp_path / "repository" / "proposal" / "source.pdf")
    highlight = make_pdf(tmp_path / "highlighted" / "result.pdf")
    watermark = make_pdf(tmp_path / "watermarked" / (64 * "a" + ".pdf"))
    document = Document(title="source.pdf", document_type="proposal", file_path=str(source),
                        source_origin="alumni", repository_status="published")
    db.add(document)
    db.flush()
    check = PlagiarismCheck(document_id=document.id, status="completed", overall_similarity=0.55,
                            highlighted_file_path=str(highlight))
    db.add(check)
    db.commit()
    assert migrate(db, tmp_path)["ready_to_copy"] == 3
    assert storage.client.files == {}

    with (tmp_path / "report.jsonl").open("w+", encoding="utf-8") as report:
        assert migrate(db, tmp_path, storage=storage, report=report)["copied"] == 3
        assert all(path.is_file() for path in (source, highlight, watermark))
        assert "/repository/proposal/" in document.file_path
        assert check.overall_similarity == 0.55 and "/highlights/" in check.highlighted_file_path
        key = (storage.bucket, storage.object_key(check.highlighted_file_path))
        storage.client.files[key] = b"damaged"
        report.seek(0)
        counts = cleanup_local(db, tmp_path, storage, report)
        assert counts["removed"] == 2 and counts["verification_failed"] == 1
        assert highlight.is_file()
        storage.upload(check.highlighted_file_path, highlight)
        report.seek(0)
        counts = cleanup_local(db, tmp_path, storage, report)
        assert counts["removed"] == 1 and counts["already_removed"] == 2
        assert not highlight.exists()
