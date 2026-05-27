"""Write segmentation_map.md and segments.csv from segmentation results."""
import csv
from pathlib import Path


_SEGMENT_FIELDS = [
    "trinomial", "source_file", "label", "year",
    "pages", "page_count",
    "form_pages", "narrative_pages", "nrhp_pages",
]


def append_segments_csv(out_dir, trinomial: str, segments: list[dict]) -> None:
    """Append segment rows for one trinomial to <out_dir>/segments.csv.

    Creates the file with a header row if it does not yet exist.
    """
    csv_path = Path(out_dir) / "segments.csv"
    write_header = not csv_path.exists()

    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_SEGMENT_FIELDS)
        if write_header:
            writer.writeheader()
        for seg in segments:
            pages = seg.get("pages", [])
            writer.writerow({
                "trinomial":       trinomial,
                "source_file":     seg.get("_source_file", ""),
                "label":           seg.get("label", ""),
                "year":            seg.get("year", ""),
                "pages":           ";".join(str(p) for p in pages),
                "page_count":      len(pages),
                "form_pages":      ";".join(str(p) for p in seg.get("form_pages", [])),
                "narrative_pages": ";".join(str(p) for p in seg.get("narrative_pages", [])),
                "nrhp_pages":      ";".join(str(p) for p in seg.get("nrhp_pages", [])),
            })


def append_segmentation_map(
    out_dir,
    trinomial: str,
    segments: list[dict],
    seg_model: str = "",
    seg_type: str = "",
) -> None:
    """Append a trinomial's segmentation results to <out_dir>/segmentation_map.md.

    Creates the file with a header on first write.
    """
    map_path = Path(out_dir) / "segmentation_map.md"
    write_header = not map_path.exists()

    lines = []
    if write_header:
        lines += ["# Segmentation Map", ""]
        if seg_model:
            lines.append(f"**Segmentation model:** {seg_model}")
        if seg_type:
            lines.append(f"**Segment type:** {seg_type}")
        lines += ["", "---", ""]

    lines += [f"## {trinomial}", ""]

    for seg in segments:
        label  = seg.get("label", "Unknown")
        year   = seg.get("year")
        source = seg.get("_source_file", "")
        pages  = seg.get("pages", [])

        year_str   = f" ({year})" if year else ""
        source_str = f" · *{source}*" if source else ""
        lines.append(f"**{label}**{year_str}{source_str}")
        lines.append(f"- All pages ({len(pages)}): {_fmt_pages(pages)}")

        for key, label_str in [
            ("form_pages",      "Form pages"),
            ("narrative_pages", "Narrative pages"),
            ("nrhp_pages",      "NRHP pages"),
        ]:
            if key in seg:
                lines.append(f"- {label_str}: {_fmt_pages(seg[key])}")

        lines.append("")

    lines += ["---", ""]

    with map_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _fmt_pages(pages: list) -> str:
    return ", ".join(str(p) for p in pages) if pages else "—"
