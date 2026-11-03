from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, DocumentChunk
from app.services.document_chunk_service import DocumentChunkService
from app.services.pdf_service import PDF_EXTRACTION_VERSION


class FakePdfService:
    def __init__(self):
        self.calls = 0

    def extract_sentences_with_pages(self, file_path, document_type):
        self.calls += 1
        return [
            {
                "page": 1,
                "sentence": "Sistem informasi akademik digunakan untuk mengelola data mahasiswa.",
                "page_start_position": 20,
                "page_end_position": 84,
            },
            {
                "page": 2,
                "sentence": "Kalimat kedua ini menjadi chunk berikutnya untuk pengujian.",
            },
        ]


class FakeSimilarityService:
    def clean_text(self, text):
        return text.lower().replace(".", "")


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()


def test_get_or_build_sentence_chunks_stores_and_reuses_cache():
    db = _make_session()
    doc = Document(
        title="sample.pdf",
        document_type="skripsi",
        file_path="sample.pdf",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    pdf_service = FakePdfService()
    service = DocumentChunkService(
        pdf_service=pdf_service,
        similarity_service=FakeSimilarityService(),
    )

    first_chunks = service.get_or_build_sentence_chunks(doc, db)
    second_chunks = service.get_or_build_sentence_chunks(doc, db)

    assert len(first_chunks) == 2
    assert len(second_chunks) == 2
    assert first_chunks[0]["page"] == 1
    assert first_chunks[0]["chunk_index"] == 0
    assert first_chunks[0]["clean"] == "sistem informasi akademik digunakan untuk mengelola data mahasiswa"
    assert first_chunks[0]["page_start_position"] == 20
    assert first_chunks[0]["page_end_position"] == 84
    assert second_chunks[0]["page_start_position"] == 20
    assert second_chunks[0]["page_end_position"] == 84
    assert doc.extraction_metadata["chunk_extraction_version"] == PDF_EXTRACTION_VERSION
    assert pdf_service.calls == 1


def test_to_reference_corpus_preserves_source_name():
    service = DocumentChunkService()

    corpus = service.to_reference_corpus(
        chunks=[
            {
                "clean": "sistem informasi akademik",
                "sentence": "Sistem informasi akademik.",
            }
        ],
        matched_source="Dokumen Repository",
    )

    assert corpus == [
            {
                "clean": "sistem informasi akademik",
                "sentence": "Sistem informasi akademik.",
                "chapter": None,
                "matched_source": "Dokumen Repository",
                "source_document_id": None,
                "source_chunk_index": None,
                "source_page": None,
            }
    ]


def _old_chunk_document(db, version=None):
    metadata = {"text_extraction_version": PDF_EXTRACTION_VERSION, "custom_metadata": "retained"}
    if version is not None:
        metadata["chunk_extraction_version"] = version
    doc = Document(
        title="sample.pdf", document_type="skripsi", file_path="sample.pdf",
        extraction_metadata=metadata,
    )
    db.add(doc)
    db.flush()
    db.add(DocumentChunk(
        document_id=doc.id, chunk_type="sentence", chunk_index=0, page_number=1,
        raw_text="BAB I PENDAHULUAN Latar Belakang", cleaned_text="bab pendahuluan latar belakang",
        embedding_model="old-embedding", embedding_generated_at=datetime(2025, 1, 1),
    ))
    db.commit()
    return doc


@pytest.mark.parametrize("version", [None, PDF_EXTRACTION_VERSION - 1])
def test_old_chunks_refresh_and_invalidate_old_embeddings(version):
    with _make_session() as db:
        doc = _old_chunk_document(db, version)
        service = DocumentChunkService(pdf_service=FakePdfService(), similarity_service=FakeSimilarityService())

        chunks = service.get_or_build_sentence_chunks(doc, db)

        rows = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).all()
        assert len(chunks) == len(rows) == 2
        assert all("BAB" not in row.raw_text for row in rows)
        assert all(row.embedding_model is None and row.embedding_generated_at is None for row in rows)
        assert doc.extraction_metadata["chunk_extraction_version"] == PDF_EXTRACTION_VERSION
        assert doc.extraction_metadata["text_extraction_version"] == PDF_EXTRACTION_VERSION
        assert doc.extraction_metadata["custom_metadata"] == "retained"


@pytest.mark.parametrize("refresh_cache", [False, True])
def test_failed_chunk_extraction_preserves_previous_rows(refresh_cache):
    class FailingPdfService:
        def extract_sentences_with_pages(self, *args, **kwargs):
            raise ValueError("PDF unavailable")

    with _make_session() as db:
        doc = _old_chunk_document(db)
        service = DocumentChunkService(pdf_service=FailingPdfService())

        with pytest.raises(ValueError, match="PDF unavailable"):
            service.get_or_build_sentence_chunks(doc, db, refresh_cache=refresh_cache)

        row = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).one()
        assert row.raw_text == "BAB I PENDAHULUAN Latar Belakang"
        assert row.embedding_model == "old-embedding"
        assert "chunk_extraction_version" not in doc.extraction_metadata


def test_failed_chunk_commit_rolls_back_cache_replacement(monkeypatch):
    with _make_session() as db:
        doc = _old_chunk_document(db)
        service = DocumentChunkService(pdf_service=FakePdfService(), similarity_service=FakeSimilarityService())

        def fail_commit():
            raise RuntimeError("commit unavailable")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit unavailable"):
            service.get_or_build_sentence_chunks(doc, db)

        row = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).one()
        assert row.raw_text == "BAB I PENDAHULUAN Latar Belakang"
        assert row.embedding_model == "old-embedding"
        assert "chunk_extraction_version" not in doc.extraction_metadata


def test_empty_current_chunk_cache_is_reused():
    class EmptyPdfService:
        calls = 0

        def extract_sentences_with_pages(self, *args, **kwargs):
            self.calls += 1
            return []

    with _make_session() as db:
        doc = _old_chunk_document(db)
        pdf_service = EmptyPdfService()
        service = DocumentChunkService(pdf_service=pdf_service)

        assert service.get_or_build_sentence_chunks(doc, db) == []
        assert service.get_or_build_sentence_chunks(doc, db) == []
        assert pdf_service.calls == 1
        assert db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).count() == 0
        assert doc.extraction_metadata["chunk_extraction_version"] == PDF_EXTRACTION_VERSION


def test_reference_corpus_preserves_page_offsets_when_available():
    service = DocumentChunkService()
    corpus = service.to_reference_corpus(
        [{"clean": "sistem informasi akademik", "sentence": "Sistem informasi akademik.",
          "page_start_position": 31, "page_end_position": 56}],
        matched_source="Repository",
    )
    assert corpus[0]["source_page_start_position"] == 31
    assert corpus[0]["source_page_end_position"] == 56
