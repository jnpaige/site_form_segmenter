"""Render PDF pages to base64-encoded JPEG images for vision model input.

Requires PyMuPDF (already in pyproject.toml). If not installed, run:
  uv sync
"""
import base64
from pathlib import Path


def render_pages_to_images(pdf_path: Path, dpi: int = 150) -> dict[int, str]:
    """Render each page of a PDF to a base64-encoded JPEG string.

    Returns {page_num: base64_string} with 0-indexed page numbers matching
    the === Page N === convention in text_docling.txt.

    Raises ImportError if PyMuPDF is not installed.
    """
    try:
        import pymupdf as fitz          # PyMuPDF >= 1.24 canonical import
    except ImportError:
        try:
            import fitz                 # legacy name, still works on older installs
        except ImportError:
            raise ImportError(
                "PyMuPDF is required for vision mode.\n"
                "It is already declared in pyproject.toml — just run:\n"
                "  uv sync\n"
                "or manually: pip install PyMuPDF"
            )

    doc = fitz.open(str(pdf_path))
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    images: dict[int, str] = {}

    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        pix  = page.get_pixmap(matrix=mat)
        images[page_num] = base64.b64encode(pix.tobytes("jpeg")).decode("utf-8")

    doc.close()
    return images
