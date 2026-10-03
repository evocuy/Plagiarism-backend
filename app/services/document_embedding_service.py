from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.schemas import Document, DocumentChunk
from app.services.document_chunk_service import DocumentChunkService
from app.services.embedding_service import EmbeddingService


class DocumentEmbeddingService:
    def __init__(
        self,
        embedding_service: Optional[EmbeddingService] = None,
        document_chunk_service: Optional[DocumentChunkService] = None,
        batch_size: int = 32,
    ):
        self.embedding_service = embedding_service or EmbeddingService()
        self.document_chunk_service = document_chunk_service or DocumentChunkService()
        self.batch_size = batch_size

    def summarize_document_embeddings(
        self,
        document: Document,
        db: Session,
    ) -> Dict[str, Any]:
        self.document_chunk_service.get_or_build_sentence_chunks(document, db)
        chunks = (
            db.query(DocumentChunk)
            .filter(
                DocumentChunk.document_id == document.id,
                DocumentChunk.chunk_type == "sentence",
            )
            .order_by(DocumentChunk.chunk_index.asc())
            .all()
        )
        embeddable_chunks = [
            chunk for chunk in chunks
            if chunk.cleaned_text.strip()
        ]
        indexed_chunks = [
            chunk for chunk in embeddable_chunks
            if chunk.embedding_model == self.embedding_service.model
            and chunk.embedding_generated_at is not None
            and not chunk.embedding_error
        ]
        pending_chunks = [
            chunk for chunk in embeddable_chunks
            if (
                chunk.embedding_model != self.embedding_service.model
                or chunk.embedding_generated_at is None
            )
            and not chunk.embedding_error
        ]
        failed_chunks = [
            chunk for chunk in embeddable_chunks
            if chunk.embedding_error
        ]

        return {
            "document_id": document.id,
            "title": document.title,
            "embedding_model": self.embedding_service.model,
            "total_chunks": len(chunks),
            "total_embeddable_chunks": len(embeddable_chunks),
            "total_indexed_chunks": len(indexed_chunks),
            "total_pending_chunks": len(pending_chunks),
            "total_failed_chunks": len(failed_chunks),
        }

    def ensure_document_embeddings(
        self,
        document: Document,
        db: Session,
        refresh_chunks: bool = False,
        refresh_embeddings: bool = False,
    ) -> Dict[str, Any]:
        if not self.embedding_service.is_configured():
            return {
                "document_id": document.id,
                "title": document.title,
                "embedding_model": self.embedding_service.model,
                "status": "skipped",
                "reason": "embedding_config_not_configured",
            }

        summary = self.summarize_document_embeddings(document, db)
        if (
            not refresh_embeddings
            and summary["total_pending_chunks"] == 0
            and summary["total_failed_chunks"] > 0
        ):
            return {
                **summary,
                "status": "skipped",
                "reason": "previous_embedding_errors",
                "index_result": None,
            }

        if not refresh_embeddings and summary["total_pending_chunks"] == 0:
            return {
                **summary,
                "status": "skipped",
                "reason": "up_to_date",
                "index_result": None,
            }

        index_result = self.index_document_embeddings(
            document=document,
            db=db,
            refresh_chunks=refresh_chunks,
            refresh_embeddings=refresh_embeddings,
        )
        if index_result["total_failed"] and not index_result["total_indexed"]:
            status = "failed"
        elif index_result["total_failed"]:
            status = "partial"
        else:
            status = "completed"

        return {
            **summary,
            "status": status,
            "reason": "indexed",
            "index_result": index_result,
        }

    def index_document_embeddings(
        self,
        document: Document,
        db: Session,
        refresh_chunks: bool = False,
        refresh_embeddings: bool = False,
    ) -> Dict[str, Any]:
        self.document_chunk_service.get_or_build_sentence_chunks(
            document,
            db,
            refresh_cache=refresh_chunks,
        )
        chunks = (
            db.query(DocumentChunk)
            .filter(
                DocumentChunk.document_id == document.id,
                DocumentChunk.chunk_type == "sentence",
            )
            .order_by(DocumentChunk.chunk_index.asc())
            .all()
        )

        chunks_to_embed = [
            chunk for chunk in chunks
            if chunk.cleaned_text.strip()
            and (
                refresh_embeddings
                or chunk.embedding_model != self.embedding_service.model
                or chunk.embedding_generated_at is None
            )
            and (refresh_embeddings or not chunk.embedding_error)
        ]
        skipped_failed_chunks = [
            chunk for chunk in chunks
            if chunk.cleaned_text.strip()
            and chunk.embedding_error
            and not refresh_embeddings
        ]

        indexed = 0
        failed = []

        for start in range(0, len(chunks_to_embed), self.batch_size):
            batch = chunks_to_embed[start:start + self.batch_size]
            try:
                vectors = self.embedding_service.embed_texts([chunk.cleaned_text for chunk in batch])
            except Exception as err:
                for chunk in batch:
                    self._mark_chunk_embedding_error(chunk, db, str(err))
                failed.extend([
                    {
                        "chunk_id": chunk.id,
                        "chunk_index": chunk.chunk_index,
                        "error": str(err),
                    }
                    for chunk in batch
                ])
                break

            for chunk, vector in zip(batch, vectors):
                try:
                    self._store_chunk_embedding(chunk, vector, db)
                    indexed += 1
                except Exception as err:
                    self._mark_chunk_embedding_error(chunk, db, str(err))
                    failed.append({
                        "chunk_id": chunk.id,
                        "chunk_index": chunk.chunk_index,
                        "error": str(err),
                    })

        return {
            "document_id": document.id,
            "title": document.title,
            "embedding_model": self.embedding_service.model,
            "total_chunks": len(chunks),
            "total_pending": len(chunks_to_embed),
            "total_pending_chunks": len(chunks_to_embed),
            "total_indexed": indexed,
            "total_failed": len(failed),
            "total_skipped_failed_chunks": len(skipped_failed_chunks),
            "failed": failed,
        }

    def _store_chunk_embedding(self, chunk: DocumentChunk, vector: List[float], db: Session) -> None:
        generated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.execute(
            text("""
                UPDATE document_chunks
                SET embedding = CAST(:embedding AS vector),
                    embedding_model = :embedding_model,
                    embedding_generated_at = :embedding_generated_at,
                    embedding_error = NULL
                WHERE id = :chunk_id
            """),
            {
                "chunk_id": chunk.id,
                "embedding": self._format_vector_literal(vector),
                "embedding_model": self.embedding_service.model,
                "embedding_generated_at": generated_at,
            },
        )
        db.commit()

    def _mark_chunk_embedding_error(self, chunk: DocumentChunk, db: Session, error: str) -> None:
        db.rollback()
        chunk.embedding_model = self.embedding_service.model
        chunk.embedding_generated_at = None
        chunk.embedding_error = error
        db.add(chunk)
        db.commit()

    def _format_vector_literal(self, vector: List[float]) -> str:
        return "[" + ",".join(str(float(value)) for value in vector) + "]"
