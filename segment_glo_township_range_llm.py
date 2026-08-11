#!/usr/bin/env python3
"""
segment_glo_township_range_llm.py — LLM-based Township/Range/Section
segmentation of GLO (General Land Office) survey field notes, replacing the
regex-based segment_glo_township_range.py's detection step with an LLM read
of the actual page text.

Why this exists: segment_glo_township_range.py's STAMP_RE only recognizes one
running-header format ("T. 2N R. 6W S.W.D."). A read-the-page evaluation of
its output on two real volumes found that both documents switch, partway
through, to a second, spelled-out header format ("Field Notes of Township
No. 3 North... Range No. 4 West") that the regex never matches at all —
those pages then get silently absorbed into whichever neighboring block the
fill/interpolation algorithm happens to guess, with no actual confirmation.
On the two volumes checked, this produced segments that were each really 3-4
distinct townships compressed into one label, covering roughly a third of
each document. A second, smaller failure mode: a table-of-contents page
listing many townships (for other surveyors, elsewhere in the volume) got
matched as if it were a real running-header stamp. Because OCR/manuscript
handwriting genuinely does vary in how each surveyor recorded (or an OCR
error genuinely does misread) a header, no single fixed regex generalizes —
this is a language-understanding problem, not a pattern-matching one.

Architecture: this mirrors segment_reports_pass0.py's/report_pass1's shape
(one linear pass identifying sequential structural blocks across the whole
document), not report_pass2's shape (a separate full-document search per
known entity) — GLO township/range blocks are laid out sequentially like
report sections, not scattered/interleaved like trinomial mentions, so a
single sweep is the right mechanism. The one thing pass1 has that GLO
documents don't is a cheap, compact heading list to send in one call —
pdf_ocr does not reliably produce headings.json for this corpus, so this
script sends real page text in OVERLAPPING windows instead (window_size
pages per call, advancing by window_stride pages, so consecutive windows
share window_size - window_stride pages). Pages inside the overlap are seen
by two independent calls; if both agree, that page's assignment is
corroborated evidence, not one script's silent construction of an answer
which is what "interpolated"/"edge-extrapolated" meant in the regex version.
If they disagree, that page is flagged (see confidence_counts /
_boundary_disagreements) rather than resolved by an arbitrary tie-break.

Within each window, the LLM assigns EVERY page (not just pages carrying a
visible header) a Township/Range/Section directly, using narrative
continuity for continuation pages that carry no header of their own — one
call replaces the old script's two separate mechanisms (regex line-match,
then a statistical forward/backward-fill across unconfirmed pages).

Output schema is intentionally identical to segment_glo_township_range.py's
segments.json (see that script's docstring) — label/township/range/
district/pages/page_span/n_pages/confidence_counts/sections, plus the
same t{n}{dir}_r{n}{dir}[_s{n}]_pages convenience keys — so nothing
downstream (site_vocab_extractor, locate_and_annotate.py) needs to change to
consume either segmenter's output interchangeably. The one deliberate
addition is a top-level _boundary_disagreements list: pages where two
overlapping windows produced different answers, surfaced for manual review
rather than silently resolved — the regex version's "zero unmapped pages"
looked reassuring but really meant "the fill algorithm always produces an
answer, even a wrong one." This version can say "here are the specific
pages we're not sure about" instead.

Usage:
    uv run python segment_glo_township_range_llm.py --input-dir "path/to/pdf_ocr/output"
    uv run python segment_glo_township_range_llm.py --config config_glo_township_range_llm.yaml
    uv run python segment_glo_township_range_llm.py --config config_glo_township_range_llm.yaml --report "507_00056__..."
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "lib"))
from ollama_client import extract_json, get_stats, reset_stats
from page_parser import parse_pages
from inventory import write_inventory_csv

PROMPT_FILE = Path(__file__).parent / "segment_types" / "glo_township_range_pages.txt"
DEFAULT_MODEL = "qwen2.5:32b"
DEFAULT_BASE_URL = "http://localhost:11434"
MAX_PAGE_CHARS = 1500  # per-page truncation inside a window, so one unusually
                        # long page can't crowd the other ~24 pages out of context


def _source_path(doc_dir: Path, source: str) -> Path:
    if source == "md":
        return doc_dir / f"{doc_dir.name}.md"
    return doc_dir / "text_docling.txt"


def _mode(values: list) -> object | None:
    if not values:
        return None
    counts: dict = {}
    order: list = []
    for v in values:
        if v not in counts:
            counts[v] = 0
            order.append(v)
        counts[v] += 1
    return max(order, key=lambda v: counts[v])


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------

def _make_windows(page_nums: list[int], window_size: int, stride: int) -> list[list[int]]:
    """Overlapping windows over the actual page numbers present (not assumed
    contiguous). Consecutive windows share (window_size - stride) pages."""
    windows = []
    i = 0
    n = len(page_nums)
    while i < n:
        windows.append(page_nums[i:i + window_size])
        if i + window_size >= n:
            break
        i += stride
    return windows


def _format_window_pages(pages: dict[int, str], page_nums: list[int]) -> str:
    parts = []
    for n in page_nums:
        text = pages.get(n, "")
        if len(text) > MAX_PAGE_CHARS:
            text = text[:MAX_PAGE_CHARS] + "\n[... truncated]"
        parts.append(f"=== Page {n} ===\n{text}")
    return "\n\n".join(parts)


def _centeredness(pos: int, size: int) -> int:
    """Distance from the nearer edge of the window — higher means this
    window's read of the page is less likely to be an edge-of-context
    guess, so it's preferred when two windows disagree."""
    return min(pos, size - 1 - pos)


