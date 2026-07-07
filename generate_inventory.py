#!/usr/bin/env python3
"""
generate_inventory.py — backfill inventory.csv for existing run folders.

Handles both:
  - new flat layout:   <run>/<model_slug>__<stem>.<ext>
  - old nested layout: <run>/<model_slug>/<stem>.<ext>

Fields that can't be recovered from the run folder are filled with the
literal string "not recorded" rather than left blank or guessed, so gaps in
historical provenance stay visible.

Usage:
    uv run python generate_inventory.py <run_dir>
    uv run python generate_inventory.py --all [--runs-root runs]
    uv run python generate_inventory.py "G:/path/to/segments_0/20260629_1833_b34ed56" --tool "site_form_segmenter:reports"
"""
import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "lib"))
from inventory import write_inventory_csv, NOT_RECORDED

OUTPUT_EXTS = (".segments.json", ".coded.json", ".trinomials.json", ".terms.json")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _run_metadata(run_dir: Path) -> dict:
    p = run_dir / "run_metadata.json"
    return _load_json(p) if p.exists() else {}


def _config_snapshot(run_dir: Path) -> dict:
    for p in run_dir.glob("*.yaml"):
        try:
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
    return {}


def _is_output(path: Path) -> bool:
    name = path.name
    if name.startswith("all_"):
        return False
    return any(name.endswith(ext) for ext in OUTPUT_EXTS)


def _iter_output_files(run_dir: Path):
    """Yield (model_slug, file_path) for every output file in a run dir,
    handling both flat (model__stem.ext) and nested (model/stem.ext) layouts."""
    for child in sorted(run_dir.iterdir()):
        if child.is_dir():
            model_slug = child.name
            for f in sorted(child.iterdir()):
                if f.is_file() and _is_output(f):
                    yield model_slug, f
        elif child.is_file() and _is_output(child):
            name = child.name
            if "__" in name:
                model_slug, _, _ = name.partition("__")
            else:
                model_slug = NOT_RECORDED
            yield model_slug, child


def build_rows(run_dir: Path, tool: str) -> list[dict]:
    meta = _run_metadata(run_dir)
    cfg  = _config_snapshot(run_dir)
    chunking = meta.get("chunking", {})
    chunk_strategy = ("; ".join(f"{k}={v}" for k, v in chunking.items())
                      if chunking else NOT_RECORDED)

    rows = []
    for model_slug, f in _iter_output_files(run_dir):
        data = _load_json(f)
        model = (data.get("model") or data.get("_model")
                 or data.get("segmentation_model") or meta.get("model") or model_slug)
        produced_at = (data.get("segmented_at") or data.get("coded_at")
                       or data.get("_extracted_at") or NOT_RECORDED)
        source_input = (data.get("_source_file") or data.get("source_file")
                        or data.get("report") or data.get("trinomial")
                        or data.get("_item") or NOT_RECORDED)
        rows.append({
            "run_id":              run_dir.name,
            "tool":                tool,
            "model":               model,
            "file_name":           f.name,
            "file_path":           str(f),
            "source_input":        source_input,
            "prompt_file":         meta.get("prompt_file", NOT_RECORDED),
            "prompt_snapshot_key": NOT_RECORDED,
            "temperature":         cfg.get("temperature", NOT_RECORDED),
            "num_ctx":             cfg.get("num_ctx", NOT_RECORDED),
            "chunk_strategy":      chunk_strategy,
            "produced_at":         produced_at,
            "output_file_path":    str(f),
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", default=None, metavar="RUN_DIR",
                     help="Path to a single run folder (old nested or new flat layout)")
    ap.add_argument("--all", action="store_true",
                     help="Sweep every run folder under --runs-root")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--tool", default="site_form_segmenter",
                     help="Tool label recorded in inventory.csv (e.g. 'site_form_segmenter:reports')")
    args = ap.parse_args()

    if args.all:
        root = Path(args.runs_root)
        if not root.is_dir():
            sys.exit(f"runs-root not found: {root}")
        run_dirs = [d for d in sorted(root.iterdir()) if d.is_dir()]
    elif args.run_dir:
        run_dirs = [Path(args.run_dir)]
    else:
        sys.exit("Provide a RUN_DIR, or pass --all to sweep --runs-root")

    for run_dir in run_dirs:
        if not run_dir.is_dir():
            print(f"  [skip] {run_dir} — not a directory")
            continue
        rows = build_rows(run_dir, args.tool)
        if not rows:
            print(f"  [skip] {run_dir.name} — no output files found")
            continue
        write_inventory_csv(run_dir, rows)
        print(f"  [done] {run_dir.name} — {len(rows)} file(s) -> inventory.csv")


if __name__ == "__main__":
    main()
