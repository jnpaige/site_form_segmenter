#!/usr/bin/env python3
"""
segment_glo_township_range.py — Regex-based Township/Range segmentation of
GLO (General Land Office) survey field notes produced by pdf_ocr.

GLO field notes follow a rigid, formulaic historical convention: each
township/range block opens with a verbose header ("Field Notes of Township
No. 3 North ... in Range No. 4 West of the Basis Meridian") and then repeats
a compact running stamp on nearly every page within that block (e.g.
"T. 2N R. 6W S.W.D." or "T3N R4W N.W.D."). That repetition is the key signal
this script exploits: Township/Range digits occasionally get OCR-mangled on
any single page, but because the stamp repeats dozens of times per block,
the correct value is almost always recoverable from neighboring pages.
Section numbers do not have this redundancy — each section-boundary line
("North Boundary of Section No. 23") is mentioned once or twice in the whole
document — so section pages are nested under their parent township/range
block from direct, cleanly-parsed mentions only, with no cross-page denoise
or interpolation. A mention whose digit token didn't parse cleanly is kept
in `_section_mentions` (raw, unresolved) rather than guessed at.

This is a rule-based counterpart to segment_reports_pass0.py: no LLM, no
Ollama call, no headings.json dependency (headings.json is not reliably
produced by the current pdf_ocr pipeline). It reads text_docling.txt
directly and applies the same run-folder conventions as the rest of this
repo — every invocation creates its own new run/<YYYYMMDD_HHMM>_<gitsha>/
folder with a config snapshot, a patterns.yaml snapshot of the regex/algorithm
used (the regex-tool equivalent of prompts.yaml), run_metadata.json, and
inventory.csv using the same shared schema as the LLM-based scripts in this
repo.

Output segments.json has the same shape site_vocab_extractor already knows
how to read via segments_dir/page_scope: one segment per distinct
Township/Range found in the document, each carrying a uniquely-named
`t{n}{n_s}_r{n}{e_w}_pages` key (e.g. "t3n_r4w_pages") alongside the generic
`pages` list, PLUS one further key per section resolved within that block —
`t{n}{n_s}_r{n}{e_w}_s{section}_pages` (e.g. "t3n_r4w_s23_pages") — so
extraction can be scoped down to a single section, a whole township, or
anything in between. No code changes are needed in site_vocab_extractor to
consume this — point `segments_dir` at this script's run folder and set
`page_scope` to whichever key(s) match the granularity you want.

Usage:
    uv run python segment_glo_township_range.py --input-dir "path/to/pdf_ocr/output"
    uv run python segment_glo_township_range.py --config config_glo_township_range.yaml
    uv run python segment_glo_township_range.py --config config_glo_township_range.yaml --report "507_00121__..."
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "lib"))
from page_parser import parse_pages
from inventory import write_inventory_csv

METHOD = "regex"
PATTERN_VERSION = "glo_township_range_v1"

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Digit-like OCR confusions seen on this corpus: "I"/"l"/"|" misread for "1".
_DIGIT_ALIASES = str.maketrans({"I": "1", "l": "1", "|": "1"})

# Compact running stamp, e.g. "T. 2N R. 6W", "T3N R4W", "T. 1 N. R. 1 E.".
# Tolerates missing/extra periods and spaces, lowercase direction letters, and
# two letter-shape OCR confusions confirmed on this corpus: the leading "T"
# is sometimes read as "J" (an entire book's running header used this form
# throughout, e.g. "Exteriors J. 3 N. R. 6 W. continued"), and "W" is
# occasionally read as "V" or "F"/"E" get confused with each other.
STAMP_RE = re.compile(
    r'[TJtj]\.?\s*([0-9Il|]{1,2})\s*([NSns])\.?[\s,.-]*'
    r'R\.?\s*([0-9Il|]{1,2})\s*([EWFVewfv])\.?',
)

# District code, e.g. "S.W.D.", "N.W.D.", "SED", tolerating missing periods.
DISTRICT_RE = re.compile(r'\b([NS])\.?\s*([EW])\.?\s*D\.?\b')

# Block-boundary hint: "Basis Meridian" is the one phrase in the verbose
# township header that OCR reads reliably (it's long/distinctive, unlike the
# adjacent digits), so a page containing it is very likely the first page of
# a new township/range block.
BOUNDARY_HINT_RE = re.compile(r'Basis Meridian', re.IGNORECASE)

# Section boundary-line mentions, e.g. "West Boundary of Section No. 23" —
# recorded as a best-effort auxiliary signal only; NOT forward-filled or
# denoised the way Township/Range is, because each occurrence is a one-off
# with no repeated confirmation to correct against.
SECTION_RE = re.compile(
    r'(North|South|East|West)\s+Bo[uw]nd[ao]ry\s+of\s+Sections?\s*N[o0°º]?\.?\s*([0-9A-Za-z]{1,3})',
    re.IGNORECASE,
)


def _norm_num(token: str) -> str | None:
    """Normalize a captured digit token, correcting I/l/| -> 1. None if not a clean number."""
    t = token.translate(_DIGIT_ALIASES)
    return t if t.isdigit() else None


def _norm_dir_ew(letter: str) -> str:
    return "E" if letter.upper() in ("E", "F") else "W"  # W family also covers "V" misreads


# ---------------------------------------------------------------------------
# Per-page extraction
# ---------------------------------------------------------------------------

# Real running-header stamps are short, isolated lines ("T. 2N R. 6W S.W.D.").
# Body text also contains "T"/"R" digit pairs in an unrelated convention —
# witness-tree blaze marks like "...bears N51E 26 links marked T4. R4." — which
# are longer sentences and consistently preceded by "marked". Both checks
# below exist specifically to reject that pattern rather than to generically
# cap line length.
_STAMP_LINE_MAX_LEN = 60
_BLAZE_MARK_CONTEXT_RE = re.compile(r'\bmarked\b', re.IGNORECASE)


def _page_stamp_candidates(text: str) -> list[tuple[str, str, str, str]]:
    """Return every (twp_num, twp_dir, rng_num, rng_dir) match found on a page.

    Only considered on short, isolated lines that don't look like a witness-
    tree blaze-mark citation — see module docstring / comment above.
    """
    out = []
    for line in text.splitlines():
        if len(line) > _STAMP_LINE_MAX_LEN or _BLAZE_MARK_CONTEXT_RE.search(line):
            continue
        for m in STAMP_RE.finditer(line):
            twp = _norm_num(m.group(1))
            rng = _norm_num(m.group(3))
            if twp is None or rng is None:
                continue
            out.append((twp, m.group(2).upper(), rng, _norm_dir_ew(m.group(4))))
    return out


def _page_district_candidates(text: str) -> list[str]:
    out = []
    for m in DISTRICT_RE.finditer(text):
        out.append(f"{m.group(1).upper()}{m.group(2).upper()}D")
    return out


def _page_section_mentions(page_num: int, text: str) -> list[dict]:
    mentions = []
    for m in SECTION_RE.finditer(text):
        raw = m.group(2)
        section = int(raw) if raw.isdigit() else None
        mentions.append({
            "page": page_num,
            "direction": m.group(1).capitalize(),
            "section": section,
            "raw": raw,
        })
    return mentions


def _mode(values: list) -> object | None:
    """Most common value in a list, or None if empty. Ties broken by first-seen order."""
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
# Denoise + fill: the core robustness logic
# ---------------------------------------------------------------------------

def _denoise(confirmed: list[tuple[int, object]]) -> list[tuple[int, object]]:
    """Drop single-page value flips flanked by matching neighbors.

    If page i's confirmed value differs from both its immediate confirmed
    neighbors, but those two neighbors agree with each other, page i is
    almost certainly an isolated OCR misread rather than a real one-page
    township/range block — drop it so it gets filled by interpolation instead.
    """
    if len(confirmed) < 3:
        return confirmed
    keep = [True] * len(confirmed)
    for i in range(1, len(confirmed) - 1):
        prev_v = confirmed[i - 1][1]
        cur_v  = confirmed[i][1]
        next_v = confirmed[i + 1][1]
        if cur_v != prev_v and cur_v != next_v and prev_v == next_v:
            keep[i] = False
    return [c for c, k in zip(confirmed, keep) if k]


def _fill_pages(
    n_pages: int,
    confirmed: list[tuple[int, object]],
    boundary_hint_pages: set[int],
) -> dict[int, tuple[object, str]]:
    """Assign every page 0..n_pages-1 a (value, confidence) pair.

    confidence is one of: direct, interpolated, interpolated-boundary,
    edge-extrapolated. Pages with no confirmed value anywhere in the document
    are omitted from the returned dict (caller treats them as unmapped).
    """
    if not confirmed:
        return {}

    confirmed = sorted(confirmed, key=lambda c: c[0])
    confirmed_map = dict(confirmed)
    result: dict[int, tuple[object, str]] = {}

    for page in range(n_pages):
        if page in confirmed_map:
            result[page] = (confirmed_map[page], "direct")
            continue

        # nearest confirmed page before / after
        before = None
        after = None
        for p, v in confirmed:
            if p < page:
                before = (p, v)
            elif p > page and after is None:
                after = (p, v)

        if before is not None and after is not None:
            if before[1] == after[1]:
                result[page] = (before[1], "interpolated")
            else:
                # Real transition zone. Prefer splitting exactly at a
                # "Basis Meridian" boundary-hint page if one falls in the gap;
                # otherwise split at the midpoint.
                hints_in_gap = sorted(p for p in boundary_hint_pages if before[0] < p <= after[0])
                if hints_in_gap:
                    split_at = hints_in_gap[0]
                    result[page] = (before[1] if page < split_at else after[1],
                                     "interpolated-boundary")
                else:
                    midpoint = (before[0] + after[0]) / 2
                    result[page] = (before[1] if page < midpoint else after[1],
                                     "interpolated-boundary")
        elif before is not None:
            result[page] = (before[1], "edge-extrapolated")
        elif after is not None:
            result[page] = (after[1], "edge-extrapolated")
        # else: no confirmed values anywhere in the doc — leave unmapped

    return result


# ---------------------------------------------------------------------------
# Per-document segmentation
# ---------------------------------------------------------------------------

def _source_path(doc_dir: Path, source: str) -> Path:
    """source: "txt" (default) reads pdf_ocr's text_docling.txt; "md" reads
    pdf_ocr's <stem>.md — as of pdf_ocr's page-scoped Markdown rewrite, both
    share the identical `=== Page N ===` delimiter parse_pages() expects, and
    .md additionally carries table structure (docling's real markdown export)
    on pages that didn't need the raw-OCR fallback."""
    if source == "md":
        return doc_dir / f"{doc_dir.name}.md"
    return doc_dir / "text_docling.txt"


def segment_document(doc_dir: Path, source: str = "txt") -> dict | None:
    txt_path = _source_path(doc_dir, source)
    if not txt_path.exists():
        return None

    pages = parse_pages(txt_path)
    if not pages:
        return None
    n_pages = max(pages.keys()) + 1

    tr_confirmed: list[tuple[int, tuple[str, str, str, str]]] = []
    district_confirmed: list[tuple[int, str]] = []
    boundary_hint_pages: set[int] = set()
    section_mentions: list[dict] = []
    n_pages_with_stamp = 0

    for page_num in sorted(pages):
        text = pages[page_num]

        stamps = _page_stamp_candidates(text)
        if stamps:
            n_pages_with_stamp += 1
            tr_confirmed.append((page_num, _mode(stamps)))

        districts = _page_district_candidates(text)
        if districts:
            district_confirmed.append((page_num, _mode(districts)))

        if BOUNDARY_HINT_RE.search(text):
            boundary_hint_pages.add(page_num)

        section_mentions.extend(_page_section_mentions(page_num, text))

    tr_confirmed_denoised = _denoise(tr_confirmed)
    district_confirmed_denoised = _denoise(district_confirmed)

    tr_filled = _fill_pages(n_pages, tr_confirmed_denoised, boundary_hint_pages)
    district_filled = _fill_pages(n_pages, district_confirmed_denoised, boundary_hint_pages)

    # Group pages by (township, range) value into segments, regardless of
    # contiguity — the goal is "all pages that belong to this township",
    # not "one segment per contiguous run".
    by_value: dict[tuple, dict] = {}
    for page, (value, confidence) in tr_filled.items():
        entry = by_value.setdefault(value, {"pages": [], "confidence": {}, "districts": []})
        entry["pages"].append(page)
        entry["confidence"][confidence] = entry["confidence"].get(confidence, 0) + 1
        if page in district_filled:
            entry["districts"].append(district_filled[page][0])

    # Nest section numbers under whichever township/range block their page
    # belongs to. Unlike the township/range stamp, a given section number is
    # typically mentioned only once or twice in the whole document (each
    # boundary line is described once), so there is no cross-page redundancy
    # to denoise/interpolate against — only direct, parseable mentions are
    # used. Mentions whose digit token didn't parse cleanly are kept in
    # _section_mentions (raw, unresolved) rather than guessed at.
    section_pages_by_tr: dict[tuple, dict[int, set[int]]] = {}
    n_section_direct = 0
    n_section_unresolved = 0
    for m in section_mentions:
        if m["section"] is None or not (1 <= m["section"] <= 36):
            n_section_unresolved += 1
            continue
        page = m["page"]
        if page not in tr_filled:
            n_section_unresolved += 1
            continue
        tr_value = tr_filled[page][0]
        section_pages_by_tr.setdefault(tr_value, {}).setdefault(m["section"], set()).add(page)
        n_section_direct += 1

    segments = []
    for (twp, twp_dir, rng, rng_dir), entry in sorted(by_value.items()):
        tr_value = (twp, twp_dir, rng, rng_dir)
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
        for section_num, section_pages in sorted(section_pages_by_tr.get(tr_value, {}).items()):
            section_pages_sorted = sorted(section_pages)
            seg[f"{base_key}_s{section_num}_pages"] = section_pages_sorted
            sections_summary.append({"section": section_num, "pages": section_pages_sorted})
        seg["sections"] = sections_summary

        segments.append(seg)

    unmapped_pages = sorted(set(pages.keys()) - set(tr_filled.keys()))

    return {
        "document":     doc_dir.name,
        "method":       METHOD,
        "pattern_version": PATTERN_VERSION,
        "segmented_at": datetime.now().isoformat(timespec="seconds"),
        "n_pages":      n_pages,
        "n_pages_with_direct_stamp": n_pages_with_stamp,
        "n_boundary_hint_pages":     len(boundary_hint_pages),
        "n_township_range_blocks":  len(segments),
        "n_section_mentions_resolved":   n_section_direct,
        "n_section_mentions_unresolved": n_section_unresolved,
        "unmapped_pages": unmapped_pages,
        "segments": segments,
        "_section_mentions": section_mentions,
    }


# ---------------------------------------------------------------------------
# Run-folder plumbing (mirrors segment_reports_pass0.py)
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


def _write_map_md(run_dir: Path, results: list[dict]) -> None:
    """Human-reviewable summary, same purpose as segmentation_map.md for site forms."""
    lines = ["# GLO Township/Range segmentation map", ""]
    for r in results:
        lines.append(f"## {r['document']}")
        lines.append(f"- pages: {r['n_pages']}  |  direct stamp hits: {r['n_pages_with_direct_stamp']}"
                      f"  |  township/range blocks: {r['n_township_range_blocks']}"
                      f"  |  unmapped pages: {len(r['unmapped_pages'])}")
        lines.append("")
        lines.append("| Township/Range | District | Pages | Span | direct | interpolated | boundary | edge |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for seg in r["segments"]:
            cc = seg["confidence_counts"]
            lines.append(
                f"| {seg['label']} | {seg['district'] or '?'} | {seg['n_pages']} "
                f"| {seg['page_span'][0]}-{seg['page_span'][1]} "
                f"| {cc.get('direct', 0)} | {cc.get('interpolated', 0)} "
                f"| {cc.get('interpolated-boundary', 0)} | {cc.get('edge-extrapolated', 0)} |"
            )
        if r["unmapped_pages"]:
            lines.append("")
            lines.append(f"Unmapped pages (no township/range stamp found anywhere in the document): "
                          f"{r['unmapped_pages']}")

        lines.append("")
        lines.append(f"Section-boundary mentions: {r['n_section_mentions_resolved']} resolved to a "
                      f"section number  |  {r['n_section_mentions_unresolved']} unresolved "
                      f"(garbled digit or page outside any township/range block) — best-effort, direct "
                      f"mentions only, not denoised/interpolated like township/range")
        for seg in r["segments"]:
            if seg["sections"]:
                sec_str = "  ".join(f"S{s['section']}:{len(s['pages'])}pp" for s in seg["sections"])
                lines.append(f"- {seg['label']}: {sec_str}")
        lines.append("")
    (run_dir / "township_range_map.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, metavar="PATH",
                    help="YAML config file (CLI flags override config values)")
    ap.add_argument("--input-dir", default=None, metavar="PATH",
                    help="Root directory containing document subdirectories with text_docling.txt")
    ap.add_argument("--report", default=None, metavar="NAME",
                    help="Process only this document directory (exact name)")
    ap.add_argument("--output-dir", default=None, metavar="PATH")
    ap.add_argument("--source", default=None, choices=["txt", "md"], metavar="txt|md",
                    help="Which pdf_ocr output to segment: text_docling.txt (default) or <stem>.md")
    args = ap.parse_args()

    cfg: dict = {}
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            sys.exit(f"Config not found: {cfg_path}")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    input_dir_str = args.input_dir or cfg.get("input_dir")
    output_dir_str = args.output_dir or cfg.get("output_dir", "runs")
    source = args.source or cfg.get("source", "txt")

    if not input_dir_str:
        sys.exit("--input-dir is required (or set input_dir in config)")
    root = Path(input_dir_str)
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")

    # Discover document directories: any immediate subdirectory that has the
    # selected source file. Same discovery pattern as segment_reports_pass0.py
    # but keyed on pdf_ocr's own text output directly rather than
    # headings.json (which the current pdf_ocr pipeline does not reliably
    # produce).
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

    # Config snapshot: copy the original file if one was given, else write a
    # synthesized snapshot of the resolved settings so every run still has one.
    if args.config and Path(args.config).exists():
        cfg_path = Path(args.config)
        shutil.copy(cfg_path, run_dir / cfg_path.name)
    else:
        (run_dir / "config_snapshot.yaml").write_text(
            yaml.dump({"input_dir": str(root), "output_dir": output_dir_str}, sort_keys=False),
            encoding="utf-8",
        )

    # patterns.yaml — the regex-tool equivalent of prompts.yaml: a full
    # snapshot of the actual patterns and algorithm parameters used, so a
    # run folder is self-describing even without reading this script's source.
    (run_dir / "patterns.yaml").write_text(
        yaml.dump({
            "method": METHOD,
            "pattern_version": PATTERN_VERSION,
            "source": source,
            "regex_patterns": {
                "township_range_stamp": STAMP_RE.pattern,
                "district_code":        DISTRICT_RE.pattern,
                "block_boundary_hint":  BOUNDARY_HINT_RE.pattern,
                "section_boundary_mention": SECTION_RE.pattern,
                "blaze_mark_exclusion": _BLAZE_MARK_CONTEXT_RE.pattern,
            },
            "digit_normalization": {"I": "1", "l": "1", "|": "1"},
            "stamp_line_max_len": _STAMP_LINE_MAX_LEN,
            "stamp_false_positive_filter": "township_range_stamp is only matched on lines "
                "<= stamp_line_max_len chars that don't match blaze_mark_exclusion — "
                "distinguishes the short page-header running stamp ('T. 2N R. 6W S.W.D.') "
                "from witness-tree blaze-mark citations in body text ('...bears N51E 26 "
                "links marked T4. R4.'), which use the same T#/R# notation for a different purpose",
            "algorithm": {
                "denoise": "drop a single confirmed page whose value disagrees with "
                           "both immediate confirmed neighbors when those two neighbors "
                           "agree with each other (treated as an isolated OCR misread)",
                "fill": "forward/backward-fill unconfirmed pages from the nearest "
                        "confirmed neighbor(s); a genuine transition between two "
                        "differing confirmed values is split at the nearest "
                        "'Basis Meridian' boundary-hint page if one falls in the gap, "
                        "else at the midpoint",
                "confidence_levels": ["direct", "interpolated", "interpolated-boundary",
                                      "edge-extrapolated"],
                "section_level": "each resolvable 'Boundary of Section No. X' mention is "
                                  "nested under whichever township/range block its page "
                                  "belongs to, as its own t{n}{dir}_r{n}{dir}_s{section}_pages "
                                  "key — but, unlike township/range, NOT denoised or "
                                  "interpolated across pages: a section number is only "
                                  "recorded on the page(s) it was directly, cleanly parsed "
                                  "on, since each mention occurs once or twice in the whole "
                                  "document with no repeated confirmation to correct OCR "
                                  "errors against. Unparseable mentions are kept in "
                                  "_section_mentions (raw, unresolved) rather than guessed at.",
            },
        }, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    print(f"\nRun dir    : {run_dir}")
    print(f"Method     : {METHOD} ({PATTERN_VERSION})")
    print(f"Documents  : {len(doc_dirs)}\n")

    run_started = datetime.now(timezone.utc)
    run_t0 = time.monotonic()
    doc_timings: list[dict] = []
    inventory_rows: list[dict] = []
    results: list[dict] = []
    n_processed = 0

    for doc_dir in doc_dirs:
        print(f"  [segment] {doc_dir.name} ...", end=" ", flush=True)
        t0 = time.monotonic()

        result = segment_document(doc_dir, source=source)

        elapsed = time.monotonic() - t0

        if result is None:
            print("[skip] no text_docling.txt / empty document")
            continue

        out_path = run_dir / f"{METHOD}__{doc_dir.name}.segments.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        summary = "  ".join(f"{s['label']}:{s['n_pages']}pp" for s in result["segments"])
        print(f"{elapsed:.1f}s  |  {summary or '(no township/range stamp found)'}")

        doc_timings.append({"document": doc_dir.name, "seconds": round(elapsed, 2)})
        n_processed += 1
        results.append(result)

        inventory_rows.append({
            "run_id":              run_dir.name,
            "tool":                "site_form_segmenter:glo_township_range",
            "model":               METHOD,
            "file_name":           out_path.name,
            "file_path":           str(out_path),
            "source_input":        doc_dir.name,
            "prompt_file":         str(Path(__file__).name),
            "prompt_snapshot_key": PATTERN_VERSION,
            "chunk_strategy":      "not applicable (single regex pass per document)",
            "produced_at":         result["segmented_at"],
            "output_file_path":    str(out_path),
        })

    run_elapsed = time.monotonic() - run_t0

    _write_map_md(run_dir, results)

    (run_dir / "run_metadata.json").write_text(
        json.dumps({
            "run_id": run_dir.name,
            "started_at": run_started.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(run_elapsed, 1),
            "method": METHOD,
            "pattern_version": PATTERN_VERSION,
            "input_dir": str(root),
            "n_documents_processed": n_processed,
            "avg_seconds_per_document": round(run_elapsed / n_processed, 2) if n_processed else None,
            "document_timings": doc_timings,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    write_inventory_csv(run_dir, inventory_rows)

    print(f"\nDone.  {n_processed} documents  |  {run_elapsed:.1f}s total")
    print(f"Run directory -> {run_dir}")
    print(f"Review        -> {run_dir / 'township_range_map.md'}")


if __name__ == "__main__":
    main()
