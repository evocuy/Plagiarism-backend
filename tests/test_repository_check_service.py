import asyncio

from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from app.database.session import Base
from app.models.schemas import Document, PlagiarismCheck, SimilarityMatch, SimilarityResult, User
from app.routes.plagiarism import (
    get_check_history,
    get_check_matches,
    get_check_processing_status,
    get_check_top_matches,
)
from app.routes import documents as document_routes
from app.services.repository_check_service import (
    RepositoryCheckService,
    run_repository_check_in_background,
)


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine)
    return session_local()


class FakeTextService:
    def __init__(self, error=None):
        self.error = error

    def get_or_extract_text(self, _document, _db):
        if self.error:
            raise self.error
        return {
            "cleaned_text": "sistem informasi akademik",
            "chapter_validation": {"has_warning": False},
        }


class FakeChunkService:
    def get_or_build_sentence_chunks(self, _document, _db):
        return [
            {
                "sentence": "Sistem informasi akademik.",
                "clean": "sistem informasi akademik",
                "page": 1,
            }
        ]


class FakePdfService:
    def generate_highlighted_pdf(self, **_kwargs):
        return ""


class FakeEmbeddingEngine:
    model = "test-model"

    @staticmethod
    def is_configured():
        return False


class FakeEmbeddingService:
    embedding_service = FakeEmbeddingEngine()


def _make_service(text_service):
    return RepositoryCheckService(
        pdf_service=FakePdfService(),
        document_text_service=text_service,
        document_chunk_service=FakeChunkService(),
        document_embedding_service=FakeEmbeddingService(),
    )