_NUM_RE = re.compile(r'^\d{1,3}$')


def _clean_number(v) -> str | None:
    """A township/range number must be 1-3 clean digits after stripping
    whitespace and leading zeros (so "1" and "01" collapse to the same key).
    Anything else (a garbled OCR fragment the model echoed back verbatim
    instead of committing to null, e.g. "N" or ".N.") is rejected rather than
    accepted into a segment label — a bad label is worse than a missing one,
    since it silently creates a phantom extra segment instead of just
    leaving the page unassigned."""
    if v is None:
        return None
    s = str(v).strip()
    if not _NUM_RE.match(s):
        return None
    return str(int(s))  # drops leading zeros: "01" -> "1"


def _clean_dir(v, valid: str) -> str | None:
    """valid is "NS" or "EW". Rejects anything but exactly one of those two
    letters (case-insensitive) — same rationale as _clean_number."""
    if v is None:
        return None
    s = str(v).strip().upper()
    return s if s in valid else None


def _tr_key(entry: dict) -> tuple | None:
    twp = _clean_number(entry.get("township"))
    twp_dir = _clean_dir(entry.get("township_dir"), "NS")
    rng = _clean_number(entry.get("range"))
    rng_dir = _clean_dir(entry.get("range_dir"), "EW")
    if twp is None or twp_dir is None or rng is None or rng_dir is None:
        return None
    return (twp, twp_dir, rng, rng_dir)


# ---------------------------------------------------------------------------
# Per-document segmentation
# ---------------------------------------------------------------------------

