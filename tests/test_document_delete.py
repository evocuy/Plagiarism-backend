from pathlib import Path

import inspect

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.session import Base
from app.models.schemas import (
    Document,
    DocumentChunk,
    PlagiarismCheck,
    SimilarityMatch,
    SimilarityResult,
    User,
)
from app.routes import documents as document_routes
from app.routes.auth import require_admin


def _make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _write_pdf_placeholder(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-placeholder")


def test_delete_document_removes_its_artifacts_and_dependent_results(tmp_path, monkeypatch):
    db = _make_session()
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(document_routes, "UPLOAD_DIR", upload_dir)

    target_pdf = upload_dir / "target.pdf"
    highlighted_pdf = upload_dir / "highlighted" / "target-highlighted.pdf"
    retained_pdf = upload_dir / "retained.pdf"
    for path in (target_pdf, highlighted_pdf, retained_pdf):
        _write_pdf_placeholder(path)

    admin = User(identifier="admin", password="secret", role="admin")
    owner = User(identifier="owner", password="secret", role="mahasiswa")
    db.add_all([admin, owner])
    db.flush()

    target = Document(
        user_id=owner.id,
        title="target.pdf",
        document_type="proposal",
        file_path=str(target_pdf),
    )
    retained = Document(
        user_id=owner.id,
        title="retained.pdf",
        document_type="proposal",
        file_path=str(retained_pdf),
    )
    db.add_all([target, retained])
    db.flush()
    db.add(
        DocumentChunk(
            document_id=target.id,
            chunk_type="sentence",
            chunk_index=0,
            raw_text="Kalimat target.",
            cleaned_text="kalimat target",
        )
    )

    target_check = PlagiarismCheck(
        document_id=target.id,
        user_id=owner.id,
        overall_similarity=0.5,
        highlighted_file_path=str(highlighted_pdf),
    )
    retained_check = PlagiarismCheck(
        document_id=retained.id,
        user_id=owner.id,
        overall_similarity=0.4,
    )
    db.add_all([target_check, retained_check])
    db.flush()

    source_result = SimilarityResult(
        check_id=retained_check.id,
        source_document_id=target.id,
        similarity_score=0.5,
    )
    db.add(source_result)
    db.flush()
    source_match = SimilarityMatch(
        result_id=source_result.id,
        source_text="Kalimat sumber.",
        submitted_text="Kalimat target.",
        similarity_score=1.0,
        page_number=1,
    )
    db.add(source_match)
    db.commit()

    result = document_routes.delete_document(target.id, admin=admin, db=db)

    assert result["document_id"] == target.id
    assert result["deleted_check_count"] == 1
    assert result["deleted_file_count"] == 2
    assert db.get(Document, target.id) is None
    assert db.get(DocumentChunk, 1) is None
    assert db.get(PlagiarismCheck, target_check.id) is None
    assert db.get(SimilarityResult, source_result.id) is None
    assert db.get(SimilarityMatch, source_match.id) is None
    assert db.get(Document, retained.id) is not None
    assert db.get(PlagiarismCheck, retained_check.id) is not None
    assert not target_pdf.exists()
    assert not highlighted_pdf.exists()
    assert retained_pdf.exists()


def test_delete_document_keeps_a_pdf_referenced_by_another_document(tmp_path, monkeypatch):
    db = _make_session()
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(document_routes, "UPLOAD_DIR", upload_dir)
    shared_pdf = upload_dir / "shared.pdf"
    _write_pdf_placeholder(shared_pdf)

    admin = User(identifier="admin", password="secret", role="admin")
    db.add(admin)
    db.flush()
    target = Document(title="first.pdf", document_type="proposal", file_path=str(shared_pdf))
    retained = Document(title="second.pdf", document_type="proposal", file_path=str(shared_pdf))
    db.add_all([target, retained])
    db.commit()

    result = document_routes.delete_document(target.id, admin=admin, db=db)

    assert result["deleted_file_count"] == 0
    assert db.get(Document, target.id) is None
    assert db.get(Document, retained.id) is not None
    assert shared_pdf.exists()


def test_delete_document_returns_404_for_unknown_id():
    db = _make_session()
    admin = User(identifier="admin", password="secret", role="admin")
    db.add(admin)
    db.commit()

    try:
        document_routes.delete_document(999, admin=admin, db=db)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Expected a missing document to return 404")


def test_delete_document_rejects_a_document_with_an_active_check():
    db = _make_session()
    admin = User(identifier="admin", password="secret", role="admin")
    db.add(admin)
    db.flush()
    document = Document(title="active.pdf", document_type="proposal", file_path="active.pdf")
    db.add(document)
    db.flush()
    db.add(
        PlagiarismCheck(
            document_id=document.id,
            overall_similarity=0.0,
            status="processing",
        )
    )
    db.commit()

    try:
        document_routes.delete_document(document.id, admin=admin, db=db)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("Expected an active check to block document deletion")

    assert db.get(Document, document.id) is not None


def test_delete_document_endpoint_requires_an_admin():
    admin = User(identifier="admin", password="secret", role="admin")
    student = User(identifier="student", password="secret", role="mahasiswa")

    for endpoint in (
        document_routes.clear_all_documents,
        document_routes.delete_document,
    ):
        admin_dependency = inspect.signature(endpoint).parameters["admin"].default
        assert admin_dependency.dependency is require_admin

    assert require_admin(admin) is admin
    try:
        require_admin(student)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Expected a non-admin user to be denied")

    delete_paths = [
        route.path
        for route in document_routes.router.routes
        if "DELETE" in route.methods
    ]
    assert delete_paths.index("/clear") < delete_paths.index("/{document_id}")
