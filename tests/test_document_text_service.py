from datetime import datetime, timezone

import pytest

from app.services.document_text_service import DocumentTextService
from app.services.pdf_service import PDF_EXTRACTION_VERSION


class FakeDocument:
    def __init__(self):
        self.file_path = "sample.pdf"
        self.document_type = "skripsi"
        self.extracted_text = None
        self.cleaned_text = None
        self.extraction_metadata = None
        self.text_extracted_at = None
        self.text_extraction_error = None


class FakeDb:
    def __init__(self):
        self.commits = 0

    def add(self, _item):
        pass

    def commit(self):
        self.commits += 1

    def refresh(self, _item):
        pass


class FakePdfService:
    def __init__(self):
        self.calls = 0

    def extract_text_from_pdf(self, file_path, document_type):
        self.calls += 1
        return {
            "full_text": "Sistem informasi akademik.",
            "total_pages": 2,
            "checked_pages_range": {
                "start_page": 1,
                "end_page": 2,
                "total_checked_pages": 2,
            },
            "chapter_validation": {
                "has_warning": False,
            },
        }


class FakeSimilarityService:
    def clean_text(self, text):
        return text.lower().replace(".", "")


def test_get_or_extract_text_uses_cached_text():
    doc = FakeDocument()
    doc.extracted_text = ""
    doc.cleaned_text = ""
    doc.text_extracted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    doc.extraction_metadata = {
        "chapter_validation": {"has_warning": False},
        "text_extraction_version": PDF_EXTRACTION_VERSION,
    }
    db = FakeDb()
    pdf_service = FakePdfService()
    service = DocumentTextService(
        pdf_service=pdf_service,
        similarity_service=FakeSimilarityService(),
    )

    result = service.get_or_extract_text(doc, db)

    assert result["full_text"] == ""
    assert result["cleaned_text"] == ""
    assert pdf_service.calls == 0
    assert db.commits == 0


def test_get_or_extract_text_extracts_and_stores_cache():
    doc = FakeDocument()
    db = FakeDb()
    pdf_service = FakePdfService()
    service = DocumentTextService(
        pdf_service=pdf_service,
        similarity_service=FakeSimilarityService(),
    )

    result = service.get_or_extract_text(doc, db)

    assert result["full_text"] == "Sistem informasi akademik."
    assert result["cleaned_text"] == "sistem informasi akademik"
    assert result["total_pages"] == 2
    assert doc.text_extracted_at is not None
    assert doc.text_extraction_error is None
    assert pdf_service.calls == 1
    assert db.commits == 1
    assert doc.extraction_metadata["text_extraction_version"] == PDF_EXTRACTION_VERSION


@pytest.mark.parametrize("version", [None, PDF_EXTRACTION_VERSION - 1])
def test_old_text_cache_refreshes_and_preserves_chunk_metadata(version):
    doc = FakeDocument()
    doc.extracted_text = "BAB I PENDAHULUAN 1.1 Latar Belakang Sistem informasi akademik."
    doc.cleaned_text = "bab pendahuluan latar belakang sistem informasi akademik"
    doc.text_extracted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    doc.extraction_metadata = {
        "chunk_extraction_version": PDF_EXTRACTION_VERSION,
        "custom_metadata": "retained",
    }
    if version is not None:
        doc.extraction_metadata["text_extraction_version"] = version
    pdf_service = FakePdfService()
    service = DocumentTextService(pdf_service=pdf_service, similarity_service=FakeSimilarityService())

    result = service.get_or_extract_text(doc, FakeDb())

    assert result["full_text"] == "Sistem informasi akademik."
    assert result["cleaned_text"] == "sistem informasi akademik"
    assert pdf_service.calls == 1
    assert doc.extraction_metadata["text_extraction_version"] == PDF_EXTRACTION_VERSION
    assert doc.extraction_metadata["chunk_extraction_version"] == PDF_EXTRACTION_VERSION
    assert doc.extraction_metadata["custom_metadata"] == "retained"


def test_failed_old_text_refresh_preserves_old_cache_but_never_returns_it():
    class FailingPdfService:
        def extract_text_from_pdf(self, *args, **kwargs):
            raise ValueError("PDF unavailable")

    doc = FakeDocument()
    doc.extracted_text = "BAB I PENDAHULUAN"
    doc.cleaned_text = "bab pendahuluan"
    doc.text_extracted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    doc.extraction_metadata = {"text_extraction_version": PDF_EXTRACTION_VERSION - 1}
    service = DocumentTextService(pdf_service=FailingPdfService())

    with pytest.raises(ValueError, match="PDF unavailable"):
        service.get_or_extract_text(doc, FakeDb())

    assert doc.extracted_text == "BAB I PENDAHULUAN"
    assert doc.cleaned_text == "bab pendahuluan"
    assert doc.extraction_metadata["text_extraction_version"] == PDF_EXTRACTION_VERSION - 1
    assert not service._has_cached_text(doc)
