from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.models.schemas import Document
from app.services.document_chunk_service import DocumentChunkService
from app.services.embedding_service import EmbeddingService
from app.services.tfidf_service import TfidfService


class RepositoryCandidateService:
    def __init__(
        self,
        document_chunk_service: Optional[DocumentChunkService] = None,
        tfidf_service: Optional[TfidfService] = None,
        embedding_service: Optional[EmbeddingService] = None,
        semantic_matches_per_chunk: int = 5,
        semantic_min_coverage: float = 0.95,
        semantic_highlight_threshold: float = 0.78,
        semantic_highlight_matches_per_chunk: int = 3,
    ):
        self.document_chunk_service = document_chunk_service or DocumentChunkService()
        self.tfidf_service = tfidf_service or TfidfService()
        self.embedding_service = embedding_service or EmbeddingService()
        self.semantic_matches_per_chunk = semantic_matches_per_chunk
        self.semantic_min_coverage = semantic_min_coverage
        self.semantic_highlight_threshold = semantic_highlight_threshold
        self.semantic_highlight_matches_per_chunk = semantic_highlight_matches_per_chunk

    def find_candidates(
        self,
        target_document: Document,
        repository_documents: Sequence[Document],
        db: Session,
        top_k: int = 20,
        min_score: float = 0.0,
    ) -> Dict[str, Any]:
        repository_documents = list(repository_documents)
        if not repository_documents:
            return {
                "target_chunks": [],
                "reference_corpus": [],
                "candidates": [],
                "total_repository_documents": 0,
                "total_repository_chunks": 0,
                "target_chapters": [],
                "chapter_aware": False,
                "candidate_strategy": "none",
                "candidate_strategy_reason": "repository_empty",
                "semantic_search_used": False,
                "semantic_fallback_reason": None,
                "total_target_embedding_chunks": 0,
                "total_repository_embedding_chunks": 0,
            }

        target_chunks = self.document_chunk_service.get_or_build_sentence_chunks(target_document, db)
        target_chapters = self._extract_chapters(target_chunks)
        documents_by_id = {document.id: document for document in repository_documents}

        semantic_result = self._rank_semantic_candidates(
            target_document=target_document,
            repository_documents=repository_documents,
            documents_by_id=documents_by_id,
            db=db,
            top_k=top_k,
            min_score=min_score,
            target_chunk_count=len(target_chunks),
        )
        semantic_candidates = semantic_result["candidates"]
        semantic_search_used = bool(semantic_candidates)

        if semantic_search_used:
            candidates = semantic_candidates
            candidate_strategy = "pgvector_semantic"
            candidate_strategy_reason = semantic_result["reason"]
            reference_corpus = []
            total_repository_chunks = semantic_result["total_repository_chunks"]
        else:
            reference_corpus = self._build_reference_corpus(repository_documents, db)
            total_repository_chunks = len(reference_corpus)
            candidate_strategy = "tfidf_chunks"
            fallback_reason = semantic_result["reason"]
            candidate_strategy_reason = (
                "tfidf_chunk_ranking"
                if fallback_reason == "semantic_disabled"
                else f"semantic_fallback:{fallback_reason}"
            )

            candidates = self._rank_candidates(
                target_chunks=target_chunks,
                reference_corpus=reference_corpus,
                documents_by_id=documents_by_id,
                min_score=min_score,
            )

        if not candidates:
            candidates = [
                {
                    "document": document,
                    "candidate_score": 0.0,
                    "matched_chunk_count": 0,
                    "chapter_matched_count": 0,
                    "average_score": 0.0,
                }
                for document in repository_documents
            ]

        limited_candidates = candidates[:top_k]
        candidate_doc_ids = {candidate["document"].id for candidate in limited_candidates}
        if semantic_search_used:
            candidate_reference_corpus = self._build_reference_corpus(
                [candidate["document"] for candidate in limited_candidates],
                db,
            )
        else:
            candidate_reference_corpus = [
                item for item in reference_corpus
                if item.get("source_document_id") in candidate_doc_ids
            ]
        repository_chapters = self._extract_chapters(candidate_reference_corpus)

        return {
            "target_chunks": target_chunks,
            "reference_corpus": candidate_reference_corpus,
            "candidates": limited_candidates,
            "total_repository_documents": len(repository_documents),
            "total_repository_chunks": total_repository_chunks,
            "target_chapters": target_chapters,
            "chapter_aware": bool(set(target_chapters) & set(repository_chapters)),
            "candidate_strategy": candidate_strategy,
            "candidate_strategy_reason": candidate_strategy_reason,
            "semantic_search_used": semantic_search_used,
            "semantic_fallback_reason": None if semantic_search_used else semantic_result["reason"],
            "total_target_embedding_chunks": semantic_result["total_target_embedding_chunks"],
            "total_repository_embedding_chunks": semantic_result["total_repository_embedding_chunks"],
        }

    def _build_reference_corpus(
        self,
        repository_documents: Sequence[Document],
        db: Session,
    ) -> List[Dict[str, Any]]:
        reference_corpus = []
        for document in repository_documents:
            try:
                chunks = self.document_chunk_service.get_or_build_sentence_chunks(document, db)
            except Exception:
                continue
            reference_corpus.extend(
                self.document_chunk_service.to_reference_corpus(
                    chunks=chunks,
                    matched_source=document.title,
                    source_document_id=document.id,
                )
            )
        return reference_corpus

    def _rank_semantic_candidates(
        self,
        target_document: Document,
        repository_documents: Sequence[Document],
        documents_by_id: Dict[int, Document],
        db: Session,
        top_k: int,
        min_score: float,
        target_chunk_count: int,
    ) -> Dict[str, Any]:
        empty_result = {
            "candidates": [],
            "reason": "semantic_disabled",
            "total_target_embedding_chunks": 0,
            "total_repository_embedding_chunks": 0,
            "total_repository_chunks": 0,
        }
        if db is None:
            return {
                **empty_result,
                "reason": "db_session_unavailable",
            }
        if not self.embedding_service.model:
            return {
                **empty_result,
                "reason": "embedding_model_not_configured",
            }

        repository_document_ids = [
            document.id for document in repository_documents
            if document.id is not None
        ]
        if not repository_document_ids:
            return {
                **empty_result,
                "reason": "repository_empty",
            }

        try:
            target_embeddings = self._fetch_embedded_target_chunks(target_document, db)
            if not target_embeddings:
                return {
                    **empty_result,
                    "reason": "target_embeddings_not_indexed",
                }
            target_coverage = self._calculate_coverage(
                indexed_count=len(target_embeddings),
                total_count=target_chunk_count,
            )
            if target_coverage < self.semantic_min_coverage:
                return {
                    **empty_result,
                    "reason": "target_embedding_coverage_low",
                    "total_target_embedding_chunks": len(target_embeddings),
                }

            total_repository_embedding_chunks = self._count_repository_embedding_chunks(
                repository_document_ids,
                db,
            )
            total_repository_chunks = self._count_repository_chunks(
                repository_document_ids,
                db,
            )
            if total_repository_embedding_chunks == 0:
                return {
                    **empty_result,
                    "reason": "repository_embeddings_not_indexed",
                    "total_target_embedding_chunks": len(target_embeddings),
                    "total_repository_chunks": total_repository_chunks,
                }
            repository_coverage = self._calculate_coverage(
                indexed_count=total_repository_embedding_chunks,
                total_count=total_repository_chunks,
            )
            if repository_coverage < self.semantic_min_coverage:
                return {
                    **empty_result,
                    "reason": "repository_embedding_coverage_low",
                    "total_target_embedding_chunks": len(target_embeddings),
                    "total_repository_embedding_chunks": total_repository_embedding_chunks,
                    "total_repository_chunks": total_repository_chunks,
                }

            stats: Dict[int, Dict[str, Any]] = {}
            query_limit = max(top_k, self.semantic_matches_per_chunk)
            for target in target_embeddings:
                rows = self._query_semantic_matches(
                    document_ids=repository_document_ids,
                    query_embedding=target["embedding"],
                    db=db,
                    limit=query_limit,
                    chapter=target.get("chapter"),
                )
                if not rows and target.get("chapter"):
                    rows = self._query_semantic_matches(
                        document_ids=repository_document_ids,
                        query_embedding=target["embedding"],
                        db=db,
                        limit=query_limit,
                    )
                self._accumulate_semantic_stats(
                    stats=stats,
                    rows=rows,
                    target_chapter=target.get("chapter"),
                    documents_by_id=documents_by_id,
                    min_score=min_score,
                )

            candidates = self._semantic_stats_to_candidates(stats)
            return {
                "candidates": candidates[:top_k],
                "reason": "semantic_candidates_found" if candidates else "semantic_no_matches",
                "total_target_embedding_chunks": len(target_embeddings),
                "total_repository_embedding_chunks": total_repository_embedding_chunks,
                "total_repository_chunks": total_repository_chunks,
            }
        except Exception as err:
            if hasattr(db, "rollback"):
                db.rollback()
            return {
                **empty_result,
                "reason": f"semantic_query_failed:{err}",
            }

    def _calculate_coverage(self, indexed_count: int, total_count: int) -> float:
        if total_count <= 0:
            return 0.0
        return min(float(indexed_count) / float(total_count), 1.0)

    def _fetch_embedded_target_chunks(
        self,
        target_document: Document,
        db: Session,
    ) -> List[Dict[str, Any]]:
        rows = db.execute(
            text("""
                SELECT
                    id,
                    chapter,
                    page_number,
                    chunk_index,
                    raw_text,
                    cleaned_text,
                    embedding::text AS embedding
                FROM document_chunks
                WHERE document_id = :document_id
                  AND chunk_type = 'sentence'
                  AND embedding IS NOT NULL
                  AND embedding_model = :embedding_model
                  AND NULLIF(BTRIM(cleaned_text), '') IS NOT NULL
                ORDER BY chunk_index ASC
            """),
            {
                "document_id": target_document.id,
                "embedding_model": self.embedding_service.model,
            },
        ).mappings().all()
        return [dict(row) for row in rows if row.get("embedding")]

    def find_semantic_sentence_matches(
        self,
        target_document: Document,
        repository_documents: Sequence[Document],
        db: Session,
        min_score: Optional[float] = None,
        top_n: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if db is None or not self.embedding_service.model:
            return []

        repository_documents = [
            document for document in repository_documents
            if document.id is not None
        ]
        if not repository_documents:
            return []

        document_ids = [document.id for document in repository_documents]
        documents_by_id = {document.id: document for document in repository_documents}
        threshold = self.semantic_highlight_threshold if min_score is None else min_score
        limit = top_n or self.semantic_highlight_matches_per_chunk

        try:
            target_embeddings = self._fetch_embedded_target_chunks(target_document, db)
            if not target_embeddings:
                return []

            matches = []
            seen = set()
            for target in target_embeddings:
                rows = self._query_semantic_matches(
                    document_ids=document_ids,
                    query_embedding=target["embedding"],
                    db=db,
                    limit=limit,
                    chapter=target.get("chapter"),
                )
                if not rows and target.get("chapter"):
                    rows = self._query_semantic_matches(
                        document_ids=document_ids,
                        query_embedding=target["embedding"],
                        db=db,
                        limit=limit,
                    )

                target_sentence = (target.get("raw_text") or "").strip()
                if not target_sentence:
                    continue

                for row in rows:
                    score = float(row.get("score") or 0.0)
                    if score < threshold:
                        continue

                    source_document_id = row.get("document_id")
                    source_document = documents_by_id.get(source_document_id)
                    reference_sentence = (row.get("raw_text") or "").strip()
                    if not source_document or not reference_sentence:
                        continue

                    key = (
                        target.get("id"),
                        source_document_id,
                        row.get("chunk_index"),
                    )
                    if key in seen:
                        continue
                    seen.add(key)

                    matches.append({
                        "page": target.get("page_number"),
                        "chapter": target.get("chapter"),
                        "sentence": target_sentence,
                        "submitted_sentence": target_sentence,
                        "similarity": round(score * 100, 2),
                        "matched_source": source_document.title,
                        "source_document_id": source_document_id,
                        "source_chunk_index": row.get("chunk_index"),
                        "source_page": row.get("page_number"),
                        "reference_sentence": reference_sentence,
                        "reference_full_sentence": reference_sentence,
                        "match_type": "semantic_sentence",
                        "start_position": 0,
                        "end_position": len(target_sentence),
                    })

            return matches
        except Exception:
            if hasattr(db, "rollback"):
                db.rollback()
            return []

    def _count_repository_embedding_chunks(
        self,
        document_ids: Sequence[int],
        db: Session,
    ) -> int:
        statement = text("""
            SELECT COUNT(*)
            FROM document_chunks
            WHERE document_id IN :document_ids
              AND chunk_type = 'sentence'
              AND embedding IS NOT NULL
              AND embedding_model = :embedding_model
        """).bindparams(bindparam("document_ids", expanding=True))
        return int(db.execute(
            statement,
            {
                "document_ids": list(document_ids),
                "embedding_model": self.embedding_service.model,
            },
        ).scalar() or 0)

    def _count_repository_chunks(
        self,
        document_ids: Sequence[int],
        db: Session,
    ) -> int:
        statement = text("""
            SELECT COUNT(*)
            FROM document_chunks
            WHERE document_id IN :document_ids
              AND chunk_type = 'sentence'
        """).bindparams(bindparam("document_ids", expanding=True))
        return int(db.execute(
            statement,
            {"document_ids": list(document_ids)},
        ).scalar() or 0)

    def _query_semantic_matches(
        self,
        document_ids: Sequence[int],
        query_embedding: str,
        db: Session,
        limit: int,
        chapter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        chapter_filter = "AND chapter = :chapter" if chapter else ""
        statement = text(f"""
            SELECT
                document_id,
                chapter,
                page_number,
                chunk_index,
                raw_text,
                cleaned_text,
                1 - (embedding <=> CAST(:query_embedding AS vector)) AS score
            FROM document_chunks
            WHERE document_id IN :document_ids
              AND chunk_type = 'sentence'
              AND embedding IS NOT NULL
              AND embedding_model = :embedding_model
              AND NULLIF(BTRIM(cleaned_text), '') IS NOT NULL
              {chapter_filter}
            ORDER BY embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit
        """).bindparams(bindparam("document_ids", expanding=True))
        params = {
            "document_ids": list(document_ids),
            "query_embedding": query_embedding,
            "embedding_model": self.embedding_service.model,
            "limit": limit,
        }
        if chapter:
            params["chapter"] = chapter
        rows = db.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _accumulate_semantic_stats(
        self,
        stats: Dict[int, Dict[str, Any]],
        rows: Sequence[Dict[str, Any]],
        target_chapter: Optional[str],
        documents_by_id: Dict[int, Document],
        min_score: float,
    ) -> None:
        for row in rows:
            score = float(row.get("score") or 0.0)
            if score <= min_score:
                continue

            source_document_id = row.get("document_id")
            document = documents_by_id.get(source_document_id)
            if not document:
                continue

            if source_document_id not in stats:
                stats[source_document_id] = {
                    "document": document,
                    "candidate_score": 0.0,
                    "matched_chunk_count": 0,
                    "chapter_matched_count": 0,
                    "score_sum": 0.0,
                }

            item = stats[source_document_id]
            item["candidate_score"] = max(item["candidate_score"], score)
            item["matched_chunk_count"] += 1
            if target_chapter and target_chapter == row.get("chapter"):
                item["chapter_matched_count"] += 1
            item["score_sum"] += score

    def _semantic_stats_to_candidates(
        self,
        stats: Dict[int, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        candidates = []
        for item in stats.values():
            matched_chunk_count = item["matched_chunk_count"]
            candidates.append({
                "document": item["document"],
                "candidate_score": item["candidate_score"],
                "matched_chunk_count": matched_chunk_count,
                "chapter_matched_count": item["chapter_matched_count"],
                "average_score": item["score_sum"] / matched_chunk_count if matched_chunk_count else 0.0,
            })

        candidates.sort(
            key=lambda item: (
                item["candidate_score"],
                item["chapter_matched_count"],
                item["matched_chunk_count"],
                item["average_score"],
            ),
            reverse=True,
        )
        return candidates

    def _rank_candidates(
        self,
        target_chunks: Sequence[Dict[str, Any]],
        reference_corpus: Sequence[Dict[str, Any]],
        documents_by_id: Dict[int, Document],
        min_score: float,
    ) -> List[Dict[str, Any]]:
        valid_targets = [
            chunk for chunk in target_chunks
            if (chunk.get("clean") or "").strip()
        ]
        valid_references = [
            item for item in reference_corpus
            if (item.get("clean") or "").strip()
        ]

        if not valid_targets or not valid_references:
            return []

        stats = {}
        all_reference_indexes = list(range(len(valid_references)))
        reference_indexes_by_chapter = self._group_reference_indexes_by_chapter(valid_references)

        for target in valid_targets:
            target_chapter = target.get("chapter")
            scoped_indexes = reference_indexes_by_chapter.get(target_chapter) if target_chapter else None
            reference_indexes = scoped_indexes or all_reference_indexes
            scoped_references = [valid_references[index] for index in reference_indexes]

            best_matches = self.tfidf_service.find_best_matches(
                query_texts=[target["clean"]],
                corpus_texts=[item["clean"] for item in scoped_references],
            )
            if not best_matches:
                continue

            best_match = best_matches[0]
            scoped_reference_index = int(best_match.get("index", -1))
            score = float(best_match.get("score", 0.0))

            if scoped_reference_index < 0 or score <= min_score:
                continue

            reference_index = reference_indexes[scoped_reference_index]
            reference = valid_references[reference_index]
            source_document_id = reference.get("source_document_id")
            document = documents_by_id.get(source_document_id)
            if not document:
                continue

            if source_document_id not in stats:
                stats[source_document_id] = {
                    "document": document,
                    "candidate_score": 0.0,
                    "matched_chunk_count": 0,
                    "chapter_matched_count": 0,
                    "score_sum": 0.0,
                }

            item = stats[source_document_id]
            item["candidate_score"] = max(item["candidate_score"], score)
            item["matched_chunk_count"] += 1
            if target_chapter and target_chapter == reference.get("chapter"):
                item["chapter_matched_count"] += 1
            item["score_sum"] += score

        candidates = []
        for item in stats.values():
            matched_chunk_count = item["matched_chunk_count"]
            candidates.append({
                "document": item["document"],
                "candidate_score": item["candidate_score"],
                "matched_chunk_count": matched_chunk_count,
                "chapter_matched_count": item["chapter_matched_count"],
                "average_score": item["score_sum"] / matched_chunk_count if matched_chunk_count else 0.0,
            })

        candidates.sort(
            key=lambda item: (
                item["candidate_score"],
                item["chapter_matched_count"],
                item["matched_chunk_count"],
                item["average_score"],
            ),
            reverse=True,
        )
        return candidates

    def _group_reference_indexes_by_chapter(
        self,
        references: Sequence[Dict[str, Any]],
    ) -> Dict[str, List[int]]:
        indexes_by_chapter = {}
        for index, item in enumerate(references):
            chapter = item.get("chapter")
            if chapter:
                indexes_by_chapter.setdefault(chapter, []).append(index)
        return indexes_by_chapter

    def _extract_chapters(self, chunks: Sequence[Dict[str, Any]]) -> List[str]:
        return sorted({
            item.get("chapter") for item in chunks
            if item.get("chapter")
        })
