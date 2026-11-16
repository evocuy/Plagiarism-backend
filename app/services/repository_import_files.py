"""Filesystem planning/validation, usable without a database or JWT settings."""
from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.services.document_structure_service import DocumentStructureService


class ImportInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImportFile:
    path: Path
    relative_path: str
    document_type: str | None


def discover_pdfs(root: Path, document_type: str | None = None, limit: int | None = None) -> list[ImportFile]:
    if root.is_symlink() or not root.is_dir():
        raise ImportInputError("invalid_input_root", "Folder input harus berupa direktori biasa yang dapat dibaca.")
    root = root.resolve()
    files = []
    def unreadable(_error):
        raise ImportInputError("input_unreadable", "Sebagian folder input tidak dapat dibaca. Periksa izin folder.")

    for directory, subdirectories, filenames in os.walk(root, followlinks=False, onerror=unreadable):
        subdirectories[:] = sorted(name for name in subdirectories if not (Path(directory) / name).is_symlink())
        for name in sorted(filenames):
            candidate = Path(directory) / name
            if candidate.suffix.lower() != ".pdf" or candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                continue
            relative = resolved.relative_to(root)
            inferred = root.name.casefold() if root.name.casefold() in {"proposal", "skripsi"} else relative.parts[0].casefold()
            kind = document_type or (inferred if inferred in {"proposal", "skripsi"} else None)
            files.append(ImportFile(resolved, relative.as_posix(), kind))
    return sorted(files, key=lambda item: item.relative_path)[:limit]


def ensure_separate_directories(root: Path, uploads: Path) -> None:
    root, uploads = root.resolve(), uploads.resolve()
    if root.is_relative_to(uploads) or uploads.is_relative_to(root):
        raise ImportInputError("overlapping_directories", "Folder input dan penyimpanan uploads harus terpisah.")


def pdf_identity(path: Path, max_bytes: int) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            if size == 0 and b"%PDF-" not in block[:1024]:
                raise ImportInputError("invalid_pdf_signature", "File tidak memiliki header PDF yang valid.")
            size += len(block)
            if size > max_bytes:
                raise ImportInputError("file_too_large", "Ukuran PDF melebihi MAX_UPLOAD_SIZE_MB.")
            digest.update(block)
    if not size:
        raise ImportInputError("empty_pdf", "File PDF tidak boleh kosong.")
    return digest.hexdigest(), size


def validate_pdf(candidate: ImportFile, structure: DocumentStructureService) -> None:
    if candidate.document_type not in {"proposal", "skripsi"}:
        raise ImportInputError("unknown_document_type", "Letakkan PDF di folder proposal/skripsi atau isi --document-type.")
    result = asyncio.run(structure.validate_pdf_structure_for_upload(candidate.path, candidate.document_type))
    if not result.get("is_valid"):
        raise ImportInputError(result.get("error_code", "invalid_pdf"), result.get("message", "PDF tidak valid."))


def copy_pdf_atomically(source: Path, target: Path, expected_digest: str, max_bytes: int) -> int:
    """Reuse an orphaned completed copy after interruption; never serve a partial PDF."""
    if target.is_file():
        actual, _ = pdf_identity(target, max_bytes)
        if actual != expected_digest:
            raise ImportInputError("stored_file_changed", "Salinan PDF tersimpan memiliki checksum berbeda; periksa manual.")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{uuid4().hex}.part")
    try:
        digest, size = hashlib.sha256(), 0
        with source.open("rb") as origin, temporary.open("xb") as destination:
            while block := origin.read(1024 * 1024):
                size += len(block)
                if size > max_bytes:
                    raise ImportInputError("file_too_large", "Ukuran PDF berubah dan melebihi batas upload.")
                digest.update(block)
                destination.write(block)
            destination.flush()
            os.fsync(destination.fileno())
        if digest.hexdigest() != expected_digest:
            raise ImportInputError("source_changed", "PDF input berubah saat disalin. Ulangi setelah file selesai dipindahkan.")
        temporary.chmod(0o640)
        os.replace(temporary, target)
        return size
    finally:
        temporary.unlink(missing_ok=True)
