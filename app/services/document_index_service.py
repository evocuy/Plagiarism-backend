from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.schemas import Document
from app.services.document_chunk_service import DocumentChunkService
from app.services.document_text_service import DocumentTextService


class DocumentIndexService:
    def __init__(
        self,
        document_text_service: Optional[DocumentTextService] = None,
        document_chunk_service: Optional[DocumentChunkService] = None,
    ):
        self.document_text_service = document_text_service or DocumentTextService()
        self.document_chunk_service = document_chunk_service or DocumentChunkService()

    def reindex_document(self, document: Document, db: Session) -> Dict[str, Any]:
        text_data = self.document_text_service.get_or_extract_text(
            document,
            db,
            refresh_cache=True,
        )
        chunks = self.document_chunk_service.get_or_build_sentence_chunks(
            document,
            db,
            refresh_cache=True,
        )
        chapters = sorted({
            chunk.get("chapter") for chunk in chunks
            if chunk.get("chapter")
        })

        return {
            "document_id": document.id,
            "title": document.title,
            "text_cache_status": "completed",
            "chunk_cache_status": "completed",
            "chunk_count": len(chunks),
            "chapters": chapters,
            "chapter_validation": text_data.get("chapter_validation"),
        }
