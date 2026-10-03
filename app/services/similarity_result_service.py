from typing import Any, Dict, Iterable, Optional

from sqlalchemy.orm import Session

from app.models.schemas import SimilarityMatch, SimilarityResult


class SimilarityResultService:
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
