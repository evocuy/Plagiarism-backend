import re
from typing import Any, Dict, Iterable, Optional

from sqlalchemy.orm import Session

from app.models.schemas import SimilarityMatch, SimilarityResult


class SimilarityResultService:
    @staticmethod
    def _count_words(text: Any) -> int:
        """Count display words without applying NLP preprocessing."""
        return len(re.findall(r"\b[\w'-]+\b", str(text or ""), flags=re.UNICODE))

    @staticmethod
    def _normalize_visual_text(text: Any) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip()).casefold()

    @staticmethod
    def _page_number(match: Dict[str, Any]) -> Optional[int]:
        try:
            page_number = int(match.get("page"))
        except (TypeError, ValueError):
            return None
        return page_number if page_number > 0 else None

    def summarize_highlight_matches(self, matches: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Build API-ready counts for the text that was detected and highlighted.

        A target segment can match more than one repository document.  The
        detected counts retain every source match, while highlighted counts
        de-duplicate that target segment because it is only highlighted once
        in the generated PDF.
        """
        match_list = list(matches or [])
        detected_word_count = 0
        visual_matches = {}
        sources = {}

        for match in match_list:
            submitted_text = match.get("sentence") or match.get("submitted_sentence") or ""
            submitted_word_count = self._count_words(submitted_text)
            detected_word_count += submitted_word_count

            page_number = self._page_number(match)
            visual_key = (
                page_number,
                self._normalize_visual_text(submitted_text),
                match.get("start_position"),
                match.get("end_position"),
            )
            visual_matches.setdefault(
                visual_key,
                {
                    "word_count": submitted_word_count,
                    "page_number": page_number,
                },
            )

            source_document_id = match.get("source_document_id")
            source_title = str(match.get("matched_source") or "Dokumen sumber tidak diketahui")
            source_key = (source_document_id, source_title.casefold())
            source = sources.setdefault(
                source_key,
                {
                    "document_id": source_document_id,
                    "title": source_title,
                    "match_count": 0,
                    "matched_word_count": 0,
                    "page_numbers": set(),
                },
            )
            source["match_count"] += 1
            source["matched_word_count"] += submitted_word_count
            if page_number is not None:
                source["page_numbers"].add(page_number)

        highlighted_page_numbers = sorted(
            {
                visual_match["page_number"]
                for visual_match in visual_matches.values()
                if visual_match["page_number"] is not None
            }
        )
        source_documents = []
        for source in sources.values():
            page_numbers = sorted(source["page_numbers"])
            source_documents.append(
                {
                    "document_id": source["document_id"],
                    "title": source["title"],
                    "match_count": source["match_count"],
                    "matched_word_count": source["matched_word_count"],
                    "page_numbers": page_numbers,
                    "page_count": len(page_numbers),
                }
            )
        source_documents.sort(
            key=lambda source: (source["title"].casefold(), str(source["document_id"] or ""))
        )

        return {
            "detected_match_count": len(match_list),
            "detected_word_count": detected_word_count,
            "highlighted_match_count": len(visual_matches),
            "highlighted_word_count": sum(
                visual_match["word_count"] for visual_match in visual_matches.values()
            ),
            "highlighted_page_numbers": highlighted_page_numbers,
            "highlighted_page_count": len(highlighted_page_numbers),
            "source_documents": source_documents,
        }

    def create_result(
        self,
        db: Session,
        check_id: int,
        source_document_id: int,
        similarity_score: float,
        chapter: Optional[str] = None,
    ) -> SimilarityResult:
        result = SimilarityResult(
            check_id=check_id,
            source_document_id=source_document_id,
            chapter=chapter,
            similarity_score=similarity_score,
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        return result

    def create_matches(
        self,
        db: Session,
        result_id: int,
        matches: Iterable[Dict[str, Any]],
    ) -> int:
        match_records = []
        for match in matches:
            similarity_percent = float(match.get("similarity") or 0.0)
            match_records.append(
                SimilarityMatch(
                    result_id=result_id,
                    source_text=match.get("reference_sentence") or "",
                    submitted_text=match.get("sentence") or "",
                    similarity_score=similarity_percent / 100,
                    page_number=match.get("page"),
                    start_position=match.get("start_position"),
                    end_position=match.get("end_position"),
                )
            )

        if not match_records:
            return 0

        db.add_all(match_records)
        db.commit()
        return len(match_records)
