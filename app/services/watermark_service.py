"""Bounded generation of cached campus-watermarked reference PDFs."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

import pymupdf
from fastapi import HTTPException

from app.services.pdf_limits import positive_int_setting
from app.services.pdf_service import PDFService, UPLOAD_DIR

LOGO_PATH = Path(__file__).resolve().parents[1] / "assets" / "campus-logo.png"
WATERMARK_VERSION = b"campus-watermark-v1-opacity-0.12"


def render_watermarked_pdf(source: Path, output: Path, logo: Path = LOGO_PATH) -> None:
    """Worker-only PDF processing; never modifies the original document."""
    pixmap = pymupdf.Pixmap(str(logo))
    if pixmap.colorspace != pymupdf.csRGB:
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
    if not pixmap.alpha:
        pixmap = pymupdf.Pixmap(pixmap, 1)
    scale = min(1.0, 1024 / max(pixmap.width, pixmap.height))
    if scale < 1:
        pixmap = pymupdf.Pixmap(pixmap, max(1, int(pixmap.width * scale)), max(1, int(pixmap.height * scale)))
    # MuPDF samples contain premultiplied RGB. Scale all channels with alpha
    # so the original transparency and logo colours remain correct.
    samples = pixmap.samples.translate(bytes(round(value * .12) for value in range(256)))
    faded = pymupdf.Pixmap(pymupdf.csRGB, pixmap.width, pixmap.height, samples, True)
    with pymupdf.open(source) as document:
        if document.needs_pass:
            raise ValueError("Encrypted source PDF")
        PDFService()._read_page_texts(document)
        image_xref = 0
        for page in document:
            size = min(page.rect.width, page.rect.height) * .65
            center = page.rect.tl + (page.rect.br - page.rect.tl) / 2
            rectangle = pymupdf.Rect(center.x - size / 2, center.y - size / 2,
                                    center.x + size / 2, center.y + size / 2)
            # PyMuPDF inserts in unrotated page coordinates, while rect uses
            # displayed coordinates. Keep the watermark centered and upright.
            rectangle = rectangle * page.derotation_matrix
            image_xref = page.insert_image(rectangle, pixmap=faded if not image_xref else None,
                                           xref=image_xref, rotate=page.rotation, overlay=True)
        document.save(output, garbage=4, deflate=True)


class WatermarkService:
    def __init__(self, upload_dir: Path = UPLOAD_DIR, logo_path: Path = LOGO_PATH):
        self.upload_dir = upload_dir.resolve()
        self.logo_path = logo_path
        self.timeout = positive_int_setting("WATERMARK_TIMEOUT_SECONDS", 120)
        self.slots = threading.BoundedSemaphore(positive_int_setting("WATERMARK_MAX_CONCURRENT", 2))

    def resolve_source(self, stored_path: str) -> Path | None:
        candidate = Path(stored_path).resolve()
        if not candidate.is_file():
            candidate = (self.upload_dir / Path(stored_path).name).resolve()
        if not candidate.is_relative_to(self.upload_dir) or not candidate.is_file():
            return None
        return candidate

    def get_pdf(self, source: Path) -> Path:
        if not self.slots.acquire(blocking=False):
            raise HTTPException(503, "Pembuatan PDF sedang penuh. Silakan coba lagi.", headers={"Retry-After": "5"})
        temporary_path = None
        try:
            digest = hashlib.sha256(WATERMARK_VERSION)
            digest.update(self.logo_path.read_bytes())
            with source.open("rb") as original:
                for chunk in iter(lambda: original.read(1024 * 1024), b""):
                    digest.update(chunk)
            directory = self.upload_dir / "watermarked"
            directory.mkdir(exist_ok=True)
            output = directory / f"{digest.hexdigest()}.pdf"
            if output.is_file():
                return output
            # Unique temporary files + atomic replace prevent serving partial
            # PDFs when two requests first generate the same source at once.
            handle, temporary = tempfile.mkstemp(suffix=".pdf", dir=directory)
            os.close(handle)
            temporary_path = Path(temporary)
            subprocess.run(
                [sys.executable, "-m", "app.workers.watermark_pdf", str(source),
                 str(temporary_path), str(self.logo_path)],
                timeout=self.timeout, check=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                cwd=str(Path(__file__).resolve().parents[2]),
            )
            os.replace(temporary_path, output)
            return output
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Pembuatan PDF melewati batas waktu. Silakan coba lagi.") from None
        except (OSError, subprocess.SubprocessError, ValueError):
            raise HTTPException(503, "PDF pembanding sementara tidak dapat disiapkan.") from None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            self.slots.release()
