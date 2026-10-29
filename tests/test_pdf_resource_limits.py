import asyncio
import subprocess

import pymupdf
import pytest
from fastapi import HTTPException
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.routes import documents as document_routes
from app.services import document_structure_service as structure_module
from app.services.document_structure_service import DocumentStructureService
from app.services.pdf_limits import PDFLimitExceeded, PDFProcessingLimits
from app.services.pdf_service import PDFService
from app.services.tfidf_service import TfidfService


class FakePage:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def get_text(self):
        self.calls += 1
        return self.text


class FakeDocument:
    def __init__(self, texts):
        self.pages = [FakePage(text) for text in texts]

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_page_limit_is_checked_before_any_page_parsing():
    document = FakeDocument(["a", "b", "c"])
    service = PDFService(PDFProcessingLimits(max_pages=2))

    with pytest.raises(PDFLimitExceeded, match="2 halaman"):
        service._read_page_texts(document)

    assert [page.calls for page in document.pages] == [0, 0, 0]


def test_text_limit_stops_cumulative_extraction():
    document = FakeDocument(["first", "second", "not reached"])
    service = PDFService(PDFProcessingLimits(max_text_chars=8))

    with pytest.raises(PDFLimitExceeded, match="Teks PDF"):
        service._read_page_texts(document)

    assert [page.calls for page in document.pages] == [1, 1, 0]


def test_chapter_detection_and_content_filter_reuse_one_extraction(tmp_path, monkeypatch):
    document = FakeDocument([
        "BAB I\nSistem informasi akademik membantu mahasiswa menyusun naskah.",
        "BAB II\nDokumen repositori digunakan sebagai sumber pembanding internal.",
        "BAB III\nPengujian menghitung nilai kemiripan berdasarkan teks dokumen.",
    ])
    file_path = tmp_path / "proposal.pdf"
    file_path.touch()
    monkeypatch.setattr(pymupdf, "open", lambda _path: document)

    extracted = PDFService().extract_text_from_pdf(str(file_path), document_type="proposal")

    assert extracted["total_pages"] == 3
    assert extracted["chapter_validation"]["total_chapters_detected"] == 3
    assert [page.calls for page in document.pages] == [1, 1, 1]


def test_sentence_count_limit_rejects_excess_chunks(monkeypatch):
    service = PDFService(PDFProcessingLimits(max_chunks=1))
    monkeypatch.setattr(service, "extract_text_from_pdf", lambda *_args, **_kwargs: {
        "chapter_validation": {"detected_chapters": []},
        "pages": [{"page": 1, "text": "Sistem informasi akademik membantu mahasiswa. Dokumen pembanding berasal dari repositori kampus."}],
    })

    with pytest.raises(PDFLimitExceeded, match="potongan teks"):
        service.extract_sentences_with_pages("unused.pdf")


def test_isolated_validation_accepts_a_text_based_proposal(tmp_path):
    file_path = tmp_path / "proposal.pdf"
    with pymupdf.open() as document:
        for chapter in ("I", "II", "III"):
            page = document.new_page()
            page.insert_text((72, 72), f"BAB {chapter}\nDokumen penelitian ini memiliki teks untuk pemeriksaan.")
        document.save(str(file_path))

    result = asyncio.run(DocumentStructureService().validate_pdf_structure_for_upload(file_path, "proposal"))

    assert result["is_valid"] is True
    assert result["detected_chapters"] == [1, 2, 3]


def test_validation_timeout_and_busy_capacity_return_safe_errors(monkeypatch):
    monkeypatch.setenv("PDF_VALIDATION_MAX_CONCURRENT", "2")
    service = DocumentStructureService()

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("pdf-validator", service.validation_timeout)

    monkeypatch.setattr(structure_module.subprocess, "run", timeout)
    result = service._validate_in_subprocess("private/path.pdf", "proposal")
    assert result["error_code"] == "PDF_VALIDATION_TIMEOUT"
    assert "private" not in result["message"]
    # The failed child must release its admission slot.
    assert service._validate_in_subprocess("private/path.pdf", "proposal")["error_code"] == "PDF_VALIDATION_TIMEOUT"

    assert service._validation_slots.acquire(blocking=False)
    assert service._validation_slots.acquire(blocking=False)
    try:
        assert service._validate_in_subprocess("unused.pdf", "proposal")["error_code"] == "PDF_VALIDATION_BUSY"
    finally:
        service._validation_slots.release()
        service._validation_slots.release()


def test_reset_is_disabled_by_default_and_always_disabled_in_production(monkeypatch):
    class UntouchedDb:
        def execute(self, *_args):
            raise AssertionError("A disabled reset must not access the database")

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("ENABLE_DESTRUCTIVE_RESET", raising=False)
    with pytest.raises(HTTPException) as result:
        document_routes.clear_all_documents(admin=None, db=UntouchedDb())
    assert result.value.status_code == 404

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENABLE_DESTRUCTIVE_RESET", "true")
    with pytest.raises(HTTPException) as result:
        document_routes.clear_all_documents(admin=None, db=UntouchedDb())
    assert result.value.status_code == 404


def test_batched_tfidf_matches_full_matrix_scores(monkeypatch):
    corpus = ["sistem informasi akademik", "analisis beton bangunan", "informasi akademik mahasiswa"]
    queries = ["informasi mahasiswa", "beton bangunan", "akademik sistem"]
    vectorizer = TfidfVectorizer()
    corpus_matrix = vectorizer.fit_transform(corpus)
    expected = cosine_similarity(vectorizer.transform(queries), corpus_matrix)
    monkeypatch.setenv("TFIDF_SIMILARITY_BATCH_CELLS", "3")

    matches = TfidfService().find_top_matches(queries, corpus, top_n=3)

    for row, row_matches in zip(expected, matches):
        assert [match["index"] for match in row_matches] == list(row.argsort()[::-1])
        for match in row_matches:
            assert match["score"] == pytest.approx(row[match["index"]])
