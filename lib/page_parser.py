"""Parse text_docling.txt into page-indexed dicts.

text_docling.txt uses '=== Page N ===' as page boundary markers.
"""
import re
from pathlib import Path

_PAGE_HEADER = re.compile(r'^=== Page (\d+) ===$', re.MULTILINE)


def parse_pages(txt_path: Path) -> dict[int, str]:
    """Split text_docling.txt on === Page N === markers.

    Returns {page_num: page_text} with whitespace stripped per page.
    Returns empty dict if no page markers found.
    """
    text = txt_path.read_text(encoding="utf-8", errors="ignore")
    splits = list(_PAGE_HEADER.finditer(text))
    if not splits:
        return {}

    pages = {}
    for i, match in enumerate(splits):
        page_num = int(match.group(1))
        start = match.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(text)
        pages[page_num] = text[start:end].strip()

    return pages


def build_page_preview(pages: dict[int, str], max_chars: int = 500) -> str:
    """Build a truncated multi-page view for the segmentation prompt."""
    parts = []
    for page_num in sorted(pages):
        content = pages[page_num]
        preview = content[:max_chars]
        if len(content) > max_chars:
            preview += "\n[... truncated]"
        parts.append(f"=== Page {page_num} ===\n{preview}")
    return "\n\n".join(parts)
