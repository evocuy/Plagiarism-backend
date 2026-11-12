"""Expose fixed public messages and references, never raw processing errors."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger(__name__)
TEXT_ERROR = "Teks dokumen tidak dapat diproses."
CHUNK_ERROR = "Potongan teks dokumen tidak dapat diproses."
EMBEDDING_ERROR = "Embedding dokumen tidak dapat diproses."
REINDEX_ERROR = "Indeks dokumen tidak dapat diperbarui."
DELETE_ERROR = "Dokumen tidak dapat dinonaktifkan."
CHECK_ERROR = "Pengecekan gagal diproses. Silakan coba lagi."
INTERNAL_ERROR = "Permintaan gagal diproses. Silakan coba lagi."
_PUBLIC_MESSAGES = frozenset({TEXT_ERROR, CHUNK_ERROR, EMBEDDING_ERROR, REINDEX_ERROR, DELETE_ERROR, CHECK_ERROR, INTERNAL_ERROR})
_PUBLIC_ERROR_PATTERN = re.compile(
    "(" + "|".join(re.escape(message) for message in _PUBLIC_MESSAGES) + r") \(ID error: [0-9a-f]{32}\)"
)


@dataclass(frozen=True)
class PublicError:
    message: str
    error_id: str

    @property
    def text(self) -> str:
        return f"{self.message} (ID error: {self.error_id})"

    def http_exception(self, status_code: int = 500) -> HTTPException:
        return HTTPException(status_code=status_code, detail=self.text, headers={
            "X-Error-ID": self.error_id, "Cache-Control": "no-store",
        })


def report_error(exc: Exception, message: str, *, operation: str) -> PublicError:
    error = PublicError(message, uuid4().hex)
    # Do not log str(exc), SQL parameters, vendor bodies, URLs or credentials.
    logger.error("operation=%s error_id=%s exception_type=%s", operation, error.error_id, type(exc).__name__)
    return error


def public_error_message(value: str | None, fallback: str) -> str | None:
    """Also redact historical database errors saved before this hardening."""
    if not value:
        return None
    return value if value in _PUBLIC_MESSAGES or _PUBLIC_ERROR_PATTERN.fullmatch(value) else fallback


async def sanitized_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Validation responses otherwise echo input values, including passwords.
    errors = [{"type": error["type"], "loc": error["loc"], "msg": error["msg"]} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors}, headers={"Cache-Control": "no-store"})


class ErrorHandlingMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def track_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, track_send)
        except Exception as exc:
            error = report_error(exc, INTERNAL_ERROR, operation="unhandled_request")
            if started:
                raise RuntimeError(f"Response interrupted; error_id={error.error_id}") from None
            response = JSONResponse(status_code=500, content={"detail": error.message, "error_id": error.error_id}, headers={
                "Cache-Control": "no-store", "X-Error-ID": error.error_id,
            })
            await response(scope, receive, send)
