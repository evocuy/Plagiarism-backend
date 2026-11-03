"""Eligibility shared by repository retrieval and direct comparisons."""

from app.models.schemas import Document, REPOSITORY_STATUS_PUBLISHED


def comparison_source_filters() -> tuple:
    return (
        Document.deleted_at.is_(None),
        Document.repository_status == REPOSITORY_STATUS_PUBLISHED,
        Document.source_origin == "alumni",
        Document.user_id.is_(None),
    )


def is_comparison_source(document: Document) -> bool:
    return (
        document.deleted_at is None
        and document.repository_status == REPOSITORY_STATUS_PUBLISHED
        and document.source_origin == "alumni"
        and document.user_id is None
    )
