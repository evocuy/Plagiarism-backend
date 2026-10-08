import asyncio
from pathlib import Path

from fastapi import HTTPException

from app.routes import documents as document_routes
from app.services.document_structure_service import DocumentStructureService
from app.services.pdf_service import PDFService


def _metadata(*chapters):
    return {
        "detected_chapters": [
            {"chapter": chapter, "page": page}
            for page, chapter in enumerate(chapters, start=1)
        ]
    }


def test_proposal_requires_exactly_bab_1_through_bab_3():
    service = DocumentStructureService()

    result = service.validate_chapter_metadata(_metadata(1, 2, 3), "proposal")

    assert result["is_valid"] is True
    assert result["missing_chapters"] == []
    assert result["unexpected_chapters"] == []


def test_proposal_rejects_missing_chapter_even_when_three_are_detected():
    service = DocumentStructureService()

    result = service.validate_chapter_metadata(_metadata(1, 2, 4), "proposal")

    assert result["is_valid"] is False
    assert result["missing_chapters"] == [3]
    assert result["unexpected_chapters"] == [4]
    assert result["error_code"] == "INVALID_CHAPTER_STRUCTURE"


def test_pdf_detector_warns_when_a_required_chapter_is_replaced_by_another_one():
    class FakePage:
        def __init__(self, text):
            self.text = text

        def get_text(self):
            return self.text

    class FakeDocument:
        def __init__(self):
            self.pages = [FakePage("BAB I"), FakePage("BAB II"), FakePage("BAB IV")]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

    result = PDFService().detect_chapters_in_doc(FakeDocument(), "proposal")

    assert result["has_warning"] is True
    assert result["warning_type"] == "PROPOSAL_CHAPTER_OVERFLOW"


def test_proposal_rejects_chapter_overflow():
    service = DocumentStructureService()

    result = service.validate_chapter_metadata(_metadata(1, 2, 3, 4), "proposal")

    assert result["is_valid"] is False
    assert result["unexpected_chapters"] == [4]


def test_skripsi_requires_bab_1_through_bab_5_in_order():
    service = DocumentStructureService()

    valid = service.validate_chapter_metadata(_metadata(1, 2, 3, 4, 5), "skripsi")
    incomplete = service.validate_chapter_metadata(_metadata(1, 2, 3, 4), "skripsi")
    out_of_order = service.validate_chapter_metadata(_metadata(1, 2, 3, 5, 4), "skripsi")

    assert valid["is_valid"] is True
    assert incomplete["is_valid"] is False
    assert incomplete["missing_chapters"] == [5]
    assert out_of_order["is_valid"] is False


def test_skripsi_allows_additional_chapters_after_bab_5():
    service = DocumentStructureService()

    result = service.validate_chapter_metadata(_metadata(1, 2, 3, 4, 5, 6), "skripsi")

    assert result["is_valid"] is True
    assert result["unexpected_chapters"] == [6]


def test_scanned_pdf_without_extractable_text_is_rejected():
    class BlankPdfService:
        def extract_text_from_pdf(self, *_args, **_kwargs):
            return {"full_text": "", "chapter_validation": {"detected_chapters": []}}

    service = DocumentStructureService(pdf_service=BlankPdfService())

    result = service.validate_pdf_structure("scanned.pdf", "proposal")

    assert result["is_valid"] is False
    assert result["error_code"] == "PDF_TEXT_NOT_EXTRACTABLE"


def test_chapter_detector_accepts_common_heading_punctuation_and_finds_overflow():
    class FakePage:
        def __init__(self, text):
            self.text = text

        def get_text(self):
            return self.text

    class FakeDocument:
        def __init__(self, pages):
            self.pages = [FakePage(page) for page in pages]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

    document = FakeDocument(
        [
            "BAB I. PENDAHULUAN",
            "BAB II: TINJAUAN PUSTAKA",
            "BAB III - METODOLOGI",
            "BAB VII PENUTUP TAMBAHAN",
        ]
    )

    result = PDFService().detect_chapters_in_doc(document, "proposal")

    assert [item["chapter"] for item in result["detected_chapters"]] == [1, 2, 3, 7]
    assert result["warning_type"] == "PROPOSAL_CHAPTER_OVERFLOW"


def test_invalid_upload_is_deleted_before_a_document_row_is_created(monkeypatch):
    class FakeUpload:
        filename = "proposal.pdf"
        content_type = "application/pdf"

        def __init__(self):
            self._chunks = [b"not-important-for-this-test", b""]

        async def read(self, _size):
            return self._chunks.pop(0)

    class FakeStructureService:
        seen_file_path = None

        @staticmethod
        def normalize_document_type(document_type):
            return document_type

        def validate_pdf_structure(self, file_path, _document_type):
            assert Path(file_path).exists()
            self.seen_file_path = Path(file_path)
            return {
                "is_valid": False,
                "error_code": "INVALID_CHAPTER_STRUCTURE",
                "message": "Struktur Proposal tidak sesuai.",
            }

    class FakeDb:
        def __init__(self):
            self.added = []
            self.rollback_count = 0

        def add(self, value):
            self.added.append(value)

        def rollback(self):
            self.rollback_count += 1

    db = FakeDb()
    structure_service = FakeStructureService()
    monkeypatch.setattr(document_routes, "document_structure_service", structure_service)

    # The test sandbox prevents Python from creating arbitrary temporary
    # directories, so use the application's writable upload directory.  The
    # random storage filename and final assertion keep this isolated.
    try:
        asyncio.run(
            document_routes._store_uploaded_document(
                file=FakeUpload(),
                document_type="proposal",
                user_id=1,
                db=db,
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 422
        assert exc.detail == "Struktur Proposal tidak sesuai."
    else:
        raise AssertionError("Expected invalid structure upload to be rejected")

    assert db.added == []
    assert db.rollback_count == 1
    assert structure_service.seen_file_path is not None
    assert not structure_service.seen_file_path.exists()
