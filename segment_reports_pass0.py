#!/usr/bin/env python3
"""
segment_reports_pass0.py — Structural section segmentation of Phase II reports
using the heading map (headings.json) produced by pdf_ocr.

For each report directory that has a headings.json, formats the heading list
and sends it to the pass0 prompt, asking the LLM to assign pages to structural
sections (executive_summary, methods, results, recommendations, other).

This is cheaper and faster than full-text segmentation: the LLM sees only the
heading list, not the full document text.

Every run creates its own, never-reused flat directory — runs/<YYYYMMDD_HHMM>_<gitsha>/ —
with output files prefixed by the model that produced them
(<model_slug>__<report>.segments.json), a config snapshot, a prompts.yaml
text snapshot, and an inventory.csv. There is no cross-run resumability by
design: if a run is interrupted, re-run with --report for the remaining
reports — that run's outputs land in a new folder, and each folder's
run_metadata.json lets you cross-reference which reports came from which run.

Usage:
    uv run python segment_reports_pass0.py --input-dir "G:/path/to/reports"
    uv run python segment_reports_pass0.py --input-dir "G:/path" --report "22-7597_Zieschang et al. 2024"
    uv run python segment_reports_pass0.py --config config_reports.yaml --model qwen2.5:72b --report "22-7597_Zieschang et al. 2024"
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
from ollama_client import extract_json, get_stats, reset_stats
from page_parser import parse_pages
from inventory import write_inventory_csv

PROMPT_FILE = Path(__file__).parent / "segment_types" / "report_pass0_sections.txt"
DEFAULT_MODEL = "qwen2.5:32b"
DEFAULT_BASE_URL = "http://localhost:11434"


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "nogit"


def _make_run_dir(base: Path) -> Path:
    """Create a fresh, never-reused run directory <base>/<YYYYMMDD_HHMM>_<gitsha>.

    If another run already claimed that exact minute+sha, suffix with
    -b, -c, ... rather than reusing the folder.
    """
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


def _extract_page_snippets(txt_path: Path, max_chars: int = 120) -> dict[int, str]:
    """Return first ~max_chars of non-heading body text per page.

    Used to help the LLM distinguish real section headings (followed by prose)
    from table-of-contents listings (followed by dot-leaders and page numbers).
    """
    heading_re = re.compile(r'^#{1,6}\s+')
    snippets: dict[int, str] = {}
    for page_num, text in parse_pages(txt_path).items():
        parts: list[str] = []
        chars = 0
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) < 4:
                continue
            if heading_re.match(stripped):
                continue
            parts.append(stripped)
            chars += len(stripped)
            if chars >= max_chars:
                break
        snippet = " ".join(parts)[:max_chars].strip()
        if snippet:
            snippets[page_num] = snippet
    return snippets


def _format_headings(
    headings: list[dict],
    snippets: dict[int, str] | None = None,
    max_per_page: int | None = None,
) -> str:
    """Format heading list as compact text for the prompt placeholder.

    Each page's snippet is shown only on the first heading for that page.
    If max_per_page is set, only the first N headings per page are included —
    useful for very large reports where sub-headings within a section would
    otherwise overflow the context window.
    """
    lines = []
    snippet_shown: set[int] = set()
    page_count: dict[int, int] = {}
    for h in headings:
        page = h["page"]
        page_count[page] = page_count.get(page, 0) + 1
        if max_per_page is not None and page_count[page] > max_per_page:
            continue
        line = f"p{page:>4}  {h['text']}"
        if snippets and page not in snippet_shown:
            snip = snippets.get(page, "")
            if snip:
                line += f'  |  "{snip}"'
            snippet_shown.add(page)
        lines.append(line)
    return "\n".join(lines)


def _all_pages(result: dict) -> set[int]:
    """Collect all pages mentioned in a structured result dict."""
    pages = set()
    for section_entries in result.values():
        if isinstance(section_entries, list):
            for entry in section_entries:
                if isinstance(entry, dict):
                    pages.update(entry.get("pages", []))
                elif isinstance(entry, int):
                    pages.add(entry)
    return pages


def _call_chunk(
    headings: list[dict],
    snippets: dict[int, str] | None,
    prompt_template: str,
    model: str,
    base_url: str,
    temperature: float,
    timeout: float,
    num_ctx: int,
    label: str,
) -> dict | None:
    """Make one LLM call for a slice of the heading list."""
    heading_text = _format_headings(headings, snippets)
    prompt       = prompt_template.replace("{HEADINGS}", heading_text)
    return extract_json(
        system_prompt=prompt,
        user_content="Analyze the heading list above and return the section map.",
        model=model,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
        label=label,
        num_ctx=num_ctx,
    )


def _merge_chunk_results(chunk_results: list[dict]) -> dict:
    """Merge multiple per-chunk section maps into one combined result."""
    merged: dict[str, list] = {}
    for r in chunk_results:
        for section_type, entries in r.items():
            if isinstance(entries, list):
                merged.setdefault(section_type, [])
                merged[section_type].extend(entries)
    return merged


def segment_report(
    report_dir: Path,
    prompt_template: str,
    model: str,
    base_url: str,
    temperature: float,
    timeout: float,
    num_ctx: int,
    chunk_size: int = 0,
) -> dict | None:
    headings_path = report_dir / "headings.json"
    if not headings_path.exists():
        print(f"  [skip] {report_dir.name} — no headings.json")
        return None

    h_data = json.loads(headings_path.read_text(encoding="utf-8"))
    headings = h_data.get("headings", [])
    if not headings:
        print(f"  [skip] {report_dir.name} — empty headings.json")
        return None

    txt_path = report_dir / "text_docling.txt"
    snippets = _extract_page_snippets(txt_path) if txt_path.exists() else None
    n_pages  = max(h["page"] for h in headings) + 1

    # Chunk the heading list if it exceeds chunk_size.
    chunked  = chunk_size > 0 and len(headings) > chunk_size
    n_chunks = 1
    if chunked:
        chunks = [headings[i:i + chunk_size] for i in range(0, len(headings), chunk_size)]
        n_chunks = len(chunks)
        print(f"\n    chunking: {len(headings)} headings -> {len(chunks)} chunks of ~{chunk_size}", flush=True)
        chunk_results = []
        for idx, chunk in enumerate(chunks):
            label = f"pass0:{report_dir.name}[{idx+1}/{len(chunks)}]"
            r = _call_chunk(chunk, snippets, prompt_template, model, base_url,
                            temperature, timeout, num_ctx, label)
            if r is None:
                print(f"\n    [error] chunk {idx+1} returned no JSON — skipping", flush=True)
            else:
                chunk_results.append(r)
        if not chunk_results:
            print(f"  [error] {report_dir.name} — all chunks failed")
            return None
        result = _merge_chunk_results(chunk_results)
    else:
        result = _call_chunk(headings, snippets, prompt_template, model, base_url,
                             temperature, timeout, num_ctx, f"pass0:{report_dir.name}")

    if result is None:
        print(f"  [error] {report_dir.name} — LLM returned no parseable JSON")
        return None

    # Flatten LLM output {section_type: [{label, pages}]} into the shared
    # segments.json format used across this pipeline.
    section_pages: dict[str, list[int]] = {}
    section_detail: dict = {}
    for section_type, entries in result.items():
        if not isinstance(entries, list):
            continue
        flat: list[int] = []
        for entry in entries:
            if isinstance(entry, dict):
                flat.extend(int(p) for p in entry.get("pages", []))
        if flat:
            section_pages[f"{section_type}_pages"] = sorted(set(flat))
            section_detail[section_type] = entries

    all_pages = sorted({p for plist in section_pages.values() for p in plist})

    return {
        "report":       report_dir.name,
        "model":        model,
        "segmented_at": datetime.now().isoformat(timespec="seconds"),
        "chunked":      chunked,
        "n_chunks":     n_chunks,
        "n_headings":   len(headings),
        "n_pages":      n_pages,
        "segments": [
            {
                "label": report_dir.name,
                "pages": all_pages,
                **section_pages,
                "_section_detail": section_detail,
            }
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, metavar="PATH",
                    help="YAML config file (CLI flags override config values)")
    ap.add_argument("--input-dir", default=None, metavar="PATH",
                    help="Root directory containing report subdirectories with headings.json")
    ap.add_argument("--report", default=None, metavar="NAME",
                    help="Process only this report directory (exact name)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("--num-ctx", type=int, default=None)
    args = ap.parse_args()

    # Load config file if provided; CLI flags override
    cfg: dict = {}
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            sys.exit(f"Config not found: {cfg_path}")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    input_dir_str = args.input_dir or cfg.get("input_dir")
    model         = args.model     or cfg.get("model",          DEFAULT_MODEL)
    base_url      = args.base_url  or cfg.get("base_url",       DEFAULT_BASE_URL)
    temperature   = args.temperature if args.temperature is not None else float(cfg.get("temperature",    0.05))
    timeout       = args.timeout    if args.timeout    is not None else float(cfg.get("timeout_seconds", 1800))
    num_ctx       = args.num_ctx    if args.num_ctx    is not None else int(cfg.get("num_ctx",           32768))
    chunk_size    = int(cfg.get("chunk_size_headings", 0))

    if not input_dir_str:
        sys.exit("--input-dir is required (or set input_dir in config)")
    root = Path(input_dir_str)
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")

    if not PROMPT_FILE.exists():
        sys.exit(f"Prompt not found: {PROMPT_FILE}")
    prompt_template = PROMPT_FILE.read_text(encoding="utf-8")

    # Discover report directories
    report_dirs = sorted(
        d for d in root.rglob("headings.json")
        if d.parent.is_dir()
    )
    report_dirs = [p.parent for p in report_dirs]

    if args.report:
        report_dirs = [d for d in report_dirs if d.name == args.report]
        if not report_dirs:
            sys.exit(f"Report not found: {args.report}")

    if not report_dirs:
        sys.exit(f"No headings.json files found under: {root}")

    run_dir = _make_run_dir(Path(cfg.get("output_dir", "runs")))
    model_slug = _slug(model)

    # Config snapshot: copy the original file if one was given, else write a
    # synthesized snapshot of the resolved settings so every run still has one.
    if args.config and Path(args.config).exists():
        cfg_path = Path(args.config)
        shutil.copy(cfg_path, run_dir / cfg_path.name)
    else:
        (run_dir / "config_snapshot.yaml").write_text(
            yaml.dump({
                "input_dir": str(root), "model": model, "base_url": base_url,
                "temperature": temperature, "timeout_seconds": timeout,
                "num_ctx": num_ctx, "chunk_size_headings": chunk_size,
            }, sort_keys=False),
            encoding="utf-8",
        )

    (run_dir / "prompts.yaml").write_text(
        yaml.dump({"segmentation_prompts": {"pass0_sections": prompt_template}},
                  allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    print(f"\nRun dir    : {run_dir}")
    print(f"Model      : {model}")
    if chunk_size:
        print(f"Chunk size : {chunk_size} headings (reports exceeding this are split)")
    print(f"Reports    : {len(report_dirs)}\n")

    reset_stats()
    run_started = datetime.now(timezone.utc)
    run_t0 = time.monotonic()
    report_timings: list[dict] = []
    inventory_rows: list[dict] = []
    n_processed = 0

    for report_dir in report_dirs:
        out_path = run_dir / f"{model_slug}__{report_dir.name}.segments.json"

        print(f"  [segment] {report_dir.name} ...", end=" ", flush=True)
        t0 = time.monotonic()

        result = segment_report(
            report_dir, prompt_template,
            model, base_url, temperature, timeout, num_ctx,
            chunk_size=chunk_size,
        )

        elapsed = time.monotonic() - t0

        if result is None:
            continue

        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        # Summary line
        seg = result["segments"][0]
        counts = {k[:-len("_pages")]: len(v)
                  for k, v in seg.items()
                  if k.endswith("_pages") and k != "other_pages" and isinstance(v, list)}
        summary = "  ".join(f"{k}:{n}pp" for k, n in counts.items() if n > 0)
        chunk_note = f"  [{result['n_chunks']} chunks]" if result["chunked"] else ""
        print(f"{elapsed:.0f}s  |  {summary}{chunk_note}")

        report_timings.append({"report": report_dir.name, "seconds": round(elapsed, 1)})
        n_processed += 1

        chunk_desc = (f"chunk_size_headings={chunk_size} chunked=True n_chunks={result['n_chunks']}"
                      if result["chunked"] else f"chunk_size_headings={chunk_size} chunked=False")
        inventory_rows.append({
            "run_id":              run_dir.name,
            "tool":                "site_form_segmenter:reports",
            "model":               model,
            "file_name":           out_path.name,
            "file_path":           str(out_path),
            "source_input":        report_dir.name,
            "prompt_file":         str(PROMPT_FILE),
            "prompt_snapshot_key": "pass0_sections",
            "temperature":         temperature,
            "num_ctx":             num_ctx,
            "chunk_strategy":      chunk_desc,
            "produced_at":         result["segmented_at"],
            "output_file_path":    str(out_path),
        })

    run_elapsed = time.monotonic() - run_t0
    stats = get_stats()

    (run_dir / "run_metadata.json").write_text(
        json.dumps({
            "run_id": run_dir.name,
            "started_at": run_started.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(run_elapsed, 1),
            "model": model,
            "prompt_file": str(PROMPT_FILE),
            "input_dir": str(root),
            "n_reports_processed": n_processed,
            "avg_seconds_per_report": round(run_elapsed / n_processed, 1) if n_processed else None,
            "chunking": {
                "strategy": "chunk_size_headings",
                "chunk_size_headings": chunk_size,
            },
            "report_timings": report_timings,
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

    print(f"\nDone.  {n_processed} reports  |  {run_elapsed:.0f}s total")
    print(f"Run directory -> {run_dir}")


if __name__ == "__main__":
    main()
