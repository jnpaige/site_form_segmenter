"""
codebook_parser.py — read the procedural unit and technological mode codebooks
into structured entries.

Both codebooks share a layout: a level-2 heading names the code, and the body
carries labelled fields — Short Description, Definition, Inclusion criteria,
Exclusion criteria, Typical exemplars, Atypical exemplars, Close but no.

The last field is the reason codes are judged in thematic batches rather than
one at a time. "Close but no" is written as cross-references — Mode D1's says
"Steeply retouched backed pieces (Mode D2 or D4)" — so a code is only decidable
alongside the codes it is contrasted with.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

FIELDS = ["Short Description", "Definition", "Inclusion criteria",
          "Exclusion criteria", "Typical exemplars", "Atypical exemplars",
          "Close but no"]
_FIELD_RE = re.compile(
    r"\*\*\s*(" + "|".join(re.escape(f) for f in FIELDS) + r")\.?\s*:?\s*\*\*\.?\s*:?",
    re.I)
_HEAD_RE = re.compile(r"^##\s+(?!#)(.+?)\s*$", re.M)


@dataclass
class Code:
    key: str                    # column name, e.g. "PU.facetting"
    name: str                   # heading text, e.g. "Faceting of core platform"
    book: str                   # "PU" or "MODE"
    group: str = ""             # thematic batch
    fields: dict = field(default_factory=dict)

    def render(self) -> str:
        out = [f"### {self.key} — {self.name}"]
        for f in FIELDS:
            v = (self.fields.get(f) or "").strip()
            if v:
                out.append(f"{f}: {v}")
        return "\n".join(out)


def _split_entries(text: str, start_marker: str, end_marker: str | None) -> list[tuple[str, str]]:
    if start_marker in text:
        text = text.split(start_marker, 1)[1]
    if end_marker and end_marker in text:
        text = text.split(end_marker, 1)[0]
    heads = list(_HEAD_RE.finditer(text))
    out = []
    for i, m in enumerate(heads):
        body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        out.append((m.group(1).strip(), body))
    return out


def _parse_fields(body: str) -> dict:
    marks = list(_FIELD_RE.finditer(body))
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        # Normalise the label's capitalisation back to the canonical spelling.
        canon = next(f for f in FIELDS if f.lower() == m.group(1).lower())
        out[canon] = re.sub(r"\s+", " ", body[m.end():end]).strip()
    return out


def load(pu_path: Path, mode_path: Path, groups: dict | None = None) -> list[Code]:
    codes: list[Code] = []
    for path, book, start, end in [
        (pu_path, "PU", "# Procedural unit codes", "# Figures"),
        (mode_path, "MODE", "# Traits", "# Bibliography"),
    ]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, body in _split_entries(text, start, end):
            fields = _parse_fields(body)
            if not fields:
                continue
            codes.append(Code(key=_key_for(name, book), name=name, book=book,
                              fields=fields))
    if groups:
        by_name = {c.name.lower(): c for c in codes}
        for group, names in groups.items():
            for n in names:
                c = by_name.get(n.lower())
                if c:
                    c.group = group
    for c in codes:
        if not c.group:
            c.group = f"{c.book}.other"
    return codes


def _key_for(name: str, book: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    slug = re.sub(r"^(the|a)_", "", slug)
    return f"{book}.{slug[:44]}"


def batches(codes: list[Code]) -> dict[str, list[Code]]:
    out: dict[str, list[Code]] = {}
    for c in codes:
        out.setdefault(c.group, []).append(c)
    return out
