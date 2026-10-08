#!/usr/bin/env python3
"""
classify_figures.py — flag each figure extracted by pdf_ocr
(docling.extract_figures) into one or more visual classes using a small
local vision model.

Reads <input_dir>/*/figures/figures.json — pdf_ocr's per-document figure
manifest (image file, page, bbox, and, when pdf_ocr's figure-context
enrichment ran, the figure's own printed caption, active heading path, and
surrounding body-text snippets) — and classifies each figure's PNG against
the class taxonomy in config.yaml's figure_classes list.

This is a separate pipeline stage from page/investigation segmentation:
figures aren't scoped to a trinomial/investigation the way pages are, so
this walks pdf_ocr's output directory directly (one figures.json per
document) rather than using grouper.py's trinomial grouping.

Classes are config-driven, not hardcoded, and a figure may be tagged with
more than one — a single composite figure showing both a map and a
stratigraphic column should get both, not be forced into one bucket. Each
document's output carries a `<class_key>_figures` list per configured class,
mirroring the page segmenter's `<type>_pages` convention so downstream tools
can request e.g. `lithic_tool_figures` the same way they already request
`narrative_pages`, with no code changes when a new class is added to config.

Each run creates its own, never-reused directory: runs/<YYYYMMDD_HHMM>_<gitsha>/

Per run directory:
  config.yaml                     config snapshot
  prompts.yaml                    the rendered classification prompt
  figure_classification_map.md    human-readable map (appended per document)
  figure_classes.csv              one row per figure
  run_metadata.json               run-level stats
  inventory.csv                   one row per output file
  <model_slug>__<pdf_stem>.figure_segments.json   full structured output per document

Usage:
    python classify_figures.py                          # all documents, config_figures.yaml
    python classify_figures.py --pdf "16WN385"           # single document (by folder name)
    python classify_figures.py --config other.yaml
    python classify_figures.py --model qwen2.5vl:7b      # override vision_model
"""
import argparse
import base64
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from ollama_client import extract_json_vision, get_stats, reset_stats
from figure_reporter import append_figure_classes_csv, append_figure_classification_map
from inventory import write_inventory_csv


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "nogit"


def _make_run_dir(base: Path) -> Path:
    """Create a fresh, never-reused run directory — same convention as
    segmenter.py's _make_run_dir."""
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


def _slug(s: str) -> str:
    return s.replace(":", "_").replace("/", "_").replace(".", "_")


def _find_documents(input_dir: Path) -> list[Path]:
    """Return every figures.json under input_dir, one per pdf_ocr document
    folder — <input_dir>/<pdf_stem>/figures/figures.json."""
    return sorted(
        input_dir.glob("*/figures/figures.json"),
        key=lambda p: p.parent.parent.name.upper(),
    )


def _render_class_list(figure_classes: list[dict]) -> str:
    return "\n".join(
        f'- "{c["key"]}" — {c["label"]}: {c["description"]}' for c in figure_classes
    )


def _figure_context(entry: dict) -> str:
    """Build the text context block for one figure from pdf_ocr's manifest
    entry. caption/heading_path/text_before/text_after are only present when
    pdf_ocr's figure-context enrichment ran (see lib/ocr_docling.py's
    _figure_context in pdf_ocr) — degrade gracefully to page-only for older
    figures.json files that predate it."""
    lines = [f"Page: {entry.get('page')}"]
    if entry.get("caption"):
        lines.append(f"Printed caption: {entry['caption']}")
    if entry.get("heading_path"):
        lines.append(f"Section: {' > '.join(entry['heading_path'])}")
    if entry.get("text_before"):
        lines.append(f"Text before figure: {entry['text_before']}")
    if entry.get("text_after"):
        lines.append(f"Text after figure: {entry['text_after']}")
    # Docling's own guess is deliberately NOT passed to the model. On lithic
    # papers its taxonomy files artefact plates under `icon` and `logo` —
    # Araho's "Nearly complete example of a Type 1 stemmed tool" comes back as
    # `icon`, Broich's "Selected artifacts from the Late Early Neolithic" as
    # `logo` — so including it anchors the vision model toward a wrong label on
    # precisely the class this stage exists to find.
    return "\n".join(lines)


