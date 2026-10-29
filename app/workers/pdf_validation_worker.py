"""One bounded upload-validation task; deliberately imports no database/app."""

import json
import sys

from app.services.document_structure_service import DocumentStructureService


if __name__ == "__main__":
    result = DocumentStructureService().validate_pdf_structure(sys.argv[1], sys.argv[2])
    print(json.dumps(result, ensure_ascii=True))
