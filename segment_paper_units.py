#!/usr/bin/env python3
"""Find a paper's layers and assemblages and link its figures, pieces and text to them (paper_units/1).

Reads <corpus>/<paper>/paper_elements.json from pdf_ocr; writes
<corpus>/000_segments/runs/<run>/<model>__<paper>.paper_units.json.

Usage:
    uv run python segment_paper_units.py --input-dir "<corpus>"
    uv run python segment_paper_units.py --input-dir "<corpus>" --only "<paper>" --no-text
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))
import fitz
from ollama_client import extract_json, extract_json_vision

PROMPTS = Path(__file__).parent / "segment_types"
STRAT_WORDS = re.compile(r"\b(layer|level|unit|horizon|stratigraph|spit|bed|member|complex|locus|trench|"
                         r"assemblage|excavat|sequence|deposit|occupation)", re.I)
BACK_MATTER = re.compile(r"^\W*(\d+(\.\d+)*\.?\s*)?(appendix\s*\w?\.?\s*)?(references?( cited)?|acknowledge?ments?|"
                         r"bibliography|supplementary (online )?(material|information|data))\W*$", re.I)
INVENTORY_CHARS = 60000
TEXT_CHUNK_CHARS = 12000
IMAGE_MAX_EDGE = 1024


def prompt(name: str) -> str:
    return (PROMPTS / f"paper_units_{name}.txt").read_text(encoding="utf-8")


def norm(name: str) -> str:
    """Comparison key for layer names: lower case, no 'layer', no punctuation."""
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\b(layers?|level|unit)\b", " ", (name or "").lower()))


def line(e: dict) -> str:
    return f"[{e['id']}] {e.get('text') or e.get('caption') or ''}"


def back_matter(e: dict) -> bool:
    """True when the last part of the element's section path is a back-matter title."""
    return bool(BACK_MATTER.match((e.get("section") or "").split(">")[-1]))


def flat(e: dict) -> str:
    return re.sub(r"\s+", " ", e.get("text") or e.get("caption") or "").strip()


def inventory_excerpts(elements: list[dict]) -> str:
    """The evidence the inventory call sees: front matter, headings, captions, tables, stratigraphy prose."""
    body = [e for e in elements if e["kind"] != "figure" and not back_matter(e)]
    front = {e["id"] for e in [b for b in body if b["kind"] == "paragraph" and b["page"] <= 1][:6]}
    keep = [e for e in body if e["id"] in front or e["kind"] in ("heading", "caption", "table_row")
            or (e["kind"] == "paragraph" and STRAT_WORDS.search(e["text"]))]
    lines, last_header = [], ""
    for e in keep:
        text = flat(e)
        if e["kind"] == "table_row" and " || row:" in text:
            header, row = text.split(" || row:", 1)
            text = row if header == last_header else text
            last_header = header
        lines.append(f"[{e['id']}] {text}"[:1500])
    return "\n".join(lines)[:INVENTORY_CHARS]


def build_inventory(elements: list[dict], ask) -> tuple[list[dict], list[dict]]:
    """One call: layers and assemblages, with ids assigned and evidence checked against real element ids."""
    reply = ask(prompt("inventory"), inventory_excerpts(elements), label="inventory") or {}
    ids = {e["id"] for e in elements}
    layers = []
    for i, l in enumerate(reply.get("layers") or [], 1):
        layers.append({"id": f"L{i}", "name": l.get("name") or f"layer {i}",
                       "kind": l.get("kind") if l.get("kind") in ("archaeological", "geological") else "archaeological",
                       "locality": l.get("locality") or "", "artefact_bearing": bool(l.get("artefact_bearing")),
                       "aliases": l.get("aliases") or [], "evidence": [x for x in l.get("evidence") or [] if x in ids]})
    by_name = {norm(n): l["id"] for l in layers for n in [l["name"], *l["aliases"]]}
    assemblages = []
    for i, a in enumerate(reply.get("assemblages") or [], 1):
        layer_ids = sorted({by_name[norm(n)] for n in a.get("layer_names") or [] if norm(n) in by_name})
        assemblages.append({"id": f"A{i}", "label": a.get("label") or f"assemblage {i}",
                            "locality": a.get("locality") or "", "layer_ids": layer_ids,
                            "industry": a.get("industry"), "n_artefacts": a.get("n_artefacts"),
                            "evidence": [x for x in a.get("evidence") or [] if x in ids]})
    return layers, assemblages


