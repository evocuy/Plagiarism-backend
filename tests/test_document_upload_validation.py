import asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fastapi import HTTPException

from app.routes import documents as document_routes
from app.database.session import Base
from app.models.schemas import User


def test_filename_allows_normal_document_naming():
    filename = "SEMINAR INSTIKI (INFORMATIKA) Revisi-1.pdf"

    assert document_routes._validate_uploaded_filename(filename) == filename


def test_filename_rejects_shell_like_special_characters():
    for filename in (
        "proposal%final.pdf",
        "proposal&final.pdf",
        "proposal*final.pdf",
        "proposal$final.pdf",
        "../proposal.pdf",
        "..\\proposal.pdf",
    ):
        try:
            document_routes._validate_uploaded_filename(filename)
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "Nama file" in exc.detail
        else:
            raise AssertionError(f"Expected {filename} to be rejected")


def test_empty_pdf_is_rejected_before_a_document_or_check_can_be_created(monkeypatch):
    class EmptyUpload:
        filename = "proposal-valid.pdf"
        content_type = "application/pdf"

        async def read(self, _size):
            return b""

    class FakeStructureService:
        @staticmethod
        def normalize_document_type(document_type):
            return document_type

        @staticmethod
        def validate_pdf_structure(*_args, **_kwargs):
            raise AssertionError("Structure validation must not run for an empty file")

    class FakeDb:
        def __init__(self):
            self.added = []
            self.rollback_count = 0

        def add(self, value):
            self.added.append(value)

        def rollback(self):
            self.rollback_count += 1

    db = FakeDb()
    monkeypatch.setattr(document_routes, "document_structure_service", FakeStructureService())

    try:
        asyncio.run(
            document_routes._store_uploaded_document(
                file=EmptyUpload(),
                document_type="proposal",
                user_id=1,
                db=db,
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "File PDF tidak boleh kosong."
    else:
        raise AssertionError("Expected an empty PDF to be rejected")

    assert db.added == []
    assert db.rollback_count == 1


def test_only_ownerless_alumni_import_can_be_published(tmp_path, monkeypatch):
    class Upload:
        filename = "document.pdf"
        content_type = "application/pdf"

        def __init__(self):
            self.content = b"temporary-pdf"

        async def read(self, _size):
            content, self.content = self.content, b""
            return content

    class Structure:
        @staticmethod
        def normalize_document_type(value):
            return value

        @staticmethod
        async def validate_pdf_structure_for_upload(*_args):
            return {"is_valid": True}

    monkeypatch.setattr(document_routes, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(document_routes, "document_structure_service", Structure())
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        student = User(identifier="student", password="unused", role="mahasiswa")
        db.add(student)
        db.commit()
        alumni = asyncio.run(document_routes._store_uploaded_document(
            file=Upload(), document_type="proposal", user_id=None,
            source_origin="alumni", repository_status="published", db=db,
        ))
        assert alumni.source_origin == "alumni"
        assert alumni.repository_status == "published"
        assert alumni.repository_published_at is not None
        assert alumni.user_id is None
        owned = asyncio.run(document_routes._store_uploaded_document(
            file=Upload(), document_type="proposal", user_id=student.id,
            source_origin="alumni", repository_status="published", db=db,
        ))
        assert owned.source_origin == "student"
        assert owned.repository_status == "draft"
        assert owned.repository_published_at is None
    engine.dispose()


def test_removed_role_cannot_upload_a_check():
    retired = User(identifier="retired", password="unused", role="dosen")
    try:
        asyncio.run(document_routes.upload_and_check_document(
            file=None, document_type="proposal", current_user=retired, db=None,
        ))
    except HTTPException as error:
        assert error.status_code == 403
    else:
        raise AssertionError("Removed role must be rejected before accepting an upload")
