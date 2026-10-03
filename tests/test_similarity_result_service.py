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
