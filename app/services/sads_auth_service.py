"""Opt-in SADS login and temporary, manually approved student access.

Only the supplied POST JSON contract and active role are interpreted here.
The meanings of id1, id2 and sbt are deliberately not inferred. Until the KRS
API is available, a time-limited username list is a manual testing permission,
not evidence that a student is enrolled in a particular course.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException as HTTPClientError
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.schemas import Mahasiswa, User


def auth_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@dataclass(frozen=True)
class StudentAccessPolicy:
    provider: str = "local"
    allowed_usernames: frozenset[str] = frozenset()
    expires_at: datetime | None = None

    @classmethod
    def from_environment(cls) -> "StudentAccessPolicy":
        provider = os.getenv("STUDENT_AUTH_PROVIDER", "local").strip().lower()
        if provider not in {"local", "sads"}:
            raise RuntimeError("STUDENT_AUTH_PROVIDER harus local atau sads.")
        if provider == "local":
            return cls()
        allowed = frozenset(
            item.strip()
            for item in os.getenv("SADS_TEST_ALLOWED_USERNAMES", "").split(",")
            if item.strip()
        )
        if any(not re.fullmatch(r"[0-9]{1,100}", item) for item in allowed):
            raise RuntimeError("SADS_TEST_ALLOWED_USERNAMES harus berisi NIM, dipisahkan koma.")
        expiry = os.getenv("SADS_TEST_ACCESS_EXPIRES_AT", "").strip()
        expires_at = None
        if expiry:
            try:
                expires_at = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if expires_at.tzinfo is None:
                    raise ValueError
                expires_at = expires_at.astimezone(timezone.utc)
            except ValueError:
                raise RuntimeError(
                    "SADS_TEST_ACCESS_EXPIRES_AT harus berupa waktu ISO 8601 dengan zona waktu."
                ) from None
        return cls(provider, allowed, expires_at)

    def require_test_access(self, username: str) -> None:
        if (
            self.provider != "sads"
            or username not in self.allowed_usernames
            or self.expires_at is None
            or self.expires_at <= datetime.now(timezone.utc)
        ):
            raise auth_error(
                403,
                "Akses mahasiswa belum diizinkan. Verifikasi mata kuliah aktif belum tersedia; "
                "hubungi admin untuk izin uji sementara.",
            )


@dataclass(frozen=True)
class SadsLoginSettings:
    url: str
    timeout_seconds: int = 10
    extra_headers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_environment(cls) -> "SadsLoginSettings":
        url = os.getenv("SADS_LOGIN_URL", "").strip()
        try:
            parts = urlsplit(url)
            valid_url = (
                parts.scheme == "https"
                and bool(parts.hostname)
                and parts.username is None
                and parts.password is None
                and not parts.fragment
                and not any(character.isspace() or ord(character) < 32 for character in url)
            )
            parts.port  # Validate a supplied port without exposing the URL.
        except ValueError:
            valid_url = False
        if not valid_url:
            raise RuntimeError("Isi SADS_LOGIN_URL dengan URL HTTPS endpoint login resmi.")
        try:
            timeout = int(os.getenv("SADS_LOGIN_TIMEOUT_SECONDS", "10"))
        except ValueError:
            timeout = 0
        if not 1 <= timeout <= 30:
            raise RuntimeError("SADS_LOGIN_TIMEOUT_SECONDS harus antara 1 dan 30.")
        try:
            headers = json.loads(os.getenv("SADS_LOGIN_HEADERS_JSON", "{}"))
        except (json.JSONDecodeError, TypeError):
            headers = None
        reserved = {"host", "content-length", "transfer-encoding", "connection", "content-type", "accept"}
        if not isinstance(headers, dict) or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
            or key.lower() in reserved
            or not isinstance(value, str)
            or any(ord(character) < 32 or ord(character) > 126 for character in value)
            for key, value in headers.items()
        ):
            raise RuntimeError("SADS_LOGIN_HEADERS_JSON harus berupa object header autentikasi yang valid.")
        return cls(url, timeout, tuple(headers.items()))


@dataclass(frozen=True)
class SadsStudentProfile:
    username: str
    name: str
    study_program: str


class _NoLoginRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never resend login data or integration headers to a redirect target.
        return None


class SadsAuthService:
    max_response_bytes = 256 * 1024

    def __init__(self, settings: SadsLoginSettings | None = None):
        self.settings = settings or SadsLoginSettings.from_environment()
        self.opener = build_opener(_NoLoginRedirects())

    def authenticate(self, username: str, password: str) -> SadsStudentProfile | None:
        request = Request(
            self.settings.url,
            data=json.dumps({"username": username, "password": password}).encode("utf-8"),
            headers={
                **dict(self.settings.extra_headers),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.settings.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise auth_error(502, "Respons login SADS tidak dapat diproses.")
                content = response.read(self.max_response_bytes + 1)
        except HTTPError as error:
            code = error.code
            error.close()
            if code in {401, 403}:
                return None
            raise auth_error(503, "Login SADS sementara tidak tersedia. Silakan coba kembali.") from None
        except (URLError, OSError, HTTPClientError, ValueError):
            raise auth_error(503, "Login SADS sementara tidak tersedia. Silakan coba kembali.") from None
        if len(content) > self.max_response_bytes:
            raise auth_error(502, "Respons login SADS tidak dapat diproses.")
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeError, RecursionError):
            raise auth_error(502, "Respons login SADS tidak dapat diproses.") from None
        if not isinstance(payload, dict):
            raise auth_error(502, "Respons login SADS tidak dapat diproses.")
        role = payload.get("role")
        if not isinstance(role, dict) or not isinstance(role.get("name"), str):
            raise auth_error(502, "Respons login SADS tidak dapat diproses.")
        if role["name"].strip().casefold() != "mahasiswa":
            raise auth_error(403, "Login SADS untuk aplikasi ini hanya tersedia bagi mahasiswa.")
        name = payload.get("nama")
        program = payload.get("jurusan")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 255
            or not isinstance(program, str)
            or not program.strip()
            or len(program) > 255
            or any(
                not isinstance(payload.get(key), (str, int))
                or isinstance(payload.get(key), bool)
                or not str(payload.get(key)).strip()
                for key in ("id1", "id2")
            )
        ):
            raise auth_error(502, "Respons login SADS tidak dapat diproses.")
        return SadsStudentProfile(username, name.strip(), program.strip())

    @staticmethod
    def provision_student(
        db: Session,
        profile: SadsStudentProfile,
        hash_password: Callable[[str], str],
    ) -> User:
        user = db.query(User).filter(User.identifier == profile.username).with_for_update().first()
        if user and (user.role != "mahasiswa" or not user.is_active):
            raise auth_error(403, "Akun ini tidak dapat digunakan untuk login mahasiswa.")
        if user and user.auth_provider not in {"local", "sads"}:
            raise auth_error(403, "Akun ini tidak dapat digunakan untuk login mahasiswa.")
        if not user:
            user = User(identifier=profile.username, role="mahasiswa", auth_provider="sads")
            db.add(user)
        if not user.password or user.auth_provider == "local":
            # The SADS password is never passed to this method or saved. Retire
            # the old local password when linking an existing matching NIM.
            user.password = hash_password(secrets.token_urlsafe(48))
        user.auth_provider = "sads"
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise auth_error(409, "Akun sedang diperbarui. Silakan ulangi login.") from None
        if user.mahasiswa is None:
            user.mahasiswa = Mahasiswa(
                nama_lengkap=profile.name,
                program_studi=profile.study_program,
                fakultas="",  # Not supplied by the observed SADS response.
            )
        else:
            user.mahasiswa.nama_lengkap = profile.name
            user.mahasiswa.program_studi = profile.study_program
        # The caller commits the profile and session-version rotation together.
        return user
