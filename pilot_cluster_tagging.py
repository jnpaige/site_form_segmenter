#!/usr/bin/env python3
"""
pilot_cluster_tagging.py — pilot for the combined "identify assemblage/layer
clusters + tag every text block with which cluster(s) it discusses" pass.

Single-document pilot, modeled on the Tryon figure-coding pilot in the
sibling interpreting_archaeology_figures repo: scoped down, no run-metadata
machinery yet — that gets added once the approach is validated against a
real paper. See segment_types/report_pass_clusters.txt for the prompt and
lib/block_parser.py for how the document is split into taggable units.

Usage:
    uv run python pilot_cluster_tagging.py --md-path "path/to/report.md"
"""
import argparse
import functools
import re
from collections import defaultdict
import math
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))
from ollama_client import extract_json
from page_parser import parse_pages
from heading_recovery import recover_headings, inject_headings
from block_parser import (
    split_blocks, stitch_continuations, format_blocks_for_prompt, is_placeholder, Block,
)

PASS0_PROMPT_FILE = Path(__file__).parent / "segment_types" / "report_pass0_lithic_journal_sections.txt"
PASS1_PROMPT_FILE = Path(__file__).parent / "segment_types" / "report_pass1_clusters.txt"
PASS2_PROMPT_FILE = Path(__file__).parent / "segment_types" / "report_pass2_block_tags.txt"
# qwen3.8:27b (architecture qwen35, 262K native context) replaced qwen2.5:32b,
# whose 32K window was the binding constraint on stage 2 chunking. It is also
# Clef's backbone, so generative and decision-head runs compare same-base.
# Its outputs before 2026-10-07 in the pilot/holdout folders are qwen2.5:32b.
DEFAULT_MODEL = "qwen3.8:27b"
DEFAULT_PASS0_MODEL = "qwen3.8:27b"  # heading list is tiny — fits any context easily
DEFAULT_BASE_URL = "http://localhost:11434"

# Categories from report_pass0_lithic_journal_sections.txt worth feeding into
# the big cluster/tagging pass. Excluded: front_matter, abstract, background
# (general intro — not site-specific), boilerplate_end (competing interest,
# acknowledgments, supplementary, references).
RELEVANT_PASS0_CATEGORIES = ("site_context", "methods", "results", "discussion", "conclusions")

SNIPPET_MAX_CHARS = 120
# A heading at the foot of a page is described by the next page's opening text.
SNIPPET_MAX_PAGE_SPILL = 1

# Stage 1's context is assembled from whole sections, so it must be bounded
# explicitly — a 40-page article with a five-page introduction and a dozen
# tables should not silently overrun the model's context window.
STAGE1_HEADINGS_CHAR_CAP = 6000
STAGE1_SITE_TEXT_CHAR_CAP = 12000
STAGE1_TABLES_CHAR_CAP = 14000
# Rows shown per table, so one long data table cannot crowd out the summary
# tables elsewhere in the paper.
STAGE1_MAX_ROWS_PER_TABLE = 25
STAGE1_CAPTIONS_CHAR_CAP = 6000
STAGE1_TITLE_CHAR_CAP = 1500

# Stage 2 sizing. Blocks cap the reply (the model emits one JSON entry per
# block); the char budget caps the prompt. Both are needed — a paper can be
# long in blocks (many table rows) or long in prose, and either overruns.
STAGE2_MAX_CHUNK_BLOCKS = 60
STAGE2_RESERVED_TOKENS = 6000   # prompt template + room for the reply
CHARS_PER_TOKEN = 3.5           # the estimate already used in stage2 logging


