#!/usr/bin/env python3
"""
build_segments_from_anchors.py — turn segment_glo_page_anchors.py's per-page
EVIDENCE MAP into the standard segments.json every downstream tool consumes.

WHY THIS IS A SEPARATE SCRIPT
-----------------------------
segment_glo_page_anchors.py deliberately stops at evidence: for each 0-indexed
PDF page it records what the title card said, what volume page number was in
the margin, which index row that number falls in, and whether those agree. It
draws no conclusion. This script draws the conclusion, and it is separate on
purpose — the regex segmenter's "zero unmapped pages" looked reassuring but
really meant the fill algorithm always produced an answer, right or wrong.
Keeping observation and conclusion in two files means the conclusion can be
re-derived under a different policy without re-reading a single page, and a
reviewer can always see which one they are looking at.

THE ASSIGNMENT POLICY, AND WHY THE CARD WINS
--------------------------------------------
Per page, in order:

  corroborated  card and index independently agree            -> assign, best confidence
  card_only     only the card named a township/range          -> assign from card
  index_only    only the index did                            -> assign from index
  conflict      they disagree                                 -> assign from the CARD
  none          neither                                       -> UNASSIGNED

The card wins a conflict because it is machine-printed text photographed onto
the page, while the index is handwritten and reaches the page only through a
second inference (the margin page number). Measured on 507_00056, 16 of 47
conflicts were already explained by a margin number the outlier check had
flagged as out of sequence — i.e. the index side was wrong, not the card. Every
conflict is still listed in `_conflicts` with both readings, so preferring the
card never hides that a disagreement existed.

A page with no evidence is left UNASSIGNED and listed in `unmapped_pages`. It
is deliberately NOT filled from its neighbours: that is precisely the
interpolation this pipeline moved away from. Downstream, an unassigned page
simply is not sent to extraction under any township.

OUTPUT — the shared segments.json contract
------------------------------------------
    {"segments": [{"label": "T1N_R3E", "township": "1N", "range": "3E",
                   "district": "SWD", "pages": [...], "page_span": [lo, hi],
                   "n_pages": N, "t1n_r3e_pages": [...],
                   "confidence_counts": {...}, "sections": []}, ...]}

`sections` is always empty: the anchors pass reads a page's top strip, which
carries the township/range card and page number but never a section boundary
("North Boundary of Section No. 23" is body text). site_vocab_extractor's
_segment_extraction_units() falls back to one unit per township/range block
when sections is empty, which is the intended granularity here.

The `t{n}{ns}_r{n}{ew}_pages` convenience key matches both existing GLO
segmenters, so page_scope selection downstream works unchanged.

Usage:
    uv run python build_segments_from_anchors.py --anchors-run runs/<anchors run>
    uv run python build_segments_from_anchors.py --anchors-run runs/<run> --report "507_00615__..."
"""

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))
from inventory import write_inventory_csv


def _tr_key(township: str, rng: str) -> str:
    return f"t{township}_r{rng}".lower()


def assign_pages(anchors: dict, prefer: str = "card") -> tuple[list[dict], list[int], list[dict]]:
    """Apply the assignment policy to an anchors evidence map.

    Returns (segments, unmapped_pages, conflicts_recorded).
    """
    by_block: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"pages": [], "districts": Counter(), "confidence": Counter()})
    unmapped: list[int] = []
    conflicts: list[dict] = []

    for p in anchors.get("pages", []):
        page = p["page"]
        card_tr = (p.get("township"), p.get("range"))
        match = p.get("index_match") or {}
        index_tr = (match.get("township"), match.get("range"))
        agreement = p.get("agreement")

        if agreement == "corroborated":
            chosen, conf = card_tr, "corroborated"
        elif agreement == "card_only":
            chosen, conf = card_tr, "card_only"
        elif agreement == "index_only":
            chosen, conf = index_tr, "index_only"
        elif agreement == "conflict":
            chosen = card_tr if prefer == "card" else index_tr
            conf = f"conflict_{prefer}_preferred"
            conflicts.append({
                "page": page,
                "card_says": f"T{card_tr[0]}_R{card_tr[1]}",
                "index_says": f"T{index_tr[0]}_R{index_tr[1]}",
                "assigned_from": prefer,
                "margin_page_number": p.get("margin_page_number"),
                "margin_suspect": p.get("margin_suspect"),
            })
        else:
            unmapped.append(page)
            continue

        if not all(chosen):
            unmapped.append(page)
            continue

        blk = by_block[chosen]
        blk["pages"].append(page)
        blk["confidence"][conf] += 1
        if p.get("district"):
            blk["districts"][p["district"]] += 1

    segments = []
    for (township, rng), blk in sorted(by_block.items()):
        pages = sorted(blk["pages"])
        label = f"T{township}_R{rng}"
        seg = {
            "label": label,
            "township": township,
            "range": rng,
            "district": (blk["districts"].most_common(1)[0][0]
                         if blk["districts"] else None),
            "pages": pages,
            "page_span": [pages[0], pages[-1]],
            "n_pages": len(pages),
            "confidence_counts": dict(blk["confidence"]),
            "sections": [],
        }
        seg[f"{_tr_key(township, rng)}_pages"] = pages
        segments.append(seg)

    return segments, sorted(unmapped), conflicts


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "nogit"


