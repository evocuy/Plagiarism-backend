"""Bound HTTP bodies before FastAPI parses/spools multipart uploads."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _positive_setting(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} harus berupa bilangan bulat positif.") from exc
    if value < 1:
        raise RuntimeError(f"{name} harus berupa bilangan bulat positif.")
    return value


@dataclass(frozen=True)
class RequestLimitSettings:
    max_body_bytes: int
    max_concurrent: int = 4
    timeout_seconds: int = 60

    @classmethod
    def from_environment(cls) -> "RequestLimitSettings":
        # Permit multipart metadata in addition to the actual PDF byte limit.
        upload_bytes = _positive_setting("MAX_UPLOAD_SIZE_MB", 10) * 1024 * 1024
        return cls(
            max_body_bytes=_positive_setting(
                "MAX_REQUEST_BODY_BYTES", upload_bytes + 1024 * 1024
            ),
            max_concurrent=_positive_setting("MAX_CONCURRENT_BODY_REQUESTS", 4),
            timeout_seconds=_positive_setting("REQUEST_BODY_TIMEOUT_SECONDS", 60),
        )


class RequestLimitMiddleware:
    """Buffer a bounded body in memory, with a deadline and concurrency cap.

    Buffering before entering the app is deliberate: route dependencies and
    UploadFile size checks run after multipart ingestion. This middleware also
    checks streamed bodies without Content-Length. It must sit inside CORS so
    rejected requests still receive the configured CORS response headers.
    """

    def __init__(self, app: ASGIApp, settings: RequestLimitSettings | None = None):
        self.app = app
        self.settings = settings or RequestLimitSettings.from_environment()
        self._active_requests = 0

    @staticmethod
    async def _reject(send: Send, status: int, detail: str, *, retry: bool = False) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
        ]
        if retry:
            headers.append((b"retry-after", b"5"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return

        length_headers = [
            value for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        declared_length = None
        if length_headers:
            try:
                if len(length_headers) != 1 or not length_headers[0].isdigit():
                    raise ValueError
                declared_length = int(length_headers[0])
            except ValueError:
                await self._reject(send, 400, "Content-Length tidak valid.")
                return
            if declared_length > self.settings.max_body_bytes:
                await self._reject(send, 413, "Ukuran request melebihi batas yang diizinkan.")
                return

        # No await between admission check and increment: one ASGI event loop
        # cannot admit more than the configured number of bounded buffers.
        if self._active_requests >= self.settings.max_concurrent:
            await self._reject(send, 503, "Server sedang sibuk. Silakan coba kembali.", retry=True)
            return
        self._active_requests += 1
        try:
            chunks: list[bytes] = []
            total = 0
            try:
                async with asyncio.timeout(self.settings.timeout_seconds):
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            return
                        body = event.get("body", b"")
                        total += len(body)
                        if total > self.settings.max_body_bytes:
                            await self._reject(send, 413, "Ukuran request melebihi batas yang diizinkan.")
                            return
                        chunks.append(body)
                        if not event.get("more_body", False):
                            break
            except TimeoutError:
                await self._reject(send, 408, "Waktu unggah request habis. Silakan coba kembali.")
                return

            if declared_length is not None and total != declared_length:
                await self._reject(send, 400, "Ukuran request tidak sesuai Content-Length.")
                return

            buffered_body = b"".join(chunks)
            chunks.clear()
            delivered = False

            async def replay_receive() -> Message:
                nonlocal delivered, buffered_body
                if delivered:
                    return await receive()
                delivered = True
                event = {"type": "http.request", "body": buffered_body, "more_body": False}
                buffered_body = b""
                return event

            await self.app(scope, replay_receive, send)
        finally:
            self._active_requests -= 1
