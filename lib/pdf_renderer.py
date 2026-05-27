"""Render PDF pages to base64-encoded JPEG images for vision model input.

Requires pymupdf: pip install pymupdf
"""
import base64
from pathlib import Path


def render_pages_to_images(pdf_path: Path, dpi: int = 150) -> dict[int, str]:
    """Render each page of a PDF to a base64-encoded JPEG string.

    Returns {page_num: base64_string} with 0-indexed page numbers matching
    the === Page N === convention in text_docling.txt.

    Raises ImportError if pymupdf is not installed.
    """
    try:
        import fitz
    except ImportError:
        raise ImportError(
            "pymupdf is required for vision mode.  Install it with:\n"
            "  pip install pymupdf\n"
            "or add it to pyproject.toml and run: uv sync"
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
