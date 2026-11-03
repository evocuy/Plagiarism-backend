from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.models.schemas import Document, DocumentChunk
from app.services.pdf_limits import PDFLimitExceeded, PDFProcessingLimits
from app.services.pdf_service import PDFService, PDF_EXTRACTION_VERSION
from app.services.similarity_service import SimilarityService


class DocumentChunkService:
    def __init__(
        self,
        pdf_service: Optional[PDFService] = None,
        similarity_service: Optional[SimilarityService] = None,
    ):
        self.pdf_service = pdf_service or PDFService()
        self.similarity_service = similarity_service or SimilarityService()
        self.limits = PDFProcessingLimits.from_environment()

    def get_or_build_sentence_chunks(
        self,
        document: Document,
        db: Session,
        refresh_cache: bool = False,
    ) -> List[Dict[str, Any]]:
        metadata = document.extraction_metadata or {}
        if not refresh_cache and metadata.get("chunk_extraction_version") == PDF_EXTRACTION_VERSION:
            # A current extraction may intentionally contain no body sentences.
            return self._format_chunks(self._get_existing_chunks(document, db))

        # Extract/validate first: a failed refresh must not erase a usable cache.
        # Old versions still raise instead of returning heading-bearing chunks.
        sentences = self.pdf_service.extract_sentences_with_pages(
            document.file_path,
            document_type=document.document_type,
        )
        if len(sentences) > self.limits.max_chunks:
            raise PDFLimitExceeded("Jumlah potongan teks PDF melebihi batas pemrosesan.")

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
                    page_start_position=item.get("page_start_position"),
                    page_end_position=item.get("page_end_position"),
                    chunk_index=len(chunk_records),
                    raw_text=raw_text,
                    cleaned_text=cleaned_text,
                )
            )

        try:
            self._delete_existing_chunks(document, db)
            db.add_all(chunk_records)
            document.extraction_metadata = {
                **(document.extraction_metadata or {}),
                "chunk_extraction_version": PDF_EXTRACTION_VERSION,
            }
            db.add(document)
            db.commit()
        except Exception:
            db.rollback()
            raise

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
                **{
                    f"source_{key}": chunk[key]
                    for key in ("page_start_position", "page_end_position")
                    if chunk.get(key) is not None
                },
            }
            for chunk in chunks
            if (chunk.get("clean") or "").strip()
        ]

    def _get_existing_chunks(self, document: Document, db: Session) -> List[DocumentChunk]:
        chunks = (
            db.query(DocumentChunk)
            .filter(
                DocumentChunk.document_id == document.id,
                DocumentChunk.chunk_type == "sentence",
            )
            .order_by(DocumentChunk.chunk_index.asc())
            .limit(self.limits.max_chunks + 1)
            .all()
        )
        if len(chunks) > self.limits.max_chunks:
            raise PDFLimitExceeded("Jumlah potongan teks tersimpan melebihi batas pemrosesan.")
        return chunks

    def _delete_existing_chunks(self, document: Document, db: Session) -> None:
        (
            db.query(DocumentChunk)
            .filter(DocumentChunk.document_id == document.id)
            .delete(synchronize_session="fetch")
        )

    def _format_chunks(self, chunks: Sequence[Any]) -> List[Dict[str, Any]]:
        return [
            {
                "page": chunk.page_number,
                "chapter": chunk.chapter,
                "sentence": chunk.raw_text,
                "clean": chunk.cleaned_text,
                "chunk_index": chunk.chunk_index,
                "page_start_position": chunk.page_start_position,
                "page_end_position": chunk.page_end_position,
            }
            for chunk in chunks
        ]
