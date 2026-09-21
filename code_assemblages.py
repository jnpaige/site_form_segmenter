#!/usr/bin/env python3
"""
code_assemblages.py — decide procedural unit and technological mode presence
for each assemblage, from the prose, the tables and the figures together.

THE DECISION UNIT IS THE ASSEMBLAGE, NOT THE BLOCK. Several exclusion criteria
are about frequency rather than existence — faceting is excluded on "single or
very sparse instances of facetted or dihedral platforms" — so no per-paragraph
call can decide them. Everything tagged to one assemblage is assembled into a
dossier and judged together.

NO RETRIEVAL FILTER. An earlier design shortlisted candidate codes by matching
the codebook's exemplar phrases against each block. That was wrong twice over.
It would silently lose recall, because a paper can describe faceting without
using any word in the exemplar list; and it was unnecessary, because a median
assemblage dossier is about 7,000 tokens and the largest in this corpus is
23,000 — the whole thing fits in context, so there is nothing to filter for.
Half of all tagged items are table rows, and they carry the counts the
frequency criteria depend on, so they go in whole.

CODES ARE JUDGED IN THEMATIC BATCHES. The codebooks' "Close but no" fields are
cross-references — Mode D1's points at D2 and D4, Mode C's at F1 and F2 — so a
code is only decidable alongside the codes it is contrasted with.

WHAT THE MODEL IS NOT TRUSTED WITH. Every verdict must cite evidence by id.
Citations are checked against the dossier and an uncited or invented one is
dropped in code, not argued with. Figure-derived claims pass through the
codebooks' own view gate: a faceting claim from a piece showing no platform
view is refused however confident the model was.

OUTPUT
    <out>.csv    one row per assemblage, one column per code — the shape of the
                 hand-coded reference dataset, so the two can sit side by side
    <out>.xlsx   the same sheet, plus one row per cited piece of evidence with
                 page, paragraph, table, figure and artefact numbers

Usage:
    uv run python code_assemblages.py --out coded_sample
    uv run python code_assemblages.py --docs Coutouly,Stout --batches PU.platform_prep
"""
import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))
import codebook_parser as cb
from ollama_client import extract_json

ROOT = Path(r"C:\Users\jpaige\Box\4. Research\LLM_segmenting_procedural_units")
CORPORA = ["pdfs_ocr_figures_pilot", "pdfs_ocr_figures_holdout"]
PU_BOOK = Path(r"C:\Users\jpaige\Desktop\research_repositories\Procedural.unit.codebook\paper\Procedural unit codebook.txt")
MODE_BOOK = Path(r"C:\Users\jpaige\Desktop\research_repositories\technological.mode.codebook\paper\Lithic_Modes_AI_Codebook_v_0_1.Rmd")

GROUPS = {
    "PU.material": ["Raw material treatment", "Ochre use", "Asphalt use"],
    "PU.platform_prep": ["Faceting of core platform", "Abrasion/grinding",
                         "Overhang removal/microchipping of area below platform",
                         "Core tablet removals"],
    "PU.face_shaping": ["Face shaping through radial removals", "Lateral trimming",
                        "Distal trimming", "Back shaping", "Cresting"],
    "PU.removal_types": ["*Débordant*", "Overshot flakes", "Kombewa", "Invasive flaking"],
    "PU.percussion": ["Percussion by striking with hard hammer", "Soft Hammer",
                      "Indirect percussion", "Flaking with application of pressure",
                      "Pecking/hammer dressing", "Core supported by hand",
                      "Use of anvil", "Core rotation"],
    "PU.retouch": ["Retouch", "Abrupt retouch.", "Notching", "Burination",
                   "Tranchet removal.", "Pressure retouch", "Invasive retouch", "Tanging"],
    "MODE.percussion_cores": ["Mode A", "Mode B", "Mode C"],
    "MODE.retouched_flakes": ["Mode D1", "Mode D2", "Mode D3", "Mode D4"],
    "MODE.core_tools": ["Mode E1", "Mode E2", "Mode E3", "Mode E4"],
    "MODE.hierarchical_cores": ["Mode F1", "Mode F2", "Mode G1", "Mode G2"],
    "MODE.ground": ["Mode H", "Mode I"],
}