def _create_pending_check(db):
    document = Document(
        title="proposal.pdf",
        document_type="proposal",
        file_path="proposal.pdf",
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    check = PlagiarismCheck(
        document_id=document.id,
        overall_similarity=0.0,
        status="pending",
        progress=0,
        processing_stage="queued",
    )
    db.add(check)
    db.commit()
    db.refresh(check)
    return document, check


def test_repository_check_reuses_provisional_check_and_completes_progress():
    db = _make_session()
    document, provisional_check = _create_pending_check(db)
    progress_events = []

    result = _make_service(FakeTextService()).run_repository_check(
        document_id=document.id,
        existing_check_id=provisional_check.id,
        requester_user_id=None,
        db=db,
        auto_index_embeddings=False,
        report_progress=lambda progress, stage, _message: progress_events.append((progress, stage)),
    )

    db.refresh(provisional_check)
    assert result["check_id"] == provisional_check.id
    assert provisional_check.status == "completed"
    assert provisional_check.progress == 100
    assert provisional_check.processing_stage == "completed"
    assert provisional_check.completed_at is not None
    assert [progress for progress, _stage in progress_events] == [5, 15, 28, 35, 96, 100]


def test_repository_check_marks_provisional_check_failed_without_exposing_raw_error():
    db = _make_session()
    document, provisional_check = _create_pending_check(db)

    try:
        _make_service(FakeTextService(error=RuntimeError("internal parser detail"))).run_repository_check(
            document_id=document.id,
            existing_check_id=provisional_check.id,
            requester_user_id=None,
            db=db,
            auto_index_embeddings=False,
        )
    except Exception:
        pass
    else:
        raise AssertionError("Expected repository check to fail")

    db.refresh(provisional_check)
    assert provisional_check.status == "failed"
    assert provisional_check.processing_stage == "failed"
    assert provisional_check.error_message == "Pengecekan gagal diproses. Silakan coba lagi."
    assert "internal parser detail" not in provisional_check.error_message


def test_background_runner_opens_and_closes_its_own_session():
    calls = []

    class FakeSession:
        def close(self):
            calls.append("closed")

    class FakeService:
        def run_repository_check(self, **kwargs):
            calls.append(kwargs)

    run_repository_check_in_background(
        document_id=7,
        check_id=11,
        requester_user_id=3,
        auto_index_embeddings=False,
        session_factory=FakeSession,
        service_factory=FakeService,
    )

    assert calls[0]["document_id"] == 7
    assert calls[0]["existing_check_id"] == 11
    assert calls[0]["requester_user_id"] == 3
    assert calls[-1] == "closed"


def test_visual_highlight_deduplication_keeps_one_annotation_per_target_segment():
    matches = [
        {
            "page": 13,
            "sentence": "Kalimat yang sama persis.",
            "start_position": 0,
            "end_position": 25,
            "source_document_id": 4,
        },
        {
            "page": 13,
            "sentence": "Kalimat yang sama persis.",
            "start_position": 0,
            "end_position": 25,
            "source_document_id": 6,
        },
    ]

    visual_matches = RepositoryCheckService._deduplicate_visual_highlight_matches(matches)

    assert visual_matches == [matches[0]]


def test_status_endpoint_returns_progress_only_to_the_check_owner():
    db = _make_session()
    owner = User(identifier="owner", password="secret", role="mahasiswa")
    other_student = User(identifier="other", password="secret", role="mahasiswa")
    db.add_all([owner, other_student])
    db.commit()
    db.refresh(owner)
    db.refresh(other_student)

    document = Document(
        user_id=owner.id,
        title="proposal.pdf",
        document_type="proposal",
        file_path="proposal.pdf",
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    check = PlagiarismCheck(
        document_id=document.id,
        user_id=owner.id,
        overall_similarity=0.0,
        status="processing",
        progress=55,
        processing_stage="comparing_documents",
        processing_message="Membandingkan dokumen 1 dari 2.",
    )
    db.add(check)
    db.commit()
    db.refresh(check)

    payload = get_check_processing_status(check.id, current_user=owner, db=db)
    assert payload["progress"] == 55
    assert payload["stage"] == "comparing_documents"
    assert payload["is_finished"] is False

    try:
        get_check_processing_status(check.id, current_user=other_student, db=db)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Expected another student to be denied")


def test_matches_endpoint_returns_final_payload_after_progress_completes():
    db = _make_session()
    owner = User(identifier="owner", password="secret", role="mahasiswa")
    source_owner = User(identifier="source-owner", password="secret", role="mahasiswa")
    db.add_all([owner, source_owner])
    db.commit()
    db.refresh(owner)
    db.refresh(source_owner)

    target = Document(
        user_id=owner.id,
        title="target.pdf",
        document_type="proposal",
        file_path="target.pdf",
        extraction_metadata={
            "chapter_validation": {
                "has_warning": False,
                "detected_chapters": [{"chapter": 1, "page": 1}],
            }
        },
    )
    source = Document(
        user_id=source_owner.id,
        title="source.pdf",
        document_type="proposal",
        file_path="source.pdf",
    )
    db.add_all([target, source])
    db.commit()
    db.refresh(target)
    db.refresh(source)

    check = PlagiarismCheck(
        document_id=target.id,
        user_id=owner.id,
        overall_similarity=0.816,
        status="completed",
        progress=100,
        processing_stage="completed",
        processing_message="Pengecekan kemiripan selesai.",
    )
    db.add(check)
    db.commit()
    db.refresh(check)

    result = SimilarityResult(
        check_id=check.id,
        source_document_id=source.id,
        similarity_score=0.816,
    )
    db.add(result)
    db.commit()
    db.refresh(result)
    db.add(
        SimilarityMatch(
            result_id=result.id,
            source_text="Kalimat sumber yang sama.",
            submitted_text="Kalimat target yang sama.",
            similarity_score=0.915,
            page_number=3,
            start_position=0,
            end_position=25,
        )
    )
    db.commit()

    payload = get_check_matches(check.id, current_user=owner, db=db)

    assert payload["status"] == "completed"
    assert payload["target_document"] == "target.pdf"
    assert payload["highest_similarity_percentage"] == "81.6%"
    assert payload["total_repository_checked"] == 1
    assert payload["total_repository_available"] == 1
    assert payload["matches"] == [
        {
            "repository_document_id": source.id,
            "title": "source.pdf",
            "similarity_score": 0.816,
            "similarity_percentage": "81.6%",
        }
    ]
    assert payload["total_plagiarized_sentences"] == 1
    assert payload["highlight_summary"]["highlighted_match_count"] == 1
    assert payload["highlight_summary"]["highlighted_page_numbers"] == [3]
    assert payload["highlight_summary"]["source_documents"][0]["title"] == "source.pdf"
    assert payload["chapter_validation"]["has_warning"] is False
    assert payload["results"][0]["matches"][0]["submitted_text"] == "Kalimat target yang sama."


def test_top_matches_endpoint_returns_only_three_highest_sources_for_history():
    db = _make_session()
    owner = User(identifier="owner", password="secret", role="mahasiswa")
    outsider = User(identifier="outsider", password="secret", role="mahasiswa")
    db.add_all([owner, outsider])
    db.commit()
    db.refresh(owner)
    db.refresh(outsider)

    target = Document(
        user_id=owner.id,
        title="target.pdf",
        document_type="skripsi",
        file_path="target.pdf",
    )
    sources = [
        Document(title=f"source-{number}.pdf", document_type="skripsi", file_path=f"source-{number}.pdf")
        for number in range(1, 5)
    ]
    db.add_all([target, *sources])
    db.commit()
    db.refresh(target)
    for source in sources:
        db.refresh(source)

    check = PlagiarismCheck(
        document_id=target.id,
        user_id=owner.id,
        overall_similarity=0.91,
        status="completed",
        progress=100,
    )
    db.add(check)
    db.commit()
    db.refresh(check)

    scores = [0.32, 0.91, 0.65, 0.80]
    for source, score in zip(sources, scores):
        result = SimilarityResult(
            check_id=check.id,
            source_document_id=source.id,
            similarity_score=score,
        )
        db.add(result)
        db.flush()
        db.add(
            SimilarityMatch(
                result_id=result.id,
                source_text="Kalimat sumber.",
                submitted_text="Kalimat target.",
                similarity_score=0.9,
                page_number=2,
            )
        )
    db.commit()

    payload = get_check_top_matches(check.id, current_user=owner, db=db)
    history = get_check_history(current_user=owner, db=db)

    assert payload["total_repository_checked"] == 4
    assert payload["top_match_count"] == 3
    assert [item["title"] for item in payload["top_matches"]] == [
        "source-2.pdf",
        "source-4.pdf",
        "source-3.pdf",
    ]
    assert [item["rank"] for item in payload["top_matches"]] == [1, 2, 3]
    assert payload["top_matches"][0]["matched_sentence_count"] == 1
    assert payload["top_matches"][0]["matched_page_numbers"] == [2]
    assert history[0]["top_matches_url"] == f"/api/plagiarism/check/{check.id}/top-matches"

    empty_check = PlagiarismCheck(
        document_id=target.id,
        user_id=owner.id,
        overall_similarity=0.0,
        status="completed",
        progress=100,
    )
    processing_check = PlagiarismCheck(
        document_id=target.id,
        user_id=owner.id,
        overall_similarity=0.0,
        status="processing",
        progress=50,
    )
    db.add_all([empty_check, processing_check])
    db.commit()
    db.refresh(empty_check)
    db.refresh(processing_check)

    empty_payload = get_check_top_matches(empty_check.id, current_user=owner, db=db)
    history_by_id = {
        item["id"]: item for item in get_check_history(current_user=owner, db=db)
    }
    assert empty_payload["top_match_count"] == 0
    assert empty_payload["top_matches"] == []
    assert history_by_id[empty_check.id]["top_matches_url"] == (
        f"/api/plagiarism/check/{empty_check.id}/top-matches"
    )
    assert history_by_id[processing_check.id]["top_matches_url"] is None

    try:
        get_check_top_matches(processing_check.id, current_user=owner, db=db)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("Expected an unfinished check to reject top-match access")

    try:
        get_check_top_matches(check.id, current_user=outsider, db=db)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Expected an unrelated student to be denied")

    try:
        get_check_top_matches(99999, current_user=owner, db=db)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Expected an unknown check to return 404")


def test_upload_and_check_returns_pending_check_and_schedules_background_work(monkeypatch):
    db = _make_session()
    owner = User(identifier="uploader", password="secret", role="mahasiswa")
    db.add(owner)
    db.commit()
    db.refresh(owner)

    document = Document(
        user_id=owner.id,
        title="proposal.pdf",
        document_type="proposal",
        file_path="proposal.pdf",
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    async def fake_store_uploaded_document(**_kwargs):
        return document

    monkeypatch.setattr(document_routes, "_store_uploaded_document", fake_store_uploaded_document)
    background_tasks = BackgroundTasks()

    payload = asyncio.run(
        document_routes.upload_and_check_document(
            background_tasks=background_tasks,
            file=object(),
            document_type="proposal",
            user_id=None,
            candidate_limit=20,
            auto_index_embeddings=False,
            auto_index_repository_limit=0,
            current_user=owner,
            db=db,
        )
    )

    check = db.query(PlagiarismCheck).filter(PlagiarismCheck.id == payload["check_id"]).first()
    assert payload["status"] == "pending"
    assert payload["progress"] == 0
    assert payload["status_url"] == f"/api/plagiarism/check/{check.id}/status"
    assert payload["result_url"] == f"/api/plagiarism/check/{check.id}/matches"
    assert check.status == "pending"
    assert check.document_id == document.id
    assert len(background_tasks.tasks) == 1
