from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.models.schemas import Document, DocumentChunk
from app.services.pdf_service import PDFService
from app.services.similarity_service import SimilarityService


class DocumentChunkService:
    def __init__(
        self,
        pdf_service: Optional[PDFService] = None,
        similarity_service: Optional[SimilarityService] = None,
    ):
        self.pdf_service = pdf_service or PDFService()
        self.similarity_service = similarity_service or SimilarityService()

    def get_or_build_sentence_chunks(
        self,
        document: Document,
        db: Session,
        refresh_cache: bool = False,
    ) -> List[Dict[str, Any]]:
        if refresh_cache:
            self._delete_existing_chunks(document, db)

        existing_chunks = self._get_existing_chunks(document, db)
        if existing_chunks:
            return self._format_chunks(existing_chunks)

        sentences = self.pdf_service.extract_sentences_with_pages(
            document.file_path,
            document_type=document.document_type,
        )

        chunk_records = []
        for item in sentences:
            raw_text = (item.get("sentence") or "").strip()
            if not raw_text:
                continue

            cleaned_text = self.similarity_service.clean_text(raw_text)
            if not cleaned_text.strip():
                continue

            chunk_records.append(
                DocumentChunk(
                    document_id=document.id,
                    chunk_type="sentence",
                    chapter=item.get("chapter"),
                    page_number=item.get("page"),
                    chunk_index=len(chunk_records),
                    raw_text=raw_text,
                    cleaned_text=cleaned_text,
                )
            )

        if chunk_records:
            db.add_all(chunk_records)
            db.commit()

        return self._format_chunks(chunk_records)

    def to_reference_corpus(
        self,
        chunks: Sequence[Dict[str, Any]],
        matched_source: str,
        source_document_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return [
            {
                "clean": chunk["clean"],
                "sentence": chunk["sentence"],
                "matched_source": matched_source,
                "source_document_id": source_document_id,
                "source_chunk_index": chunk.get("chunk_index"),
                "source_page": chunk.get("page"),
                "chapter": chunk.get("chapter"),
            }
            for chunk in chunks
            if (chunk.get("clean") or "").strip()
        ]

    def _get_existing_chunks(self, document: Document, db: Session) -> List[DocumentChunk]:
        return (
            db.query(DocumentChunk)
            .filter(
                DocumentChunk.document_id == document.id,
                DocumentChunk.chunk_type == "sentence",
            )
            .order_by(DocumentChunk.chunk_index.asc())
            .all()
        )

    def _delete_existing_chunks(self, document: Document, db: Session) -> None:
        (
            db.query(DocumentChunk)
            .filter(DocumentChunk.document_id == document.id)
            .delete(synchronize_session=False)
        )
        db.commit()

    def _format_chunks(self, chunks: Sequence[Any]) -> List[Dict[str, Any]]:
        return [
            {
                "page": chunk.page_number,
                "chapter": chunk.chapter,
                "sentence": chunk.raw_text,
                "clean": chunk.cleaned_text,
                "chunk_index": chunk.chunk_index,
            }
            for chunk in chunks
        ]
