"""Resuming imports must preserve private drafts, earlier scores and indexed chunks."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, DocumentChunk, PlagiarismCheck, User, utc_now
from app.services.document_structure_service import DocumentStructureService
from app.services.repository_import_files import ImportFile, ImportInputError, copy_pdf_atomically, discover_pdfs, ensure_separate_directories, pdf_identity
from app.services.repository_import_service import RepositoryImportService
from app.services.repository_policy_service import is_comparison_source
from test_document_embedding_service import ControlledEmbeddingService, MetadataDocumentEmbeddingService


def make_pdf(path, kind="proposal", marker="akademik"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as pdf:
        for chapter in range(1, 4 if kind == "proposal" else 6):
            page = pdf.new_page()
            roman = ["I", "II", "III", "IV", "V"][chapter - 1]
            page.insert_text((72, 72), f"BAB {roman}\n"
                             f"Sistem informasi {marker} membantu mahasiswa mengelola jadwal kuliah.\n"
                             "Penelitian perpustakaan menggunakan metode pengujian aplikasi kampus.")
        pdf.save(path)
    return path


class InlineStructure(DocumentStructureService):
    async def validate_pdf_structure_for_upload(self, path, kind):
        # Real parsing/validation of our generated PDFs; subprocess exercised separately.
        return self.validate_pdf_structure(path, kind)


@pytest.fixture
def context(tmp_path):
    root, uploads = tmp_path / "inbox", tmp_path / "uploads"
    root.mkdir()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        db.add_all([User(identifier="cihuy", password="unused", role="admin"),
                    User(identifier="student", password="unused", role="mahasiswa")])
        db.commit()
        provider = ControlledEmbeddingService()
        embeddings = MetadataDocumentEmbeddingService(provider)
        importer = RepositoryImportService(db, uploads, min_free_bytes=0, structure_service=InlineStructure(),
                                           embedding_service=embeddings)
        events = []
        yield SimpleNamespace(root=root, uploads=uploads, db=db, importer=importer,
                              provider=provider, embeddings=embeddings, events=events)
    engine.dispose()


def run(context, with_embeddings=False):
    return context.importer.run(discover_pdfs(context.root), with_embeddings=with_embeddings, on_event=context.events.append)


def rows(context):
    return [event for event in context.events if event["event"] == "file"]


def test_imports_only_ownerless_alumni_and_preserves_document_types(context):
    proposal = make_pdf(context.root / "proposal" / "Proposal A.PDF")
    make_pdf(context.root / "skripsi" / "Skripsi B.pdf", "skripsi")
    summary = run(context)
    assert summary["imported"] == 2 and summary["failed"] == 0
    documents = context.db.query(Document).order_by(Document.id).all()
    assert {document.document_type for document in documents} == {"proposal", "skripsi"}
    for document in documents:
        assert document.user_id is None and is_comparison_source(document)
        assert document.extracted_text and document.cleaned_text
        assert document.extraction_metadata["repository_import"]["admin_identifier"] == "cihuy"
        assert context.db.query(DocumentChunk).filter_by(document_id=document.id).count() > 0
        assert Path(document.file_path).is_relative_to(context.uploads)
    assert documents[0].content_sha256 == hashlib.sha256(proposal.read_bytes()).hexdigest()
    assert proposal.is_file()  # Input is copied, never moved/deleted.


def test_identical_files_and_repeated_runs_do_not_duplicate_or_reembed(context):
    first = make_pdf(context.root / "proposal" / "a.pdf")
    shutil.copyfile(first, first.with_name("b.pdf"))
    assert run(context, True)["imported"] == 1
    calls = context.provider.calls
    first_id = context.db.query(Document.id).scalar()
    context.events.clear()
    summary = run(context, True)
    assert summary["existing"] == 1 and summary["duplicate_in_input"] == 1
    assert summary["embedding_completed"] == 1 and summary["failed"] == 0
    assert context.provider.calls == calls
    assert context.db.query(Document).one().id == first_id
    assert len(list(context.uploads.rglob("*.pdf"))) == 1


def test_retry_indexes_only_failed_chunks_and_keeps_successful_vectors(context):
    make_pdf(context.root / "proposal" / "a.pdf")
    context.embeddings.fail_chunk_index = 1
    first = run(context, True)
    assert first["imported"] == 1 and first["embedding_failed"] == 1 and first["failed"] == 1
    document = context.db.query(Document).one()
    assert is_comparison_source(document)  # TF-IDF remains usable after indexing failure.
    identifier = document.id
    previous = {chunk.id: chunk.embedding_generated_at for chunk in context.db.query(DocumentChunk).all()
                if chunk.embedding_error is None}
    calls = context.provider.calls
    context.embeddings.fail_chunk_index = None
    context.events.clear()
    second = run(context, True)
    assert second["failed"] == 0 and second["embedding_completed"] == 1
    assert context.db.query(Document).one().id == identifier
    assert context.provider.calls == calls + 1
    for chunk in context.db.query(DocumentChunk).all():
        assert not chunk.embedding_error
        if chunk.id in previous:
            assert chunk.embedding_generated_at == previous[chunk.id]


def test_extraction_failure_can_resume_same_document_and_redacts_private_error(context, monkeypatch, caplog):
    make_pdf(context.root / "proposal" / "a.pdf")
    original = context.importer.texts.get_or_extract_text

    def broken(*_args, **_kwargs):
        raise RuntimeError("synthetic-private-service-detail")

    monkeypatch.setattr(context.importer.texts, "get_or_extract_text", broken)
    assert run(context)["failed"] == 1
    document = context.db.query(Document).one()
    identifier = document.id
    assert document.repository_status == "draft" and not is_comparison_source(document)
    assert "synthetic-private-service-detail" not in json.dumps(context.events) + caplog.text
    monkeypatch.setattr(context.importer.texts, "get_or_extract_text", original)
    assert run(context)["resumed"] == 1
    assert context.db.query(Document).one().id == identifier
    assert is_comparison_source(context.db.query(Document).one())


def test_interruption_resumes_without_new_document_or_pdf(context, monkeypatch):
    make_pdf(context.root / "proposal" / "a.pdf")
    original = context.importer.texts.get_or_extract_text

    def interrupt(*_args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(context.importer.texts, "get_or_extract_text", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(context)
    context.db.rollback()
    identifier = context.db.query(Document).one().id
    monkeypatch.setattr(context.importer.texts, "get_or_extract_text", original)
    assert run(context)["resumed"] == 1
    assert context.db.query(Document).one().id == identifier
    assert len(list(context.uploads.rglob("*.pdf"))) == 1


def test_soft_deleted_alumni_are_not_restored(context):
    make_pdf(context.root / "proposal" / "a.pdf")
    run(context)
    document = context.db.query(Document).one()
    document.deleted_at = utc_now()
    context.db.commit()
    deleted_at = document.deleted_at
    context.events.clear()
    assert run(context)["soft_deleted"] == 1
    assert context.db.query(Document).one().deleted_at == deleted_at
    assert rows(context)[0]["status"] == "skipped"


def test_matching_private_student_document_is_never_reused_or_published(context):
    path = make_pdf(context.root / "proposal" / "a.pdf")
    owner = context.db.query(User).filter_by(identifier="student").one()
    private = Document(user_id=owner.id, title="private.pdf", file_path=str(path),
                       content_sha256=pdf_identity(path, 10 * 1024 * 1024)[0], source_origin="student", repository_status="draft")
    context.db.add(private)
    context.db.commit()
    identifier, owner_id = private.id, owner.id
    assert run(context)["imported"] == 1
    assert context.db.query(Document).count() == 2
    private = context.db.get(Document, identifier)
    assert private.source_origin == "student" and private.repository_status == "draft" and private.user_id == owner_id


def test_legacy_duplicate_rows_are_backfilled_without_deleting_scores(context):
    path = make_pdf(context.root / "proposal" / "a.pdf")
    context.uploads.mkdir()
    legacy = context.uploads / "legacy.pdf"
    shutil.copyfile(path, legacy)
    first = Document(title="first", file_path=str(legacy), document_type="proposal", source_origin="alumni", repository_status="published")
    second = Document(title="second", file_path=str(legacy), document_type="proposal", source_origin="alumni", repository_status="published")
    context.db.add_all([first, second])
    context.db.flush()
    check = PlagiarismCheck(document_id=first.id, overall_similarity=.42, status="completed")
    context.db.add(check)
    context.db.commit()
    identifier, check_id = first.id, check.id
    assert run(context)["existing"] == 1
    assert context.db.query(Document).count() == 2
    assert all(document.content_sha256 for document in context.db.query(Document).all())
    assert context.db.get(PlagiarismCheck, check_id).overall_similarity == .42
    assert rows(context)[0]["document_id"] == identifier


def test_invalid_chapters_do_not_enter_repository(context):
    make_pdf(context.root / "skripsi" / "actually-proposal.pdf", "proposal")
    assert run(context)["failed"] == 1
    assert context.db.query(Document).count() == 0
    assert not list(context.uploads.rglob("*.pdf"))


@pytest.mark.parametrize("kind", ["inactive_admin", "student", "missing_admin", "unconfigured_embedding"])
def test_configuration_and_admin_are_checked_before_storing(context, kind):
    make_pdf(context.root / "proposal" / "a.pdf")
    if kind == "inactive_admin":
        context.db.query(User).filter_by(identifier="cihuy").update({"is_active": False})
        context.db.commit()
    elif kind == "student":
        context.importer.admin_identifier = "student"
    elif kind == "missing_admin":
        context.importer.admin_identifier = "absent"
    else:
        context.provider.api_key = ""
    with pytest.raises(ImportInputError):
        run(context, kind == "unconfigured_embedding")
    assert context.db.query(Document).count() == 0
    assert not context.uploads.exists()


def test_low_disk_stops_remaining_files_without_storing_them(context, monkeypatch):
    make_pdf(context.root / "proposal" / "a.pdf")
    make_pdf(context.root / "proposal" / "b.pdf", marker="berbeda")
    monkeypatch.setattr("app.services.repository_import_service.shutil.disk_usage", lambda _path: SimpleNamespace(free=1))
    result = run(context)
    assert result["processed"] == 1 and result["not_attempted"] == 1
    assert result["stopped_for_storage"] == 1
    assert rows(context)[0]["error_code"] == "disk_space_low"
    assert context.db.query(Document).count() == 0


def test_atomic_copy_detects_changed_input_and_reuses_completed_orphan(tmp_path):
    source = make_pdf(tmp_path / "inbox" / "a.pdf")
    expected, _ = pdf_identity(source, 10 * 1024 * 1024)
    target = tmp_path / "uploads" / "copy.pdf"
    assert copy_pdf_atomically(source, target, expected, 10 * 1024 * 1024) > 0
    assert copy_pdf_atomically(source, target, expected, 10 * 1024 * 1024) == 0
    target.unlink()
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ImportInputError, match="berubah"):
        copy_pdf_atomically(source, target, expected, 10 * 1024 * 1024)
    assert not target.exists() and not list(target.parent.glob("*.part"))


def test_preview_in_fresh_process_needs_no_database_and_writes_nothing(tmp_path):
    root = tmp_path / "inbox"
    source = make_pdf(root / "proposal" / "a.pdf")
    shutil.copyfile(source, source.with_name("duplicate.pdf"))
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    environment.pop("DATABASE_URL", None)
    # Disable dotenv explicitly so no developer private configuration is read.
    code = "from app.workers import import_repository as cli; cli.load_dotenv = lambda: None; raise SystemExit(cli.main())"
    result = subprocess.run([sys.executable, "-c", code, "--root", str(root), "--dry-run", "--with-embeddings"],
                            cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary["valid_unique"] == 1 and summary["duplicate_in_input"] == 1
    assert summary["database_checked"] is False and summary["embedding_executed"] is False
    assert before == {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}


def test_discovery_handles_nested_types_limit_and_unknown_files(tmp_path):
    make_pdf(tmp_path / "Proposal" / "2024" / "a.PDF")
    make_pdf(tmp_path / "other" / "b.pdf")
    files = discover_pdfs(tmp_path)
    assert files[0].document_type == "proposal" and files[1].document_type is None
    assert len(discover_pdfs(tmp_path, "skripsi", 1)) == 1
    assert discover_pdfs(tmp_path / "Proposal")[0].document_type == "proposal"
    with pytest.raises(ImportInputError):
        ensure_separate_directories(tmp_path, tmp_path / "uploads")


@pytest.mark.parametrize("content,limit,code", [(b"", 100, "empty_pdf"), (b"not PDF", 100, "invalid_pdf_signature"),
                                                (b"%PDF-1.7" + b"x" * 100, 10, "file_too_large")])
def test_invalid_file_identity_has_controlled_errors(tmp_path, content, limit, code):
    path = tmp_path / "a.pdf"
    path.write_bytes(content)
    with pytest.raises(ImportInputError) as error:
        pdf_identity(path, limit)
    assert error.value.code == code


@pytest.mark.parametrize("flags", [["--limit", "0"], ["--embedding-batch-size", "129"], ["--min-free-mb", "-1"],
                                  ["--dry-run", "--report", "report.jsonl"]])
def test_cli_rejects_invalid_limits_before_accessing_configuration(tmp_path, flags, monkeypatch):
    from app.workers import import_repository as cli
    monkeypatch.setattr(cli, "load_dotenv", lambda: pytest.fail("Configuration must not be accessed"))
    with pytest.raises(SystemExit) as error:
        cli.main(["--root", str(tmp_path), *flags])
    assert error.value.code == 2


def test_cli_internal_errors_are_redacted(tmp_path, monkeypatch, capsys, caplog):
    from app.workers import import_repository as cli

    def fail(_args):
        raise RuntimeError("synthetic-private-database-detail")

    monkeypatch.setattr(cli, "execute", fail)
    assert cli.main(["--root", str(tmp_path)]) == 2
    assert "synthetic-private-database-detail" not in capsys.readouterr().out + caplog.text
