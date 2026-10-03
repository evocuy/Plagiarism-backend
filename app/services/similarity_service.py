from typing import Any, Dict, List, Optional, Sequence

from app.services.preprocessing_service import PreprocessingService
from app.services.tfidf_service import TfidfService


class SimilarityService:
    def __init__(
        self,
        preprocessor: Optional[PreprocessingService] = None,
        tfidf_service: Optional[TfidfService] = None,
    ):
        self.preprocessor = preprocessor or PreprocessingService()
        self.tfidf_service = tfidf_service or TfidfService()

    def clean_text(self, text: str) -> str:
        return self.preprocessor.clean_text(text or "")

    def calculate_text_similarity(self, text_a: str, text_b: str) -> float:
        clean_a = self.clean_text(text_a)
        clean_b = self.clean_text(text_b)
        return self.calculate_clean_text_similarity(clean_a, clean_b)

    def calculate_clean_text_similarity(self, clean_text_a: str, clean_text_b: str) -> float:
        return self.tfidf_service.calculate_pair_similarity(clean_text_a, clean_text_b)

    def build_sentence_reference_corpus(
        self,
        sentences: Sequence[Dict[str, Any]],
        matched_source: str,
        source_document_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        reference_corpus = []

        for item in sentences:
            sentence = (item.get("sentence") or "").strip()
            clean_sentence = self.clean_text(sentence)
            if clean_sentence.strip():
                reference_corpus.append({
                    "clean": clean_sentence,
                    "sentence": sentence,
                    "matched_source": matched_source,
                    "source_document_id": source_document_id,
                })

        return reference_corpus

    def find_sentence_matches(
        self,
        target_sentences: Sequence[Dict[str, Any]],
        reference_sentences: Sequence[Dict[str, Any]],
        matched_source: str,
        source_document_id: Optional[int] = None,
        threshold: float = 0.70,
    ) -> List[Dict[str, Any]]:
        reference_corpus = self.build_sentence_reference_corpus(
            reference_sentences,
            matched_source,
            source_document_id=source_document_id,
        )
        return self.find_sentence_matches_against_references(
            target_sentences=target_sentences,
            reference_corpus=reference_corpus,
            threshold=threshold,
        )

    def find_sentence_matches_against_references(
        self,
        target_sentences: Sequence[Dict[str, Any]],
        reference_corpus: Sequence[Dict[str, Any]],
        threshold: float = 0.70,
    ) -> List[Dict[str, Any]]:
        valid_references = [
            item for item in reference_corpus
            if (item.get("clean") or "").strip()
        ]

        valid_targets = []
        for item in target_sentences:
            sentence = (item.get("sentence") or "").strip()
            clean_sentence = self.clean_text(sentence)
            if clean_sentence.strip():
                valid_targets.append((item, clean_sentence))

        if not valid_references or not valid_targets:
            return []

        best_matches = self.tfidf_service.find_best_matches(
            query_texts=[clean for _, clean in valid_targets],
            corpus_texts=[item["clean"] for item in valid_references],
        )

        matches = []
        for (target_item, _), best_match in zip(valid_targets, best_matches):
            score = best_match["score"]
            reference_index = int(best_match["index"])

            if reference_index < 0 or score < threshold:
                continue

            reference = valid_references[reference_index]
            matches.append({
                "page": target_item.get("page"),
                "sentence": target_item.get("sentence"),
                "similarity": round(score * 100, 2),
                "matched_source": reference.get("matched_source", ""),
                "source_document_id": reference.get("source_document_id"),
                "source_chunk_index": reference.get("source_chunk_index"),
                "source_page": reference.get("source_page"),
                "reference_sentence": reference.get("sentence", ""),
            })

        return matches
