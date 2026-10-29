"""Resource ceilings shared by PDF extraction and cached sentence chunks."""

import os
from dataclasses import dataclass


class PDFLimitExceeded(ValueError):
    """The document exceeds a configured processing limit."""


def positive_int_setting(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} harus berupa bilangan bulat positif.") from exc
    if value < 1:
        raise RuntimeError(f"{name} harus lebih besar dari nol.")
    return value


@dataclass(frozen=True)
class PDFProcessingLimits:
    max_pages: int = 500
    max_text_chars: int = 2_000_000
    max_chunks: int = 10_000

    @classmethod
    def from_environment(cls) -> "PDFProcessingLimits":
        return cls(
            max_pages=positive_int_setting("MAX_PDF_PAGES", 500),
            max_text_chars=positive_int_setting("MAX_EXTRACTED_TEXT_CHARS", 2_000_000),
            max_chunks=positive_int_setting("MAX_DOCUMENT_CHUNKS", 10_000),
        )
