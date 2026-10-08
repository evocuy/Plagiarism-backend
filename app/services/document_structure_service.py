"""Validate a PDF's chapter structure before it is accepted for checking."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List

from app.services.pdf_service import PDFService


class DocumentStructureService:
    """Keep the document-type and BAB rules outside FastAPI route handlers."""

    _DOCUMENT_TYPE_ALIASES = {
        "proposal": "proposal",
        "sempro": "proposal",
        "skripsi": "skripsi",
    }
    _REQUIRED_CHAPTERS = {
        "proposal": [1, 2, 3],
        "skripsi": [1, 2, 3, 4, 5],
    }

    def __init__(self, pdf_service: PDFService | None = None):
        self.pdf_service = pdf_service or PDFService()

    def normalize_document_type(self, document_type: str) -> str:
        normalized = (document_type or "").strip().lower()
        canonical_type = self._DOCUMENT_TYPE_ALIASES.get(normalized)
        if not canonical_type:
            raise ValueError("Tipe dokumen harus berupa Skripsi atau Proposal.")
        return canonical_type

    def validate_pdf_structure(
        self,
        file_path: str | Path,
        document_type: str,
    ) -> Dict[str, Any]:
        """Inspect a text-based PDF and return a user-safe validation result."""
        canonical_type = self.normalize_document_type(document_type)

        try:
            extracted = self.pdf_service.extract_text_from_pdf(
                str(file_path),
                filter_bab=False,
                document_type=canonical_type,
            )
        except Exception:
            return {
                "is_valid": False,
                "error_code": "PDF_UNREADABLE",
                "message": "PDF tidak dapat dibaca. Unggah file PDF yang valid.",
                "document_type": canonical_type,
                "expected_chapters": self._REQUIRED_CHAPTERS[canonical_type],
                "detected_chapters": [],
            }

        if not (extracted.get("full_text") or "").strip():
            return {
                "is_valid": False,
                "error_code": "PDF_TEXT_NOT_EXTRACTABLE",
                "message": (
                    "PDF kosong atau teksnya tidak dapat diekstrak untuk memverifikasi "
                    "struktur BAB. Unggah PDF berbasis teks yang memuat naskah."
                ),
                "document_type": canonical_type,
                "expected_chapters": self._REQUIRED_CHAPTERS[canonical_type],
                "detected_chapters": [],
            }

        return self.validate_chapter_metadata(
            extracted.get("chapter_validation") or {},
            canonical_type,
        )

    def validate_chapter_metadata(
        self,
        chapter_validation: Dict[str, Any],
        document_type: str,
    ) -> Dict[str, Any]:
        """Validate the detected chapter set and order without reading a PDF.

        The detector can find the same number of chapters while still missing one
        of the required BABs (for example BAB 1, 2, and 4).  Therefore this
        validates the actual chapter numbers and their page order, not only the
        aggregate count.
        """
        canonical_type = self.normalize_document_type(document_type)
        expected = self._REQUIRED_CHAPTERS[canonical_type]
        detected_entries = self._normalise_detected_chapters(
            chapter_validation.get("detected_chapters") or []
        )
        detected = sorted({entry["chapter"] for entry in detected_entries})
        ordered_detected = [entry["chapter"] for entry in detected_entries]
        required_in_document_order = [
            chapter for chapter in ordered_detected if chapter in expected
        ]
        missing = [chapter for chapter in expected if chapter not in detected]
        unexpected = [chapter for chapter in detected if chapter not in expected]
        in_expected_order = required_in_document_order == expected

        if canonical_type == "proposal":
            is_valid = not missing and not unexpected and in_expected_order
        else:
            # A skripsi can include BAB tambahan after BAB 5, but its mandatory
            # BAB 1--5 must all exist and appear in order.
            is_valid = not missing and in_expected_order

        result = {
            "is_valid": is_valid,
            "error_code": None if is_valid else "INVALID_CHAPTER_STRUCTURE",
            "document_type": canonical_type,
            "expected_chapters": expected,
            "detected_chapters": detected,
            "missing_chapters": missing,
            "unexpected_chapters": unexpected,
            "chapters_in_document_order": ordered_detected,
            "message": self._build_message(
                document_type=canonical_type,
                expected=expected,
                detected=detected,
                missing=missing,
                unexpected=unexpected,
                in_expected_order=in_expected_order,
            ),
        }
        return result

    @staticmethod
    def _normalise_detected_chapters(raw_chapters: Iterable[Any]) -> List[Dict[str, int]]:
        unique_chapters: Dict[int, Dict[str, int]] = {}
        for item in raw_chapters:
            if not isinstance(item, dict):
                continue
            try:
                chapter = int(item.get("chapter"))
            except (TypeError, ValueError):
                continue
            if chapter < 1 or chapter in unique_chapters:
                continue

            try:
                page = int(item.get("page"))
            except (TypeError, ValueError):
                page = 0
            unique_chapters[chapter] = {"chapter": chapter, "page": page}

        # Metadata from PDFService has page numbers.  Sorting by the chapter as
        # a tie-breaker keeps manually supplied metadata deterministic as well.
        return sorted(
            unique_chapters.values(),
            key=lambda item: (item["page"] if item["page"] > 0 else 10**9, item["chapter"]),
        )

    @staticmethod
    def _format_chapters(chapters: List[int]) -> str:
        if not chapters:
            return "tidak ada"
        return ", ".join(f"BAB {chapter}" for chapter in chapters)

    def _build_message(
        self,
        *,
        document_type: str,
        expected: List[int],
        detected: List[int],
        missing: List[int],
        unexpected: List[int],
        in_expected_order: bool,
    ) -> str:
        detected_text = self._format_chapters(detected)

        if document_type == "proposal":
            if not missing and not unexpected and in_expected_order:
                return "Struktur Proposal valid: BAB 1, BAB 2, dan BAB 3 terdeteksi."

            details = []
            if missing:
                details.append(f"belum ditemukan {self._format_chapters(missing)}")
            if unexpected:
                details.append(
                    f"tidak boleh memuat {self._format_chapters(unexpected)}"
                )
            if not in_expected_order and not missing:
                details.append("urutan BAB tidak sesuai")
            detail_text = "; ".join(details)
            return (
                "Struktur Proposal tidak sesuai. Dokumen harus memuat tepat BAB 1, "
                f"BAB 2, dan BAB 3. Terdeteksi: {detected_text}. {detail_text}."
            )

        if not missing and in_expected_order:
            return "Struktur Skripsi valid: BAB 1 sampai BAB 5 terdeteksi."

        details = []
        if missing:
            details.append(f"belum ditemukan {self._format_chapters(missing)}")
        if not in_expected_order and not missing:
            details.append("urutan BAB 1 sampai BAB 5 tidak sesuai")
        detail_text = "; ".join(details)
        return (
            "Struktur Skripsi belum lengkap. Dokumen harus memuat minimal BAB 1 sampai "
            f"BAB 5. Terdeteksi: {detected_text}. {detail_text}."
        )