def _heading_snippets(blocks: list[Block], max_chars: int = SNIPPET_MAX_CHARS) -> dict[str, str]:
    """First body text that actually FOLLOWS each heading, keyed by heading
    block id — helps pass0 tell a real section heading from a TOC listing.

    Anchored to the heading rather than to the page. Two systematic failures
    come from anchoring per page, and both bite hardest on the two-column
    layouts that put several headings on one page: the page's only snippet
    went to its first heading, leaving later ones — often the section that
    defines the site's layers — as bare strings with no evidence at all;
    and a heading that was not the first thing on its page received the
    text ABOVE it, i.e. the preceding section's content, presented to the
    model as if it described this section.
    """
    snippets: dict[str, str] = {}
    for i, b in enumerate(blocks):
        if b.type != "heading":
            continue
        parts: list[str] = []
        chars = 0
        for nxt in blocks[i + 1:]:
            if nxt.type == "heading":
                break
            if nxt.page > b.page + SNIPPET_MAX_PAGE_SPILL:
                break
            text = nxt.text.strip()
            if len(text) < 4 or is_placeholder(text):
                continue
            parts.append(text)
            chars += len(text)
            if chars >= max_chars:
                break
        snippet = " ".join(parts)[:max_chars].strip()
        if snippet:
            snippets[b.block_id] = snippet
    return snippets


def _format_headings_for_pass0(blocks: list[Block], snippets: dict[str, str]) -> str:
    lines = []
    for b in blocks:
        if b.type != "heading":
            continue
        line = f"p{b.page:>4}  {b.text}"
        snippet = snippets.get(b.block_id)
        if snippet:
            line += f'  |  "{snippet}"'
        lines.append(line)
    return "\n".join(lines)


PAGE_SNIPPET_CHARS = 90
_CAPTION_START = re.compile(r"^(?:table|fig(?:ure)?)\.?\s*\d+", re.IGNORECASE)


def _format_page_inventory(pages: dict[int, str], blocks: list[Block],
                           snippets: dict[str, str]) -> str:
    """One line per page of the document, listing what is on it.

    pass0 used to be handed the heading list alone, which meant a page with
    no heading was never mentioned in its prompt in any form. The prompt then
    asked it to "assign every page to exactly one of the categories" — an
    instruction it could not follow for pages it had not been told existed.
    Those pages came back unassigned and were silently dropped. Measured over
    the pilot corpus that cost 36% of pages, 22% of table rows and 39% of
    figures, and the losses were concentrated exactly where papers put
    full-page plates and data tables: Harmand's Fig. 4 artefact photographs,
    Broich's Table 6 (the four-way phase comparison), Stout's Fig. 9 core
    plates.

    Listing every page also gives the model something a purely arithmetic
    gap-fill can never have — knowing that a page holds artefact photographs
    or a data table is what should decide whether it belongs to results or to
    the back matter.
    """
    by_page_headings: dict[int, list[str]] = defaultdict(list)
    table_rows: dict[int, int] = defaultdict(int)
    for b in blocks:
        if b.type == "heading":
            by_page_headings[b.page].append(b)
        elif b.type == "table_row":
            table_rows[b.page] += 1

    lines = []
    for page in sorted(pages):
        marks = []
        if table_rows.get(page):
            marks.append(f"{table_rows[page]} table rows")

        caption = ""
        body = ""
        for line in pages[page].splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("|"):
                continue
            if not caption and _CAPTION_START.match(s):
                caption = s[:PAGE_SNIPPET_CHARS]
            elif not body and len(s) > 40 and not s.startswith("<!--"):
                body = s[:PAGE_SNIPPET_CHARS]
            if caption and body:
                break
        if caption:
            marks.append("figure/table caption")

        head = f"p{page:>4}"
        marker = f"  [{', '.join(marks)}]" if marks else ""
        lines.append(f"{head}{marker}")
        for b in by_page_headings.get(page, []):
            snippet = snippets.get(b.block_id)
            lines.append(f"        {b.text}" + (f'  |  "{snippet}"' if snippet else ""))
        if caption:
            lines.append(f'        caption: "{caption}"')
        if body and not by_page_headings.get(page):
            lines.append(f'        text: "{body}"')
    return "\n".join(lines)


