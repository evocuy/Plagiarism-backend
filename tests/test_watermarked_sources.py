import hashlib
from pathlib import Path
import subprocess

import pymupdf
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, PlagiarismCheck, SimilarityResult, User, utc_now
from app.routes import plagiarism
from app.services import watermark_service as watermark_module
from app.services.watermark_service import WatermarkService


@pytest.fixture
def environment(tmp_path, monkeypatch):
    directory = tmp_path / "uploads"
    directory.mkdir()
    source_path = directory / "alumni.pdf"
    with pymupdf.open() as pdf:
        for number, (width, height) in enumerate([(595, 842), (842, 595), (595, 842)]):
            page = pdf.new_page(width=width, height=height)
            page.insert_text((72, 100), "Dokumen sumber alumni untuk penelitian kampus.")
            if number == 2:
                page.set_rotation(90)
        pdf.save(source_path)
    service = WatermarkService(directory)
    monkeypatch.setattr(plagiarism, "watermark_service", service)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        owner = User(identifier="owner", password="unused", role="mahasiswa")
        outsider = User(identifier="outsider", password="unused", role="mahasiswa")
        admin = User(identifier="admin", password="unused", role="admin")
        db.add_all([owner, outsider, admin])
        db.flush()
        target = Document(user_id=owner.id, title="target.pdf", file_path="target.pdf", document_type="skripsi")
        sources = [Document(title=f"source-{n}.pdf", file_path=str(source_path), document_type="skripsi",
                            source_origin="alumni", repository_status="published") for n in range(4)]
        db.add_all([target, *sources])
        db.flush()
        check = PlagiarismCheck(document_id=target.id, user_id=owner.id, status="completed", overall_similarity=.9)
        db.add(check)
        db.flush()
        db.add_all([SimilarityResult(check_id=check.id, source_document_id=source.id,
                                     similarity_score=.9 - n * .1) for n, source in enumerate(sources)])
        db.commit()
        yield db, owner, outsider, admin, check, sources, service, source_path
    engine.dispose()


def test_owner_can_open_only_watermarked_top_three_and_original_is_unchanged(environment, monkeypatch):
    db, owner, _, admin, check, sources, service, original = environment
    before = hashlib.sha256(original.read_bytes()).digest()
    top = plagiarism.get_check_top_matches(check.id, current_user=owner, db=db)["top_matches"]
    assert len(top) == 3
    assert all(item["pdf_available"] and item["pdf_watermarked"] for item in top)
    assert top[0]["pdf_url"] == f"/api/plagiarism/check/{check.id}/sources/{sources[0].id}/pdf"
    response = plagiarism.get_comparison_source_pdf(check.id, sources[0].id, current_user=owner, db=db)
    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    with pymupdf.open(response.path) as pdf, pymupdf.open(original) as source:
        assert len(pdf) == len(source) == 3
        image_xrefs = []
        for page, original_page in zip(pdf, source):
            assert page.get_text() == original_page.get_text()
            images = page.get_images()
            assert len(images) == 1
            visible_rect = page.get_image_rects(images[0][0])[0] * page.rotation_matrix
            assert visible_rect.x0 >= -1 and visible_rect.y0 >= -1
            assert visible_rect.x1 <= page.rect.width + 1 and visible_rect.y1 <= page.rect.height + 1
            assert abs((visible_rect.x0 + visible_rect.x1) / 2 - page.rect.width / 2) < 1
            image_xrefs.append(images[0][0])
            image = pdf.extract_image(images[0][0])
            assert image["width"] <= 1024 and image["height"] <= 1024
            assert images[0][1] > 0  # Transparency mask is embedded.
        assert len(set(image_xrefs)) == 1
    assert hashlib.sha256(original.read_bytes()).digest() == before
    monkeypatch.setattr(watermark_module.subprocess, "run", lambda *_a, **_k: pytest.fail("cache must be reused"))
    cached = plagiarism.get_comparison_source_pdf(check.id, sources[0].id, current_user=admin, db=db)
    assert cached.path == response.path
    with pytest.raises(HTTPException) as error:
        plagiarism.get_comparison_source_pdf(check.id, sources[3].id, current_user=owner, db=db)
    assert error.value.status_code == 404


def test_other_student_cannot_list_or_open_sources(environment, monkeypatch):
    db, _, outsider, _, check, sources, service, _ = environment
    monkeypatch.setattr(service, "get_pdf", lambda *_a: pytest.fail("unauthorized request must not generate PDF"))
    for request in [lambda: plagiarism.get_check_top_matches(check.id, current_user=outsider, db=db),
                    lambda: plagiarism.get_comparison_source_pdf(check.id, sources[0].id, current_user=outsider, db=db)]:
        with pytest.raises(HTTPException) as error:
            request()
        assert error.value.status_code == 403


@pytest.mark.parametrize("change", ["processing", "deleted", "student", "missing", "outside"])
def test_unavailable_source_is_never_served(environment, tmp_path, change):
    db, owner, _, _, check, sources, service, original = environment
    source = sources[0]
    expected = 404
    if change == "processing":
        check.status = "processing"
        expected = 409
    elif change == "deleted":
        source.deleted_at = utc_now()
    elif change == "student":
        source.source_origin = "student"
        source.user_id = owner.id
    elif change == "missing":
        source.file_path = str(original.parent / "missing.pdf")
    elif change == "outside":
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(original.read_bytes())
        source.file_path = str(outside)
    db.commit()
    with pytest.raises(HTTPException) as error:
        plagiarism.get_comparison_source_pdf(check.id, source.id, current_user=owner, db=db)
    assert error.value.status_code == expected
    if change != "processing":
        top = plagiarism.get_check_top_matches(check.id, current_user=owner, db=db)
        assert top["top_matches"][0]["pdf_available"] is False
        assert top["top_matches"][0]["pdf_url"] is None


def test_timeout_cleans_temporary_pdf_and_releases_capacity(environment, monkeypatch):
    *_, service, original = environment
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("watermark-worker", 1)
    monkeypatch.setattr(watermark_module.subprocess, "run", timeout)
    for _ in range(3):
        with pytest.raises(HTTPException) as error:
            service.get_pdf(original)
        assert error.value.status_code == 504
        assert str(original) not in error.value.detail
    assert list((service.upload_dir / "watermarked").iterdir()) == []
    assert service.slots.acquire(blocking=False)
    assert service.slots.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as error:
            service.get_pdf(original)
        assert error.value.status_code == 503
    finally:
        service.slots.release()
        service.slots.release()