# A figure-derived feature may only support a code when the piece actually
# shows the view that feature lives on. Straight from the codebooks: faceting
# "requires figures of the platforms themselves."
VIEW_REQUIRED = {
    "faceted_platform": {"platform"},
    "dihedral_platform": {"platform"},
    "plain_platform": {"platform"},
    "cortical_platform": {"platform"},
    "platform_abrasion": {"platform", "profile"},
    "overhang_removal": {"platform", "profile"},
    "ventral_on_ventral": {"ventral"},
    "core_tablet_profile": {"profile", "section"},
    "burin_spall": {"platform", "profile", "section"},
}

SYSTEM = """You are coding one archaeological assemblage against a codebook.

You will be given: the codebook entries for a group of related codes, and the
complete evidence for one assemblage from a published paper — prose paragraphs,
table rows, figure captions, and structured observations of individual
artefacts drawn from that paper's figures. Every evidence item has an id.

For EACH code, decide whether the procedural unit or technological mode is
PRESENT in this assemblage.

Return JSON: {"verdicts": [ {...}, ... ]} where each verdict has:
  "code":      the code key exactly as given
  "present":   1 if present, 0 if positively absent, null if not determinable
  "evidence":  list of evidence ids that support your decision. REQUIRED when
               present is 1. Cite only ids that appear in the dossier.
  "reason":    one or two sentences, quoting the wording you relied on
  "confidence": "high", "medium" or "low"

Rules that matter:
- Apply the Inclusion criteria, Exclusion criteria and especially the
  "Close but no" field. That field exists to stop near-misses being coded.
- Several codes are excluded when the evidence is a single or very sparse
  instance. Table counts are there so you can judge frequency; use them.
- A code may be established by the text OR by the figures. Both count.
- If the evidence does not settle it, return null. Do not guess. A null is a
  correct answer when the paper is silent.
- Never cite an id that is not in the dossier."""


def load_documents(only: list[str] | None):
    for corpus in CORPORA:
        base = ROOT / corpus
        if not base.is_dir():
            continue
        for d in sorted(x for x in base.iterdir() if x.is_dir()):
            if only and not any(o.lower() in d.name.lower() for o in only):
                continue
            tags = next(d.glob("*.cluster_tags.json"), None)
            if tags:
                yield d, json.loads(tags.read_text(encoding="utf-8"))


def figure_evidence(doc: Path) -> tuple[dict, list[dict]]:
    """Figure captions and per-artefact observations, keyed by page."""
    caps, obs = {}, []
    fj = doc / "figures" / "figures.json"
    if fj.exists():
        data = json.loads(fj.read_text(encoding="utf-8"))
        for f in (data if isinstance(data, list) else data.get("figures", [])):
            if isinstance(f, dict) and (f.get("caption") or "").strip():
                caps[Path(str(f.get("file"))).name] = f
    oj = doc / "figures" / "piece_observations.json"
    if oj.exists():
        obs = json.loads(oj.read_text(encoding="utf-8"))
    return caps, obs


def gated_features(o: dict) -> list[str]:
    """Drop feature claims the piece's own views cannot support."""
    views = set(o.get("views") or [])
    return [f for f in (o.get("features") or [])
            if not VIEW_REQUIRED.get(f) or (VIEW_REQUIRED[f] & views)]


