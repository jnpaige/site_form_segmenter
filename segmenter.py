#!/usr/bin/env python3
"""
site_form_segmenter — identify investigation boundaries and key page types in
Louisiana archaeological site form PDFs.

Two modes:
  text   — 4-pass approach using the text_model on OCR text for all passes
  vision — vision_model for pass 1 (investigation boundaries via rendered PDF images),
            then text_model for passes 2–4 (form page, narrative, NRHP)

Each run creates a versioned directory:
  runs/<YYYYMMDD_HH_gitsha>/<mode_slug>/

Per run directory:
  config.yaml          config snapshot
  prompts.yaml         every prompt used
  segmentation_map.md  human-readable page map (appended per site)
  segments.csv         machine-readable segment table (appended per site)
  <trinomial>.segments.json  full structured output per site

Usage:
    python segmenter.py                          # all sites, mode from config
    python segmenter.py --trinomial 16WN385      # single site
    python segmenter.py --mode text              # override mode
    python segmenter.py --mode vision            # override mode
    python segmenter.py --force                  # re-run existing outputs
    python segmenter.py --config config.yaml     # explicit config path
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from grouper       import group_by_trinomial
from page_parser   import parse_pages, build_page_preview
from pdf_renderer  import render_pages_to_contact_sheet
from ollama_client import extract_json, extract_json_vision, get_stats
from reporter      import append_segments_csv, append_segmentation_map


def _check_pymupdf() -> bool:
    """Return True if PyMuPDF is importable in the current Python environment."""
    try:
        import pymupdf      # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import fitz         # noqa: F401
        return True
    except ImportError:
        return False


_PYMUPDF_HINT = """\
[error] PyMuPDF is not installed in the current Python environment.
        Python: {python}

        Fix — activate this project's venv first:
          .venv\\Scripts\\activate        (Windows)
          source .venv/bin/activate      (Mac/Linux)
          python segmenter.py

        Or use uv run so the venv is selected automatically:
          uv run python segmenter.py

        If the venv doesn't exist yet:
          uv sync
          uv run python segmenter.py
