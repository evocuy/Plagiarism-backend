import asyncio

from fastapi import HTTPException

from app.routes import documents as document_routes


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
