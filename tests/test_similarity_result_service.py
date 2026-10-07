from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, PlagiarismCheck, SimilarityMatch, SimilarityResult
from app.services.similarity_result_service import SimilarityResultService


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()


def test_create_result_and_matches_persists_similarity_detail():
    db = _make_session()
    target = Document(title="target.pdf", document_type="skripsi", file_path="target.pdf")
    source = Document(title="source.pdf", document_type="skripsi", file_path="source.pdf")
    db.add_all([target, source])
    db.commit()
    db.refresh(target)
    db.refresh(source)

    check = PlagiarismCheck(
        document_id=target.id,
        user_id=None,
        overall_similarity=0.82,
        status="completed",
    )
    db.add(check)
    db.commit()
    db.refresh(check)

    service = SimilarityResultService()
    result = service.create_result(
        db=db,
        check_id=check.id,
        source_document_id=source.id,
        similarity_score=0.82,
    )
    count = service.create_matches(
        db=db,
        result_id=result.id,
        matches=[
            {
                "sentence": "Kalimat target mirip.",
                "reference_sentence": "Kalimat sumber mirip.",
                "similarity": 91.5,
                "page": 7,
            }
        ],
    )

    stored_result = db.query(SimilarityResult).filter(SimilarityResult.id == result.id).first()
    stored_match = db.query(SimilarityMatch).filter(SimilarityMatch.result_id == result.id).first()

    assert count == 1
    assert stored_result.source_document_id == source.id
    assert stored_result.similarity_score == 0.82
    assert stored_match.source_text == "Kalimat sumber mirip."
    assert stored_match.submitted_text == "Kalimat target mirip."
    assert stored_match.similarity_score == 0.915
    assert stored_match.page_number == 7


def test_summarize_highlight_matches_reports_words_pages_and_sources():
    service = SimilarityResultService()

    summary = service.summarize_highlight_matches(
        [
            {
                "sentence": "Sistem informasi akademik",
                "matched_source": "Sumber A.pdf",
                "source_document_id": 11,
                "page": 3,
                "start_position": 0,
                "end_position": 26,
            },
            {
                # Target segment yang sama muncul terhadap sumber lain. PDF
                # hanya memberi satu stabilo untuk segment ini.
                "sentence": "Sistem informasi akademik",
                "matched_source": "Sumber B.pdf",
                "source_document_id": 12,
                "page": 3,
                "start_position": 0,
                "end_position": 26,
            },
            {
                "sentence": "data mahasiswa",
                "matched_source": "Sumber A.pdf",
                "source_document_id": 11,
                "page": 5,
                "start_position": 30,
                "end_position": 45,
            },
        ]
    )

    assert summary["detected_match_count"] == 3
    assert summary["detected_word_count"] == 8
    assert summary["highlighted_match_count"] == 2
    assert summary["highlighted_word_count"] == 5
    assert summary["highlighted_page_numbers"] == [3, 5]
    assert summary["highlighted_page_count"] == 2
    assert summary["source_documents"] == [
        {
            "document_id": 11,
            "title": "Sumber A.pdf",
            "match_count": 2,
            "matched_word_count": 5,
            "page_numbers": [3, 5],
            "page_count": 2,
        },
        {
            "document_id": 12,
            "title": "Sumber B.pdf",
            "match_count": 1,
            "matched_word_count": 3,
            "page_numbers": [3],
            "page_count": 1,
        },
    ]
