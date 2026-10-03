from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document
from app.services.document_chunk_service import DocumentChunkService


class FakePdfService:
    def __init__(self):
        self.calls = 0

    def extract_sentences_with_pages(self, file_path, document_type):
        self.calls += 1
        return [
            {
                "page": 1,
                "sentence": "Sistem informasi akademik digunakan untuk mengelola data mahasiswa.",
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
