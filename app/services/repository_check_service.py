"""Repository similarity checking with persistent processing progress."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.models.schemas import Document, PlagiarismCheck, SimilarityResult, utc_now
from app.services.document_chunk_service import DocumentChunkService
from app.services.document_embedding_service import DocumentEmbeddingService
from app.services.document_text_service import DocumentTextService
from app.services.pdf_service import PDFService
from app.services.repository_candidate_service import RepositoryCandidateService
from app.services.similarity_result_service import SimilarityResultService
from app.services.similarity_service import SimilarityService


ProgressReporter = Callable[[int, str, str], None]


class RepositoryCheckService:
    """Run a repository check and persist its observable processing state.

    The same service is used by the synchronous legacy endpoint and the
    background upload flow.  Passing ``existing_check_id`` lets the latter
    continue the provisional check created before the HTTP response.
    """

    def __init__(
        self,
        pdf_service: Optional[PDFService] = None,
        similarity_service: Optional[SimilarityService] = None,
        similarity_result_service: Optional[SimilarityResultService] = None,
        document_chunk_service: Optional[DocumentChunkService] = None,
        document_text_service: Optional[DocumentTextService] = None,
        repository_candidate_service: Optional[RepositoryCandidateService] = None,
        document_embedding_service: Optional[DocumentEmbeddingService] = None,
    ):
        self.pdf_service = pdf_service or PDFService()
        self.similarity_service = similarity_service or SimilarityService()
        self.similarity_result_service = similarity_result_service or SimilarityResultService()
        self.document_chunk_service = document_chunk_service or DocumentChunkService(
            pdf_service=self.pdf_service,
            similarity_service=self.similarity_service,
        )
        self.document_text_service = document_text_service or DocumentTextService(
            pdf_service=self.pdf_service,
            similarity_service=self.similarity_service,
        )
        self.repository_candidate_service = repository_candidate_service or RepositoryCandidateService(
            document_chunk_service=self.document_chunk_service,
        )
        self.document_embedding_service = document_embedding_service or DocumentEmbeddingService(
            document_chunk_service=self.document_chunk_service,
        )
        self.logger = logging.getLogger(__name__)

    def run_repository_check(
        self,
        *,
        document_id: int,
        db: Session,
        requester_user_id: Optional[int] = None,
        existing_check_id: Optional[int] = None,
        candidate_limit: int = 20,
        auto_index_embeddings: bool = True,
        auto_index_repository_limit: int = 50,
        report_progress: Optional[ProgressReporter] = None,
    ) -> Dict[str, Any]:
        """Compare one document against the repository and save its results."""
        target_doc = db.query(Document).filter(Document.id == document_id).first()
        if not target_doc:
            raise HTTPException(status_code=404, detail="Dokumen target tidak ditemukan.")

        target_owner_id = target_doc.user_id or requester_user_id
        check_record = self._get_or_create_check(
            db=db,
            document_id=target_doc.id,
            effective_user_id=target_owner_id,
            existing_check_id=existing_check_id,
        )

        try:
            self._set_progress(
                check_record,
                db,
                5,
                "preparing",
                "Menyiapkan pengecekan dokumen.",
                report_progress,
            )
            self._set_progress(
                check_record,
                db,
                15,
                "extracting_text",
                "Mengekstrak teks dari dokumen.",
                report_progress,
            )
            try:
                target_data = self.document_text_service.get_or_extract_text(target_doc, db)
                clean_target = target_data["cleaned_text"]
            except Exception as exc:
                self.logger.error("Gagal mengekstrak teks dari dokumen target %s: %s", target_doc.id, exc)
                raise HTTPException(
                    status_code=500,
                    detail=f"Gagal membaca teks dokumen target '{target_doc.title}': {str(exc)}",
                ) from exc

            self._set_progress(
                check_record,
                db,
                28,
                "building_chunks",
                "Menyiapkan potongan teks untuk perbandingan.",
                report_progress,
            )
            target_chunks = self.document_chunk_service.get_or_build_sentence_chunks(target_doc, db)

            repo_query = db.query(Document).filter(Document.id != document_id)
            if target_owner_id is not None:
                # Jangan membandingkan revisi mahasiswa terhadap dokumen miliknya sendiri.
                repo_query = repo_query.filter(Document.user_id != target_owner_id)
            repo_docs = repo_query.all()

            self._set_progress(
                check_record,
                db,
                35,
                "indexing_embeddings",
                "Menyiapkan indeks dokumen repositori.",
                report_progress,
            )
            embedding_auto_index = (
                self._auto_index_embeddings_for_check(
                    target_doc=target_doc,
                    repo_docs=repo_docs,
                    db=db,
                    enabled=auto_index_embeddings,
                    repository_limit=auto_index_repository_limit,
                    report_progress=lambda progress, stage, message: self._set_progress(
                        check_record,
                        db,
                        progress,
                        stage,
                        message,
                        report_progress,
                    ),
                )
                if repo_docs
                else self._empty_embedding_status(
                    enabled=auto_index_embeddings,
                    repository_limit=auto_index_repository_limit,
                )
            )

            if not repo_docs:
                check_record.overall_similarity = 0.0
                self._set_progress(
                    check_record,
                    db,
                    96,
                    "generating_highlight",
                    "Menyiapkan salinan PDF hasil pengecekan.",
                    report_progress,
                )
                try:
                    highlighted_file = self._generate_highlighted_pdf(
                        check_record=check_record,
                        target_doc=target_doc,
                        plagiarized_sentences=[],
                        db=db,
                    )
                except Exception as highlight_error:
                    self.logger.warning(
                        "Gagal membuat highlighted PDF untuk check %s: %s",
                        check_record.id,
                        highlight_error,
                    )
                    highlighted_file = self._generate_fallback_highlighted_pdf(
                        check_record=check_record,
                        target_doc=target_doc,
                        db=db,
                    )
                self._complete_check(check_record, db, report_progress)
                return {
                    "check_id": check_record.id,
                    "target_document": target_doc.title,
                    "highest_similarity_percentage": "0.0%",
                    "total_repository_checked": 0,
                    "total_repository_available": 0,
                    "total_repository_chunks": 0,
                    "total_target_embedding_chunks": 0,
                    "total_repository_embedding_chunks": 0,
                    "candidate_limit": candidate_limit,
                    "candidate_strategy": "none",
                    "candidate_strategy_reason": "repository_empty",
                    "semantic_search_used": False,
                    "semantic_fallback_reason": None,
                    "embedding_auto_index": embedding_auto_index,
                    "chapter_aware": False,
                    "target_chapters": [],
                    "matches": [],
                    "total_plagiarized_sentences": 0,
                    "highlight_summary": self.similarity_result_service.summarize_highlight_matches([]),
                    "message": "Dokumen berhasil disimpan. Belum ada dokumen lain di repositori kampus untuk dibandingkan.",
                    "highlighted_pdf_available": bool(highlighted_file and os.path.exists(highlighted_file)),
                    "chapter_validation": target_data.get("chapter_validation"),
                }

            self._set_progress(
                check_record,
                db,
                50,
                "selecting_candidates",
                "Mencari dokumen repositori yang relevan.",
                report_progress,
            )
            candidate_result = self.repository_candidate_service.find_candidates(
                target_document=target_doc,
                repository_documents=repo_docs,
                db=db,
                top_k=candidate_limit,
            )
            candidate_docs = [candidate["document"] for candidate in candidate_result["candidates"]]
            repo_sentences_all = candidate_result["reference_corpus"]

            results = []
            max_score = 0.0
            total_candidates = len(candidate_docs)
            for index, repo_doc in enumerate(candidate_docs, start=1):
                progress = 60 + int(18 * index / max(total_candidates, 1))
                self._set_progress(
                    check_record,
                    db,
                    progress,
                    "comparing_documents",
                    f"Membandingkan dokumen {index} dari {total_candidates}.",
                    report_progress,
                )
                try:
                    repo_data = self.document_text_service.get_or_extract_text(repo_doc, db)
                    clean_repo = repo_data["cleaned_text"]
                    score = self.similarity_service.calculate_clean_text_similarity(clean_target, clean_repo)
                    max_score = max(max_score, score)
                    results.append(
                        {
                            "repository_document_id": repo_doc.id,
                            "title": repo_doc.title,
                            "similarity_score": round(score, 4),
                            "similarity_percentage": f"{round(score * 100, 2)}%",
                        }
                    )
                except Exception as repo_err:
                    self.logger.warning(
                        "Melewati dokumen repositori ID %s karena error: %s",
                        repo_doc.id,
                        repo_err,
                    )

            results.sort(key=lambda item: item["similarity_score"], reverse=True)
            check_record.overall_similarity = max_score
            db.add(check_record)
            db.commit()
            db.refresh(check_record)

            self._set_progress(
                check_record,
                db,
                82,
                "saving_results",
                "Menyimpan hasil kemiripan.",
                report_progress,
            )
            result_records_by_doc_id = {}
            for item in results:
                result_record = self.similarity_result_service.create_result(
                    db=db,
                    check_id=check_record.id,
                    source_document_id=item["repository_document_id"],
                    similarity_score=item["similarity_score"],
                )
                result_records_by_doc_id[item["repository_document_id"]] = result_record

            highlighted_file = None
            plagiarized_sentences: List[Dict[str, Any]] = []
            highlight_summary = self.similarity_result_service.summarize_highlight_matches([])
            try:
                self._set_progress(
                    check_record,
                    db,
                    87,
                    "matching_sentences",
                    "Mencari kalimat yang sama persis.",
                    report_progress,
                )
                lexical_matches = self.similarity_service.find_sentence_matches_per_source(
                    target_sentences=target_chunks,
                    reference_corpus=repo_sentences_all,
                    threshold=0.70,
                )
                # Highlight hanya merepresentasikan kecocokan teks leksikal.
                plagiarized_sentences = self._merge_highlight_matches(lexical_matches)
                highlight_summary = self.similarity_result_service.summarize_highlight_matches(
                    plagiarized_sentences
                )

                self._set_progress(
                    check_record,
                    db,
                    93,
                    "saving_matches",
                    "Menyimpan detail kalimat yang terdeteksi.",
                    report_progress,
                )
                matches_by_source_document_id: Dict[int, List[Dict[str, Any]]] = {}
                for match in plagiarized_sentences:
                    source_document_id = match.get("source_document_id")
                    if source_document_id is not None:
                        matches_by_source_document_id.setdefault(source_document_id, []).append(match)

                for source_document_id, matches in matches_by_source_document_id.items():
                    result_record = result_records_by_doc_id.get(source_document_id)
                    if result_record:
                        self.similarity_result_service.create_matches(
                            db=db,
                            result_id=result_record.id,
                            matches=matches,
                        )

                self._set_progress(
                    check_record,
                    db,
                    96,
                    "generating_highlight",
                    "Membuat PDF dengan penanda kalimat.",
                    report_progress,
                )
                highlighted_file = self._generate_highlighted_pdf(
                    check_record=check_record,
                    target_doc=target_doc,
                    plagiarized_sentences=self._deduplicate_visual_highlight_matches(
                        plagiarized_sentences
                    ),
                    db=db,
                )
            except Exception as highlight_error:
                # Similarity result remains useful even if the optional PDF artifact fails.
                self.logger.warning(
                    "Gagal membuat highlighted PDF untuk check %s: %s",
                    check_record.id,
                    highlight_error,
                )
                highlighted_file = self._generate_fallback_highlighted_pdf(
                    check_record=check_record,
                    target_doc=target_doc,
                    db=db,
                )

            self._complete_check(check_record, db, report_progress)
            return {
                "check_id": check_record.id,
                "target_document": target_doc.title,
                "highest_similarity_percentage": f"{round(max_score * 100, 2)}%",
                "total_repository_checked": len(results),
                "total_repository_available": candidate_result["total_repository_documents"],
                "total_repository_chunks": candidate_result["total_repository_chunks"],
                "total_target_embedding_chunks": candidate_result["total_target_embedding_chunks"],
                "total_repository_embedding_chunks": candidate_result["total_repository_embedding_chunks"],
                "candidate_limit": candidate_limit,
                "candidate_strategy": candidate_result["candidate_strategy"],
                "candidate_strategy_reason": candidate_result["candidate_strategy_reason"],
                "semantic_search_used": candidate_result["semantic_search_used"],
                "semantic_fallback_reason": candidate_result["semantic_fallback_reason"],
                "embedding_auto_index": embedding_auto_index,
                "chapter_aware": candidate_result["chapter_aware"],
                "target_chapters": candidate_result["target_chapters"],
                "matches": results,
                "total_plagiarized_sentences": len(plagiarized_sentences),
                "highlight_summary": highlight_summary,
                "highlighted_pdf_available": bool(highlighted_file and os.path.exists(highlighted_file)),
                "chapter_validation": target_data.get("chapter_validation"),
            }
        except Exception as exc:
            self._mark_failed(check_record.id, db, exc)
            raise

    def _get_or_create_check(
        self,
        *,
        db: Session,
        document_id: int,
        effective_user_id: Optional[int],
        existing_check_id: Optional[int],
    ) -> PlagiarismCheck:
        if existing_check_id is not None:
            check_record = db.query(PlagiarismCheck).filter(PlagiarismCheck.id == existing_check_id).first()
            if not check_record or check_record.document_id != document_id:
                raise ValueError("Check proses tidak cocok dengan dokumen yang diproses.")
            for result in list(check_record.similarity_results):
                db.delete(result)
        else:
            check_record = PlagiarismCheck(
                document_id=document_id,
                user_id=effective_user_id,
                overall_similarity=0.0,
            )
            db.add(check_record)

        check_record.status = "processing"
        check_record.progress = 0
        check_record.processing_stage = "queued"
        check_record.processing_message = "Pengecekan sedang menunggu untuk diproses."
        check_record.error_message = None
        check_record.overall_similarity = 0.0
        check_record.highlighted_file_path = None
        check_record.started_at = utc_now()
        check_record.completed_at = None
        check_record.updated_at = utc_now()
        db.commit()
        db.refresh(check_record)
        return check_record

    def _set_progress(
        self,
        check_record: PlagiarismCheck,
        db: Session,
        progress: int,
        stage: str,
        message: str,
        report_progress: Optional[ProgressReporter] = None,
    ) -> None:
        bounded_progress = max(0, min(99, int(progress)))
        check_record.status = "processing"
        check_record.progress = max(int(check_record.progress or 0), bounded_progress)
        check_record.processing_stage = stage
        check_record.processing_message = message
        check_record.error_message = None
        check_record.started_at = check_record.started_at or utc_now()
        check_record.updated_at = utc_now()
        db.add(check_record)
        db.commit()
        db.refresh(check_record)
        self._notify_progress(report_progress, check_record.progress, stage, message)

    def _complete_check(
        self,
        check_record: PlagiarismCheck,
        db: Session,
        report_progress: Optional[ProgressReporter],
    ) -> None:
        check_record.status = "completed"
        check_record.progress = 100
        check_record.processing_stage = "completed"
        check_record.processing_message = "Pengecekan kemiripan selesai."
        check_record.error_message = None
        check_record.completed_at = utc_now()
        check_record.updated_at = utc_now()
        db.add(check_record)
        db.commit()
        db.refresh(check_record)
        self._notify_progress(report_progress, 100, "completed", check_record.processing_message)

    def _mark_failed(self, check_id: int, db: Session, error: Exception) -> None:
        self.logger.exception("Pengecekan repository %s gagal: %s", check_id, error)
        try:
            db.rollback()
            check_record = db.query(PlagiarismCheck).filter(PlagiarismCheck.id == check_id).first()
            if not check_record:
                return
            check_record.status = "failed"
            check_record.processing_stage = "failed"
            check_record.processing_message = "Pengecekan gagal diproses. Silakan coba lagi."
            check_record.error_message = "Pengecekan gagal diproses. Silakan coba lagi."
            check_record.updated_at = utc_now()
            db.add(check_record)
            db.commit()
        except Exception as persist_error:
            self.logger.error(
                "Gagal menyimpan status gagal untuk check %s: %s",
                check_id,
                persist_error,
            )

    def _auto_index_embeddings_for_check(
        self,
        *,
        target_doc: Document,
        repo_docs: Sequence[Document],
        db: Session,
        enabled: bool,
        repository_limit: int,
        report_progress: Optional[ProgressReporter] = None,
    ) -> Dict[str, Any]:
        status: Dict[str, Any] = {
            "enabled": enabled,
            "configured": self.document_embedding_service.embedding_service.is_configured(),
            "embedding_model": self.document_embedding_service.embedding_service.model,
            "repository_limit": repository_limit,
            "target": None,
            "repository": [],
            "total_repository_considered": 0,
            "total_failed": 0,
            "reason": None,
        }
        if not enabled:
            status["reason"] = "disabled_by_request"
            return status
        if not status["configured"]:
            status["reason"] = "embedding_config_not_configured"
            return status

        self._notify_progress(report_progress, 36, "indexing_embeddings", "Mengindeks dokumen yang diunggah.")
        try:
            target_result = self.document_embedding_service.ensure_document_embeddings(target_doc, db)
            status["target"] = self._summarize_embedding_index_result(target_result)
            if target_result.get("status") == "failed":
                status["total_failed"] += 1
            target_indexed_chunks = target_result.get("total_indexed_chunks") or 0
            target_newly_indexed = (target_result.get("index_result") or {}).get("total_indexed") or 0
            if target_result.get("status") == "failed" or (
                target_indexed_chunks == 0
                and target_newly_indexed == 0
                and target_result.get("reason") in {"previous_embedding_errors", "embedding_config_not_configured"}
            ):
                status["reason"] = "target_embedding_unavailable_tfidf_fallback"
                return status
        except Exception as err:
            db.rollback()
            status["target"] = {
                "document_id": target_doc.id,
                "title": target_doc.title,
                "status": "failed",
                "reason": str(err),
            }
            status["total_failed"] += 1
            status["reason"] = "target_embedding_failed_tfidf_fallback"
            self.logger.warning("Gagal auto-index embedding target %s: %s", target_doc.id, err)
            return status

        repository_docs_to_index = list(repo_docs[:repository_limit]) if repository_limit else []
        status["total_repository_considered"] = len(repository_docs_to_index)
        if not repository_docs_to_index:
            status["reason"] = status["reason"] or "repository_auto_index_empty"
            return status

        total_to_index = len(repository_docs_to_index)
        for index, document in enumerate(repository_docs_to_index, start=1):
            progress = 36 + int(12 * index / max(total_to_index, 1))
            self._notify_progress(
                report_progress,
                progress,
                "indexing_embeddings",
                f"Mengindeks dokumen repositori {index} dari {total_to_index}.",
            )
            try:
                result = self.document_embedding_service.ensure_document_embeddings(document, db)
                status["repository"].append(self._summarize_embedding_index_result(result))
                if result.get("status") == "failed":
                    status["total_failed"] += 1
            except Exception as err:
                db.rollback()
                status["repository"].append(
                    {
                        "document_id": document.id,
                        "title": document.title,
                        "status": "failed",
                        "reason": str(err),
                    }
                )
                status["total_failed"] += 1
                self.logger.warning("Gagal auto-index embedding repository %s: %s", document.id, err)

        status["reason"] = status["reason"] or "best_effort_completed"
        return status

    @staticmethod
    def _summarize_embedding_index_result(result: Dict[str, Any]) -> Dict[str, Any]:
        index_result = result.get("index_result") or {}
        return {
            "document_id": result.get("document_id"),
            "title": result.get("title"),
            "status": result.get("status"),
            "reason": result.get("reason"),
            "total_chunks": result.get("total_chunks"),
            "total_indexed_chunks": result.get("total_indexed_chunks"),
            "total_pending_chunks": result.get("total_pending_chunks"),
            "total_failed_chunks": result.get("total_failed_chunks"),
            "total_indexed": index_result.get("total_indexed", 0),
            "total_failed": index_result.get("total_failed", 0),
        }

    def _empty_embedding_status(self, *, enabled: bool, repository_limit: int) -> Dict[str, Any]:
        return {
            "enabled": enabled,
            "configured": self.document_embedding_service.embedding_service.is_configured(),
            "embedding_model": self.document_embedding_service.embedding_service.model,
            "repository_limit": repository_limit,
            "target": None,
            "repository": [],
            "total_repository_considered": 0,
            "total_failed": 0,
            "reason": "repository_empty",
        }

    def _generate_highlighted_pdf(
        self,
        *,
        check_record: PlagiarismCheck,
        target_doc: Document,
        plagiarized_sentences: Iterable[Dict[str, Any]],
        db: Session,
    ) -> str:
        output_filename = f"highlighted_check_{check_record.id}_{Path(target_doc.file_path).stem}.pdf"
        highlighted_file = self.pdf_service.generate_highlighted_pdf(
            source_pdf_path=target_doc.file_path,
            plagiarized_sentences=plagiarized_sentences,
            output_filename=output_filename,
        )
        check_record.highlighted_file_path = highlighted_file
        check_record.updated_at = utc_now()
        db.add(check_record)
        db.commit()
        db.refresh(check_record)
        return highlighted_file

    def _generate_fallback_highlighted_pdf(
        self,
        *,
        check_record: PlagiarismCheck,
        target_doc: Document,
        db: Session,
    ) -> Optional[str]:
        try:
            return self._generate_highlighted_pdf(
                check_record=check_record,
                target_doc=target_doc,
                plagiarized_sentences=[],
                db=db,
            )
        except Exception as fallback_error:
            self.logger.warning(
                "Gagal membuat fallback PDF untuk check %s: %s",
                check_record.id,
                fallback_error,
            )
            return None

    @staticmethod
    def _merge_highlight_matches(*match_groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        seen = set()
        for group in match_groups:
            for match in group or []:
                key = (
                    match.get("page"),
                    (match.get("sentence") or "").strip().lower(),
                    match.get("source_document_id"),
                    (match.get("reference_sentence") or "").strip().lower(),
                    match.get("match_type"),
                )
                if key not in seen:
                    seen.add(key)
                    merged.append(match)
        return merged

    @staticmethod
    def _deduplicate_visual_highlight_matches(
        matches: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Keep one PDF annotation for a target segment shared by many sources."""
        visual_matches: List[Dict[str, Any]] = []
        seen = set()
        for match in matches or []:
            sentence = " ".join((match.get("sentence") or "").split()).casefold()
            if not sentence:
                continue
            key = (
                match.get("page"),
                sentence,
                match.get("start_position"),
                match.get("end_position"),
            )
            if key in seen:
                continue
            seen.add(key)
            visual_matches.append(match)
        return visual_matches

    def _notify_progress(
        self,
        report_progress: Optional[ProgressReporter],
        progress: int,
        stage: str,
        message: str,
    ) -> None:
        if not report_progress:
            return
        try:
            report_progress(progress, stage, message)
        except Exception as callback_error:
            self.logger.warning("Gagal mengirim pembaruan progress: %s", callback_error)


