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

from app.database.session import Base
from app.models.schemas import Document, PlagiarismCheck, User
from app.routes import documents
from app.services import pdf_storage
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


def test_new_upload_import_check_and_watermark_use_minio(tmp_path, monkeypatch, storage, db):
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
    RepositoryCheckService().run_repository_check(
        document_id=target.id, requester_user_id=student.id, existing_check_id=accepted["check_id"],
        db=db, auto_index_embeddings=False,
    )
    check = db.get(PlagiarismCheck, accepted["check_id"])
    assert check.status == "completed" and check.overall_similarity > 0.99
    assert "/highlights/" in check.highlighted_file_path
    watermark = WatermarkService(uploads)
    watermarked_ref = watermark.get_pdf(alumni.file_path)
    assert "/watermark-cache/" in watermarked_ref
    assert watermark.get_pdf(alumni.file_path) == watermarked_ref
    with storage.materialize(watermarked_ref) as path, pymupdf.open(path) as marked:
        assert len(marked[0].get_images()) == 1
    assert list(uploads.rglob("*.pdf")) == []


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
