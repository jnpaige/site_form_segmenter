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
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))
from ollama_client import extract_json
from page_parser import parse_pages
from block_parser import split_blocks, format_blocks_for_prompt, Block

PASS0_PROMPT_FILE = Path(__file__).parent / "segment_types" / "report_pass0_lithic_journal_sections.txt"
PASS1_PROMPT_FILE = Path(__file__).parent / "segment_types" / "report_pass1_clusters.txt"
PASS2_PROMPT_FILE = Path(__file__).parent / "segment_types" / "report_pass2_block_tags.txt"
DEFAULT_MODEL = "qwen2.5:32b"
DEFAULT_PASS0_MODEL = "qwen2.5:32b"  # heading list is tiny — fits its 32K context easily
DEFAULT_BASE_URL = "http://localhost:11434"

# Categories from report_pass0_lithic_journal_sections.txt worth feeding into
# the big cluster/tagging pass. Excluded: front_matter, abstract, background
# (general intro — not site-specific), boilerplate_end (competing interest,
# acknowledgments, supplementary, references).
RELEVANT_PASS0_CATEGORIES = ("site_context", "methods", "results", "discussion", "conclusions")


def _page_snippets(pages: dict[int, str], max_chars: int = 120) -> dict[int, str]:
    """First ~max_chars of non-heading body text per page — helps pass0 tell
    a real section heading (followed by prose) from a TOC listing."""
    heading_re = re.compile(r'^#{1,6}\s+')
    snippets: dict[int, str] = {}
    for page_num, text in pages.items():
        parts, chars = [], 0
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) < 4 or heading_re.match(stripped):
                continue
            parts.append(stripped)
            chars += len(stripped)
            if chars >= max_chars:
                break
        snippet = " ".join(parts)[:max_chars].strip()
        if snippet:
            snippets[page_num] = snippet
    return snippets


def _format_headings_for_pass0(blocks: list[Block], snippets: dict[int, str]) -> str:
    lines, snippet_shown = [], set()
    for b in blocks:
        if b.type != "heading":
            continue
        line = f"p{b.page:>4}  {b.text}"
        if b.page not in snippet_shown:
            snip = snippets.get(b.page, "")
            if snip:
                line += f'  |  "{snip}"'
            snippet_shown.add(b.page)
        lines.append(line)
    return "\n".join(lines)


def run_pass0(
    blocks: list[Block], pages: dict[int, str],
    model: str, base_url: str, temperature: float, timeout: float, num_ctx: int,
    label: str,
) -> tuple[set[int], dict | None]:
    """Classify pages by section type off the heading list alone (cheap —
    tiny context regardless of document length), then return the set of
    pages worth feeding into the full block-tagging pass. Falls back to
    "every page" if pass0 fails, rather than silently dropping content."""
    snippets = _page_snippets(pages)
    heading_text = _format_headings_for_pass0(blocks, snippets)
    prompt = PASS0_PROMPT_FILE.read_text(encoding="utf-8").replace("{HEADINGS}", heading_text)

    result = extract_json(
        system_prompt=prompt,
        user_content="Analyze the heading list above and return the section map.",
        model=model, base_url=base_url, temperature=temperature, timeout=timeout,
        label=f"pass0:{label}", num_ctx=num_ctx,
    )
    if result is None:
        print("  [warn] pass0 returned no JSON — falling back to every page")
        return set(pages), None

    relevant: set[int] = set()
    for category in RELEVANT_PASS0_CATEGORIES:
        for entry in result.get(category, []) or []:
            if isinstance(entry, dict):
                relevant.update(int(p) for p in entry.get("pages", []))
    if not relevant:
        print("  [warn] pass0 found no relevant pages — falling back to every page")
        return set(pages), result
    return relevant, result


def build_stage1_context(all_blocks: list[Block], pass0_result: dict | None) -> str:
    """Small, targeted context for cluster identification — NOT the full
    document: every heading (cheap, already validated useful by pass0),
    the site/stratigraphy description (where layers are usually first
    defined — identified via pass0's site_context pages), and every
    unique table column-header row in the document (cluster names are
    very often literally the columns of a summary table). Deliberately
    excludes full Results/Discussion prose — that's what made the combined
    single-shot pass too large; the cluster names themselves don't need it."""
    site_pages: set[int] = set()
    if pass0_result:
        for entry in pass0_result.get("site_context", []) or []:
            if isinstance(entry, dict):
                site_pages.update(int(p) for p in entry.get("pages", []))

    headings = [f"p{b.page} {b.text}" for b in all_blocks if b.type == "heading"]
    site_text = [b.text for b in all_blocks if b.page in site_pages and b.type != "heading"]

    seen_headers: set[str] = set()
    table_headers: list[str] = []
    for b in all_blocks:
        if b.type != "table_row":
            continue
        header = b.text.split("||")[0].strip()
        if header not in seen_headers:
            seen_headers.add(header)
            table_headers.append(header)

    parts = [
        "HEADINGS:\n" + "\n".join(headings),
        "SITE/STRATIGRAPHY DESCRIPTION:\n" + "\n".join(site_text) if site_text else "",
        "TABLE COLUMN HEADERS (deduplicated, from every table in the paper):\n" + "\n".join(table_headers),
    ]
    return "\n\n".join(p for p in parts if p)


def run_stage1_clusters(
    all_blocks: list[Block], pass0_result: dict | None,
    model: str, base_url: str, temperature: float, timeout: float, num_ctx: int,
    label: str,
) -> list[dict]:
    context = build_stage1_context(all_blocks, pass0_result)
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

    chunks = chunk_blocks_by_page(blocks, n_chunks)
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
    ap.add_argument("--stage2-chunks", type=int, default=3, help="Number of page-range chunks for stage2 tagging")
    ap.add_argument("--out", default=None, help="Output JSON path (default: alongside the .md file)")
    args = ap.parse_args()

    md_path = Path(args.md_path)
    if not md_path.is_file():
        sys.exit(f"Not found: {md_path}")

    pages = parse_pages(md_path)
    if not pages:
        sys.exit(f"No '=== Page N ===' markers found in {md_path}")

    all_blocks = split_blocks(pages)

    pass0_result = None
    if args.no_pass0:
        blocks = all_blocks
    else:
        print(f"Pass0 model : {args.pass0_model}")
        print("Running pass0 (heading-only page classification)...")
        relevant_pages, pass0_result = run_pass0(
            all_blocks, pages, args.pass0_model, args.base_url,
            args.temperature, args.timeout, args.num_ctx, md_path.stem,
        )
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
    )
    if not clusters:
        sys.exit("Stage 1 returned no clusters — stopping (nothing to tag against).")
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
        print(f"  {c.get('cluster_id')}: {c.get('label_as_reported')}  "
              f"(layers: {c.get('constituent_layers')}, granularity: {c.get('granularity')})")

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
        "pass0_result": pass0_result,
        "n_blocks": len(blocks),
        "clusters": clusters,
        "block_tags": tags,
        "blocks": [{"block_id": b.block_id, "page": b.page, "type": b.type, "text": b.text} for b in blocks],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