def build_dossier(doc: Path, data: dict) -> dict[str, dict]:
    """One dossier per cluster: every evidence item with a citable id."""
    blocks = {b["block_id"]: b for b in data.get("blocks", [])}
    clusters = {c.get("cluster_id") or f"c{i+1}": c
                for i, c in enumerate(data.get("clusters", []))}
    caps, obs = figure_evidence(doc)
    pages_by_cluster = defaultdict(set)
    items = defaultdict(list)

    for tag in data.get("block_tags", []):
        b = blocks.get(tag.get("block_id"))
        if not b:
            continue
        text = re.sub(r"\s+", " ", (b.get("text") or "")).strip()
        if not text:
            continue
        kind = "table" if b.get("type") == "table_row" else "para"
        for cid in (tag.get("cluster_ids") or []):
            items[cid].append({
                "id": f"{kind}:{b['block_id']}",
                "kind": kind,
                "page": b.get("page"),
                "block_id": b.get("block_id"),
                "text": text[:1200],
            })
            pages_by_cluster[cid].add(b.get("page"))

    # Figures attach to a cluster through the page they sit on.
    for cid, pages in pages_by_cluster.items():
        for name, f in caps.items():
            if f.get("page") in pages:
                items[cid].append({
                    "id": f"figcap:{name}",
                    "kind": "figure_caption",
                    "page": f.get("page"),
                    "figure": f.get("printed_figure_number"),
                    "text": re.sub(r"\s+", " ", f["caption"]).strip()[:600],
                })
        for o in obs:
            if o.get("page") not in pages or o.get("uncertain"):
                continue
            if o.get("object") in ("not_an_artefact", "diagram", None):
                continue
            feats = gated_features(o)
            size = f", longest {o['longest_cm']} cm" if o.get("longest_cm") else ""
            items[cid].append({
                "id": f"piece:{o['piece_id']}",
                "kind": "artefact",
                "page": o.get("page"),
                "figure": o.get("printed_figure_number"),
                "artefact": o.get("piece_index"),
                "text": (f"{o['object']}{size}; views shown: "
                         f"{', '.join(o.get('views') or ['unknown'])}; "
                         f"features visible: {', '.join(feats) or 'none confirmed'}"),
            })

    return {cid: {"cluster": clusters.get(cid, {}), "items": v}
            for cid, v in items.items()}


