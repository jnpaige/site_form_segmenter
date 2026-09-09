"""Split a document's page-indexed markdown text into addressable blocks —
paragraphs, headings, and individual table rows — for LLM tagging.

This exists because tagging needs stable, referenceable units: the LLM tags
a block by id, rather than free-quoting text back (which doesn't scale past
a handful of items and can't be reliably matched back to its source). Table
rows are split individually (not left as one block per table) because
comparison tables are exactly where cross-context evidence lives — a row
like "Cores | 12 | 8 | 3" needs its own tag set, not the whole table's.
"""
import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r'^#{1,6}\s+')
_TABLE_ROW_RE = re.compile(r'^\s*\|.*\|\s*$')
_TABLE_SEP_RE = re.compile(r'^\s*\|[\s:|-]+\|\s*$')  # |---|---| separator row


@dataclass
class Block:
    block_id: str
    page: int
    type: str  # "heading" | "paragraph" | "table_row"
    text: str


def _split_page(page_num: int, page_text: str, counter: list[int]) -> list[Block]:
    """counter is a single-element mutable list used as a running block index
    within the page, so table-row handling (which consumes multiple raw
    lines per emitted block) can share the same numbering as paragraphs."""
    blocks: list[Block] = []
    lines = page_text.splitlines()
    i = 0
    para_buf: list[str] = []

    def flush_para():
        if para_buf:
            text = " ".join(l.strip() for l in para_buf if l.strip())
            if text:
                counter[0] += 1
                blocks.append(Block(f"p{page_num}_b{counter[0]}", page_num, "paragraph", text))
            para_buf.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_para()
            i += 1
            continue

        if _HEADING_RE.match(stripped):
            flush_para()
            counter[0] += 1
            blocks.append(Block(f"p{page_num}_b{counter[0]}", page_num, "heading", stripped))
            i += 1
            continue

        if _TABLE_ROW_RE.match(stripped):
            flush_para()
            # Collect the contiguous run of table lines.
            table_lines = []
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i].strip()):
                table_lines.append(lines[i].strip())
                i += 1
            if not table_lines:
                continue
            header = table_lines[0]
            data_rows = [
                l for l in table_lines[1:] if not _TABLE_SEP_RE.match(l)
            ]
            for row in data_rows:
                counter[0] += 1
                blocks.append(Block(
                    f"p{page_num}_b{counter[0]}", page_num, "table_row",
                    f"columns: {header}  ||  row: {row}",
                ))
            continue

        para_buf.append(stripped)
        i += 1

    flush_para()
    return blocks


def split_blocks(pages: dict[int, str]) -> list[Block]:
    """Split every page's markdown text into addressable blocks, in page
    order. Block ids are unique within the document (p{page}_b{n})."""
    all_blocks: list[Block] = []
    for page_num in sorted(pages):
        counter = [0]
        all_blocks.extend(_split_page(page_num, pages[page_num], counter))
    return all_blocks


def format_blocks_for_prompt(blocks: list[Block]) -> str:
    return "\n".join(f"[{b.block_id}] {b.text}" for b in blocks)
