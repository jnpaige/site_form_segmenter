#!/usr/bin/env python3
"""
compare_to_gold.py — score a coding run against the hand-coded reference set.

The reference is the full-chain rows of the procedural unit dataset (Single
Chain = Yes), one row per assemblage with a binary column per procedural unit.
Its column names are the project's own abbreviations, so the mapping to the
codebook keys is written out explicitly below rather than guessed: the two
vocabularies were built years apart and a positional match would break silently
the first time either list changed.

Scoring treats "not determinable" (null) separately from 0. A null is not a
wrong answer — it is a refusal — and lumping it in with absent would flatter or
punish the coder depending on which way the gold happened to fall.

Usage:
    uv run python compare_to_gold.py --coded path/to/opus_coded.csv --label opus
    uv run python compare_to_gold.py --coded a.csv --label opus --coded b.csv --label qwen
"""
import argparse
import csv
from pathlib import Path

GOLD = Path(r"C:\Users\jpaige\Box\4. Research\2.0 Evolution of stone tool technologies projects\Spacetime paper\space_time_model_2026\Procedural units.csv")

# gold column -> codebook key. PU.rt.bif has no codebook entry in the parsed
# procedural unit codebook and is reported as unmapped rather than dropped.
MAP = {
    "PU.RM": "PU.raw_material_treatment",
    "PU.facetting": "PU.faceting_of_core_platform",
    "PU.faceshaping rad.": "PU.face_shaping_through_radial_removals",
    "PU.lat trimming": "PU.lateral_trimming",
    "PU.dist trimming": "PU.distal_trimming",
    "PU.back shaping": "PU.back_shaping",
    "PU.cresting": "PU.cresting",
    "PU.debord": "PU.d_bordant",
    "PU.overshot": "PU.overshot_flakes",
    "PU.kombewa": "PU.kombewa",
    "PU.tablet": "PU.core_tablet_removals",
    "PU.abrasion": "PU.abrasion_grinding",
    "PU.overhang": "PU.overhang_removal_microchipping_of_area_below",
    "PU.hard hammer percussion": "PU.percussion_by_striking_with_hard_hammer",
    "PU.use of hand to support percussed object": "PU.core_supported_by_hand",
    "PU.use of anvil to support percussed object": "PU.use_of_anvil",
    "PU.core rotation": "PU.core_rotation",
    "PU.Soft": "PU.soft_hammer",
    "PU.indirect": "PU.indirect_percussion",
    "PU.Flaking through pressure": "PU.flaking_with_application_of_pressure",
    "PU.pecking": "PU.pecking_hammer_dressing",
    "PU.invasive": "PU.invasive_flaking",
    "PU.ochre": "PU.ochre_use",
    "PU.asphalt": "PU.asphalt_use",
    "PU.retouch": "PU.retouch",
    "PU.backing": "PU.abrupt_retouch",
    "PU.notching": "PU.notching",
    "PU.burin": "PU.burination",
    "PU.tang": "PU.tanging",
    "PU.tranchet": "PU.tranchet_removal",
    "PU.rt.invasive": "PU.invasive_retouch",
    "PU.rt. pressure flake": "PU.pressure_retouch",
}

# coded assemblage label -> gold Sitename (spelling differs in the reference)
SITES = {
    "Amakomanak": "Amakomanak",
    "La Ferrassie Level J": "La Farressie",
    "Garba IVD": "Garba IVD",
    "EG-12": "EG12",
    "EG-10": "EG10",
}


def norm(v):
    v = (v or "").strip().lower()
    if v in ("1", "y", "yes", "true", "present"):
        return 1
    if v in ("0", "n", "no", "false", "absent"):
        return 0
    return None


