"""Authorize SADS students using their current-period enrolled courses."""
from __future__ import annotations

import json
from dataclasses import dataclass
from http.client import HTTPException as HTTPClientError
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from app.services.sads_auth_service import _NoLoginRedirects, SadsStudentProfile, auth_error, read_sads_http_settings


@dataclass(frozen=True)
class SadsActiveCoursesSettings:
    url_template: str
    timeout_seconds: int = 10
    extra_headers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_environment(cls) -> "SadsActiveCoursesSettings":
        url, timeout, headers = read_sads_http_settings(
            "SADS_ACTIVE_COURSES_URL_TEMPLATE",
            "SADS_ACTIVE_COURSES_TIMEOUT_SECONDS",
            "SADS_ACTIVE_COURSES_HEADERS_JSON",
        )
        remaining = url.replace("{nim}", "")
        if (
            url.count("{nim}") != 1
            or "{nim}" not in urlsplit(url).path
            or "{" in remaining or "}" in remaining
        ):
            raise RuntimeError("SADS_ACTIVE_COURSES_URL_TEMPLATE harus memiliki satu {nim} pada path URL.")
        if not any(key.lower() == "secret" and value.strip() for key, value in headers):
            raise RuntimeError("SADS_ACTIVE_COURSES_HEADERS_JSON harus memuat header secret yang tidak kosong.")
        return cls(url, timeout, headers)


class SadsActiveCoursesService:
    max_response_bytes = 256 * 1024
    eligible_course_names = frozenset({"seminar", "skripsi", "tugas akhir"})

    def __init__(self, settings: SadsActiveCoursesSettings | None = None):
        try:
            self.settings = settings or SadsActiveCoursesSettings.from_environment()
        except RuntimeError:
            # Missing configuration must block students while admins can still log in.
            raise auth_error(503, "Verifikasi mata kuliah aktif belum dikonfigurasi. Hubungi administrator.") from None
        self.opener = build_opener(_NoLoginRedirects())

    def require_eligible_student(self, username: str) -> None:
        self._get_eligible_student_payload(username)

    def get_eligible_student_profile(self, username: str) -> SadsStudentProfile:
        """Use only the supplied identity fields when an admin selects a NIM."""
        payload = self._get_eligible_student_payload(username)
        student = payload["mahasiswa"]
        name = student.get("nama")
        program = student.get("prodi_kode")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 255 for value in (name, program)):
            raise auth_error(502, "Profil mahasiswa dari SADS tidak dapat diproses.")
        return SadsStudentProfile(username, name.strip(), program.strip())

    def _get_eligible_student_payload(self, username: str) -> dict:
        request = Request(
            self.settings.url_template.replace("{nim}", quote(username, safe="")),
            headers={
                "User-Agent": "INSTIKI-SimilarityChecker/1.0",
                "Accept": "application/json",
                **dict(self.settings.extra_headers),
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.settings.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise auth_error(503, "Verifikasi mata kuliah aktif SADS sementara tidak tersedia.")
                content = response.read(self.max_response_bytes + 1)
        except HTTPError as error:
            code = error.code
            error.close()
            raise auth_error(
                503, f"Verifikasi mata kuliah aktif SADS sementara tidak tersedia (HTTP {code}). Silakan coba kembali.",
            ) from None
        except (URLError, OSError, HTTPClientError, ValueError):
            raise auth_error(503, "Verifikasi mata kuliah aktif SADS sementara tidak tersedia. Silakan coba kembali.") from None
        if len(content) > self.max_response_bytes:
            raise auth_error(502, "Respons mata kuliah aktif SADS tidak dapat diproses.")
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeError, RecursionError):
            raise auth_error(502, "Respons mata kuliah aktif SADS tidak dapat diproses.") from None

        if not isinstance(payload, dict):
            raise auth_error(502, "Respons mata kuliah aktif SADS tidak dapat diproses.")
        student = payload.get("mahasiswa")
        period = payload.get("periode")
        courses = payload.get("jadwal_matakuliah")
        if (
            not isinstance(student, dict)
            or not isinstance(student.get("nim"), str)
            or student["nim"].strip() != username
            or not isinstance(period, str) or not period.strip()
            or not isinstance(courses, list)
            or any(
                not isinstance(course, dict)
                or not isinstance(course.get("matkul"), str)
                or not course["matkul"].strip()
                for course in courses
            )
        ):
            raise auth_error(502, "Respons mata kuliah aktif SADS tidak dapat diproses.")

        if not any(
            " ".join(course["matkul"].split()).casefold() in self.eligible_course_names
            for course in courses
        ):
            raise auth_error(
                403,
                "Akses hanya untuk mahasiswa yang sedang mengambil mata kuliah Seminar, Skripsi, atau Tugas Akhir pada periode aktif.",
            )
        return payload
