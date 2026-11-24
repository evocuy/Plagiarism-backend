"""Serve local and MinIO PDFs through the authenticated FastAPI routes."""
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.services import pdf_storage


class _MinioPDFResponse(StreamingResponse):
    def __init__(self, response, headers: dict[str, str]):
        self.object_response = response
        super().__init__(response.stream(pdf_storage.CHUNK_SIZE), status_code=response.status,
                         media_type="application/pdf", headers=headers)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            pdf_storage.close_object(self.object_response)


def pdf_download(reference, filename: str, request: Request | None, *, missing_detail: str):
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if not pdf_storage.is_minio_reference(reference):
        return FileResponse(reference, media_type="application/pdf", filename=filename,
                            content_disposition_type="inline", headers=headers)

    byte_range = request.headers.get("range") if request else None
    # S3 handles a single range. A complete response is valid for multi-range
    # requests and for conditional ranges whose validator we have not checked.
    if byte_range and ("," in byte_range or request.headers.get("if-range")):
        byte_range = None
    try:
        response = pdf_storage.get_minio_storage().open(reference, byte_range)
    except FileNotFoundError:
        raise HTTPException(404, missing_detail, headers=headers) from None
    except pdf_storage.PDFRangeError:
        raise HTTPException(416, "Rentang byte PDF tidak tersedia.", headers=headers) from None
    except pdf_storage.PDFStorageError:
        raise HTTPException(503, "Penyimpanan PDF sementara tidak dapat diakses.", headers=headers) from None

    headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(filename, safe='')}"
    headers["Accept-Ranges"] = "bytes"
    for name in ("Content-Length", "Content-Range", "ETag", "Last-Modified"):
        if value := response.headers.get(name):
            headers[name] = value
    return _MinioPDFResponse(response, headers)