def unit_list(layers: list[dict], assemblages: list[dict]) -> str:
    """The id lists every linking call is given."""
    rows = ["ASSEMBLAGES:"] + [f"{a['id']}: {a['label']} (layers {', '.join(a['layer_ids']) or 'none'})" for a in assemblages]
    rows += ["LAYERS:"] + [f"{l['id']}: {l['name']} ({l['locality']})" for l in layers]
    return "\n".join(rows)


def clean(ids, allowed: set[str]) -> list[str]:
    """The string ids from a reply list that are in the allowed set."""
    return sorted({x for x in ids if isinstance(x, str) and x in allowed}) if isinstance(ids, list) else []


def image_b64(path: Path) -> str:
    """The image as base64 PNG, shrunk so its longer edge is at most IMAGE_MAX_EDGE pixels."""
    pix = fitz.Pixmap(str(path))
    if pix.alpha or pix.n > 3:
        pix = fitz.Pixmap(fitz.csRGB, pix, 0)
    scale = IMAGE_MAX_EDGE / max(pix.width, pix.height)
    if scale < 1:
        pix = fitz.Pixmap(pix, max(1, int(pix.width * scale)), max(1, int(pix.height * scale)), None)
    return base64.b64encode(pix.tobytes("png")).decode()


def look_at_figure(f: dict, paper_dir: Path, units: str, ask):
    """Links for a figure with no caption or citing text, judged from its image."""
    image = paper_dir / (f.get("file") or "")
    if not f.get("file") or not image.is_file():
        return None
    try:
        encoded = image_b64(image)
    except Exception:  # an unreadable image leaves the figure unlinked rather than stopping the run
        return None
    return ask(prompt("figure_image"), units, label=f"figure-image:{f['id']}", images=[encoded])


def link_figures(elements: list[dict], paper_dir: Path, units: str, allowed: tuple[set, set], ask) -> list[dict]:
    """One call per figure: from its caption and citing text, or from its image when it has neither."""
    asm_ok, layer_ok = allowed
    citing = {}
    for e in elements:
        for n in e.get("figure_refs") or []:
            citing.setdefault(n, []).append(e)
    links = []
    for f in (e for e in elements if e["kind"] == "figure"):
        cites = [c for c in citing.get(f.get("figure_number"), []) if c["kind"] != "caption"][:3]
        if not f.get("caption") and not cites:
            reply = look_at_figure(f, paper_dir, units, ask)
        else:
            body = f"{units}\n\nCAPTION: {f.get('caption') or '(none)'}\n\nCITING TEXT:\n" + "\n".join(line(c)[:1200] for c in cites)
            reply = ask(prompt("figure"), body, label=f"figure:{f['id']}")
        reply = reply if isinstance(reply, dict) else {}
        links.append({"element": f["id"], "assemblages": clean(reply.get("assemblages"), asm_ok),
                      "layers": clean(reply.get("layers"), layer_ok), "general": False})
    return links


def link_pieces(elements: list[dict], figure_links: list[dict]) -> list[dict]:
    """Pieces inherit their figure's links."""
    by_fig = {k["element"]: k for k in figure_links}
    return [{"element": p["id"], "assemblages": by_fig[p["parent"]]["assemblages"],
             "layers": by_fig[p["parent"]]["layers"], "general": False}
            for p in elements if p["kind"] == "piece" and p["parent"] in by_fig]


def link_text(elements: list[dict], units: str, allowed: tuple[set, set], ask) -> list[dict]:
    """Chunked tagging of paragraphs, headings, captions and table rows."""
    asm_ok, layer_ok = allowed
    blocks = [e for e in elements if e["kind"] in ("paragraph", "heading", "caption", "table_row")]
    chunks, cur, size = [], [], 0
    for b in blocks:
        if cur and size + len(b["text"]) > TEXT_CHUNK_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += len(b["text"])
    chunks += [cur] if cur else []
    tags = {}
    for i, chunk in enumerate(chunks, 1):
        reply = ask(prompt("text"), units + "\n\nBLOCKS:\n" + "\n".join(line(b) for b in chunk), label=f"text {i}/{len(chunks)}") or {}
        for t in reply.get("tags") or []:
            if isinstance(t, dict) and t.get("id"):
                tags[t["id"]] = t
    return [{"element": b["id"], "assemblages": clean(tags.get(b["id"], {}).get("assemblages"), asm_ok),
             "layers": clean(tags.get(b["id"], {}).get("layers"), layer_ok),
             "general": bool(tags.get(b["id"], {}).get("general"))} for b in blocks]


