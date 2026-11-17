"""One bounded upload-validation task; deliberately imports no database/app."""

import json
import os
import sys

# MuPDF can recover a PDF and extract its text while reporting drawing/font
# errors. Its default message destination is stdout, which is reserved for our
# single JSON response. Discard native diagnostics before importing the parser;
# they may also contain private document data.
os.environ["PYMUPDF_MESSAGE"] = f"path:{os.devnull}"
os.environ["PYMUPDF_LOG"] = f"path:{os.devnull}"

from app.services.document_structure_service import DocumentStructureService


if __name__ == "__main__":
    result = DocumentStructureService().validate_pdf_structure(sys.argv[1], sys.argv[2])
    print(json.dumps(result, ensure_ascii=True))
