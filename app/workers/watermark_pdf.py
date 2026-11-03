"""Separate process so native PDF work has a real wall-clock deadline."""
from pathlib import Path
import sys

from app.services.watermark_service import render_watermarked_pdf

if __name__ == "__main__":
    render_watermarked_pdf(*(Path(argument) for argument in sys.argv[1:]))
