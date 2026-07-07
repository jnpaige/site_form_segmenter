"""Shared inventory.csv writer for run-folder provenance tracking.

Every pipeline script writes one inventory.csv per run, listing every output
file it produced plus the provenance (model, prompt, chunking) that produced
it. Fields the caller doesn't have are filled with "not recorded" rather than
left blank, so gaps in provenance stay visible instead of looking like zeros.
"""
import csv
from pathlib import Path

INVENTORY_FIELDS = [
    "run_id", "tool", "model", "file_name", "file_path",
    "source_input", "prompt_file", "prompt_snapshot_key",
    "temperature", "num_ctx", "chunk_strategy", "produced_at",
    "output_file_path",
]

NOT_RECORDED = "not recorded"


def write_inventory_csv(run_dir: Path, rows: list[dict]) -> None:
    """Write <run_dir>/inventory.csv from a list of row dicts."""
    path = run_dir / "inventory.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INVENTORY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, NOT_RECORDED) for k in INVENTORY_FIELDS})