def segment_document(
    doc_dir: Path,
    prompt_template: str,
    model: str,
    base_url: str,
    temperature: float,
    timeout: float,
    num_ctx: int,
    window_size: int,
    window_stride: int,
    source: str = "txt",
) -> dict | None:
    txt_path = _source_path(doc_dir, source)
    if not txt_path.exists():
        return None

    pages = parse_pages(txt_path)
    if not pages:
        return None
    page_nums = sorted(pages.keys())
    n_pages = page_nums[-1] + 1

    windows = _make_windows(page_nums, window_size, window_stride)

    # page -> list of (centeredness, entry) from every window call that covered it
    by_page: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    n_windows_ok = 0

    for w_idx, window_pages in enumerate(windows):
        window_text = _format_window_pages(pages, window_pages)
        prompt = prompt_template.replace("{PAGES}", window_text)
        label = f"glo_llm:{doc_dir.name}[{w_idx + 1}/{len(windows)}]"
        result = extract_json(
            system_prompt=prompt,
            user_content="Analyze the pages above and return the per-page assignment.",
            model=model, base_url=base_url, temperature=temperature,
            timeout=timeout, label=label, num_ctx=num_ctx,
        )
        print(f"    [window {w_idx + 1}/{len(windows)}] pp {window_pages[0]}-{window_pages[-1]} ...", end=" ", flush=True)
        if not result or not isinstance(result.get("pages"), list):
            print("[ERROR] no parseable result")
            continue
        n_windows_ok += 1
        n_assigned = sum(1 for e in result["pages"] if e.get("township") is not None)
        print(f"{n_assigned}/{len(window_pages)} pages assigned")

        size = len(window_pages)
        pos_by_page = {p: i for i, p in enumerate(window_pages)}
        for entry in result["pages"]:
            try:
                page = int(entry["page"])
            except (KeyError, TypeError, ValueError):
                continue
            if page not in pos_by_page:
                continue
            centeredness = _centeredness(pos_by_page[page], size)
            by_page[page].append((centeredness, entry))

    if n_windows_ok == 0:
        return None

    # --- resolve each page: prefer the most-centered non-null read, flag disagreement
    final_key: dict[int, tuple | None] = {}
    final_section: dict[int, int | None] = {}
    final_district: dict[int, str | None] = {}
    page_confidence: dict[int, str] = {}
    disagreements: list[dict] = []

    for page in page_nums:
        observations = by_page.get(page, [])
        non_null = [(c, e) for c, e in observations if _tr_key(e) is not None]
        if not non_null:
            final_key[page] = None
            final_section[page] = None
            final_district[page] = None
            page_confidence[page] = "unassigned"
            continue

        non_null.sort(key=lambda ce: -ce[0])
        best_centeredness, best_entry = non_null[0]
        keys_seen = {_tr_key(e) for _, e in non_null}

        final_key[page] = _tr_key(best_entry)
        final_section[page] = best_entry.get("section")
        final_district[page] = (str(best_entry["district"]).strip().upper()
                                 if best_entry.get("district") else None)

        if len(non_null) >= 2:
            if len(keys_seen) == 1:
                page_confidence[page] = "multi_window_agreement"
            else:
                page_confidence[page] = "multi_window_disagreement"
                disagreements.append({
                    "page": page,
                    "candidates": [
                        {"township": e.get("township"), "township_dir": e.get("township_dir"),
                         "range": e.get("range"), "range_dir": e.get("range_dir"),
                         "centeredness": c}
                        for c, e in non_null
                    ],
                })
        else:
            page_confidence[page] = "single_window"

    # --- group into segments, same shape as segment_glo_township_range.py
    by_value: dict[tuple, dict] = {}
    for page in page_nums:
        key = final_key[page]
        if key is None:
            continue
        entry = by_value.setdefault(key, {"pages": [], "districts": [], "confidence": {}})
        entry["pages"].append(page)
        conf = page_confidence[page]
        entry["confidence"][conf] = entry["confidence"].get(conf, 0) + 1
        if final_district[page]:
            entry["districts"].append(final_district[page])

    section_pages_by_key: dict[tuple, dict[int, set[int]]] = {}
    n_section_resolved = 0
    for page in page_nums:
        sec = final_section[page]
        key = final_key[page]
        if sec is None or key is None:
            continue
        try:
            sec = int(sec)
        except (TypeError, ValueError):
            continue
        if not (1 <= sec <= 36):
            continue
        section_pages_by_key.setdefault(key, {}).setdefault(sec, set()).add(page)
        n_section_resolved += 1

    segments = []
    for (twp, twp_dir, rng, rng_dir), entry in sorted(by_value.items()):
        label = f"T{twp}{twp_dir}_R{rng}{rng_dir}"
        base_key = f"t{twp}{twp_dir.lower()}_r{rng}{rng_dir.lower()}"
        pages_sorted = sorted(entry["pages"])
        district = _mode(entry["districts"])

        seg = {
            "label": label,
            "township": f"{twp}{twp_dir}",
            "range": f"{rng}{rng_dir}",
            "district": district,
            "pages": pages_sorted,
            f"{base_key}_pages": pages_sorted,
            "page_span": [pages_sorted[0], pages_sorted[-1]],
            "n_pages": len(pages_sorted),
            "confidence_counts": entry["confidence"],
        }

        sections_summary = []
        for section_num, section_pages in sorted(section_pages_by_key.get((twp, twp_dir, rng, rng_dir), {}).items()):
            section_pages_sorted = sorted(section_pages)
            seg[f"{base_key}_s{section_num}_pages"] = section_pages_sorted
            sections_summary.append({"section": section_num, "pages": section_pages_sorted})
        seg["sections"] = sections_summary

        segments.append(seg)

    unmapped_pages = sorted(p for p in page_nums if final_key[p] is None)

    return {
        "document":     doc_dir.name,
        "method":       "llm",
        "model":        model,
        "segmented_at": datetime.now().isoformat(timespec="seconds"),
        "n_pages":      n_pages,
        "n_windows":    len(windows),
        "n_windows_ok": n_windows_ok,
        "window_size":  window_size,
        "window_stride": window_stride,
        "n_township_range_blocks": len(segments),
        "n_section_mentions_resolved": n_section_resolved,
        "n_boundary_disagreements": len(disagreements),
        "unmapped_pages": unmapped_pages,
        "segments": segments,
        "_boundary_disagreements": disagreements,
    }


# ---------------------------------------------------------------------------
# Run-folder plumbing (mirrors segment_glo_township_range.py / segment_reports_pass0.py)
# ---------------------------------------------------------------------------

