"""Private PDF objects and compatibility with existing upload paths."""
from __future__ import annotations

import hashlib
import os
import shutil
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

from minio import Minio
from minio.error import MinioException, S3Error
from minio.helpers import check_bucket_name
from urllib3 import PoolManager, Retry, Timeout
from urllib3.exceptions import HTTPError


OBJECT_SCHEME = "minio://"
OBJECT_PREFIX = "plagiarism/"
CHUNK_SIZE = 1024 * 1024


class PDFStorageError(RuntimeError):
    """Storage is unavailable or the object reference is invalid."""


class PDFRangeError(PDFStorageError):
    """An object-store byte range was unsatisfiable."""


def _boolean_setting(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise PDFStorageError(f"{name} harus berupa true atau false.")


def minio_enabled() -> bool:
    return _boolean_setting("MINIO_ENABLED", False)


def is_minio_reference(reference: str | Path | None) -> bool:
    return isinstance(reference, str) and reference.startswith(OBJECT_SCHEME)


def pdf_available(reference: str | None) -> bool:
    # Persisted object references are set after upload. Avoid one remote HEAD
    # request per history row. The authenticated download checks availability.
    return bool(reference and (is_minio_reference(reference) or Path(reference).is_file()))


def managed_local_pdf(reference: str, upload_dir: Path) -> Path | None:
    if is_minio_reference(reference):
        return None
    root = upload_dir.resolve()
    path = Path(reference)
    for candidate in (path, root.parent / path, root / path.name):
        resolved = candidate.resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    return None


def file_identity(path: Path) -> tuple[int, str]:
    with path.open("rb") as stream:
        return os.fstat(stream.fileno()).st_size, hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def _storage_errors():
    try:
        yield
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
            raise FileNotFoundError("PDF tidak ditemukan di penyimpanan.") from None
        if exc.code == "InvalidRange":
            raise PDFRangeError("Rentang byte PDF tidak tersedia.") from None
        raise PDFStorageError("Penyimpanan PDF sementara tidak dapat diakses.") from None
    except (MinioException, HTTPError, OSError, ValueError):
        raise PDFStorageError("Penyimpanan PDF sementara tidak dapat diakses.") from None


def close_object(response) -> None:
    try:
        response.close()
    finally:
        response.release_conn()


class MinioPDFStorage:
    def __init__(self, client: Minio, bucket: str):
        self.client = client
        self.bucket = bucket

    def reference(self, key: str) -> str:
        reference = f"{OBJECT_SCHEME}{self.bucket}/{OBJECT_PREFIX}{key}"
        self.object_key(reference)
        return reference

    def object_key(self, reference: str) -> str:
        prefix = f"{OBJECT_SCHEME}{self.bucket}/{OBJECT_PREFIX}"
        key = reference.removeprefix(prefix)
        if (not reference.startswith(prefix) or not key.endswith(".pdf")
                or "\\" in key or any(part in {"", ".", ".."} for part in key.split("/"))):
            raise PDFStorageError("Referensi penyimpanan PDF tidak valid.")
        return OBJECT_PREFIX + key

    def upload(self, reference: str, path: Path) -> None:
        key = self.object_key(reference)
        with _storage_errors():
            self.client.fput_object(self.bucket, key, str(path), content_type="application/pdf",
                                    num_parallel_uploads=1)

    def exists(self, reference: str) -> bool:
        key = self.object_key(reference)
        try:
            with _storage_errors():
                self.client.stat_object(self.bucket, key)
            return True
        except FileNotFoundError:
            return False

    def delete(self, reference: str) -> None:
        key = self.object_key(reference)
        with _storage_errors():
            self.client.remove_object(self.bucket, key)

    def open(self, reference: str, byte_range: str | None = None):
        key = self.object_key(reference)
        with _storage_errors():
            return self.client.get_object(
                self.bucket, key, request_headers={"Range": byte_range} if byte_range else None,
            )

    @contextmanager
    def _read(self, reference: str):
        response = self.open(reference)
        try:
            with _storage_errors():
                yield response
        finally:
            close_object(response)

    @contextmanager
    def materialize(self, reference: str):
        with TemporaryDirectory(prefix="minio-pdf-") as directory:
            path = Path(directory) / "input.pdf"
            with self._read(reference) as response, path.open("wb") as output:
                shutil.copyfileobj(response, output, length=CHUNK_SIZE)
            yield path

    def verify(self, reference: str, size: int, sha256: str) -> bool:
        digest = hashlib.sha256()
        downloaded = 0
        with self._read(reference) as response:
            for chunk in response.stream(CHUNK_SIZE):
                digest.update(chunk)
                downloaded += len(chunk)
        return (downloaded, digest.hexdigest()) == (size, sha256)


@lru_cache(maxsize=1)
def get_minio_storage() -> MinioPDFStorage:
    endpoint = os.getenv("MINIO_ENDPOINT", "").strip()
    access_key = os.getenv("MINIO_ACCESS_KEY", "")
    secret_key = os.getenv("MINIO_SECRET_KEY", "")
    bucket = os.getenv("MINIO_BUCKET", "").strip()
    if not all((endpoint, access_key, secret_key, bucket)):
        raise PDFStorageError("Lengkapi endpoint, bucket, dan kredensial MinIO di konfigurasi backend.")
    secure = _boolean_setting("MINIO_USE_SSL", True)
    try:
        check_bucket_name(bucket)
        client = Minio(
            endpoint, access_key=access_key, secret_key=secret_key, secure=secure,
            http_client=PoolManager(
                timeout=Timeout(connect=5, read=30),
                retries=Retry(total=1, backoff_factor=0.2, status_forcelist=(502, 503, 504)),
            ),
        )
    except ValueError:
        raise PDFStorageError("Endpoint atau nama bucket MinIO tidak valid.") from None
    return MinioPDFStorage(client, bucket)
