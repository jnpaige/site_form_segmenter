"""Recover section headings from documents where Docling's layout model found none.

Docling marks headings structurally, and when its layout model fails on a page
it falls back to raw OCR text — the words are all there and correct, but
nothing is tagged as a heading. pass0 is heading-only by design, so such a
document is unsegmentable: it either gets skipped entirely or has every page
sent to tagging unfiltered.

The headings are still present, just as ordinary lines of text. They cannot be
found by pattern, though: on a fallback page every OCR line is preserved
separately, so a line-wrapped prose fragment ("The results point to its
relevance for the cha-") is indistinguishable by shape from a real heading
("Lithic Analysis"). Telling them apart is a semantic judgement, which is what
this pass asks a model to make.

Two things this deliberately does NOT trust the model with:

- Location. The model returns each heading verbatim; the text is then located
  in the page itself. A heading whose text cannot be found on the page it was
  claimed for is dropped rather than placed approximately.
- Invention. Nothing is synthesized that isn't already in the document, so a
  hallucinated section name fails the same verbatim check.

The recovered headings are injected back as ordinary heading blocks, so every
downstream stage — snippets, pass0, stage1's heading list — works unchanged.
"""
import json
import re
from pathlib import Path

from block_parser import Block

PROMPT_FILE = Path(__file__).parent.parent / "segment_types" / "recover_headings.txt"

# A document with at least this many headings per page is considered adequately
# structured; recovery is for documents well below it, not a second opinion on
# documents Docling already handled.
MIN_HEADINGS_PER_PAGE = 0.15
# One call's worth of document text. qwen2.5:32b at num_ctx 32768 handles a
# 26-page paper (~21k tokens) in a single pass; longer documents are chunked.
CHUNK_CHARS = 60000

_WS_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS_RE.sub(" ", (s or "")).strip().lower()


def needs_recovery(blocks: list[Block], pages: dict[int, str]) -> bool:
    headings = sum(1 for b in blocks if b.type == "heading")
    return len(pages) > 0 and (headings / len(pages)) < MIN_HEADINGS_PER_PAGE


def _drop_bibliography(pages: dict[int, str]) -> dict[int, str]:
    """Trim from the references section onward.

    References are a long run of title-case, capitalized fragments — the single
    richest source of false heading candidates in the whole document, and never
    a section this pipeline needs segmented. Only trims on a line that is
    essentially just the heading word, so a passing mention of "references" in
    prose doesn't truncate the paper.
    """
    marker = re.compile(
        r"^\W*(references(\s+cited)?|bibliography|works\s+cited|literature\s+cited)\W*$",
        re.IGNORECASE,
    )
    cut_from = None
    for page_num in sorted(pages):
        for line in pages[page_num].splitlines():
            if marker.match(line.strip()):
                cut_from = page_num
                break
        if cut_from is not None:
            break
    if cut_from is None or cut_from < max(pages) * 0.5:
        return pages  # too early to be the bibliography; keep everything
    return {p: t for p, t in pages.items() if p <= cut_from}


def _chunks(pages: dict[int, str], budget: int) -> list[dict[int, str]]:
    out, current, size = [], {}, 0
    for page_num in sorted(pages):
        text = pages[page_num]
        if current and size + len(text) > budget:
            out.append(current)
            current, size = {}, 0
        current[page_num] = text
        size += len(text)
    if current:
        out.append(current)
    return out


def _format(pages: dict[int, str]) -> str:
    return "\n".join(f"=== Page {p} ===\n{pages[p]}" for p in sorted(pages))


def recover_headings(
    pages: dict[int, str], model: str, base_url: str,
    temperature: float, timeout: float, num_ctx: int, label: str,
    extract_json,
) -> list[tuple[int, str]]:
    """[(page, verbatim heading text)], verified to occur on the page claimed."""
    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    trimmed = _drop_bibliography(pages)
    found: list[tuple[int, str]] = []

    for i, chunk in enumerate(_chunks(trimmed, CHUNK_CHARS)):
        result = extract_json(
            system_prompt=prompt,
            user_content=_format(chunk),
            model=model, base_url=base_url, temperature=temperature,
            timeout=timeout, label=f"headings:{label}[{i+1}]", num_ctx=num_ctx,
        )
        if not result:
            continue
        for entry in result.get("headings", []) or []:
            if not isinstance(entry, dict):
                continue
            text = (entry.get("text") or "").strip()
            page = entry.get("page")
            if not text or page is None:
                continue
            try:
                page = int(page)
            except (TypeError, ValueError):
                continue
            # Verbatim check: the heading must actually be on that page.
            if page in chunk and _norm(text) in _norm(chunk[page]):
                found.append((page, text))

    seen = set()
    unique = []
    for page, text in found:
        key = (page, _norm(text))
        if key not in seen:
            seen.add(key)
            unique.append((page, text))
    return unique


def inject_headings(blocks: list[Block], recovered: list[tuple[int, str]]) -> tuple[list[Block], int]:
    """Split blocks at recovered headings, retyping the heading itself.

    On a fallback page a whole page of text collapses into one or two very
    large paragraph blocks, because there are no blank lines to split on.
    Splitting at each heading both marks the heading and breaks those slabs
    into section-sized units, which is what makes per-block tagging meaningful
    on these documents at all.
    """
    by_page: dict[int, list[str]] = {}
    for page, text in recovered:
        by_page.setdefault(page, []).append(text)

    out: list[Block] = []
    injected = 0
    for block in blocks:
        candidates = by_page.get(block.page) or []
        if block.type != "paragraph" or not candidates:
            out.append(block)
            continue

        pieces = [block]
        for heading in candidates:
            next_pieces = []
            for piece in pieces:
                idx = _norm(piece.text).find(_norm(heading))
                if piece.type != "paragraph" or idx < 0:
                    next_pieces.append(piece)
                    continue
                # Locate on the raw text, not the normalized copy.
                raw_idx = piece.text.lower().find(heading.strip().lower())
                if raw_idx < 0:
                    next_pieces.append(piece)
                    continue
                before = piece.text[:raw_idx].strip()
                head = piece.text[raw_idx:raw_idx + len(heading.strip())].strip()
                after = piece.text[raw_idx + len(heading.strip()):].strip()
                n = len(next_pieces)
                if before:
                    next_pieces.append(Block(f"{piece.block_id}s{n}", piece.page, "paragraph", before))
                next_pieces.append(Block(f"{piece.block_id}h{n}", piece.page, "heading", f"## {head}"))
                injected += 1
                if after:
                    next_pieces.append(Block(f"{piece.block_id}s{n+1}", piece.page, "paragraph", after))
            pieces = next_pieces
        out.extend(pieces)
    return out, injected
