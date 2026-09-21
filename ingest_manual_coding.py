#!/usr/bin/env python3
"""
ingest_manual_coding.py — turn hand-written verdicts into the same outputs
code_assemblages.py produces, so a second coder's results can be compared
against the local model's row for row.

Input is a JSON file: {"coder": "...", "assemblages": [ {...}, ... ]} where each
assemblage carries document, assemblage, cluster_id and a verdicts list of
{code, present, evidence, reason, confidence}. Citations are validated against
the dossiers the coder was given, exactly as the automated path validates the
model's, so neither coder gets to cite something that was not in front of them.

Usage:
    uv run python ingest_manual_coding.py --verdicts opus.json \
        --dossier-dir path/to/dossiers --out path/to/opus_coded
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))
import codebook_parser as cb
import code_assemblages as CA


def dossier_items(path: Path) -> dict[str, dict]:
    """Recover id -> item from a dumped dossier, for citation checking."""
    items = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\[([^\]]+)\]\s+\(([^,]+),\s*([^)]*)\)\s*(.*)", line)
        if not m:
            continue
        ident, kind, where, text = m.groups()
        page = re.search(r"p(\d+)", where)
        fig = re.search(r"fig(\S+)", where)
        art = re.search(r"piece (\d+)", where)
        items[ident] = {
            "id": ident, "kind": kind.strip(),
            "page": int(page.group(1)) if page else None,
            "figure": fig.group(1) if fig else None,
            "artefact": int(art.group(1)) if art else None,
            "block_id": ident.split(":", 1)[1] if ident.startswith(("para:", "table:")) else None,
            "text": text.strip(),
        }
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdicts", required=True)
    ap.add_argument("--dossier-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    codes = cb.load(CA.PU_BOOK, CA.MODE_BOOK, CA.GROUPS)
    by_key = {c.key: c for c in codes}
    all_keys = [c.key for c in codes]

    data = json.loads(Path(args.verdicts).read_text(encoding="utf-8"))
    ddir = Path(args.dossier_dir)
    index = {e["assemblage"]: e for e in
             json.loads((ddir / "index.json").read_text(encoding="utf-8"))}

    rows, evidence_rows = [], []
    for a in data["assemblages"]:
        entry = index.get(a["assemblage"], {})
        items = dossier_items(ddir / entry["file"]) if entry.get("file") else {}
        row = {"document": a.get("document", entry.get("document", "")),
               "cluster_id": a.get("cluster_id", entry.get("cluster_id", "")),
               "assemblage": a["assemblage"],
               "n_evidence": len(items),
               "n_paragraphs": sum(1 for i in items.values() if i["kind"] == "para"),
               "n_table_rows": sum(1 for i in items.values() if i["kind"] == "table"),
               "n_artefacts": sum(1 for i in items.values() if i["kind"] == "artefact")}
        row.update({k: None for k in all_keys})

        for v in a["verdicts"]:
            key = v["code"]
            if key not in row:
                print(f"  [warn] unknown code {key}")
                continue
            cited = [e for e in (v.get("evidence") or []) if e in items]
            missing = [e for e in (v.get("evidence") or []) if e not in items]
            if missing:
                print(f"  [warn] {a['assemblage']} {key}: {len(missing)} citation(s) "
                      f"not in dossier: {missing[:3]}")
            present = v.get("present")
            if present == 1 and not cited:
                print(f"  [warn] {a['assemblage']} {key}: present with no valid "
                      f"citation — demoted to null")
                present = None
            row[key] = present
            if present == 1:
                for e in cited:
                    it = items[e]
                    evidence_rows.append({
                        "document": row["document"], "assemblage": a["assemblage"],
                        "code": key, "code_name": by_key[key].name,
                        "evidence_id": e, "evidence_kind": it["kind"],
                        "page": it["page"],
                        "paragraph": it["block_id"] if it["kind"] == "para" else None,
                        "table_row": it["block_id"] if it["kind"] == "table" else None,
                        "figure": it["figure"], "artefact": it["artefact"],
                        "confidence": v.get("confidence"),
                        "reason": (v.get("reason") or "")[:500],
                        "quoted_evidence": it["text"][:400],
                    })
        rows.append(row)
        n = sum(1 for k in all_keys if row[k] == 1)
        u = sum(1 for k in all_keys if row[k] is None)
        print(f"  {a['assemblage'][:36]:38s} {n:2d} present, {u:2d} not determinable")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    CA.write_outputs(out, rows, evidence_rows, all_keys, codes)
    print(f"\ncoder: {data.get('coder')}  {len(rows)} assemblages, "
          f"{len(evidence_rows)} cited evidence rows")


if __name__ == "__main__":
    main()
