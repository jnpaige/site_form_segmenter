"""Write figure_classification_map.md and figure_classes.csv from figure
classification results — same append-per-document pattern as reporter.py's
segmentation_map.md/segments.csv, but with a flat one-row-per-figure CSV
shape instead of one-column-per-page-type, since a figure's classes are a
variable-length multi-label set rather than a handful of fixed booleans."""
import csv
from pathlib import Path

_FIGURE_FIELDS = [
    "pdf_stem", "file", "page", "model", "primary_class", "classes", "rationale",
]


def append_figure_classes_csv(out_dir, pdf_stem: str, figures: list[dict], model: str = "") -> None:
    """Append one row per classified figure to <out_dir>/figure_classes.csv."""
    csv_path = Path(out_dir) / "figure_classes.csv"
    write_header = not csv_path.exists()

    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_FIGURE_FIELDS)
        if write_header:
            writer.writeheader()
        for fig in figures:
            writer.writerow({
                "pdf_stem":      pdf_stem,
                "file":          fig.get("file", ""),
                "page":          fig.get("page", ""),
                "model":         model,
                "primary_class": fig.get("primary_class", ""),
                "classes":       ";".join(fig.get("classes", [])),
                "rationale":     fig.get("rationale", ""),
            })


def append_figure_classification_map(
    out_dir,
    pdf_stem: str,
    figures: list[dict],
    by_class: dict[str, list[str]],
    model: str = "",
) -> None:
    """Append one document's classification results to
    <out_dir>/figure_classification_map.md."""
    map_path = Path(out_dir) / "figure_classification_map.md"
    write_header = not map_path.exists()

    lines = []
    if write_header:
        lines += ["# Figure Classification Map", "", "---", ""]

    lines += [f"## {pdf_stem}", ""]
    if model:
        lines.append(f"**Model:** {model}")
    lines.append(f"**Figures classified:** {len(figures)}")
    lines.append("")

    for key, files in by_class.items():
        if files:
            lines.append(f"- **{key}** ({len(files)}): {', '.join(files)}")
    lines.append("")

    for fig in figures:
        classes_str = ", ".join(fig.get("classes", []))
        lines.append(
            f"- `{fig['file']}` (p{fig.get('page')}) — **{fig.get('primary_class')}** "
            f"[{classes_str}]: {fig.get('rationale', '')}"
        )

    lines += ["", "---", ""]

    with map_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