def _classify_one_figure(
    entry: dict,
    figures_dir: Path,
    class_list_text: str,
    valid_keys: set[str],
    ollama_cfg: dict,
    label: str,
) -> dict | None:
    img_path = figures_dir / entry["file"]
    if not img_path.exists():
        print(f"    [warn] {label}: missing image {img_path.name}")
        return None

    b64 = base64.b64encode(img_path.read_bytes()).decode("utf-8")
    system_prompt = ollama_cfg["prompt_template"].replace("{FIGURE_CLASSES}", class_list_text)
    user_content = _figure_context(entry)

    result = extract_json_vision(
        system_prompt=system_prompt,
        user_content=user_content,
        images=[b64],
        model=ollama_cfg["vision_model"],
        base_url=ollama_cfg["base_url"],
        temperature=ollama_cfg["temperature"],
        timeout=ollama_cfg["timeout"],
        label=label,
        num_ctx=ollama_cfg.get("num_ctx"),
        think=ollama_cfg.get("think"),
    )

    if not isinstance(result, dict):
        print(f"    [warn] {label}: classification failed, no result")
        return None

    classes = result.get("classes", [])
    if not isinstance(classes, list) or not classes:
        print(f"    [warn] {label}: no classes returned")
        return None
    classes = [c for c in classes if c in valid_keys] or ["other"]

    primary = result.get("primary_class")
    if primary not in classes:
        primary = classes[0]

    return {
        "file":          entry["file"],
        "page":          entry.get("page"),
        "classes":       classes,
        "primary_class": primary,
        "rationale":     result.get("rationale", ""),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config_figures.yaml")
    parser.add_argument("--pdf",    default=None, help="Process only this pdf_ocr document folder (by name)")
    parser.add_argument("--model",  default=None, help="Override vision_model from config")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    input_dir      = Path(cfg["input_dir"])
    vision_model   = args.model or cfg.get("vision_model", "qwen2.5vl:7b")
    base_url       = cfg.get("base_url", "http://localhost:11434")
    temperature    = cfg.get("temperature", 0.1)
    timeout        = cfg.get("timeout_seconds", 600)
    num_ctx        = cfg.get("num_ctx", 8192)
    think          = cfg.get("think")   # None = leave to Ollama (see lib/ollama_client._set_think)
    figure_classes = cfg["figure_classes"]

    seg_types_dir = Path("segment_types")
    prompt_template = (seg_types_dir / "figure_classify.txt").read_text(encoding="utf-8").strip()
    prompt_template += "\n\nReturn ONLY a JSON object. No other text."
    class_list_text = _render_class_list(figure_classes)
    class_keys = [c["key"] for c in figure_classes]
    valid_keys = set(class_keys) | {"other"}

    runs_root = Path(cfg.get("output_dir", "runs"))
    run_dir = _make_run_dir(runs_root)
    config_path = Path(args.config)
    shutil.copy(config_path, run_dir / config_path.name)
    (run_dir / "prompts.yaml").write_text(
        yaml.dump(
            {"figure_classify_prompts": {
                "figure_classify": prompt_template.replace("{FIGURE_CLASSES}", class_list_text),
            }},
            allow_unicode=True, default_flow_style=False, sort_keys=False,
        ),
        encoding="utf-8",
    )

    documents = _find_documents(input_dir)
    if args.pdf:
        documents = [d for d in documents if d.parent.parent.name == args.pdf]
        if not documents:
            print(f"[error] no figures.json found for '{args.pdf}' under {input_dir}")
            return

    model_slug = _slug(vision_model)
    print(f"Run dir  : {run_dir}")
    print(f"Documents: {len(documents)}")
    print(f"Model    : {vision_model}")
    print(f"Classes  : {class_keys}")

    reset_stats()
    ollama_cfg = {
        "prompt_template": prompt_template,
        "vision_model":    vision_model,
        "base_url":        base_url,
        "temperature":     temperature,
        "timeout":         timeout,
        "num_ctx":         num_ctx,
        "think":           think,
    }

    run_started = datetime.now(timezone.utc)
    run_t0 = time.monotonic()
    doc_timings: list[dict] = []
    inventory_rows: list[dict] = []
    n_processed = 0
    n_figures_total = 0

    for fig_json_path in documents:
        pdf_stem = fig_json_path.parent.parent.name
        figures_dir = fig_json_path.parent
        manifest = json.loads(fig_json_path.read_text(encoding="utf-8"))
        entries = manifest.get("figures", [])
        if not entries:
            print(f"  [skip] {pdf_stem}: no figures in manifest")
            continue

        print(f"\n[{pdf_stem}]  ({len(entries)} figure(s))")
        doc_t0 = time.monotonic()
        classified = []
        for i, entry in enumerate(entries):
            label = f"figclass:{pdf_stem}:{entry.get('file', i)}"
            result = _classify_one_figure(
                entry, figures_dir, class_list_text, valid_keys, ollama_cfg, label,
            )
            if result is not None:
                classified.append(result)
                print(f"    {result['file']}: {result['classes']} (primary: {result['primary_class']})")

        if not classified:
            print(f"  [warn] {pdf_stem}: no figures classified")
            continue

        by_class: dict[str, list[str]] = {key: [] for key in class_keys}
        by_class.setdefault("other", [])
        for fig in classified:
            for cls in fig["classes"]:
                by_class.setdefault(cls, []).append(fig["file"])

        classified_at = datetime.now().isoformat(timespec="seconds")
        out = {
            "pdf_stem":      pdf_stem,
            "classified_at": classified_at,
            "vision_model":  vision_model,
            "n_figures":     len(classified),
            "figures":       classified,
        }
        for key, files in by_class.items():
            out[f"{key}_figures"] = files

        out_file = run_dir / f"{model_slug}__{pdf_stem}.figure_segments.json"
        out_file.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

        append_figure_classes_csv(run_dir, pdf_stem, classified, model=vision_model)
        append_figure_classification_map(run_dir, pdf_stem, classified, by_class, model=vision_model)

        doc_elapsed = time.monotonic() - doc_t0
        doc_timings.append({"pdf_stem": pdf_stem, "seconds": round(doc_elapsed, 2), "n_figures": len(classified)})
        n_processed += 1
        n_figures_total += len(classified)

        inventory_rows.append({
            "run_id":              run_dir.name,
            "tool":                "site_form_segmenter:figures",
            "model":               vision_model,
            "file_name":           out_file.name,
            "file_path":           str(out_file),
            "source_input":        str(fig_json_path),
            "prompt_file":         str(seg_types_dir / "figure_classify.txt"),
            "prompt_snapshot_key": "figure_classify",
            "temperature":         temperature,
            "num_ctx":             num_ctx,
            "chunk_strategy":      "one_figure_per_call",
            "produced_at":         classified_at,
            "output_file_path":    str(out_file),
        })
        print(f"  [done] {pdf_stem} — {len(classified)}/{len(entries)} figure(s) classified ({doc_elapsed:.1f}s)")

    run_elapsed = time.monotonic() - run_t0
    stats   = get_stats()
    total   = stats["prompt_tokens"] + stats["completion_tokens"]
    elapsed = stats["elapsed_s"]
    speed   = stats["completion_tokens"] / elapsed if elapsed > 0 else 0
    print(f"\nTokens: {total:,}  ({stats['completion_tokens']:,} generated)  Speed: {speed:.1f} tok/s")

    avg_per_doc = round(run_elapsed / n_processed, 2) if n_processed else None
    (run_dir / "run_metadata.json").write_text(
        json.dumps({
            "run_id":                   run_dir.name,
            "started_at":               run_started.isoformat(),
            "completed_at":             datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds":          round(run_elapsed, 1),
            "vision_model":             vision_model,
            "figure_classes":           class_keys,
            "n_documents":              n_processed,
            "n_figures":                n_figures_total,
            "avg_seconds_per_document": avg_per_doc,
            "chunking": {
                "strategy": "one_figure_per_call",
                "num_ctx":  num_ctx,
            },
            "token_stats": {
                "prompt_tokens":     stats["prompt_tokens"],
                "completion_tokens": stats["completion_tokens"],
                "total_tokens":      total,
                "tokens_per_second": round(speed, 1),
            },
            "document_timings": doc_timings,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    write_inventory_csv(run_dir, inventory_rows)

    print(f"\nDone.  Run directory -> {run_dir}")


if __name__ == "__main__":
    main()
