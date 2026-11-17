"""Synthetic PDFs reproduce validator failures without using campus documents."""
import asyncio
import json
import subprocess
from types import SimpleNamespace

import pymupdf
import pytest

from app.services.document_structure_service import DocumentStructureService
from app.services import document_structure_service as structure_module
from app.services.pdf_service import PDFService


def make_pdf(path, pages, *, recoverable_error=False):
    with pymupdf.open() as document:
        for text in pages:
            document.new_page().insert_text((72, 72), text)
        if recoverable_error:
            stream = document[0].get_contents()[0]
            document.update_stream(
                stream, document.xref_stream(stream) + b"\n/AbsentResource Do\n",
            )
        document.save(path)
    return path


PROPOSAL_PAGES = (
    "BAB I\nPENDAHULUAN\nSistem informasi akademik membantu penelitian mahasiswa.",
    "BAB II\nTINJAUAN PUSTAKA\nDokumen alumni menjadi sumber pembanding penelitian.",
    "BAB III\nMETODE PENELITIAN\nPengujian menghitung kemiripan dari isi naskah penelitian.",
)


def test_isolated_validation_accepts_recoverable_pdf_parser_messages(tmp_path):
    path = make_pdf(tmp_path / "recoverable.pdf", PROPOSAL_PAGES, recoverable_error=True)
    service = DocumentStructureService()

    # A missing optional drawing resource does not prevent text extraction.
    assert service.validate_pdf_structure(path, "proposal")["is_valid"] is True
    result = asyncio.run(service.validate_pdf_structure_for_upload(path, "proposal"))

    assert result["is_valid"] is True
    assert result["detected_chapters"] == [1, 2, 3]


@pytest.mark.parametrize("reference", [
    "BAB III menjelaskan metode yang akan digunakan dalam penelitian ini.",
    "BAB III akan membahas metode penelitian",
    "BAB III ini merupakan bagian dari rencana sistematika penulisan.",
])
def test_body_reference_does_not_change_chapter_order(tmp_path, reference):
    path = make_pdf(tmp_path / "reference.pdf", (
        PROPOSAL_PAGES[0],
        f"1.5 Sistematika Penulisan\n{reference}",
        *PROPOSAL_PAGES[1:],
    ))

    result = DocumentStructureService().validate_pdf_structure(path, "proposal")

    assert result["is_valid"] is True
    assert result["chapters_in_document_order"] == [1, 2, 3]


def test_body_reference_does_not_replace_a_missing_chapter(tmp_path):
    path = make_pdf(tmp_path / "missing.pdf", (
        PROPOSAL_PAGES[0],
        "BAB II menjelaskan teori yang akan dibahas dalam naskah ini.",
        PROPOSAL_PAGES[2],
    ))

    result = DocumentStructureService().validate_pdf_structure(path, "proposal")

    assert result["is_valid"] is False
    assert result["missing_chapters"] == [2]


def test_body_reference_to_bab_iv_does_not_truncate_a_proposal(tmp_path):
    pages = (PROPOSAL_PAGES[0], "BAB IV akan menjelaskan hasil penelitian.", *PROPOSAL_PAGES[1:])
    path = make_pdf(tmp_path / "reference-bab-iv.pdf", pages)

    result = DocumentStructureService().validate_pdf_structure(path, "proposal")
    extracted = PDFService().extract_text_from_pdf(str(path), document_type="proposal")

    assert result["is_valid"] is True
    assert extracted["checked_pages_range"] == {"start_page": 1, "end_page": 4, "total_checked_pages": 4}
    assert "Pengujian menghitung" in extracted["full_text"]


def test_reference_on_chapter_opening_page_does_not_hide_its_heading(tmp_path):
    path = make_pdf(tmp_path / "opening-reference.pdf", (
        PROPOSAL_PAGES[0] + "\nBAB II menjelaskan dasar teori penelitian ini.",
        *PROPOSAL_PAGES[1:],
    ))

    assert DocumentStructureService().validate_pdf_structure(path, "proposal")["is_valid"] is True


def test_real_chapters_in_wrong_order_are_still_rejected(tmp_path):
    path = make_pdf(tmp_path / "wrong-order.pdf", (
        PROPOSAL_PAGES[0], PROPOSAL_PAGES[2], PROPOSAL_PAGES[1],
    ))

    result = DocumentStructureService().validate_pdf_structure(path, "proposal")

    assert result["is_valid"] is False
    assert result["chapters_in_document_order"] == [1, 3, 2]
    assert "terbaca: BAB 1, BAB 3, BAB 2" in result["message"]


def test_table_of_contents_cannot_replace_missing_body_chapters(tmp_path):
    path = make_pdf(tmp_path / "toc-only.pdf", (
        "DAFTAR ISI\nBAB I PENDAHULUAN .... 1\nBAB II TINJAUAN PUSTAKA .... 5\nBAB III METODE PENELITIAN .... 9",
        "Sistem informasi akademik membantu penelitian mahasiswa.",
    ))

    result = DocumentStructureService().validate_pdf_structure(path, "proposal")

    assert result["is_valid"] is False
    assert result["missing_chapters"] == [1, 2, 3]


def test_actually_unreadable_pdf_still_fails_validation(tmp_path):
    path = tmp_path / "not-a-pdf.pdf"
    path.write_bytes(b"%PDF-1.7\ninvalid PDF data")

    result = asyncio.run(DocumentStructureService().validate_pdf_structure_for_upload(path, "proposal"))

    assert result["is_valid"] is False
    assert result["error_code"] == "PDF_UNREADABLE"


@pytest.mark.parametrize("failure, expected_code", [
    (subprocess.CalledProcessError(-9, "private-command", output="private PDF content"), "PDF_VALIDATION_FAILED"),
    (PermissionError("private/path.pdf"), "PDF_VALIDATION_UNAVAILABLE"),
])
def test_worker_failures_are_distinct_from_unreadable_pdf(monkeypatch, failure, expected_code):
    service = DocumentStructureService()

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(structure_module.subprocess, "run", fail)
    for _ in range(3):  # Failures must release the bounded validation slot.
        result = service._validate_in_subprocess("private/path.pdf", "proposal")
        assert result["error_code"] == expected_code
        assert "private" not in json.dumps(result)
        if expected_code == "PDF_VALIDATION_FAILED":
            assert "-9" in result["message"]


@pytest.mark.parametrize("stdout", ["private warning\n{}", "[]", "null", '{"is_valid": "true"}'])
def test_invalid_worker_output_is_safe_and_does_not_claim_pdf_is_unreadable(monkeypatch, stdout):
    service = DocumentStructureService()
    monkeypatch.setattr(structure_module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=stdout))

    for _ in range(3):
        result = service._validate_in_subprocess("private/path.pdf", "proposal")
        assert result["error_code"] == "PDF_VALIDATION_INVALID_RESPONSE"
        assert "private" not in json.dumps(result)
