import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

    def _tokenize_with_positions(self, text: str) -> List[Dict[str, Any]]:
        tokens = []
        clean_cache = {}

        for match in re.finditer(r"[A-Za-z]+", text or ""):
            raw_word = match.group(0)
            cache_key = raw_word.lower()
            clean_word = clean_cache.get(cache_key)
            if clean_word is None:
                clean_word = self.clean_text(cache_key)
                clean_cache[cache_key] = clean_word

            for clean_token in clean_word.split():
                if clean_token:
                    tokens.append({
                        "clean": clean_token,
                        "start": match.start(),
                        "end": match.end(),
                    })

        return tokens

    def _normalize_exact_text(self, text: str) -> str:
        """Normalisasi ringan untuk memverifikasi teks yang akan di-highlight.

        TF-IDF dan preprocessing sengaja menghapus stopword agar perhitungan
        similarity lebih stabil. Highlight membutuhkan aturan yang lebih ketat:
        hanya perbedaan kapitalisasi dan tanda baca yang boleh diabaikan.
        """
        return re.sub(r"[\W_]+", " ", text or "", flags=re.UNICODE).strip().lower()

    def _find_matching_segments(
        self,
        target_sentence: str,
        reference_sentence: str,
        min_segment_tokens: int = 3,
    ) -> List[Dict[str, Any]]:
        target_tokens = self._tokenize_with_positions(target_sentence)
        reference_tokens = self._tokenize_with_positions(reference_sentence)

        if (
            len(target_tokens) < min_segment_tokens
            or len(reference_tokens) < min_segment_tokens
        ):
            return []

        reference_starts: Dict[str, List[int]] = {}
        for index, token in enumerate(reference_tokens):
            reference_starts.setdefault(token["clean"], []).append(index)

        segments = []
        target_index = 0
        while target_index < len(target_tokens):
            best_length = 0
            best_reference_start = None

            for reference_start in reference_starts.get(target_tokens[target_index]["clean"], []):
                length = 0
                while (
                    target_index + length < len(target_tokens)
                    and reference_start + length < len(reference_tokens)
                    and target_tokens[target_index + length]["clean"]
                    == reference_tokens[reference_start + length]["clean"]
                ):
                    length += 1

                if length > best_length:
                    best_length = length
                    best_reference_start = reference_start

            if best_length >= min_segment_tokens and best_reference_start is not None:
                target_start = target_tokens[target_index]["start"]
                target_end = target_tokens[target_index + best_length - 1]["end"]
                reference_start = reference_tokens[best_reference_start]["start"]
                reference_end = reference_tokens[best_reference_start + best_length - 1]["end"]

                target_text = target_sentence[target_start:target_end].strip()
                reference_text = reference_sentence[reference_start:reference_end].strip()
                if target_text and reference_text:
                    segments.append({
                        "text": target_text,
                        "reference_text": reference_text,
                        "start_position": target_start,
                        "end_position": target_end,
                        "token_count": best_length,
                    })

                target_index += best_length
            else:
                target_index += 1

        return segments

    def _ranges_overlap(self, left: Tuple[int, int], right: Tuple[int, int]) -> bool:
        return left[0] < right[1] and right[0] < left[1]

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
        min_segment_tokens: int = 3,
        max_reference_matches: int = 3,
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

        top_matches_by_target = self.tfidf_service.find_top_matches(
            query_texts=[clean for _, clean in valid_targets],
            corpus_texts=[item["clean"] for item in valid_references],
            top_n=max_reference_matches,
        )

        matches = []
        for (target_item, _), top_matches in zip(valid_targets, top_matches_by_target):
            target_sentence = (target_item.get("sentence") or "").strip()
            target_token_count = len(self._tokenize_with_positions(target_sentence))
            covered_ranges: List[Tuple[int, int]] = []
            emitted_segments = []
            emitted_sentence_match = False
            best_sentence_match = None

            for best_match in top_matches:
                score = best_match["score"]
                reference_index = int(best_match["index"])

                if reference_index < 0:
                    continue

                reference = valid_references[reference_index]
                reference_sentence = reference.get("sentence", "")
                if best_sentence_match is None or score > best_sentence_match["score"]:
                    best_sentence_match = {
                        "score": score,
                        "reference": reference,
                    }

                segments = self._find_matching_segments(
                    target_sentence=target_sentence,
                    reference_sentence=reference_sentence,
                    min_segment_tokens=min_segment_tokens,
                )

                if (
                    segments
                    and score >= threshold
                    and self._normalize_exact_text(target_sentence)
                    == self._normalize_exact_text(reference_sentence)
                ):
                    emitted_segments = []
                    covered_ranges = [(0, len(target_sentence))]
                    matches.append({
                        "page": target_item.get("page"),
                        "chapter": target_item.get("chapter"),
                        "sentence": target_sentence,
                        "submitted_sentence": target_sentence,
                        "similarity": round(score * 100, 2),
                        "matched_source": reference.get("matched_source", ""),
                        "source_document_id": reference.get("source_document_id"),
                        "source_chunk_index": reference.get("source_chunk_index"),
                        "source_page": reference.get("source_page"),
                        "reference_sentence": reference_sentence,
                        "reference_full_sentence": reference_sentence,
                        "match_type": "sentence",
                        "start_position": 0,
                        "end_position": len(target_sentence),
                    })
                    emitted_sentence_match = True
                    break

                for segment in segments:
                    if (
                        self._normalize_exact_text(segment["text"])
                        != self._normalize_exact_text(segment["reference_text"])
                    ):
                        continue

                    current_range = (segment["start_position"], segment["end_position"])
                    if any(self._ranges_overlap(current_range, existing) for existing in covered_ranges):
                        continue

                    covered_ranges.append(current_range)
                    coverage_score = (
                        segment["token_count"] / target_token_count
                        if target_token_count else 0.0
                    )
                    emitted_segments.append({
                        "page": target_item.get("page"),
                        "chapter": target_item.get("chapter"),
                        "sentence": segment["text"],
                        "submitted_sentence": target_sentence,
                        "similarity": round(max(score, coverage_score) * 100, 2),
                        "matched_source": reference.get("matched_source", ""),
                        "source_document_id": reference.get("source_document_id"),
                        "source_chunk_index": reference.get("source_chunk_index"),
                        "source_page": reference.get("source_page"),
                        "reference_sentence": segment["reference_text"],
                        "reference_full_sentence": reference_sentence,
                        "match_type": "segment",
                        "start_position": segment["start_position"],
                        "end_position": segment["end_position"],
                    })

            if emitted_sentence_match:
                continue

            if emitted_segments:
                matches.extend(emitted_segments)
                continue

            if not best_sentence_match or best_sentence_match["score"] < threshold:
                continue

            reference = best_sentence_match["reference"]
            reference_sentence = reference.get("sentence", "")
            if (
                self._normalize_exact_text(target_sentence)
                != self._normalize_exact_text(reference_sentence)
            ):
                continue

            matches.append({
                "page": target_item.get("page"),
                "chapter": target_item.get("chapter"),
                "sentence": target_sentence,
                "submitted_sentence": target_sentence,
                "similarity": round(best_sentence_match["score"] * 100, 2),
                "matched_source": reference.get("matched_source", ""),
                "source_document_id": reference.get("source_document_id"),
                "source_chunk_index": reference.get("source_chunk_index"),
                "source_page": reference.get("source_page"),
                "reference_sentence": reference_sentence,
                "reference_full_sentence": reference_sentence,
                "match_type": "sentence",
                "start_position": 0,
                "end_position": len(target_sentence),
            })

        return matches