def run_repository_check_in_background(
    *,
    document_id: int,
    check_id: int,
    requester_user_id: Optional[int],
    candidate_limit: int = 20,
    auto_index_embeddings: bool = True,
    auto_index_repository_limit: int = 50,
    session_factory=SessionLocal,
    service_factory=RepositoryCheckService,
) -> None:
    """Execute a persisted check using a database session owned by the task."""
    logger = logging.getLogger(__name__)
    db = session_factory()
    try:
        service = service_factory()
        service.run_repository_check(
            document_id=document_id,
            db=db,
            requester_user_id=requester_user_id,
            existing_check_id=check_id,
            candidate_limit=candidate_limit,
            auto_index_embeddings=auto_index_embeddings,
            auto_index_repository_limit=auto_index_repository_limit,
        )
    except Exception as exc:
        # The service normally persists this state itself.  This fallback also
        # covers errors before its processing loop can start.
        logger.exception("Background check %s gagal: %s", check_id, exc)
        try:
            db.rollback()
            check_record = db.query(PlagiarismCheck).filter(PlagiarismCheck.id == check_id).first()
            if check_record and check_record.status != "completed":
                check_record.status = "failed"
                check_record.processing_stage = "failed"
                check_record.processing_message = "Pengecekan gagal diproses. Silakan coba lagi."
                check_record.error_message = "Pengecekan gagal diproses. Silakan coba lagi."
                check_record.updated_at = utc_now()
                db.add(check_record)
                db.commit()
        except Exception as persist_error:
            logger.error("Gagal menyimpan kegagalan background check %s: %s", check_id, persist_error)
    finally:
        db.close()