def render_dossier(items: list[dict]) -> str:
    lines = []
    for it in items:
        where = f"p{it.get('page')}"
        if it.get("figure") is not None:
            where += f" fig{it['figure']}"
        if it.get("artefact"):
            where += f" piece {it['artefact']}"
        lines.append(f"[{it['id']}] ({it['kind']}, {where}) {it['text']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="coded_assemblages")
    ap.add_argument("--docs", help="comma-separated substrings of document names")
    ap.add_argument("--batches", help="comma-separated batch names to run")
    ap.add_argument("--model", default="qwen2.5:32b")
    ap.add_argument("--base-url", default="http://localhost:11434")
    ap.add_argument("--num-ctx", type=int, default=32768)
    args = ap.parse_args()

    codes = cb.load(PU_BOOK, MODE_BOOK, GROUPS)
    grouped = cb.batches(codes)
    if args.batches:
        wanted = {b.strip() for b in args.batches.split(",")}
        grouped = {k: v for k, v in grouped.items() if k in wanted}
    only = [d.strip() for d in args.docs.split(",")] if args.docs else None

    all_keys = [c.key for c in codes]
    rows, evidence_rows = [], []
    t0 = time.time()
    n_calls = 0

    for doc, data in load_documents(only):
        dossiers = build_dossier(doc, data)
        for cid, d in dossiers.items():
            label = (d["cluster"].get("label_as_reported") or cid).strip() or cid
            items = d["items"]
            valid_ids = {it["id"] for it in items}
            by_id = {it["id"]: it for it in items}
            rendered = render_dossier(items)
            row = {"document": doc.name, "cluster_id": cid, "assemblage": label,
                   "n_evidence": len(items),
                   "n_paragraphs": sum(1 for i in items if i["kind"] == "para"),
                   "n_table_rows": sum(1 for i in items if i["kind"] == "table"),
                   "n_artefacts": sum(1 for i in items if i["kind"] == "artefact")}
            row.update({k: None for k in all_keys})

            for gname, gcodes in grouped.items():
                entries = "\n\n".join(c.render() for c in gcodes)
                user = (f"CODEBOOK ENTRIES\n{entries}\n\n"
                        f"ASSEMBLAGE: {label}  (from {doc.name})\n"
                        f"EVIDENCE DOSSIER ({len(items)} items)\n{rendered}\n\n"
                        f"Decide these codes: {', '.join(c.key for c in gcodes)}")
                res = extract_json(
                    system_prompt=SYSTEM, user_content=user, model=args.model,
                    base_url=args.base_url, temperature=0.1, timeout=900,
                    label=f"code:{label[:18]}:{gname}", num_ctx=args.num_ctx)
                n_calls += 1
                if not isinstance(res, dict):
                    continue
                for v in (res.get("verdicts") or []):
                    key = v.get("code")
                    if key not in row:
                        continue
                    cited = [e for e in (v.get("evidence") or []) if e in valid_ids]
                    invented = [e for e in (v.get("evidence") or []) if e not in valid_ids]
                    present = v.get("present")
                    # A presence claim with no checkable citation is not a
                    # finding. Demote it rather than record it.
                    if present == 1 and not cited:
                        present = None
                    row[key] = present
                    if present == 1:
                        for e in cited:
                            it = by_id[e]
                            evidence_rows.append({
                                "document": doc.name, "assemblage": label,
                                "code": key,
                                "code_name": next(c.name for c in gcodes if c.key == key),
                                "evidence_id": e, "evidence_kind": it["kind"],
                                "page": it.get("page"),
                                "paragraph": it.get("block_id") if it["kind"] == "para" else None,
                                "table_row": it.get("block_id") if it["kind"] == "table" else None,
                                "figure": it.get("figure"),
                                "artefact": it.get("artefact"),
                                "confidence": v.get("confidence"),
                                "reason": (v.get("reason") or "")[:500],
                                "quoted_evidence": it["text"][:400],
                            })
                    if invented:
                        print(f"    [warn] {label[:20]} {key}: dropped "
                              f"{len(invented)} invented citation(s)", flush=True)
            rows.append(row)
            done = sum(1 for k in all_keys if row[k] == 1)
            print(f"  {label[:40]:42s} {len(items):4d} items -> {done:2d} codes present",
                  flush=True)

    write_outputs(Path(args.out), rows, evidence_rows, all_keys, codes)
    print(f"\n{len(rows)} assemblages, {n_calls} model calls, "
          f"{len(evidence_rows)} cited evidence rows, {time.time()-t0:.0f}s")


_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(v):
    """Excel rejects most control characters outright, and OCR'd body text
    carries them. A run that has already spent its model calls must not die at
    the write step, so strip them here rather than discovering them per corpus."""
    return _ILLEGAL.sub("", v) if isinstance(v, str) else v


def write_outputs(out: Path, rows, evidence_rows, all_keys, codes):
    import csv
    meta = ["document", "cluster_id", "assemblage", "n_evidence",
            "n_paragraphs", "n_table_rows", "n_artefacts"]
    csv_path = out.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=meta + all_keys)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {csv_path}")

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment
    except ImportError:
        print("openpyxl not available — CSV only")
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "assemblages"
    ws.append(meta + all_keys)
    for r in rows:
        ws.append([_clean(r.get(k)) for k in meta + all_keys])
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(textRotation=60 if c.column > len(meta) else 0)
    ws.freeze_panes = "D2"

    ev = wb.create_sheet("evidence")
    cols = ["document", "assemblage", "code", "code_name", "page", "paragraph",
            "table_row", "figure", "artefact", "evidence_kind", "confidence",
            "reason", "quoted_evidence", "evidence_id"]
    ev.append(cols)
    for r in evidence_rows:
        ev.append([_clean(r.get(c)) for c in cols])
    for c in ev[1]:
        c.font = Font(bold=True)
    ev.freeze_panes = "A2"
    for col, width in zip("ABCDEFGHIJKLMN",
                          [26, 24, 28, 30, 7, 12, 12, 8, 9, 14, 11, 60, 70, 22]):
        ev.column_dimensions[col].width = width

    key = wb.create_sheet("codebook")
    key.append(["code", "name", "book", "batch", "short description"])
    for c in codes:
        key.append([c.key, c.name, c.book, c.group,
                    _clean((c.fields.get("Short Description") or "")[:300])])
    for c in key[1]:
        c.font = Font(bold=True)

    xlsx = out.with_suffix(".xlsx")
    wb.save(xlsx)
    print(f"wrote {xlsx}")


if __name__ == "__main__":
    main()
