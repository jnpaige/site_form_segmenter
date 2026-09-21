#!/usr/bin/env python3
"""
dump_dossiers.py — write out exactly what code_assemblages.py shows its model.

Used to give a second coder — human or a different model — the identical
evidence, so the two codings can be compared without arguing about whether
they saw the same thing. Imports the dossier builder rather than
reimplementing it, so the two can never drift apart.

Usage:
    uv run python dump_dossiers.py --docs Coutouly,Chazan --out-dir path/
"""
import argparse
import json
from pathlib import Path

import code_assemblages as CA


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    only = [d.strip() for d in args.docs.split(",")] if args.docs else None

    index = []
    for doc, data in CA.load_documents(only):
        for cid, d in CA.build_dossier(doc, data).items():
            label = (d["cluster"].get("label_as_reported") or cid).strip() or cid
            items = d["items"]
            slug = "".join(ch if ch.isalnum() else "_" for ch in f"{doc.name[:24]}_{label}")[:70]
            body = [f"DOCUMENT: {doc.name}", f"ASSEMBLAGE: {label}",
                    f"CLUSTER ID: {cid}", f"EVIDENCE ITEMS: {len(items)}",
                    f"  paragraphs {sum(1 for i in items if i['kind']=='para')}"
                    f"  table rows {sum(1 for i in items if i['kind']=='table')}"
                    f"  figure captions {sum(1 for i in items if i['kind']=='figure_caption')}"
                    f"  artefacts {sum(1 for i in items if i['kind']=='artefact')}",
                    "", CA.render_dossier(items)]
            (out / f"{slug}.txt").write_text("\n".join(body), encoding="utf-8")
            index.append({"file": f"{slug}.txt", "document": doc.name,
                          "assemblage": label, "cluster_id": cid,
                          "n_items": len(items)})
            print(f"  {label[:38]:40s} {len(items):4d} items -> {slug}.txt")

    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"\n{len(index)} dossiers written to {out}")


if __name__ == "__main__":
    main()