def load_gold(single_chain: bool = False):
    """Gold judgements per site.

    The reference codes one row per chaine operatoire, and a site may have
    several. The pipeline codes the whole assemblage, so by default the rows
    for a site are unioned: a procedural unit counts as present in the
    assemblage if it is present in any of its chains. Comparing an
    assemblage-level coding against a single chain's row penalises it for
    evidence belonging to the site's other chains, which is a unit mismatch
    rather than a coding error. --single-chain restores the per-chain view.
    """
    rows = list(csv.DictReader(GOLD.open(encoding="utf-8-sig")))
    out: dict[str, dict] = {}
    chains: dict[str, int] = {}
    for r in rows:
        site = (r.get("Sitename") or "").strip()
        if not site:
            continue
        if single_chain:
            if (r.get("Single Chain") or "").strip().lower() != "yes":
                continue
            out[site] = r
            chains[site] = 1
            continue
        chains[site] = chains.get(site, 0) + 1
        cur = out.setdefault(site, {})
        for gcol in MAP:
            v = norm(r.get(gcol))
            prev = cur.get(gcol)
            # present anywhere wins; otherwise absent if any chain says absent
            if v == 1 or prev == 1:
                cur[gcol] = "1"
            elif v == 0 or prev == 0:
                cur[gcol] = "0"
            else:
                cur[gcol] = ""
    return out, chains