def _rescue_data_pages(result: dict, blocks: list[Block]) -> tuple[dict, int]:
    """Move any table-bearing page out of boilerplate_end and into results.

    Journals put substantive data tables after the references often enough
    that this is a recurring failure rather than an edge case: Nature's
    "Extended Data Table" pages hold the paper's assemblage counts, and
    author manuscripts routinely place all tables at the end. Once pass0
    could actually see those pages it read their titles and filed them as
    supplementary — costing Harmand all 93 of its table rows and Niang all
    108 of theirs, which cancelled out the retention this pass had gained
    elsewhere.

    The prompt now says this too, but a rule this consequential should not
    depend on the model complying. A genuine reference list, acknowledgement
    or competing-interests statement has no table rows, so the guard cannot
    misfire on real back matter.
    """
    table_pages = {b.page for b in blocks if b.type == "table_row"}
    if not table_pages:
        return result, 0

    moved: list[int] = []
    kept_entries = []
    for entry in result.get("boilerplate_end", []) or []:
        if not isinstance(entry, dict):
            kept_entries.append(entry)
            continue
        stay = [int(p) for p in entry.get("pages", []) if int(p) not in table_pages]
        go = [int(p) for p in entry.get("pages", []) if int(p) in table_pages]
        moved.extend(go)
        if stay:
            entry["pages"] = stay
            kept_entries.append(entry)

    if not moved:
        return result, 0

    result["boilerplate_end"] = kept_entries
    result.setdefault("results", []).append(
        {"label": "(data tables moved out of back matter)", "pages": sorted(set(moved))})
    return result, len(set(moved))


def _fill_unassigned(result: dict, pages: dict[int, str]) -> tuple[dict, int]:
    """Give any page pass0 left out the section of the nearest page before it.

    A safety net for pages the model still omits, not the primary mechanism —
    the inventory above is what should make omissions rare. Pages after the
    first `boilerplate_end` anchor inherit that instead of carrying a
    substantive section into the references, which is what a naive
    forward-fill does and why it inflates retention while wrecking precision.
    """
    assigned: dict[int, str] = {}
    for category, entries in result.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                for p in entry.get("pages", []):
                    assigned.setdefault(int(p), category)

    boiler = sorted(_pages_for(result, "boilerplate_end"))
    first_boiler = boiler[0] if boiler else None

    filled = 0
    last = None
    additions: dict[str, list[int]] = defaultdict(list)
    for page in sorted(pages):
        if page in assigned:
            last = assigned[page]
            continue
        target = ("boilerplate_end" if first_boiler is not None and page > first_boiler
                  else last)
        if target is None:
            continue
        additions[target].append(page)
        filled += 1

    for category, page_list in additions.items():
        result.setdefault(category, []).append(
            {"label": "(pages filled in — not assigned by pass0)", "pages": page_list})
    return result, filled


def _pages_for(pass0_result: dict | None, category: str) -> set[int]:
    pages: set[int] = set()
    for entry in (pass0_result or {}).get(category, []) or []:
        if isinstance(entry, dict):
            pages.update(int(p) for p in entry.get("pages", []))
    return pages


