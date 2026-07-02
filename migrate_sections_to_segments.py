#!/usr/bin/env python3
"""
Convert .sections.json (old format) -> .segments.json (shared pipeline format).

Old format:
  {"report": ..., "n_headings": ..., "n_pages": ...,
   "sections": {"executive_summary": [{label, pages}], ...}}

New format:
  {"report": ..., "n_headings": ..., "n_pages": ...,
   "segments": [{"label": ..., "pages": [...], "executive_summary_pages": [...],
                 ..., "_section_detail": {...}}]}

Usage:
    uv run python migrate_sections_to_segments.py <dir>
    uv run python migrate_sections_to_segments.py <dir> --dry-run
"""

import argparse
import json
from pathlib import Path


def migrate_file(src: Path, dry_run: bool = False) -> bool:
    data = json.loads(src.read_text(encoding="utf-8"))
    if "segments" in data:
        print(f"  [skip] {src.name} — already has segments key")
        return False
    if "sections" not in data:
        print(f"  [skip] {src.name} — no sections key")
        return False

    sections: dict = data["sections"]
    section_pages: dict[str, list[int]] = {}
    section_detail: dict = {}

    for section_type, entries in sections.items():
        if not isinstance(entries, list):
            continue
        flat: list[int] = []
        for entry in entries:
            if isinstance(entry, dict):
                flat.extend(int(p) for p in entry.get("pages", []))
        if flat:
            section_pages[f"{section_type}_pages"] = sorted(set(flat))
            section_detail[section_type] = entries

    all_pages = sorted({p for plist in section_pages.values() for p in plist})

    new_data = {
        "report":     data["report"],
        "n_headings": data.get("n_headings"),
        "n_pages":    data.get("n_pages"),
        "segments": [
            {
                "label":  data["report"],
                "pages":  all_pages,
                **section_pages,
                "_section_detail": section_detail,
            }
        ],
    }

    dst = src.with_suffix("").with_suffix(".segments.json")
    if not dry_run:
        dst.write_text(json.dumps(new_data, indent=2, ensure_ascii=False), encoding="utf-8")

    counts = {k[:-len("_pages")]: len(v) for k, v in section_pages.items()
              if k != "other_pages"}
    summary = "  ".join(f"{k}:{n}pp" for k, n in counts.items() if n > 0)
    tag = "[dry-run]" if dry_run else "[migrated]"
    print(f"  {tag} {src.stem}  |  {summary}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", help="Directory containing .sections.json files")
    ap.add_argument("--dry-run", action="store_true", help="Print what would happen without writing")
    args = ap.parse_args()

    target = Path(args.directory)
    if not target.is_dir():
        raise SystemExit(f"Not a directory: {target}")

    files = sorted(target.glob("*.sections.json"))
    if not files:
        raise SystemExit(f"No .sections.json files found in {target}")

    print(f"Migrating {len(files)} files in {target}\n")
    n = sum(migrate_file(f, dry_run=args.dry_run) for f in files)
    print(f"\nDone. {n} files {'would be ' if args.dry_run else ''}migrated.")


if __name__ == "__main__":
    main()