def load_reasons(coded_csv: Path) -> dict:
    """Pull each verdict's stated reason out of the companion workbook, so the
    comparison shows WHY a coder said what it said next to the gold call."""
    xlsx = coded_csv.with_suffix(".xlsx")
    out = {}
    if not xlsx.exists():
        return out
    try:
        from openpyxl import load_workbook
    except ImportError:
        return out
    wb = load_workbook(xlsx, read_only=True)
    if "evidence" not in wb.sheetnames:
        return out
    ws = wb["evidence"]
    cols = [c.value for c in next(ws.iter_rows(max_row=1))]
    for row in ws.iter_rows(min_row=2, values_only=True):
        d = dict(zip(cols, row))
        key = (d.get("assemblage"), d.get("code"))
        if key not in out and d.get("reason"):
            out[key] = d["reason"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coded", action="append", required=True)
    ap.add_argument("--label", action="append", required=True)
    ap.add_argument("--out", help="write a cell-by-cell comparison workbook here")
    ap.add_argument("--single-chain", action="store_true",
                    help="score against one chaine operatoire per site instead "
                         "of the union of the site's chains")
    args = ap.parse_args()
    cells = []

    gold, chains = load_gold(args.single_chain)
    scope = ("one chain per site" if args.single_chain
             else "union of every chain coded at the site")
    print(f"gold: {len(gold)} sites, scored as {scope}; {len(MAP)} mapped codes "
          f"(PU.rt.bif has no codebook entry and is excluded)\n")

    for path, label in zip(args.coded, args.label):
        rows = list(csv.DictReader(Path(path).open(encoding="utf-8-sig")))
        reasons = load_reasons(Path(path))
        print(f"=== {label} " + "=" * (58 - len(label)))
        tot = dict(tp=0, tn=0, fp=0, fn=0, null_gold1=0, null_gold0=0, nogold=0)
        per_code = {}
        for r in rows:
            site = SITES.get(r["assemblage"], r["assemblage"])
            g = gold.get(site)
            if not g:
                print(f"  {r['assemblage'][:34]:36s} no gold row")
                continue
            nch = chains.get(site, 1)
            s = dict(tp=0, tn=0, fp=0, fn=0, null_gold1=0, null_gold0=0)
            disagree = []
            for gcol, key in MAP.items():
                gv, cv = norm(g.get(gcol)), norm(r.get(key))
                if gv is None:
                    tot["nogold"] += 1
                    continue
                if cv is None:
                    cells.append({"coder": label, "assemblage": r["assemblage"],
                                  "code": key, "gold": gv, "coded": "not determinable",
                                  "agree": "refused",
                                  "reason": reasons.get((r["assemblage"], key), "")})
                    k = "null_gold1" if gv == 1 else "null_gold0"
                    s[k] += 1
                    per_code.setdefault(key, []).append("null")
                    continue
                cells.append({"coder": label, "assemblage": r["assemblage"],
                              "code": key, "gold": gv, "coded": cv,
                              "agree": "yes" if cv == gv else "no",
                              "reason": reasons.get((r["assemblage"], key), "")})
                if cv == 1 and gv == 1:
                    s["tp"] += 1; per_code.setdefault(key, []).append("tp")
                elif cv == 0 and gv == 0:
                    s["tn"] += 1; per_code.setdefault(key, []).append("tn")
                elif cv == 1 and gv == 0:
                    s["fp"] += 1; per_code.setdefault(key, []).append("fp")
                    disagree.append(f"+{key.split('.',1)[1]}")
                else:
                    s["fn"] += 1; per_code.setdefault(key, []).append("fn")
                    disagree.append(f"-{key.split('.',1)[1]}")
            for k in s:
                tot[k] += s[k]
            n = s["tp"] + s["tn"] + s["fp"] + s["fn"]
            acc = 100 * (s["tp"] + s["tn"]) // n if n else 0
            print(f"  {r['assemblage'][:26]:28s} {nch} chain(s)  {acc:3d}% of {n:2d} decided  "
                  f"tp{s['tp']:2d} tn{s['tn']:2d} fp{s['fp']:2d} fn{s['fn']:2d}  "
                  f"null{s['null_gold1']+s['null_gold0']:2d}")
            if disagree:
                print(f"      disagreements: {' '.join(disagree)}")

        n = tot["tp"] + tot["tn"] + tot["fp"] + tot["fn"]
        if not n:
            print()
            continue
        prec = tot["tp"] / (tot["tp"] + tot["fp"]) if tot["tp"] + tot["fp"] else 0
        rec = tot["tp"] / (tot["tp"] + tot["fn"]) if tot["tp"] + tot["fn"] else 0
        print(f"\n  decided {n}: accuracy {100*(tot['tp']+tot['tn'])//n}%  "
              f"precision {prec:.2f}  recall {rec:.2f}")
        print(f"  refusals {tot['null_gold1']+tot['null_gold0']} "
              f"({tot['null_gold1']} where gold says present, "
              f"{tot['null_gold0']} where gold says absent)\n")
        print("  codes this coder got wrong somewhere:")
        for key in sorted(per_code, key=lambda k: per_code[k].count("fn")
                          + per_code[k].count("fp"), reverse=True):
            v = per_code[key]
            if "fn" in v or "fp" in v:
                print(f"    {key.split('.', 1)[1][:46]:48s} {' '.join(v)}")
        print()

    if args.out:
        write_cells(Path(args.out), cells)


def write_cells(out: Path, cells: list[dict]):
    """One row per decided cell, so each disagreement can be read next to the
    reason the coder gave for it."""
    cols = ["coder", "assemblage", "code", "gold", "coded", "agree", "reason"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(cells)
    print(f"wrote {out.with_suffix('.csv')}")
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return
    wb = Workbook()
    ws = wb.active
    ws.title = "gold vs coded"
    ws.append(cols)
    bad = PatternFill("solid", fgColor="FFD6D6")
    warn = PatternFill("solid", fgColor="FFF0CC")
    for c in cells:
        ws.append([c.get(k) for k in cols])
        fill = bad if c["agree"] == "no" else warn if c["agree"] == "refused" else None
        if fill:
            for cell in ws[ws.max_row]:
                cell.fill = fill
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for col, wdt in zip("ABCDEFG", [12, 22, 46, 6, 18, 9, 95]):
        ws.column_dimensions[col].width = wdt
    for row in ws.iter_rows(min_row=2, min_col=7, max_col=7):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(out.with_suffix(".xlsx"))
    print(f"wrote {out.with_suffix('.xlsx')}")


if __name__ == "__main__":
    main()
