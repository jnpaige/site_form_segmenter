"""Render PDF pages for vision model input.

Two functions:
  render_pages_to_contact_sheet — all pages tiled in a grid, single JPEG (preferred)
  render_pages_to_images        — one JPEG per page, returned as a dict

Requires PyMuPDF (already in pyproject.toml). If not installed, run:
  uv sync
"""
import base64
import math
from pathlib import Path


def render_pages_to_images(
    pdf_path: Path,
    dpi: int = 96,
    max_dim: int = 1024,
) -> dict[int, str]:
    """Render each page of a PDF to a base64-encoded JPEG string.

    Returns {page_num: base64_string} with 0-indexed page numbers matching
    the === Page N === convention in text_docling.txt.

    dpi controls render resolution. max_dim caps the longest edge of the
    rendered image — if the page at the requested DPI would exceed max_dim
    pixels in either dimension, the scale is reduced to fit. This keeps
    Ollama request payloads manageable regardless of DPI setting.

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
    images: dict[int, str] = {}

    for page_num in range(len(doc)):
        page  = doc.load_page(page_num)
        rect  = page.rect
        scale = dpi / 72

        # Clamp scale so neither dimension exceeds max_dim
        if max_dim:
            fit = max_dim / max(rect.width * scale, rect.height * scale)
            if fit < 1.0:
                scale *= fit

        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        images[page_num] = base64.b64encode(pix.tobytes("jpeg")).decode("utf-8")

    doc.close()
    return images


def render_pages_to_contact_sheet(
    pdf_path: Path,
    thumb_width: int = 240,
    cols: int = 3,
) -> str:
    """Render all PDF pages as a single tiled contact-sheet JPEG.

    Pages are arranged in a grid (left-to-right, top-to-bottom). Each
    thumbnail is labeled with its 0-indexed page number in red so the vision
    model can reference specific pages in its JSON output.

    Returns a single base64-encoded JPEG string. Sending one image per
    request is more reliable with Ollama's vision endpoint than sending
    multiple images.

    Raises ImportError if PyMuPDF is not installed.
    """
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            raise ImportError(
                "PyMuPDF is required for vision mode.\n"
                "It is already declared in pyproject.toml — just run:\n"
                "  uv sync\n"
                "or manually: pip install PyMuPDF"
            )

    src_doc = fitz.open(str(pdf_path))
    n = len(src_doc)
    if n == 0:
        src_doc.close()
        return ""

    # Derive thumbnail height from the first page's aspect ratio
    ref_rect = src_doc[0].rect
    thumb_h = round(thumb_width * ref_rect.height / ref_rect.width)

    rows      = math.ceil(n / cols)
    total_w   = float(cols * thumb_width)
    total_h   = float(rows * thumb_h)

    # Build a single-page PDF to composite into
    out_doc  = fitz.open()
    out_page = out_doc.new_page(width=total_w, height=total_h)

    for i in range(n):
        row  = i // cols
        col  = i % cols
        rect = fitz.Rect(
            col * thumb_width,       row * thumb_h,
            (col + 1) * thumb_width, (row + 1) * thumb_h,
        )
        out_page.show_pdf_page(rect, src_doc, i)
        out_page.insert_text(
            fitz.Point(rect.x0 + 4, rect.y0 + 14),
            f"p{i}",
            fontsize=11,
            color=(0.9, 0.1, 0.1),
        )

    src_doc.close()

    pix  = out_page.get_pixmap(matrix=fitz.Matrix(1, 1))
    jpeg = pix.tobytes("jpeg")
    out_doc.close()
    return base64.b64encode(jpeg).decode("utf-8")