def run_pass0(
    blocks: list[Block], pages: dict[int, str],
    model: str, base_url: str, temperature: float, timeout: float, num_ctx: int,
    label: str,
) -> tuple[set[int], dict | None, bool]:
    """Classify pages by section type off the heading list alone (cheap —
    tiny context regardless of document length), then return the set of
    pages worth feeding into the full block-tagging pass. Falls back to
    "every page" if pass0 fails, rather than silently dropping content.

    Returns (relevant_pages, result, ok). `ok` is False whenever pass0 could
    not produce a usable section map — no headings to read, no JSON back, or
    a map that assigns nothing to any substantive section. The caller uses
    that to decide whether to try recovering headings and ask again."""
    snippets = _heading_snippets(blocks)
    heading_text = _format_page_inventory(pages, blocks, snippets)
    if not _format_headings_for_pass0(blocks, snippets).strip():
        # Docling extracts no headings at all from some papers. pass0 is
        # heading-only by design, so there is nothing to classify — asking
        # anyway invites an invented section map that would then silently
        # filter pages away.
        print("  [warn] pass0 skipped — no headings extracted from this document; "
              "keeping every page")
        return set(pages), None, False

    prompt = PASS0_PROMPT_FILE.read_text(encoding="utf-8").replace("{HEADINGS}", heading_text)

    result = extract_json(
        system_prompt=prompt,
        user_content="Analyze the heading list above and return the section map.",
        model=model, base_url=base_url, temperature=temperature, timeout=timeout,
        label=f"pass0:{label}", num_ctx=num_ctx,
    )
    if result is None:
        print("  [warn] pass0 returned no JSON — falling back to every page")
        return set(pages), None, False

    result, n_rescued = _rescue_data_pages(result, blocks)
    if n_rescued:
        print(f"  [note] moved {n_rescued} table-bearing page(s) out of boilerplate_end "
              "into results")

    result, n_filled = _fill_unassigned(result, pages)
    if n_filled:
        print(f"  [note] pass0 left {n_filled} page(s) unassigned — filled from the "
              "preceding section")

    if not _pages_for(result, "site_context"):
        print("  [warn] pass0 assigned no site_context — this paper's layer/stratigraphy "
              "definitions were filed under another category, so stage1 will fall back "
              "(see build_stage1_context)")

    relevant: set[int] = set()
    for category in RELEVANT_PASS0_CATEGORIES:
        relevant.update(_pages_for(result, category))
    if not relevant:
        print("  [warn] pass0 found no relevant pages — falling back to every page")
        return set(pages), result, False
    return relevant, result, True


def _capped(text: str, cap: int, what: str) -> str:
    """Truncate a stage1 context section, saying so rather than silently."""
    if len(text) <= cap:
        return text
    print(f"  [warn] stage1: {what} truncated {len(text)} -> {cap} chars")
    return text[:cap]


def _figure_captions(doc_dir: Path) -> list[str]:
    """Printed figure captions, if pdf_ocr extracted them.

    A channel this pass ignored until now, and often a decisive one: captions
    name assemblages constantly ("Fig. 2. Artifacts from Koimilot Locus 1",
    "Fig. 3. Artifacts from Koimilot locus 2"). On a paper with no summary
    table and weak headings, the captions may be the only place the contexts
    are enumerated at all.
    """
    path = doc_dir / "figures" / "figures.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    figures = data if isinstance(data, list) else data.get("figures", [])
    out, seen = [], set()
    for fig in figures:
        caption = (fig.get("caption") or "").strip()
        if caption and caption not in seen:
            seen.add(caption)
            out.append(caption)
    return out


def _tables_for_stage1(all_blocks: list[Block],
                       max_rows: int = STAGE1_MAX_ROWS_PER_TABLE) -> list[str]:
    """Whole tables — header plus rows — reassembled from their row blocks.

    Passing only column headers, or only first-column row labels, throws away
    most of what a summary table says. Assemblages appear as columns in some
    papers and as rows in others, and the cells alongside them carry the
    artefact counts, raw material groups and reduction modes that say what each
    assemblage IS. Staurset's Table 1 lists all five sites with their lithic
    totals; reading its row labels alone yielded the site names with every
    count discarded.

    Rows per table are capped so that one long data table cannot crowd out the
    summary tables in the rest of the paper.
    """
    tables: dict[tuple[int, str], list[str]] = {}
    order: list[tuple[int, str]] = []
    for b in all_blocks:
        if b.type != "table_row":
            continue
        header, _, row = b.text.partition("||")
        header = header.replace("columns:", "", 1).strip()
        row = row.strip()
        if row.lower().startswith("row:"):
            row = row[len("row:"):].strip()
        key = (b.page, header)
        if key not in tables:
            tables[key] = []
            order.append(key)
        tables[key].append(row)

    out = []
    for page, header in order:
        rows = tables[(page, header)]
        shown = rows[:max_rows]
        body = "\n".join(shown)
        if len(rows) > len(shown):
            body += f"\n... ({len(rows) - len(shown)} further rows omitted)"
        out.append(f"[table on page {page}]\n{header}\n{body}")
    return out