def _make_run_dir(base: Path) -> Path:
    stamp = f"{datetime.now().strftime('%Y%m%d_%H%M')}_{_git_sha()}"
    base.mkdir(parents=True, exist_ok=True)
    cand, n = base / stamp, 0
    while True:
        try:
            cand.mkdir(parents=True, exist_ok=False)
            return cand
        except FileExistsError:
            n += 1
            cand = base / f"{stamp}-{chr(ord('a') + n - 1)}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchors-run", required=True, metavar="PATH",
                    help="A segment_glo_page_anchors.py run folder")
    ap.add_argument("--output-dir", default="runs", metavar="PATH")
    ap.add_argument("--report", default=None, metavar="NAME",
                    help="Only this document stem")
    ap.add_argument("--prefer", default="card", choices=["card", "index"],
                    help="Which source wins a conflict (default: card)")
    args = ap.parse_args()

    src = Path(args.anchors_run)
    if not src.is_dir():
        sys.exit(f"Not a directory: {src}")
    files = sorted(src.glob("*.anchors.json"))
    if args.report:
        files = [f for f in files if args.report in f.name]
    if not files:
        sys.exit(f"No *.anchors.json found in {src}"
                 + (f" matching {args.report}" if args.report else ""))

    run_dir = _make_run_dir(Path(args.output_dir))
    print(f"\nRun dir     : {run_dir}")
    print(f"Anchors run : {src}")
    print(f"Conflicts   : assigned from the {args.prefer}")
    print(f"Documents   : {len(files)}\n")

    inv, totals = [], Counter()
    for f in files:
        anchors = json.loads(f.read_text(encoding="utf-8"))
        doc = anchors["document"]
        segments, unmapped, conflicts = assign_pages(anchors, prefer=args.prefer)

        n_pages = anchors.get("n_pdf_pages", 0)
        assigned = sum(s["n_pages"] for s in segments)
        conf_all = Counter()
        for s in segments:
            conf_all.update(s["confidence_counts"])
        totals.update(conf_all)

        out = {
            "document": doc,
            "method": "vision_page_anchors",
            "built_from": {"anchors_run": str(src), "anchors_file": f.name,
                           "conflict_policy": f"prefer_{args.prefer}"},
            "built_at": datetime.now(timezone.utc).isoformat(),
            "n_pages": n_pages,
            "n_pages_assigned": assigned,
            "index_family": (anchors.get("index") or {}).get("index_family"),
            "n_township_range_blocks": len(segments),
            "unmapped_pages": unmapped,
            "segments": segments,
            "_conflicts": conflicts,
        }
        # Model slug prefix matches the rest of the repo, and is what
        # site_vocab_extractor's `*__<item>.segments.json` glob expects.
        slug = (anchors.get("model") or "model").replace(":", "_").replace(".", "_")
        out_path = run_dir / f"{slug}__{doc}.segments.json"
        out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

        pct = 100 * assigned / n_pages if n_pages else 0
        print(f"  {doc[:48]:48s} {len(segments):3d} blocks  "
              f"{assigned}/{n_pages} pages assigned ({pct:.0f}%)  "
              f"{len(unmapped)} unmapped  {len(conflicts)} conflicts")

        inv.append({
            "run_id": run_dir.name, "tool": "build_segments_from_anchors.py",
            "model": anchors.get("model", "not recorded"),
            "file_name": out_path.name, "file_path": str(out_path),
            "source_input": str(f), "prompt_file": "n/a (no LLM call)",
            "prompt_snapshot_key": "n/a",
            "chunk_strategy": f"per-page assignment, conflicts prefer_{args.prefer}",
            "produced_at": datetime.now(timezone.utc).isoformat(),
            "output_file_path": str(out_path),
        })

    (run_dir / "run_metadata.json").write_text(json.dumps({
        "run_id": run_dir.name,
        "tool": "build_segments_from_anchors.py",
        "method": "vision_page_anchors",
        "note": "No LLM call — a policy applied to an existing evidence map.",
        "anchors_run": str(src),
        "conflict_policy": f"prefer_{args.prefer}",
        "n_documents": len(files),
        "confidence_totals": dict(totals),
        "built_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")
    write_inventory_csv(run_dir, inv)

    print(f"\nConfidence totals: {dict(totals)}")
    print(f"Run directory -> {run_dir}")


if __name__ == "__main__":
    main()