"""


def _run_id() -> str:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        sha = "nogit"
    return f"{datetime.now().strftime('%Y%m%d_%H%M')}_{sha}"


def _slug(s: str) -> str:
    return s.replace(":", "_").replace("/", "_")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",    default="config.yaml")
    parser.add_argument("--mode",      choices=["text", "vision"], default=None)
    parser.add_argument("--trinomial", default=None)
    parser.add_argument("--force",     action="store_true")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    input_dir    = Path(cfg["input_dir"])
    pattern      = cfg.get("trinomial_pattern", r"(\d{2}[A-Z]{2}\d+)")
    mode         = args.mode or cfg.get("mode", "vision")

    if mode == "vision" and not _check_pymupdf():
        print(_PYMUPDF_HINT.format(python=sys.executable))
        sys.exit(1)
    page_trunc   = cfg.get("page_truncation_chars", 500)
    thumb_width  = cfg.get("pdf_thumb_width", 240)
    base_url     = cfg["base_url"]
    text_model   = cfg["text_model"]
    vision_model = cfg.get("vision_model", "llama3.2-vision:11b")
    temperature  = cfg.get("temperature", 0.05)
    timeout      = cfg.get("timeout_seconds", 1800)
    num_ctx_min  = cfg.get("num_ctx_min", 8192)
    num_ctx_max  = cfg.get("num_ctx_max", 32768)

    seg_types_dir = Path("segment_types")

    if mode == "vision":
        mode_slug = f"vision__{_slug(vision_model)}__{_slug(text_model)}"
    else:
        mode_slug = f"text__{_slug(text_model)}"

    runs_root = Path(cfg.get("output_dir", "runs"))
    run_dir = runs_root / _run_id()
    out_dir = run_dir / mode_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.yaml").write_text(
        Path(args.config).read_text(encoding="utf-8"), encoding="utf-8"
    )

    groups = group_by_trinomial(input_dir, pattern)
    if args.trinomial:
        t = args.trinomial.upper()
        if t not in groups:
            print(f"[error] {t} not found in {input_dir}")
            return
        groups = {t: groups[t]}

    trinomials = sorted(groups.keys())
    print(f"Run dir : {out_dir}")
    print(f"Sites   : {len(trinomials)}  {trinomials}")
    print(f"Mode    : {mode}")
    if mode == "vision":
        print(f"Models  : pass1={vision_model}  passes2-4={text_model}")
    else:
        print(f"Model   : {text_model}")

    _save_prompt_snapshot(out_dir, mode, seg_types_dir)

    ollama_cfg = {
        "text_model":    text_model,
        "vision_model":  vision_model,
        "base_url":      base_url,
        "temperature":   temperature,
        "timeout":       timeout,
        "num_ctx_min":   num_ctx_min,
        "num_ctx_max":   num_ctx_max,
    }

    for trinomial in trinomials:
        entry    = groups[trinomial]
        txt_path = entry["txt"]
        pdf_path = entry.get("pdf")
        seg_file = out_dir / f"{trinomial}.segments.json"

        if seg_file.exists() and not args.force:
            print(f"  [skip] {trinomial} — exists")
            continue

        pages = parse_pages(txt_path)
        if not pages:
            print(f"  [warn] {trinomial}: no page markers in {txt_path.name}")
            continue

        contact_sheet: str = ""
        effective_mode = mode
        if mode == "vision":
            if pdf_path is None:
                print(f"  [warn] {trinomial}: no PDF found — falling back to text mode for pass 1")
                effective_mode = "text"
            else:
                print(f"  [render] {trinomial}: building contact sheet ({len(pages)} pages)")
                try:
                    contact_sheet = render_pages_to_contact_sheet(pdf_path, thumb_width=thumb_width)
                except ImportError as e:
                    print(f"  [error] {e}")
                    print(f"  [warn] falling back to text mode for pass 1")
                    effective_mode = "text"

        segments = _segment_site_form(
            trinomial, pages, contact_sheet,
            ollama_cfg, seg_types_dir, page_trunc, effective_mode,
        )
        if not segments:
            print(f"  [warn] {trinomial}: no segments produced")
            continue

        for seg in segments:
            seg["_source_file"] = txt_path.name

        seg_file.write_text(
            json.dumps({
                "trinomial":    trinomial,
                "segmented_at": datetime.now().isoformat(timespec="seconds"),
                "mode":         mode,
                "text_model":   text_model,
                "vision_model": vision_model if mode == "vision" else None,
                "segments":     segments,
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        append_segments_csv(out_dir, trinomial, segments)
        append_segmentation_map(
            out_dir, trinomial, segments,
            seg_model=(vision_model if effective_mode == "vision" else text_model),
            seg_type="site_form",
        )
        print(f"  [done] {trinomial} — {len(segments)} investigation(s)")

    stats   = get_stats()
    total   = stats["prompt_tokens"] + stats["completion_tokens"]
    elapsed = stats["elapsed_s"]
    speed   = stats["completion_tokens"] / elapsed if elapsed > 0 else 0
    print(f"\nTokens: {total:,}  ({stats['completion_tokens']:,} generated)  Speed: {speed:.1f} tok/s")
    print(f"\nDone.  Run directory -> {out_dir}")


# ---------------------------------------------------------------------------
# Core segmentation logic
# ---------------------------------------------------------------------------

def _segment_site_form(
    trinomial: str,
    pages: dict[int, str],
    contact_sheet: str,
    ollama_cfg: dict,
    seg_types_dir: Path,
    page_trunc: int,
    mode: str,
) -> list[dict]:
    text_model   = ollama_cfg["text_model"]
    vision_model = ollama_cfg["vision_model"]
    base_url     = ollama_cfg["base_url"]
    temp         = ollama_cfg["temperature"]
    timeout      = float(ollama_cfg["timeout"])
    num_ctx_min  = ollama_cfg.get("num_ctx_min", 8192)
    num_ctx_max  = ollama_cfg.get("num_ctx_max", 32768)
    page_trunc   = _adaptive_page_trunc(len(pages), page_trunc, num_ctx_max)
    full_preview = build_page_preview(pages, max_chars=page_trunc)
    page_nums    = sorted(pages.keys())

    # --- Pass 1: investigation boundaries ---
    if mode == "vision" and contact_sheet:
        p1_prompt = (seg_types_dir / "site_form_pass1_vision.txt").read_text(encoding="utf-8").strip()
        investigations = extract_json_vision(
            system_prompt=p1_prompt + "\n\nReturn ONLY a JSON array. No other text.",
            user_content=(
                f"DOCUMENT: {trinomial}\n\n"
                f"The image is a contact sheet of all {len(page_nums)} pages "
                f"(p{page_nums[0]}–p{page_nums[-1]}), arranged left-to-right then "
                f"top-to-bottom. Each thumbnail is labeled with its page number in red.\n\n"
                "Identify each distinct site investigation (site form) in this document."
            ),
            images=[contact_sheet],
            model=vision_model,
            base_url=base_url, temperature=temp, timeout=timeout,
            label=f"seg-p1-vision:{trinomial}",
        )
    else:
        p1_prompt = (seg_types_dir / "site_form_pass1_boundaries.txt").read_text(encoding="utf-8").strip()
        p1_system = p1_prompt + "\n\nReturn ONLY a JSON array. No other text."
        p1_user   = f"DOCUMENT: {trinomial}\n\n{full_preview}"
        investigations = extract_json(
            system_prompt=p1_system,
            user_content=p1_user,
            model=text_model,
            base_url=base_url, temperature=temp, timeout=timeout,
            label=f"seg-p1:{trinomial}",
            num_ctx=_estimate_num_ctx(p1_system, p1_user, min_ctx=num_ctx_min, max_ctx=num_ctx_max),
        )

    if not isinstance(investigations, list) or not investigations:
        print(f"    [warn] pass1 failed for {trinomial} — single segment fallback")
        investigations = [{"label": trinomial, "year": None, "pages": sorted(pages.keys())}]

    investigations = _fill_coverage_gaps(
        trinomial, investigations, pages, ollama_cfg, seg_types_dir, page_trunc,
    )

    segments = []
    for inv in investigations:
        label     = inv.get("label", trinomial)
        year      = inv.get("year")
        inv_pages = sorted(int(p) for p in inv.get("pages", []) if int(p) in pages)
        if not inv_pages:
            continue

        inv_preview = build_page_preview(
            {p: pages[p] for p in inv_pages}, max_chars=page_trunc
        )
        inv_context = f"INVESTIGATION: {label}\nPAGES: {inv_pages}\n\n{inv_preview}"

        # --- Pass 2: site record form page ---
        p2_prompt = (seg_types_dir / "site_form_pass2_form_page.txt").read_text(encoding="utf-8").strip()
        p2_system = p2_prompt + "\n\nReturn ONLY a JSON object. No other text."
        r2 = extract_json(
            system_prompt=p2_system,
            user_content=inv_context,
            model=text_model, base_url=base_url,
            temperature=temp, timeout=timeout,
            label=f"seg-p2:{trinomial}:{label}",
            num_ctx=_estimate_num_ctx(p2_system, inv_context, min_ctx=num_ctx_min, max_ctx=num_ctx_max),
        )
        form_pages = _page_list(r2, "form_pages", inv_pages)

        # --- Pass 3: narrative pages ---
        p3_template = (seg_types_dir / "site_form_pass3_narrative.txt").read_text(encoding="utf-8")
        p3_system   = (p3_template.replace("{form_pages}", str(form_pages)).strip()
                        + "\n\nReturn ONLY a JSON object. No other text.")
        r3 = extract_json(
            system_prompt=p3_system,
            user_content=inv_context,
            model=text_model, base_url=base_url,
            temperature=temp, timeout=timeout,
            label=f"seg-p3:{trinomial}:{label}",
            num_ctx=_estimate_num_ctx(p3_system, inv_context, min_ctx=num_ctx_min, max_ctx=num_ctx_max),
        )
        narrative_pages = _page_list(r3, "narrative_pages", inv_pages)

        # --- Pass 4: NRHP eligibility pages ---
        p4_prompt = (seg_types_dir / "site_form_pass4_nrhp.txt").read_text(encoding="utf-8").strip()
        p4_system = p4_prompt + "\n\nReturn ONLY a JSON object. No other text."
        r4 = extract_json(
            system_prompt=p4_system,
            user_content=inv_context,
            model=text_model, base_url=base_url,
            temperature=temp, timeout=timeout,
            label=f"seg-p4:{trinomial}:{label}",
            num_ctx=_estimate_num_ctx(p4_system, inv_context, min_ctx=num_ctx_min, max_ctx=num_ctx_max),
        )
        nrhp_pages = _page_list(r4, "nrhp_pages", [])

        segments.append({
            "label":           label,
            "year":            year,
            "pages":           inv_pages,
            "form_pages":      form_pages,
            "narrative_pages": narrative_pages,
            "nrhp_pages":      nrhp_pages,
        })

    return segments


def _estimate_num_ctx(
    *texts: str,
    min_ctx: int = 8192,
    max_ctx: int = 32768,
    completion_budget: int = 1024,
    chars_per_token: int = 3,
) -> int:
    """Estimate a num_ctx large enough to hold `texts` plus headroom for the
    model's response, clamped to [min_ctx, max_ctx] and rounded up to the
    nearest 1024.

    Ollama defaults num_ctx to 2048 regardless of the model's native context
    length, which silently truncates long prompts and produces garbled JSON.
    """
    total_chars = sum(len(t) for t in texts)
    est_tokens  = total_chars // chars_per_token + completion_budget
    ctx = ((est_tokens + 1023) // 1024) * 1024
    return max(min_ctx, min(max_ctx, ctx))


def _adaptive_page_trunc(
    num_pages: int,
    base_trunc: int,
    max_ctx: int,
    chars_per_token: int = 3,
    completion_budget: int = 1024,
    overhead_chars: int = 1000,
    floor: int = 500,
) -> int:
    """Reduce per-page truncation for documents with many pages so the full
    preview still fits within max_ctx, without chunking. Falls back to
    `base_trunc` for documents small enough to fit as-is.
    """
    if num_pages <= 0:
        return base_trunc
    budget_chars = (max_ctx - completion_budget) * chars_per_token - overhead_chars
    max_per_page = budget_chars // num_pages
    return max(floor, min(base_trunc, max_per_page))


def _contiguous_runs(nums: list[int]) -> list[list[int]]:
    """Group a sorted list of ints into maximal contiguous runs."""
    runs: list[list[int]] = []
    for n in nums:
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    return runs


def _fill_coverage_gaps(
    trinomial: str,
    investigations: list[dict],
    pages: dict[int, str],
    ollama_cfg: dict,
    seg_types_dir: Path,
    page_trunc: int,
    pad: int = 2,
) -> list[dict]:
    """Ensure every page in `pages` is claimed by some investigation.

    Pass 1 sometimes drops pages (often page 0, or whole multi-page runs)
    entirely from every segment's `pages` array. For each maximal run of
    unassigned pages, run a second prompt over that run plus `pad` pages of
    surrounding context, asking the model to assign the missing pages to an
    existing adjacent investigation or to a new one. Anything still
    unassigned afterwards is snapped to the nearest investigation by page
    distance, so coverage is guaranteed.
    """
    all_pages = set(pages.keys())

    for inv in investigations:
        inv["pages"] = sorted(int(p) for p in inv.get("pages", []) if int(p) in all_pages)

    assigned = {p for inv in investigations for p in inv["pages"]}
    missing  = sorted(all_pages - assigned)
    if not missing:
        return investigations

    text_model  = ollama_cfg["text_model"]
    base_url    = ollama_cfg["base_url"]
    temp        = ollama_cfg["temperature"]
    timeout     = float(ollama_cfg["timeout"])
    num_ctx_min = ollama_cfg.get("num_ctx_min", 8192)
    num_ctx_max = ollama_cfg.get("num_ctx_max", 32768)

    gapfill_file = seg_types_dir / "site_form_pass1_gapfill.txt"
    if not gapfill_file.exists():
        gapfill_template = None
    else:
        gapfill_template = gapfill_file.read_text(encoding="utf-8").strip()

    lo_bound, hi_bound = min(all_pages), max(all_pages)

    for run in _contiguous_runs(missing):
        if gapfill_template is None:
            break

        lo = max(lo_bound, run[0] - pad)
        hi = min(hi_bound, run[-1] + pad)
        window_pages = [p for p in range(lo, hi + 1) if p in all_pages]

        candidates = [
            {"label": inv["label"], "year": inv.get("year")}
            for inv in investigations
            if inv["pages"] and set(inv["pages"]) & set(window_pages)
        ]

        preview = build_page_preview({p: pages[p] for p in window_pages}, max_chars=page_trunc)
        gf_system = (
            gapfill_template
            .replace("{MISSING_PAGES}", str(run))
            .replace("{CANDIDATES}", json.dumps(candidates, ensure_ascii=False))
            + "\n\nReturn ONLY a JSON array. No other text."
        )
        gf_user = f"DOCUMENT: {trinomial}\n\n{preview}"
        result = extract_json(
            system_prompt=gf_system,
            user_content=gf_user,
            model=text_model, base_url=base_url,
            temperature=temp, timeout=timeout,
            label=f"seg-p1-gapfill:{trinomial}:{run[0]}-{run[-1]}",
            num_ctx=_estimate_num_ctx(gf_system, gf_user, min_ctx=num_ctx_min, max_ctx=num_ctx_max),
        )

        if not isinstance(result, list):
            print(f"    [warn] {trinomial}: gapfill failed for pages {run}")
            continue

        missing_set = set(run)
        for group in result:
            if not isinstance(group, dict):
                continue
            label     = group.get("label")
            new_pages = sorted(int(p) for p in group.get("pages", []) if str(p).lstrip("-").isdigit() and int(p) in missing_set)
            if not label or not new_pages:
                continue
            existing = next((inv for inv in investigations if inv["label"] == label), None)
            if existing is not None:
                existing["pages"] = sorted(set(existing["pages"]) | set(new_pages))
            else:
                investigations.append({
                    "label": label,
                    "year":  group.get("year"),
                    "pages": new_pages,
                })

    # Deterministic fallback: snap any still-unassigned page to the
    # nearest investigation by page distance.
    assigned = {p for inv in investigations for p in inv["pages"]}
    still_missing = sorted(all_pages - assigned)
    if still_missing:
        print(f"    [warn] {trinomial}: pages {still_missing} unassigned after gapfill — snapping to nearest segment")
        for p in still_missing:
            candidates = [inv for inv in investigations if inv["pages"]]
            if not candidates:
                continue
            nearest = min(candidates, key=lambda inv: min(abs(p - q) for q in inv["pages"]))
            nearest["pages"] = sorted(set(nearest["pages"]) | {p})

    investigations.sort(key=lambda inv: min(inv["pages"]) if inv["pages"] else 0)
    return investigations


def _page_list(result, key: str, valid_pages: list[int]) -> list[int]:
    if not isinstance(result, dict):
        return []
    raw = result.get(key, [])
    if not isinstance(raw, list):
        return []
    pages = [int(p) for p in raw if str(p).lstrip("-").isdigit()]
    if valid_pages:
        pages = [p for p in pages if p in valid_pages]
    return sorted(pages)


def _save_prompt_snapshot(out_dir: Path, mode: str, seg_types_dir: Path) -> None:
    p1_file = "site_form_pass1_vision.txt" if mode == "vision" else "site_form_pass1_boundaries.txt"
    p1_key  = "pass1_vision"               if mode == "vision" else "pass1_boundaries"
    files = [
        (p1_key,           p1_file),
        ("pass1_gapfill",  "site_form_pass1_gapfill.txt"),
        ("pass2_form_page", "site_form_pass2_form_page.txt"),
        ("pass3_narrative", "site_form_pass3_narrative.txt"),
        ("pass4_nrhp",      "site_form_pass4_nrhp.txt"),
    ]
    prompts = {
        name: (seg_types_dir / fname).read_text(encoding="utf-8")
        for name, fname in files
        if (seg_types_dir / fname).exists()
    }
    (out_dir / "prompts.yaml").write_text(
        yaml.dump({"segmentation_prompts": prompts},
                  allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