def build_stage1_context(all_blocks: list[Block], pass0_result: dict | None,
                         doc_dir: Path | None = None) -> str:
    """Small, targeted context for cluster identification — NOT the full
    document: every heading (cheap, already validated useful by pass0),
    the site/stratigraphy description (where layers are usually first
    defined — identified via pass0's site_context pages), and every
    unique table column-header row in the document (cluster names are
    very often literally the columns of a summary table). Deliberately
    excludes full Results/Discussion prose — that's what made the combined
    single-shot pass too large; the cluster names themselves don't need it."""
    site_pages = _pages_for(pass0_result, "site_context")
    if not site_pages:
        # A paper with no dedicated site-description heading gets its
        # layer-defining section filed under background instead. Without a
        # fallback, stage1 would then run on headings and table headers
        # alone — which happens to succeed when the layers are also table
        # columns, and fails silently when they aren't.
        site_pages = _pages_for(pass0_result, "background")
        if site_pages:
            print(f"  [warn] stage1: no site_context pages — falling back to background "
                  f"pages {sorted(site_pages)} for the site description")

    headings = [f"p{b.page} {b.text}" for b in all_blocks if b.type == "heading"]
    site_text = [b.text for b in all_blocks if b.page in site_pages and b.type != "heading"]

    tables = _tables_for_stage1(all_blocks)
    captions = _figure_captions(doc_dir) if doc_dir else []

    title_pages = _pages_for(pass0_result, "front_matter") | _pages_for(pass0_result, "abstract")
    title_text = [b.text for b in all_blocks if b.page in title_pages and b.type != "table_row"]
    if not title_text and all_blocks:
        first_page = min(b.page for b in all_blocks)
        title_text = [b.text for b in all_blocks if b.page == first_page and b.type != "table_row"]

    # Every channel is labelled by where it came from and every one is
    # optional. Which channel actually enumerates the assemblages is not
    # predictable: a paper may name them in its headings, in a summary
    # table's columns or rows, in the stratigraphy prose, in its figure
    # captions, or in none of these — in which case the paper reports a
    # single undivided assemblage and the evidence for that is the title.
    # Building the context around any one privileged source silently fails
    # on every paper that happens to use a different one.
    parts = [
        ("TITLE AND ABSTRACT:\n" + _capped(
            "\n".join(title_text), STAGE1_TITLE_CHAR_CAP, "title/abstract")) if title_text else "",
        "SECTION HEADINGS:\n" + _capped(
            "\n".join(headings), STAGE1_HEADINGS_CHAR_CAP, "headings") if headings else "",
        ("SITE/STRATIGRAPHY DESCRIPTION (prose):\n" + _capped(
            "\n".join(site_text), STAGE1_SITE_TEXT_CHAR_CAP, "site description")) if site_text else "",
        ("TABLES (column headers and rows, every table in the paper):\n" + _capped(
            "\n\n".join(tables), STAGE1_TABLES_CHAR_CAP, "tables")) if tables else "",
        ("FIGURE CAPTIONS:\n" + _capped(
            "\n".join(captions), STAGE1_CAPTIONS_CHAR_CAP, "figure captions")) if captions else "",
    ]
    present = [p.split(":", 1)[0] for p in parts if p]
    print(f"  stage1 evidence channels: {', '.join(present) if present else 'NONE'}")
    return "\n\n".join(p for p in parts if p)


def run_stage1_clusters(
    all_blocks: list[Block], pass0_result: dict | None,
    model: str, base_url: str, temperature: float, timeout: float, num_ctx: int,
    label: str, doc_dir: Path | None = None,
) -> list[dict]:
    context = build_stage1_context(all_blocks, pass0_result, doc_dir)
    prompt = PASS1_PROMPT_FILE.read_text(encoding="utf-8")
    result = extract_json(
        system_prompt=prompt,
        user_content=context,
        model=model, base_url=base_url, temperature=temperature, timeout=timeout,
        label=f"stage1:{label}", num_ctx=num_ctx,
    )
    if result is None:
        return []
    return result.get("clusters", [])


