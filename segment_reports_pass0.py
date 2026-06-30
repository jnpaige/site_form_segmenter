#!/usr/bin/env python3
"""
segment_reports_pass0.py — Structural section segmentation of Phase II reports
using the heading map (headings.json) produced by pdf_ocr.

For each report directory that has a headings.json, formats the heading list
and sends it to the pass0 prompt, asking the LLM to assign pages to structural
sections (executive_summary, methods, results, recommendations, other).

This is cheaper and faster than full-text segmentation: the LLM sees only the
heading list, not the full document text.

Usage:
    uv run python segment_reports_pass0.py --input-dir "G:/path/to/reports"
    uv run python segment_reports_pass0.py --input-dir "G:/path" --report "22-7597_Zieschang et al. 2024"
    uv run python segment_reports_pass0.py --input-dir "G:/path" --force
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "lib"))
from ollama_client import extract_json, get_stats, reset_stats
from page_parser import parse_pages

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
    stamp = f"{datetime.now().strftime('%Y%m%d_%H%M')}_{_git_sha()}"
    d = base / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d


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


def _format_headings(headings: list[dict], snippets: dict[int, str] | None = None) -> str:
    """Format heading list as compact text for the prompt placeholder."""
    lines = []
    for h in headings:
        line = f"p{h['page']:>4}  {h['text']}"
        if snippets:
            snip = snippets.get(h["page"], "")
            if snip:
                line += f'  |  "{snip}"'
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


def segment_report(
    report_dir: Path,
    prompt_template: str,
    model: str,
    base_url: str,
    temperature: float,
    timeout: float,
    num_ctx: int,
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

    heading_text = _format_headings(headings, snippets)
    n_pages = max(h["page"] for h in headings) + 1

    prompt = prompt_template.replace("{HEADINGS}", heading_text)

    result = extract_json(
        system_prompt=prompt,
        user_content="Analyze the heading list above and return the section map.",
        model=model,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
        label=f"pass0:{report_dir.name}",
        num_ctx=num_ctx,
    )

    if result is None:
        print(f"  [error] {report_dir.name} — LLM returned no parseable JSON")
        return None

    # Flatten LLM output {section_type: [{label, pages}]} into the shared
    # segments.json format used across this pipeline.  Each section type
    # becomes a <section>_pages key so downstream tools discover it the same
    # way they discover form_pages / narrative_pages / nrhp_pages on site forms.
    # The original labeled sub-segments are preserved in _section_detail for
    # human review.
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
        "report": report_dir.name,
        "n_headings": len(headings),
        "n_pages": n_pages,
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
    ap.add_argument("--force", action="store_true",
                    help="Re-run even if output already exists")
    args = ap.parse_args()

    # Load config file if provided; CLI flags override
    cfg: dict = {}
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            sys.exit(f"Config not found: {cfg_path}")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    input_dir_str  = args.input_dir  or cfg.get("input_dir")
    model          = args.model      or cfg.get("model",          DEFAULT_MODEL)
    base_url       = args.base_url   or cfg.get("base_url",       DEFAULT_BASE_URL)
    temperature    = args.temperature if args.temperature is not None else float(cfg.get("temperature",    0.05))
    timeout        = args.timeout    if args.timeout    is not None else float(cfg.get("timeout_seconds", 1800))
    num_ctx        = args.num_ctx    if args.num_ctx    is not None else int(cfg.get("num_ctx",           32768))

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
    model_slug = model.replace(":", "_").replace(".", "_")
    model_dir = run_dir / model_slug
    model_dir.mkdir(exist_ok=True)

    print(f"\nRun dir   : {run_dir}")
    print(f"Model     : {model}")
    print(f"Reports   : {len(report_dirs)}\n")

    reset_stats()
    run_started = datetime.now(timezone.utc)
    run_t0 = time.monotonic()
    report_timings: list[dict] = []
    n_processed = 0

    for report_dir in report_dirs:
        out_path = model_dir / f"{report_dir.name}.segments.json"
        if out_path.exists() and not args.force:
            print(f"  [skip] {report_dir.name}")
            continue

        print(f"  [segment] {report_dir.name} ...", end=" ", flush=True)
        t0 = time.monotonic()

        result = segment_report(
            report_dir, prompt_template,
            model, base_url, temperature, timeout, num_ctx,
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
        print(f"{elapsed:.0f}s  |  {summary}")

        report_timings.append({"report": report_dir.name, "seconds": round(elapsed, 1)})
        n_processed += 1

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

    print(f"\nDone.  {n_processed} reports  |  {run_elapsed:.0f}s total")
    print(f"Run directory -> {run_dir}")


if __name__ == "__main__":
    main()