def given_inventory(inventory_dir: Path, paper: str) -> tuple[list[dict], list[dict]] | None:
    """Layers and assemblages from another paper_units run, e.g. gold, for testing the linking steps alone."""
    for f in inventory_dir.glob("*.paper_units.json"):
        units = json.loads(f.read_text(encoding="utf-8"))
        if units["paper"] == paper:
            return units["layers"], units["assemblages"]
    return None


def segment_paper(paper_dir: Path, ask, model: str, run_id: str, with_text: bool, inventory_dir: Path | None = None) -> dict:
    data = json.loads((paper_dir / "paper_elements.json").read_text(encoding="utf-8"))
    elements = data["elements"]
    given = given_inventory(inventory_dir, data["paper"]) if inventory_dir else None
    layers, assemblages = given or build_inventory(elements, ask)
    units = unit_list(layers, assemblages)
    allowed = ({a["id"] for a in assemblages}, {l["id"] for l in layers})
    figure_links = link_figures(elements, paper_dir, units, allowed, ask)
    links = figure_links + link_pieces(elements, figure_links)
    if with_text:
        links += link_text(elements, units, allowed, ask)
    return {"schema": "paper_units/1", "paper": data["paper"],
            "producer": {"tool": "site_form_segmenter/segment_paper_units.py", "model": model, "run_id": run_id},
            "layers": layers, "assemblages": assemblages, "links": links,
            "counts": {"n_layers": len(layers), "n_assemblages": len(assemblages)}}


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL,
                                       cwd=Path(__file__).parent).decode().strip()
    except Exception:
        return "nogit"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True, type=Path)
    ap.add_argument("--only", help="substring of one paper folder name")
    ap.add_argument("--model", default="qwen3.8:27b")
    ap.add_argument("--base-url", default="http://localhost:11434")
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--no-text", action="store_true", help="skip text-block links (faster)")
    ap.add_argument("--out-dir", type=Path, help="default: <input-dir>/000_segments/runs/<run>")
    ap.add_argument("--inventory-run", type=Path, help="take layers and assemblages from this paper_units folder")
    args = ap.parse_args()

    run_id = f"{datetime.now():%Y%m%d_%H%M}_{git_sha()}"
    out_dir = args.out_dir or args.input_dir / "000_segments" / "runs" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = dict(model=args.model, base_url=args.base_url, temperature=0.05, timeout=1800, num_ctx=args.num_ctx, think=False)

    def ask_kw(system, user, label, images=None):
        if images:
            return extract_json_vision(system_prompt=system, user_content=user, images=images, label=label, **opts)
        return extract_json(system_prompt=system, user_content=user, label=label, **opts)
    papers = sorted(d for d in args.input_dir.iterdir() if (d / "paper_elements.json").exists())
    timings = {}
    for d in papers:
        if args.only and args.only not in d.name:
            continue
        t0 = time.monotonic()
        units = segment_paper(d, ask_kw, args.model, run_id, not args.no_text, args.inventory_run)
        slug = args.model.replace(":", "_").replace(".", "_")
        (out_dir / f"{slug}__{d.name}.paper_units.json").write_text(json.dumps(units, indent=1, ensure_ascii=False), encoding="utf-8")
        timings[d.name] = round(time.monotonic() - t0, 1)
        print(f"{d.name[:45]:45} layers {units['counts']['n_layers']:3}  assemblages {units['counts']['n_assemblages']:3}  {timings[d.name]}s", flush=True)
    (out_dir / "run_metadata.json").write_text(json.dumps({"run_id": run_id, "model": args.model, "text_links": not args.no_text,
                                                            "inventory_run": str(args.inventory_run) if args.inventory_run else None,
                                                            "input_dir": str(args.input_dir), "seconds": timings}, indent=1), encoding="utf-8")
    print(f"-> {out_dir}")


if __name__ == "__main__":
    main()