def plan_stage2_chunks(blocks: list[Block], requested: int, num_ctx: int) -> int:
    """How many chunks stage2 actually needs, never fewer than `requested`.

    A fixed chunk count silently overruns the context window as soon as a
    paper is long enough: at three chunks a 400-block article sends ~40k
    tokens into a 32k window, and the overflow is dropped without an error.
    Size the split by the document instead, bounding both the prompt (chars)
    and the expected reply (one JSON entry per block).
    """
    total_chars = sum(len(b.text) for b in blocks)
    by_chars = math.ceil(total_chars / max(_chunk_char_budget(num_ctx), 1))
    by_blocks = math.ceil(len(blocks) / STAGE2_MAX_CHUNK_BLOCKS)
    return max(requested, by_chars, by_blocks, 1)


def _chunk_char_budget(num_ctx: int) -> int:
    """Prompt chars per stage2 call that leave room for the reply."""
    usable_tokens = max(num_ctx - STAGE2_RESERVED_TOKENS, STAGE2_RESERVED_TOKENS)
    return int(usable_tokens * CHARS_PER_TOKEN)


def chunk_blocks_by_page(blocks: list[Block], n_chunks: int) -> list[list[Block]]:
    """Split blocks into n_chunks contiguous page-range groups (page order
    preserved, no block split across a chunk boundary mid-page)."""
    pages_present = sorted({b.page for b in blocks})
    if n_chunks <= 1 or len(pages_present) <= n_chunks:
        return [blocks]
    group_size = max(1, len(pages_present) // n_chunks)
    page_groups = [pages_present[i:i + group_size] for i in range(0, len(pages_present), group_size)]
    chunks = []
    for group in page_groups:
        page_set = set(group)
        chunk = [b for b in blocks if b.page in page_set]
        if chunk:
            chunks.append(chunk)
    return chunks


def run_stage2_tagging(
    blocks: list[Block], clusters: list[dict],
    model: str, base_url: str, temperature: float, timeout: float, num_ctx: int,
    n_chunks: int, label: str,
) -> list[dict]:
    cluster_text = "\n".join(
        f"  {c.get('cluster_id')}: {c.get('label_as_reported')}" for c in clusters
    )
    prompt_template = PASS2_PROMPT_FILE.read_text(encoding="utf-8").replace("{CLUSTERS}", cluster_text)

    planned = plan_stage2_chunks(blocks, n_chunks, num_ctx)
    if planned > n_chunks:
        print(f"  stage2: raising chunk count {n_chunks} -> {planned} to fit num_ctx={num_ctx}")
    chunks = chunk_blocks_by_page(blocks, planned)
    print(f"  stage2: {len(blocks)} blocks -> {len(chunks)} chunk(s)")

    all_tags: list[dict] = []
    for i, chunk in enumerate(chunks):
        chunk_text = format_blocks_for_prompt(chunk)
        print(f"    chunk {i+1}/{len(chunks)}: {len(chunk)} blocks, {len(chunk_text)} chars "
              f"(~{len(chunk_text)//3.5:.0f} tokens est.)")
        result = extract_json(
            system_prompt=prompt_template,
            user_content=f"TEXT BLOCKS:\n\n{chunk_text}",
            model=model, base_url=base_url, temperature=temperature, timeout=timeout,
            label=f"stage2:{label}[{i+1}/{len(chunks)}]", num_ctx=num_ctx,
        )
        if result is None:
            print(f"    [error] chunk {i+1} returned no JSON — skipping")
            continue
        all_tags.extend(t for t in result.get("block_tags", []) if isinstance(t, dict))
    return all_tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md-path", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--temperature", type=float, default=0.05)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--pass0-model", default=DEFAULT_PASS0_MODEL)
    ap.add_argument("--no-pass0", action="store_true", help="Skip pass0 page filtering, send every block")
    ap.add_argument("--no-heading-recovery", action="store_true",
                    help="Skip LLM recovery of headings on documents where docling found none")
    ap.add_argument("--no-stitch", action="store_true",
                    help="Skip reattaching mid-sentence continuation fragments to the prose they continue")
    ap.add_argument("--stage2-chunks", type=int, default=3, help="Minimum number of page-range chunks for stage2 tagging (raised automatically to fit num_ctx)")
    ap.add_argument("--out", default=None, help="Output JSON path (default: alongside the .md file)")
    ap.add_argument("--think", action="store_true",
                    help="Let thinking-capable models (qwen3.x) reason before answering. Off by "
                         "default: Ollama turns thinking on when unasked, and the reasoning tokens "
                         "count against num_ctx, which stage 2's chunk plan does not budget for")
    args = ap.parse_args()

    # Every pass reaches the model through this one name (heading recovery is
    # handed it explicitly), so binding the think choice here covers them all.
    global extract_json
    extract_json = functools.partial(extract_json, think=args.think)

    md_path = Path(args.md_path)
    if not md_path.is_file():
        sys.exit(f"Not found: {md_path}")

    pages = parse_pages(md_path)
    if not pages:
        sys.exit(f"No '=== Page N ===' markers found in {md_path}")

    all_blocks = split_blocks(pages)
    n_merged = 0
    if not args.no_stitch:
        all_blocks, n_merged = stitch_continuations(all_blocks)
        print(f"Stitched    : {n_merged} continuation fragment(s) reattached to their prose")

    n_recovered = 0
    pass0_result = None
    if args.no_pass0:
        blocks = all_blocks
    else:
        print(f"Pass0 model : {args.pass0_model}")
        print("Running pass0 (heading-only page classification)...")
        relevant_pages, pass0_result, ok = run_pass0(
            all_blocks, pages, args.pass0_model, args.base_url,
            args.temperature, args.timeout, args.num_ctx, md_path.stem,
        )

        # Recovery is driven by pass0 actually failing, not by counting
        # headings beforehand. A heading-density threshold gets this wrong in
        # both directions: Chazan has 3 headings across 14 pages, which passes
        # any reasonable density test, yet pass0 could assign nothing but
        # front_matter and abstract from them. What matters is whether pass0
        # produced a usable map, which is only knowable after asking it.
        if not ok and not args.no_heading_recovery:
            print("Headings    : pass0 could not segment this document — "
                  "recovering headings from raw text and retrying...")
            recovered = recover_headings(
                pages, args.pass0_model, args.base_url, args.temperature,
                args.timeout, args.num_ctx, md_path.stem, extract_json,
            )
            if recovered:
                all_blocks, n_recovered = inject_headings(all_blocks, recovered)
                print(f"Headings    : recovered {len(recovered)}, injected {n_recovered}")
                for page, text in recovered:
                    print(f"  p{page}: {text[:70]}")
                retry_pages, retry_result, retry_ok = run_pass0(
                    all_blocks, pages, args.pass0_model, args.base_url,
                    args.temperature, args.timeout, args.num_ctx, md_path.stem,
                )
                if retry_ok:
                    relevant_pages, pass0_result = retry_pages, retry_result
                else:
                    print("  [warn] pass0 still could not segment after recovery — "
                          "keeping every page")
            else:
                print("Headings    : none recovered — keeping every page")
        blocks = [b for b in all_blocks if b.page in relevant_pages]
        print(f"Pass0       : {len(pages)} pages -> {len(relevant_pages)} relevant pages "
              f"-> {len(all_blocks)} -> {len(blocks)} blocks")
        if pass0_result:
            for category, entries in pass0_result.items():
                if not isinstance(entries, list):
                    continue
                labels = ", ".join(e.get("label", "?") for e in entries if isinstance(e, dict))
                print(f"  {category}: {labels}")

    n_headings = sum(1 for b in blocks if b.type == "heading")
    n_paras = sum(1 for b in blocks if b.type == "paragraph")
    n_rows = sum(1 for b in blocks if b.type == "table_row")
    print(f"Pages       : {len(pages)}")
    print(f"Blocks      : {len(blocks)}  (headings={n_headings}, paragraphs={n_paras}, table_rows={n_rows})")
    print(f"Model       : {args.model}")
    print(f"num_ctx     : {args.num_ctx}")

    print("\nStage 1: identifying clusters (small targeted context)...")
    clusters = run_stage1_clusters(
        all_blocks, pass0_result, args.model, args.base_url,
        args.temperature, args.timeout, args.num_ctx, md_path.stem,
        doc_dir=md_path.parent,
    )
    if not clusters:
        # A paper reporting one undivided assemblage is a normal case, not a
        # failure — and aborting here was wrong for it. The prompt asks for a
        # single whole-assemblage cluster in that situation; this is the net
        # for when the model returns nothing anyway. Tagging every block
        # against one site-wide context is a defensible reading of such a
        # paper, and far better than discarding the document.
        clusters = [{
            "cluster_id": "c1",
            "label_as_reported": md_path.stem,
            "also_known_as": [],
            "locality": md_path.stem,
            "archaeological_layer": None,
            "geological_layer": None,
            "archaeological_stratigraphy": None,
            "n_artefacts": None,
            "evidence": ["fallback: stage1 identified no distinct assemblages"],
        }]
        print("  [warn] stage1 identified no distinct contexts — falling back to a "
              "single whole-assemblage cluster")
    print(f"  {len(clusters)} cluster(s) identified")

    print("\nStage 2: tagging blocks (chunked)...")
    tags = run_stage2_tagging(
        blocks, clusters, args.model, args.base_url,
        args.temperature, args.timeout, args.num_ctx, args.stage2_chunks, md_path.stem,
    )

    tagged_ids = {t["block_id"] for t in tags if isinstance(t, dict) and t.get("block_id")}
    all_ids = {b.block_id for b in blocks}

    print(f"\nClusters found: {len(clusters)}")
    for c in clusters:
        print(f"  {c.get('cluster_id')}: {c.get('label_as_reported')}")
        print(f"      locality={c.get('locality')!r}  "
              f"arch_layer={c.get('archaeological_layer')!r}  "
              f"geo_layer={c.get('geological_layer')!r}")
        print(f"      culture={c.get('archaeological_stratigraphy')!r}  "
              f"n={c.get('n_artefacts')}  evidence={c.get('evidence')}")

    print(f"\nBlock tags returned: {len(tags)} / {len(blocks)} blocks")
    missing = all_ids - tagged_ids
    extra = tagged_ids - all_ids
    if missing:
        print(f"  [warn] {len(missing)} blocks got no tag entry at all (treat as untagged): {sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}")
    if extra:
        print(f"  [warn] {len(extra)} tag entries reference unknown block_ids (hallucinated ids?): {sorted(extra)[:10]}")

    n_general = sum(1 for t in tags if isinstance(t, dict) and t.get("general"))
    n_clustered = sum(1 for t in tags if isinstance(t, dict) and t.get("cluster_ids"))
    n_comparative = sum(1 for t in tags if isinstance(t, dict) and len(t.get("cluster_ids") or []) > 1)
    print(f"  general: {n_general}   tagged to >=1 cluster: {n_clustered}   comparative (>=2 clusters): {n_comparative}")

    out_path = Path(args.out) if args.out else md_path.parent / f"{md_path.stem}.cluster_tags.json"
    out_path.write_text(json.dumps({
        "source_md": str(md_path),
        "model": args.model,
        "pass0_model": args.pass0_model if not args.no_pass0 else None,
        "think": args.think,
        "pass0_result": pass0_result,
        "n_continuations_stitched": None if args.no_stitch else n_merged,
        "n_headings_recovered": None if args.no_heading_recovery else n_recovered,
        "n_blocks": len(blocks),
        "clusters": clusters,
        "block_tags": tags,
        "blocks": [{"block_id": b.block_id, "page": b.page, "type": b.type, "text": b.text} for b in blocks],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