def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "nogit"


def _make_run_dir(base: Path) -> Path:
    stamp = f"{datetime.now().strftime('%Y%m%d_%H%M')}_{_git_sha()}"
    base.mkdir(parents=True, exist_ok=True)
    candidate = base / stamp
    n = 0
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            n += 1
            candidate = base / f"{stamp}-{chr(ord('a') + n - 1)}"


def _slug(model: str) -> str:
    return model.replace(":", "_").replace(".", "_")


def _write_map_md(run_dir: Path, results: list[dict]) -> None:
    lines = ["# GLO Township/Range segmentation map (LLM)", ""]
    for r in results:
        lines.append(f"## {r['document']}")
        lines.append(f"- pages: {r['n_pages']}  |  windows: {r['n_windows_ok']}/{r['n_windows']} ok"
                      f"  |  township/range blocks: {r['n_township_range_blocks']}"
                      f"  |  unmapped pages: {len(r['unmapped_pages'])}"
                      f"  |  boundary disagreements: {r['n_boundary_disagreements']}")
        lines.append("")
        lines.append("| Township/Range | District | Pages | Span | agree | single | disagree | unassigned |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for seg in r["segments"]:
            cc = seg["confidence_counts"]
            lines.append(
                f"| {seg['label']} | {seg['district'] or '?'} | {seg['n_pages']} "
                f"| {seg['page_span'][0]}-{seg['page_span'][1]} "
                f"| {cc.get('multi_window_agreement', 0)} | {cc.get('single_window', 0)} "
                f"| {cc.get('multi_window_disagreement', 0)} | {cc.get('unassigned', 0)} |"
            )
        if r["unmapped_pages"]:
            lines.append("")
            lines.append(f"Unmapped pages (no window ever assigned a township/range): {r['unmapped_pages']}")
        if r["_boundary_disagreements"]:
            lines.append("")
            lines.append("**Boundary disagreements — two overlapping windows disagreed on this page's "
                          "Township/Range. The most-centered read won, but treat these with extra "
                          "skepticism:**")
            for d in r["_boundary_disagreements"]:
                cand_str = "; ".join(
                    f"T{c['township']}{c['township_dir']}_R{c['range']}{c['range_dir']} "
                    f"(centeredness {c['centeredness']})" for c in d["candidates"]
                )
                lines.append(f"- page {d['page']}: {cand_str}")
        lines.append("")
        lines.append(f"Section mentions resolved: {r['n_section_mentions_resolved']}")
        for seg in r["segments"]:
            if seg["sections"]:
                sec_str = "  ".join(f"S{s['section']}:{len(s['pages'])}pp" for s in seg["sections"])
                lines.append(f"- {seg['label']}: {sec_str}")
        lines.append("")
    (run_dir / "township_range_map.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, metavar="PATH")
    ap.add_argument("--input-dir", default=None, metavar="PATH")
    ap.add_argument("--report", default=None, metavar="NAME",
                    help="Process only this document directory (exact name)")
    ap.add_argument("--output-dir", default=None, metavar="PATH")
    ap.add_argument("--source", default=None, choices=["txt", "md"], metavar="txt|md")
    ap.add_argument("--model", default=None)
    ap.add_argument("--window-size", type=int, default=None)
    ap.add_argument("--window-stride", type=int, default=None)
    args = ap.parse_args()

    cfg: dict = {}
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            sys.exit(f"Config not found: {cfg_path}")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    input_dir_str  = args.input_dir  or cfg.get("input_dir")
    output_dir_str = args.output_dir or cfg.get("output_dir", "runs")
    source         = args.source     or cfg.get("source", "txt")
    model          = args.model      or cfg.get("model", DEFAULT_MODEL)
    base_url       = cfg.get("base_url", DEFAULT_BASE_URL)
    temperature    = float(cfg.get("temperature", 0.05))
    timeout        = float(cfg.get("timeout_seconds", 1800))
    num_ctx        = int(cfg.get("num_ctx", 32768))
    window_size    = args.window_size   or int(cfg.get("window_size", 25))
    window_stride  = args.window_stride or int(cfg.get("window_stride", 20))

    if not input_dir_str:
        sys.exit("--input-dir is required (or set input_dir in config)")
    root = Path(input_dir_str)
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")

    if not PROMPT_FILE.exists():
        sys.exit(f"Prompt not found: {PROMPT_FILE}")
    prompt_template = PROMPT_FILE.read_text(encoding="utf-8")

    doc_dirs = sorted(
        d for d in root.iterdir() if d.is_dir() and _source_path(d, source).exists()
    )

    if args.report:
        doc_dirs = [d for d in doc_dirs if d.name == args.report]
        if not doc_dirs:
            sys.exit(f"Document not found: {args.report}")

    if not doc_dirs:
        sys.exit(f"No {'<stem>.md' if source == 'md' else 'text_docling.txt'} files found under: {root}")

    run_dir = _make_run_dir(Path(output_dir_str))
    model_slug = _slug(model)

    if args.config and Path(args.config).exists():
        cfg_path = Path(args.config)
        shutil.copy(cfg_path, run_dir / cfg_path.name)
    else:
        (run_dir / "config_snapshot.yaml").write_text(
            yaml.dump({"input_dir": str(root), "output_dir": output_dir_str, "source": source,
                       "model": model, "window_size": window_size, "window_stride": window_stride},
                      sort_keys=False),
            encoding="utf-8",
        )

    (run_dir / "prompts.yaml").write_text(
        yaml.dump({
            "method": "llm",
            "window_size": window_size,
            "window_stride": window_stride,
            "window_overlap": window_size - window_stride,
            "model": model,
            "segmentation_prompts": {"glo_township_range_pages": prompt_template},
        }, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    print(f"\nRun dir      : {run_dir}")
    print(f"Method       : llm ({model})")
    print(f"Window       : {window_size} pages, stride {window_stride} "
          f"(overlap {window_size - window_stride})")
    print(f"Documents    : {len(doc_dirs)}\n")

    reset_stats()
    run_started = datetime.now(timezone.utc)
    run_t0 = time.monotonic()
    doc_timings: list[dict] = []
    inventory_rows: list[dict] = []
    results: list[dict] = []
    n_processed = 0

    for doc_dir in doc_dirs:
        print(f"  [segment] {doc_dir.name} ...", flush=True)
        t0 = time.monotonic()

        result = segment_document(
            doc_dir, prompt_template, model, base_url, temperature, timeout, num_ctx,
            window_size, window_stride, source=source,
        )

        elapsed = time.monotonic() - t0

        if result is None:
            print(f"  [skip] {doc_dir.name} — no source text or all windows failed")
            continue

        out_path = run_dir / f"{model_slug}__{doc_dir.name}.segments.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        summary = "  ".join(f"{s['label']}:{s['n_pages']}pp" for s in result["segments"])
        print(f"  -> {elapsed:.0f}s  |  {summary or '(no blocks found)'}"
              f"  |  {result['n_boundary_disagreements']} disagreement(s)")

        doc_timings.append({"document": doc_dir.name, "seconds": round(elapsed, 1)})
        n_processed += 1
        results.append(result)

        inventory_rows.append({
            "run_id":              run_dir.name,
            "tool":                "site_form_segmenter:glo_township_range_llm",
            "model":               model,
            "file_name":           out_path.name,
            "file_path":           str(out_path),
            "source_input":        doc_dir.name,
            "prompt_file":         str(PROMPT_FILE),
            "prompt_snapshot_key": "glo_township_range_pages",
            "temperature":         temperature,
            "num_ctx":             num_ctx,
            "chunk_strategy":      f"window_size={window_size} window_stride={window_stride} "
                                    f"n_windows={result['n_windows']}",
            "produced_at":         result["segmented_at"],
            "output_file_path":    str(out_path),
        })

    run_elapsed = time.monotonic() - run_t0
    stats = get_stats()

    _write_map_md(run_dir, results)

    (run_dir / "run_metadata.json").write_text(
        json.dumps({
            "run_id": run_dir.name,
            "started_at": run_started.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(run_elapsed, 1),
            "method": "llm",
            "model": model,
            "input_dir": str(root),
            "n_documents_processed": n_processed,
            "avg_seconds_per_document": round(run_elapsed / n_processed, 1) if n_processed else None,
            "chunking": {"strategy": "overlapping_page_windows",
                         "window_size": window_size, "window_stride": window_stride},
            "document_timings": doc_timings,
            "token_stats": {
                "prompt_tokens": stats["prompt_tokens"],
                "completion_tokens": stats["completion_tokens"],
                "total_tokens": stats["prompt_tokens"] + stats["completion_tokens"],
                "tokens_per_second": round(stats["completion_tokens"] / stats["elapsed_s"], 1)
                                     if stats["elapsed_s"] > 0 else None,
            },
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    write_inventory_csv(run_dir, inventory_rows)

    print(f"\nDone.  {n_processed} documents  |  {run_elapsed:.0f}s total")
    print(f"Run directory -> {run_dir}")
    print(f"Review        -> {run_dir / 'township_range_map.md'}")


if __name__ == "__main__":
    main()
