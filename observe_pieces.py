#!/usr/bin/env python3
"""
observe_pieces.py — turn each artefact crop into a structured observation.

The vision model's job here is perception, not coding. It reports what is
visible — object type, which views are shown, which morphological features are
present — in a controlled vocabulary that maps onto the codebooks' observable
criteria. It is never asked whether a procedural unit is present: that judgment
needs the prose, the tables and the other pieces in the assemblage, and it
happens later in code_assemblages.py.

Two things are supplied to the model rather than asked of it:

  size    figure_scale_bars.py measured px_per_cm from the plate's own rule, so
          the piece's longest dimension is arithmetic. Several modes turn on
          absolute size (D2 over 5cm, D4 under 5cm, E1 over 10cm) and a model
          reading a bar off a crop is the least reliable way to get it.

  views   the model reports which views it can see, and "cannot tell" is a
          first-class answer, because the codebooks refuse figure evidence from
          the wrong view: determining faceting from figures alone "requires
          figures of the platforms themselves. Do not code based on figures
          showing only dorsal or ventral views of flakes."

OUTPUT  <doc>/figures/piece_observations.json

Usage:
    uv run python observe_pieces.py --input-dir "path/to/corpus"
    uv run python observe_pieces.py --input-dir ... --all-plates --model qwen3-vl:32b
"""
import argparse
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))
from ollama_client import extract_json_vision

OBJECTS = ["core", "flake", "blade", "bladelet", "retouched_tool", "biface",
           "hammerstone", "refit_group", "diagram", "not_an_artefact"]
VIEWS = ["dorsal", "ventral", "platform", "profile", "section", "plan"]
FEATURES = [
    "faceted_platform", "dihedral_platform", "plain_platform", "cortical_platform",
    "platform_abrasion", "overhang_removal",
    "radial_or_centripetal_scars", "unidirectional_scars", "bidirectional_scars",
    "crested_ridge", "cortical_lateral_edge", "plunging_distal",
    "ventral_on_ventral", "core_tablet_profile",
    "retouch_abrupt", "retouch_invasive", "retouch_notch", "burin_spall",
    "tranchet_scar", "tang", "bifacial_thinning",
    "battering_or_crushing", "opposed_bipolar_crushing", "ground_edge",
    "pecked_surface",
]

SYSTEM = """You are describing ONE stone artefact illustration cropped from a
figure in an archaeological paper. Report only what you can actually see.

Return JSON with exactly these keys:
  "object": one of {objects}
  "views": subset of {views} - the views of this piece that are shown
  "features": subset of {features} - only those you can positively see
  "uncertain": true if the image is too small, unclear or ambiguous to describe
  "notes": one short sentence describing the piece

Rules:
- Do NOT guess. Omit a feature rather than include one you are unsure of.
- A feature that would need a view you cannot see must be omitted. You cannot
  judge a platform from a dorsal view alone.
- If the crop shows several artefacts rather than one, set object to
  "refit_group" only if they are a refit; otherwise set "uncertain" to true.
- If the crop shows text, a scale bar, or a schematic rather than an artefact,
  set object to "not_an_artefact" or "diagram"."""


def trustworthy(entry: dict) -> bool:
    """Plates where the grouping produced something worth looking at.

    On dense photographic plates the dilation merges every artefact into one
    region; such a plate yields a single box covering most of the image, and a
    crop of the whole plate tells a per-piece model nothing.
    """
    w, h = entry.get("plate_px") or [0, 0]
    ps = entry.get("pieces") or []
    if not ps or not w or not h:
        return False
    biggest = max((g["r"] - g["l"]) * (g["b"] - g["t"]) for g in ps) / (w * h)
    exp = entry.get("caption_expected_pieces")
    if biggest > 0.5 or len(ps) < 3:
        return False
    return not exp or abs(len(ps) - exp) <= max(2, 0.25 * exp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--only")
    ap.add_argument("--all-plates", action="store_true")
    ap.add_argument("--model", default="qwen2.5vl:7b")
    ap.add_argument("--base-url", default="http://localhost:11434")
    args = ap.parse_args()

    system = (SYSTEM.replace("{objects}", json.dumps(OBJECTS))
                    .replace("{views}", json.dumps(VIEWS))
                    .replace("{features}", json.dumps(FEATURES)))
    root = Path(args.input_dir)
    n_obs = n_skip = 0
    t0 = time.time()

    for d in sorted(x for x in root.iterdir() if x.is_dir()):
        if args.only and args.only.lower() not in d.name.lower():
            continue
        fp = d / "figures" / "figure_pieces.json"
        if not fp.exists():
            continue
        entries = json.loads(fp.read_text(encoding="utf-8"))
        sb = {}
        f2 = d / "figures" / "figure_scale_bars.json"
        if f2.exists():
            sb = {e["file"]: e for e in json.loads(f2.read_text(encoding="utf-8"))}

        out = []
        for entry in entries:
            if not args.all_plates and not trustworthy(entry):
                n_skip += len(entry.get("pieces") or [])
                continue
            crops = entry.get("crops") or []
            px_cm = (sb.get(entry["file"]) or {}).get("px_per_cm")
            for i, g in enumerate(entry.get("pieces") or []):
                if i >= len(crops):
                    break
                crop = d / "figures" / "piece_crops" / crops[i]
                if not crop.exists():
                    continue
                longest_cm = (round(max(g["r"] - g["l"], g["b"] - g["t"]) / px_cm, 1)
                              if px_cm else None)
                ctx = [f"Figure: {entry.get('printed_figure_number') or entry['file']}",
                       f"Piece {i + 1} of {len(entry['pieces'])} on this plate"]
                if longest_cm:
                    ctx.append(f"Measured longest dimension: {longest_cm} cm "
                               f"(from the plate's own scale bar)")
                else:
                    ctx.append("No scale bar was measured for this plate; "
                               "do not guess absolute size.")
                b64 = base64.b64encode(crop.read_bytes()).decode()
                res = extract_json_vision(
                    system_prompt=system, user_content="\n".join(ctx), images=[b64],
                    model=args.model, base_url=args.base_url, temperature=0.1,
                    timeout=300, label=f"piece:{d.name[:18]}:{crops[i]}", num_ctx=8192)
                if not isinstance(res, dict):
                    continue
                out.append({
                    "piece_id": f"{entry['file'].rsplit('.', 1)[0]}#p{i + 1}",
                    "file": entry["file"], "crop": crops[i],
                    "piece_index": i + 1,
                    "page": entry.get("page"),
                    "printed_figure_number": entry.get("printed_figure_number"),
                    "bbox": g,
                    "longest_cm": longest_cm,
                    "object": res.get("object"),
                    "views": [v for v in (res.get("views") or []) if v in VIEWS],
                    "features": [f for f in (res.get("features") or []) if f in FEATURES],
                    "uncertain": bool(res.get("uncertain")),
                    "notes": (res.get("notes") or "")[:240],
                })
                n_obs += 1
        if out:
            (d / "figures" / "piece_observations.json").write_text(
                json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
            kept = sum(1 for o in out if o["object"] not in ("not_an_artefact", "diagram")
                       and not o["uncertain"])
            print(f"  {d.name[:44]:46s} {len(out):4d} observed, {kept:4d} usable", flush=True)

    print(f"\n{n_obs} observations in {time.time() - t0:.0f}s "
          f"({n_skip} pieces skipped on untrustworthy plates)")


if __name__ == "__main__":
    main()
