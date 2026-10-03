from datetime import datetime, timezone

from app.services.document_text_service import DocumentTextService


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
    doc.extraction_metadata = {"chapter_validation": {"has_warning": False}}
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
