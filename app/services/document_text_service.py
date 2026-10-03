from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.schemas import Document
from app.services.pdf_service import PDFService
from app.services.similarity_service import SimilarityService


class DocumentTextService:
    def __init__(
        self,
        pdf_service: Optional[PDFService] = None,
        similarity_service: Optional[SimilarityService] = None,
    ):
        self.pdf_service = pdf_service or PDFService()
        self.similarity_service = similarity_service or SimilarityService()

    def get_or_extract_text(
        self,
        document: Document,
        db: Session,
        refresh_cache: bool = False,
    ) -> Dict[str, Any]:
        if not refresh_cache and self._has_cached_text(document):
            return self._build_cached_result(document)

        try:
            extracted = self.pdf_service.extract_text_from_pdf(
                document.file_path,
                document_type=document.document_type,
            )
            cleaned_text = self.similarity_service.clean_text(extracted["full_text"])

            document.extracted_text = extracted["full_text"]
            document.cleaned_text = cleaned_text
            document.extraction_metadata = self._build_metadata(extracted)
            document.text_extracted_at = datetime.now(timezone.utc).replace(tzinfo=None)
            document.text_extraction_error = None
            db.add(document)
            db.commit()
            db.refresh(document)

            return {
                **document.extraction_metadata,
                "full_text": document.extracted_text or "",
                "cleaned_text": document.cleaned_text or "",
            }
        except Exception as exc:
            document.text_extraction_error = str(exc)
            db.add(document)
            db.commit()
            raise

    def _has_cached_text(self, document: Document) -> bool:
        return (
            document.text_extracted_at is not None
            and document.extracted_text is not None
            and document.cleaned_text is not None
        )

    def _build_cached_result(self, document: Document) -> Dict[str, Any]:
        metadata = document.extraction_metadata or {}
        return {
            **metadata,
            "full_text": document.extracted_text or "",
            "cleaned_text": document.cleaned_text or "",
        }

    def _build_metadata(self, extracted: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "total_pages": extracted.get("total_pages"),
            "checked_pages_range": extracted.get("checked_pages_range"),
            "chapter_validation": extracted.get("chapter_validation"),
        }
