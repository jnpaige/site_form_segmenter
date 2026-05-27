"""Find trinomial input directories containing a text_docling.txt and optionally a PDF."""
import re
from pathlib import Path


def group_by_trinomial(input_dir: Path, pattern: str) -> dict[str, dict]:
    """Return {trinomial: {"txt": Path, "pdf": Path|None}} for each site found.

    PDF is the first .pdf file found in the same directory as text_docling.txt.
    """
    rx = re.compile(pattern, re.IGNORECASE)
    groups: dict[str, dict] = {}

    for txt_path in sorted(input_dir.rglob("text_docling.txt")):
        trinomial = _from_name(txt_path.parent.name, rx) or _from_content(txt_path, rx)
        if trinomial is None:
            print(f"  [warn] no trinomial found for {txt_path} — skipping")
            continue
        trinomial = trinomial.upper()

        pdf_path = next(
            (p for p in sorted(txt_path.parent.iterdir()) if p.suffix.lower() == ".pdf"),
            None,
        )
        groups[trinomial] = {"txt": txt_path, "pdf": pdf_path}

    return groups


def _from_name(name: str, rx: re.Pattern) -> str | None:
    m = rx.search(name)
    return m.group(1) if m else None


def _from_content(path: Path, rx: re.Pattern) -> str | None:
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:1000]
        m = rx.search(head)
        return m.group(1) if m else None
    except Exception:
        return None
