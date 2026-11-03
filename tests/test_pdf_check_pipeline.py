"""Exercise real PDF extraction, persisted caches, matching and annotations."""
import pymupdf
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, DocumentChunk, SimilarityMatch, PlagiarismCheck, User, utc_now
from app.routes.plagiarism import get_check_matches
from app.services import pdf_service
from app.services.pdf_service import PDF_EXTRACTION_VERSION
from app.services.repository_check_service import RepositoryCheckService


def _pdf(path, text):
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), text, fontsize=11)
        pdf.save(path)


def test_real_check_filters_headings_and_preserves_body_match_locations(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_service, "UPLOAD_DIR", tmp_path)
    target_path, source_path = tmp_path / "target.pdf", tmp_path / "source.pdf"
    heading = "BAB I\nPENDAHULUAN\n1.1 Latar Belakang\n"
    body = "Sistem informasi akademik membantu mahasiswa mengelola jadwal kuliah."
    _pdf(target_path, heading + body + "\n" + body)
    _pdf(source_path, heading + body)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        owner = User(identifier="test-student", password="unused", role="mahasiswa")
        db.add(owner)
        db.flush()
        target = Document(title="target.pdf", document_type="proposal", file_path=str(target_path), user_id=owner.id)
        source = Document(title="source.pdf", document_type="proposal", file_path=str(source_path), repository_status="published", source_origin="alumni")
        # Simulate a deployed pre-fix repository cache.
        source.extracted_text = heading + body
        source.cleaned_text = "bab pendahuluan latar belakang"
        source.text_extracted_at = utc_now()
        db.add_all([target, source])
        db.flush()
        db.add(DocumentChunk(document_id=source.id, chunk_type="sentence", chunk_index=0,
                             raw_text=heading + body, cleaned_text="bab pendahuluan latar belakang", page_number=1))
        db.commit()
        result = RepositoryCheckService().run_repository_check(
            document_id=target.id, db=db, requester_user_id=owner.id, auto_index_embeddings=False,
        )
        check = db.get(PlagiarismCheck, result["check_id"])
        assert check.status == "completed"
        assert check.overall_similarity > .99
        matches = db.query(SimilarityMatch).all()
        assert len(matches) == 2
        assert all(match.submitted_text == body for match in matches)
        assert sorted(match.page_start_position for match in matches) == [0, len(body) + 1]
        assert "BAB" not in source.extracted_text
        assert source.extraction_metadata["chunk_extraction_version"] == PDF_EXTRACTION_VERSION
        assert source.extraction_metadata["text_extraction_version"] == PDF_EXTRACTION_VERSION
        response = get_check_matches(check.id, current_user=owner, db=db)
        assert response["highlight_summary"]["highlighted_match_count"] == 2
        assert len(response["results"][0]["matches"]) == 2
        with pymupdf.open(check.highlighted_file_path) as pdf:
            page = pdf[0]
            annotations = list(page.annots() or [])
            assert len(annotations) == 2
            # Heading occupies first three text lines; annotations belong to body only.
            assert all(annotation.rect.y0 > 100 for annotation in annotations)
    engine.dispose()
