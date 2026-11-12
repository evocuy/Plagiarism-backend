"""Session-bound CSRF tokens for browser requests authenticated by cookies."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send


CSRF_HEADER = "X-CSRF-Token"
CSRF_BOOTSTRAP_PATH = "/api/auth/csrf"
LOGIN_PATH = "/api/auth/login"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def normalize_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        or "*" in parsed.netloc or any(character.isspace() for character in parsed.netloc)
    ):
        raise ValueError("Invalid origin")
    port = parsed.port
    if port is not None and port < 1:
        raise ValueError("Invalid origin port")
    hostname = parsed.hostname.casefold().encode("idna").decode("ascii")
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{hostname}" + (f":{port}" if port is not None and port != default_port else "")


def trusted_csrf_origins(cors_origins: Sequence[str]) -> tuple[str, ...]:
    extra = os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    try:
        return tuple(sorted({normalize_origin(value) for value in [*cors_origins, *extra] if value.strip()}))
    except (ValueError, UnicodeError):
        raise RuntimeError("CORS_ORIGINS/CSRF_TRUSTED_ORIGINS harus berisi origin HTTP(S) utuh tanpa wildcard.") from None


def create_csrf_token(session_token: str, secret_key: str) -> str:
    # Derive a separate key rather than use the JWT signing key directly.
    csrf_key = hmac.new(secret_key.encode(), b"plagiarism-csrf-key-v1", hashlib.sha256).digest()
    return hmac.new(csrf_key, session_token.encode(), hashlib.sha256).hexdigest()


class CsrfMiddleware:
    def __init__(
        self, app: ASGIApp, *, secret_key: Callable[[], str],
        session_cookie: str, impersonation_cookie: str, trusted_origins: Sequence[str] = (),
    ):
        self.app = app
        self.secret_key = secret_key
        self.session_cookie = session_cookie
        self.impersonation_cookie = impersonation_cookie
        self.trusted_origins = frozenset(normalize_origin(value) for value in trusted_origins)

    @staticmethod
    async def _reject(send: Send, code: str) -> None:
        body = json.dumps({
            "detail": "Permintaan tidak dapat diverifikasi. Muat ulang halaman dan coba lagi.",
            "error_code": code,
        }).encode()
        await send({"type": "http.response.start", "status": 403, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
        ]})
        await send({"type": "http.response.body", "body": body})

    def _origin_is_trusted(self, request: Request, origins: list[str]) -> bool:
        if len(origins) != 1:
            return False
        try:
            origin = normalize_origin(origins[0])
            if origin in self.trusted_origins:
                return True
            # Only trust the actual request origin, never arbitrary forwarded
            # host/proto headers. Public reverse-proxy origins can be configured.
            host = request.headers.get("host")
            return bool(host) and origin == normalize_origin(f"{request.scope['scheme']}://{host}")
        except (ValueError, UnicodeError):
            return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"].rstrip("/")
        unsafe = scope["method"] not in SAFE_METHODS
        if not unsafe and path != CSRF_BOOTSTRAP_PATH:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        origins = request.headers.getlist("origin")
        if origins and not self._origin_is_trusted(request, origins):
            await self._reject(send, "csrf_origin_rejected")
            return
        cookies = request.cookies
        session = cookies.get(self.impersonation_cookie) if self.impersonation_cookie in cookies else cookies.get(self.session_cookie)
        # Login authenticates explicit credentials and replaces stale cookies.
        # Bearer-only callers have no ambient browser credentials to protect.
        if unsafe and path != LOGIN_PATH and session is not None:
            tokens = request.headers.getlist(CSRF_HEADER)
            if (
                len(tokens) != 1 or not re.fullmatch(r"[0-9a-f]{64}", tokens[0])
                or not hmac.compare_digest(tokens[0], create_csrf_token(session, self.secret_key()))
            ):
                await self._reject(send, "csrf_token_invalid")
                return
        await self.app(scope, receive, send)
