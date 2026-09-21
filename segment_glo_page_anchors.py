#!/usr/bin/env python3
"""
segment_glo_page_anchors.py — build a per-page EVIDENCE MAP for GLO survey
field-note volumes by reading, with a vision model, the two anchors that the
text pipeline currently throws away.

WHY THIS EXISTS
---------------
Both existing GLO segmenters (segment_glo_township_range.py, regex; and
segment_glo_township_range_llm.py, overlapping page windows) read OCR'd body
text — 1807 manuscript handwriting — to decide which Township/Range a page
belongs to. Measured against the 43-volume Evangeline corpus on 2026-09-14,
that is the weakest evidence in the document:

  - 80% of pages (8,654/10,817) come back from Docling with no layout
    structure at all, as raw Surya text.
  - headings.json exists for 2 of 43 volumes, with 6 junk headings each, so
    heading-driven segmentation (the Phase II report pass-1 mechanism) has
    nothing to work from.
  - The regex segmenter's own output shows the cost: in 507_00056, segment
    T1S_R6E spans 110 pages on the strength of ONE directly-confirmed stamp.

Meanwhile each page physically carries up to three much better signals, in
clean modern or printed lettering rather than manuscript script:

  1. THE INDEX PAGE (front matter) — a ruled archival finding aid,
     "Surveyors | Township | Range | Pages", one page SPAN per Township/Range.
     Present in 35/43 volumes. Its narrative text is unusable: the .md groups
     the page BY COLUMN (every township, then every range, then every page
     number), losing which township goes with which page, so only 18/43
     volumes yield even five parseable spans and 507_00056 yields zero.
     Its TABLE GEOMETRY, however, survives — see read_index_from_tables().
  2. THE ARCHIVIST'S TITLE CARD — a photographic strip laid on nearly every
     page of the volumes that have one, reading e.g. "T. 2N  R. 4W  N.W.D. /
     R.B. Paine". Surya recovers its text on only ~9% of corpus pages,
     because it is WHITE TEXT ON BLACK and Surya is tuned for dark-on-light.
  3. THE VOLUME PAGE NUMBER in the top margin, alternating corners between
     facing pages, handwritten in some volumes and machine-stamped in others.
     Not recoverable from .md at all, because of (the 80% layout fallback).

Reading (2) and (3) from a cropped top strip with a vision model recovered
6/6 page numbers and 4/4 cards in a hand-check, at ~3s/page on a 7B model.

WHAT THIS SCRIPT DOES NOT DO
----------------------------
It does not write segments.json. It produces the EVIDENCE MAP that a
segments.json can then be built from, deliberately as a separate step,
because the join has a real unsolved piece in it (see MANUSCRIPT vs PDF
PAGE NUMBERS below) and conflating "what we observed" with "what we
concluded" is what made the regex segmenter's output untrustworthy — its
"zero unmapped pages" really meant "the fill algorithm always produces an
answer, even a wrong one."

MANUSCRIPT vs PDF PAGE NUMBERS — THE PIECE THAT IS NOT AUTOMATIC
----------------------------------------------------------------
The index's page numbers are the VOLUME's own pagination, which is usually
NOT the 0-indexed position in the scanned PDF. Two families were observed:

  - 1:1 volumes (e.g. 507_00102: index max 316, PDF 317 pages) — the volume
    was archivally re-paginated and the index maps almost straight onto PDF
    page order.
  - series-numbered volumes (e.g. 507_00615: index max 570, PDF 189 pages)
    — the index covers a whole manuscript series and this PDF holds only a
    subset of it. The offset is PIECEWISE, not constant: manuscript 33 is
    PDF page 5 (offset 28) while manuscript 168 is PDF page 60 (offset 108).

So the offset must never be interpolated. It is recovered per page, from the
margin page number, which is exactly what pass B reads. `index_family` in
the output records which family a volume looks like, as an observation.

OUTPUT
------
runs/<YYYYMMDD_HHMM>_<gitsha>/<model_slug>__<document>.anchors.json

    {
      "document": ...,
      "n_pdf_pages": 189,
      "index": {"found": true, "page": 0, "rows": [...], "page_min": 1,
                "page_max": 570, "index_family": "series_numbered"},
      "pages": [
        {"page": 0,                      <- 0-INDEXED PDF PAGE, the map key
         "card_text": "T2N R5E / K. McCrummen / S.W.D.",
         "township": "2N", "range": "5E", "district": "SWD",
         "margin_page_number": 168,
         "index_match": {"township": "2N", "range": "5E", "row": 12},
         "evidence": ["card", "margin", "index"],
         "agreement": "corroborated"},
        ...
      ],
      "conflicts": [...],
      "coverage": {...}
    }

`agreement` is the point of the whole file. A page where the card and the
index (reached via the margin page number) independently name the same
Township/Range is "corroborated" — two readings of two different physical
artefacts. "conflict" pages are listed separately rather than silently
resolved, the same way segment_glo_township_range_llm.py surfaces
_boundary_disagreements.

HALLUCINATION GUARD — STRUCTURE IS PARSED FROM THE VERBATIM, NOT REPORTED
-------------------------------------------------------------------------
Both prompts ask for a VERBATIM transcription (card_text, and `raw` per index
row) alongside the parsed fields, and this script derives every structured
value by parsing that verbatim string itself (_parse_card_text,
_parse_index_raw). The model's own structured fields are kept only as a
disagreement flag, never as the answer. Same discipline as
lib/heading_recovery.py, which verifies a claimed heading against the page it
was claimed for.

Two distinct real failures, both observed in testing on 2026-09-14, are what
this is for:

  - PROMPT-EXAMPLE PARROTING. Given a prompt whose JSON example held
    realistic values ("township": "1N", "range": "4W", "surveyor":
    "R.B. Paine"), the model correctly reported card_text: null on pages
    with no card — and then filled those three fields in anyway, copied
    straight out of the example. Both prompts now use obviously-fake
    placeholders as well.
  - COLUMN SHIFT. On an index row it transcribed correctly as
    raw='1 N 2 E 9 - 27', its own fields came back township=1/dir=E,
    range=1/dir=E — shifted by a column. The verbatim was right every single
    time the fields were wrong.

A third, quieter failure is handled by _DIGIT_FIX rather than by rejection:
a title card reading "T1N R3E" is routinely transcribed "TIN R3E", and
rejecting it outright would throw away the corpus's single most reliable
signal. Letter-for-digit substitution is undone ONLY in a character position
the card's rigid printed format has already proved is a number slot — never
in free text, where "S" really is South.

THE MARGIN NUMBER IS THE LINCHPIN, SO IT GETS ITS OWN GUARD
-----------------------------------------------------------
flag_margin_outliers() marks a margin page number that breaks the volume's
own page sequence. This matters more than it first appears: the margin number
is what turns an index page span into a PDF page range, so one misread
silently relocates a page to the wrong township. Observed on 507_00102: PDF
page 7 read as margin "140", which the index resolved into its 127-188 row,
contradicting that page's own title card. There a card happened to be present
to catch it — on the 23 of 43 volumes with NO card, this guard is the only
thing that would.

It works on the OFFSET (margin number - PDF page), because that is what
separates the two volume families: one_to_one volumes hold a near-constant
offset, series_numbered volumes have a piecewise-constant one that jumps
wherever the scan skips part of the manuscript series. A genuine jump is
SHARED by a run of pages; a misread is an ISOLATED spike whose neighbours
agree with each other across it. Only the isolated case is flagged, and the
endpoints are checked too — the first and last pages of a volume are exactly
where misreads cluster, and the 507_00102 case was on the last page examined.

Nothing is renumbered or dropped; the flag rides along on the page entry and
on any conflict it explains.

SURVEY MODE
-----------
--survey samples a few pages per volume across a whole corpus and reports
only whether a title card is present, without doing the full per-page pass.
Use it to find out which volumes the card-based anchor is even available for
before committing to a full run.

USAGE
-----
    uv run python segment_glo_page_anchors.py --config config_glo_page_anchors.yaml
    uv run python segment_glo_page_anchors.py --config config_glo_page_anchors.yaml \
        --report "507_00615__2N1W+3N1W+4N2W+4N3W__NO5_SUP_SWD_MCCRUMMEN"
    uv run python segment_glo_page_anchors.py --config config_glo_page_anchors.yaml \
        --survey --survey-pages 6
"""

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "lib"))
from ollama_client import extract_json_vision, get_stats, reset_stats
from pdf_renderer import render_page_strips, render_pages_to_images
from inventory import write_inventory_csv

ANCHOR_PROMPT_FILE = Path(__file__).parent / "segment_types" / "glo_page_anchor.txt"
INDEX_PROMPT_FILE  = Path(__file__).parent / "segment_types" / "glo_index_table.txt"

DEFAULT_MODEL    = "qwen2.5vl:7b"
DEFAULT_BASE_URL = "http://localhost:11434"

# How much of the page height the top-strip crop keeps. 0.18 was checked by
# eye against all three volume families: it clears the title card and both
# margin-number corners without pulling in the first line of body text.
DEFAULT_STRIP_FRAC = 0.18

# Front-matter pages searched for the index table. Observed index pages in
# this corpus sit at PDF page 0-5.
DEFAULT_INDEX_SCAN_PAGES = 6

# Longest-edge pixel budget for the index-page render. This is load-bearing,
# not a performance knob: at 1600 the model transcribed an index row's
# handwritten "N" as "S" (caught only because the page's printed title card
# contradicted it); at 2200 every checked row on that volume was correct, and
# 3000 changed nothing further. The index page is one call per volume, so
# render it large.
DEFAULT_INDEX_MAX_DIM = 2200


# ---------------------------------------------------------------------------
# verification — never trust a structured field the verbatim text can't support
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Uppercase, strip everything but letters and digits."""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


# Letters routinely substituted for digits when a title card or an index row
# is read. Only ever applied to a character in a position the surrounding
# pattern has already proved is a NUMBER slot (the digit between "T" and a
# N/S direction letter, say) — never to free text, where "S" really is South
# and "O" really is a letter.
_DIGIT_FIX = {"I": "1", "L": "1", "|": "1", "Z": "2", "O": "0", "Q": "0",
              "B": "8", "G": "6", "S": "5", "T": "7", "A": "4"}


def _as_digit(ch: str) -> str | None:
    ch = str(ch).upper()
    if ch.isdigit():
        return ch
    return _DIGIT_FIX.get(ch)


def _tr_token(value, valid_dirs: str) -> str | None:
    """Normalize '2N' / '2 N.' / ' 2n ' to '2N'; return None if not that shape."""
    if value is None:
        return None
    m = re.search(r"([0-9A-Z])\s*\.?\s*([NSEW])", str(value).upper())
    if not m:
        return None
    digit = _as_digit(m.group(1))
    direction = m.group(2)
    if digit is None or direction not in valid_dirs:
        return None
    # PLSS township and range numbering starts at 1 — a 0 is always a misread
    # (observed: an index row came back as range "0W"), never a real value.
    if int(digit) < 1:
        return None
    return f"{int(digit)}{direction}"


# A title card's printed layout is rigid: T <num><N|S>  R <num><E|W>  <district>.
# Position is what makes the digit-confusion fix safe here.
_CARD_TR_RE = re.compile(
    r"\bT[\s.]*([0-9A-Z])[\s.]*([NS])\b[\s.,]*R[\s.]*([0-9A-Z])[\s.]*([EW])\b",
    re.IGNORECASE)
_CARD_DIST_RE = re.compile(r"\b([NS])[\s.]*([EW])[\s.]*D\b", re.IGNORECASE)

# Does a transcription actually have the SHAPE of a title card — a township
# AND a range, in the card's rigid printed order? Needed because the model
# will sometimes report a handwritten page header ("Exteriors T. 3 N. R. 4 W.
# continued", "Western Land District of the Territory of Orleans") as a card.
# Requiring both halves separates volumes that really carry cards from
# volumes where the model reached for the nearest header-looking thing.
_CARD_SHAPED_RE = re.compile(
    r"\bT[\s.]*[0-9A-Z]{1,3}[\s.]*[NS]\b.{0,12}?R[\s.]*[0-9A-Z]{1,3}[\s.]*[EW]\b",
    re.IGNORECASE | re.DOTALL)


def _is_card_shaped(text: str | None) -> bool:
    return bool(text) and bool(_CARD_SHAPED_RE.search(str(text)))


# An index row transcribed verbatim: <num> <N|S> <num> <E|W> <start>[ - <end>].
#
# The trailing span is OPTIONAL because the corpus has BOTH index formats, and
# requiring it silently routed a whole volume onto the unreliable path:
# 507_00615's index gives ranges ("1 N 3 E 29 - 39") while 507_00056's gives
# only the page a township STARTS on ("2 N 3 E 139"). With the dash mandatory,
# 507_00056 parsed 0 of 32 rows from its verbatim text and fell back to the
# model's own structured fields for every one of them — the exact path that
# produces column-shift errors.
_INDEX_ROW_RE = re.compile(
    r"([0-9A-Z])\s*\.?\s*([NS])\s+([0-9A-Z])\s*\.?\s*([EW])\s+"
    r"(\d{1,4})(?:\s*[-–—]\s*(\d{1,4}))?",
    re.IGNORECASE)


def _parse_card_text(card: str) -> dict:
    """Re-derive township/range/district from a card's VERBATIM text.

    The model's own structured fields are not trusted: in testing it both
    copied values out of the prompt's example onto cardless pages, and
    shifted range values into the township field. The card's printed format
    is rigid enough to parse directly, which removes that whole class of
    error.
    """
    out = {"township": None, "range": None, "district": None}
    m = _CARD_TR_RE.search(card)
    if m:
        t_digit = _as_digit(m.group(1))
        r_digit = _as_digit(m.group(3))
        # 0 is never a real township or range — PLSS numbering starts at 1.
        if t_digit is not None and int(t_digit) >= 1:
            out["township"] = f"{int(t_digit)}{m.group(2).upper()}"
        if r_digit is not None and int(r_digit) >= 1:
            out["range"] = f"{int(r_digit)}{m.group(4).upper()}"
    d = _CARD_DIST_RE.search(card)
    if d:
        out["district"] = f"{d.group(1)}{d.group(2)}D".upper()
    return out


def _parse_index_raw(raw) -> dict | None:
    """Re-derive one index row from its VERBATIM transcription.

    Observed failure this exists to fix: for a row the model transcribed
    correctly as raw='1 N 2 E 9 - 27', its own structured fields came back
    township=1/township_dir=E, range=1/range_dir=E — the columns shifted by
    one. The raw string was right every time the structured fields were
    wrong, so the raw string is what gets parsed.
    """
    if not raw:
        return None
    m = _INDEX_ROW_RE.search(str(raw))
    if not m:
        return None
    t_digit = _as_digit(m.group(1))
    r_digit = _as_digit(m.group(3))
    if t_digit is None or r_digit is None:
        return None
    ps = int(m.group(5))
    pe = int(m.group(6)) if m.group(6) else None   # start-only index format
    if ps <= 0 or (pe is not None and pe < ps):
        return None
    return {
        "township": f"{int(t_digit)}{m.group(2).upper()}",
        "range":    f"{int(r_digit)}{m.group(4).upper()}",
        "page_start": ps,
        "page_end":   pe,
    }


def _verify_card(result: dict) -> dict:
    """Re-derive every card field from the verbatim card_text.

    Returns a dict with card_text/township/range/district/surveyor, where a
    field is non-null ONLY if the verbatim transcription supports it. A model
    that reports no card but fills in township/range (observed — it copies
    the prompt's example) loses those fields here.
    """
    out = {"card_text": None, "township": None, "range": None,
           "district": None, "surveyor": None, "card_rejected": None}

    raw = result.get("card_text")
    if not raw or str(raw).strip().lower() in {"null", "none", ""}:
        # No card transcribed. Anything else the model claimed is unsupported.
        claimed = [k for k in ("township", "range", "district", "surveyor")
                   if result.get(k) and str(result[k]).lower() not in {"null", "none"}]
        if claimed:
            out["card_rejected"] = f"no card_text but model filled {','.join(claimed)}"
        return out

    card = str(raw).strip()
    out["card_text"] = card

    # Structure comes from parsing the verbatim card, never from the model's
    # own fields — those are only kept as a disagreement flag.
    parsed = _parse_card_text(card)
    out["township"] = parsed["township"]
    out["range"]    = parsed["range"]
    out["district"] = parsed["district"]

    claimed_t = _tr_token(result.get("township"), "NS")
    claimed_r = _tr_token(result.get("range"), "EW")
    mismatches = []
    if claimed_t and parsed["township"] and claimed_t != parsed["township"]:
        mismatches.append(f"township model={claimed_t} card={parsed['township']}")
    if claimed_r and parsed["range"] and claimed_r != parsed["range"]:
        mismatches.append(f"range model={claimed_r} card={parsed['range']}")
    if mismatches:
        out["card_rejected"] = "model fields disagree with card text: " + "; ".join(mismatches)

    surveyor = result.get("surveyor")
    if surveyor and str(surveyor).lower() not in {"null", "none"}:
        # Surveyor names are transcribed with varying punctuation/spacing, so
        # compare on letters only, and require a real overlap rather than a
        # substring of one or two characters.
        sn = re.sub(r"[^A-Z]", "", str(surveyor).upper())
        if len(sn) >= 4 and sn in re.sub(r"[^A-Z]", "", card.upper()):
            out["surveyor"] = str(surveyor).strip()

    return out


def _clean_page_number(value) -> int | None:
    """Accept a plausible volume page number, else None.

    Rejects obviously-wrong readings without pretending to know the volume's
    real pagination: a page number is a positive integer, and manuscript
    series pagination in this corpus tops out in the high hundreds.
    """
    if value is None:
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if n <= 0 or n > 2000:
        return None
    return n


# ---------------------------------------------------------------------------
# pass A — the index table
# ---------------------------------------------------------------------------

def read_index(pdf_path: Path, prompt: str, model: str, base_url: str,
               temperature: float, timeout: float, num_ctx: int,
               scan_pages: int, n_pdf_pages: int,
               max_dim: int = DEFAULT_INDEX_MAX_DIM, verbose: bool = True) -> dict:
    """Find and transcribe the volume's index table from its front matter."""
    candidates = list(range(min(scan_pages, n_pdf_pages)))
    images = render_pages_to_images(pdf_path, dpi=200, max_dim=max_dim)

    for page in candidates:
        if page not in images:
            continue
        res = extract_json_vision(
            system_prompt=prompt, user_content="",
            images=[images[page]], model=model, base_url=base_url,
            temperature=temperature, timeout=timeout,
            label=f"index p{page}", num_ctx=num_ctx,
        )
        if not isinstance(res, dict) or not res.get("is_index"):
            continue

        rows_in = res.get("rows") or []
        rows, dropped, from_raw, from_fields = [], 0, 0, 0
        for r in rows_in:
            if not isinstance(r, dict):
                dropped += 1
                continue

            # Prefer the verbatim row: the model's per-row structured fields
            # were observed shifting a column while `raw` stayed correct.
            parsed = _parse_index_raw(r.get("raw"))
            if parsed:
                from_raw += 1
                t, rg, ps, pe = (parsed["township"], parsed["range"],
                                 parsed["page_start"], parsed["page_end"])
                source = "raw"
            else:
                t  = _tr_token(f"{r.get('township')}{r.get('township_dir') or ''}", "NS")
                rg = _tr_token(f"{r.get('range')}{r.get('range_dir') or ''}", "EW")
                try:
                    ps = int(r.get("page_start"))
                except (TypeError, ValueError):
                    dropped += 1
                    continue
                try:
                    pe = int(r.get("page_end"))
                except (TypeError, ValueError):
                    pe = None
                if ps <= 0 or (pe is not None and pe < ps):
                    dropped += 1
                    continue
                # A row whose township/range failed to parse is KEPT, with
                # those fields null, as long as its page span is sound. The
                # span is what classifies the volume's index_family and what
                # anchors the lookup; dropping the row instead lost the
                # 257-316 span on 507_00102 and flipped that volume from
                # one_to_one to unclear. A null here simply carries no
                # township evidence — it never invents a conflict.
                from_fields += 1
                source = "model_fields"

            rows.append({"surveyor": r.get("surveyor"), "township": t, "range": rg,
                         "page_start": ps, "page_end": pe, "raw": r.get("raw"),
                         "parsed_from": source})

        if not rows:
            continue

        n_implied = infer_page_ends(rows)
        page_min = min(r["page_start"] for r in rows)
        page_max = max((r.get("page_end") or r.get("page_end_implied")
                        or r["page_start"]) for r in rows)

        # Which family is this volume? An observation, not a correction.
        if abs(page_max - n_pdf_pages) <= max(12, 0.05 * n_pdf_pages):
            family = "one_to_one"
        elif page_max > n_pdf_pages * 1.2:
            family = "series_numbered"
        else:
            family = "unclear"

        if verbose:
            print(f"    index found on p{page}: {len(rows)} rows "
                  f"({from_raw} from verbatim, {from_fields} from model fields, "
                  f"{dropped} dropped, {n_implied} start-only spans inferred), "
                  f"volume pages {page_min}-{page_max}, family={family}")
        return {"found": True, "page": page,
                "column_headings": res.get("column_headings"),
                "rows": rows, "n_rows_dropped": dropped,
                "n_spans_inferred": n_implied,
                "n_rows_from_raw": from_raw, "n_rows_from_model_fields": from_fields,
                "page_min": page_min, "page_max": page_max,
                "index_family": family}

    if verbose:
        print(f"    no index table found in first {len(candidates)} pages")
    return {"found": False, "page": None, "rows": [], "n_rows_dropped": 0,
            "page_min": None, "page_max": None, "index_family": None}


def read_index_from_tables(ocr_json: Path, index_page: int) -> dict | None:
    """Second, independent read of the index table — from OCR table geometry.

    pdf_ocr's 2026-09 `tables` addition records, per page, a detected table's
    grid dimensions plus every cell's text and start_row/start_col. On the
    GLO index page that grid comes back with its ROW ALIGNMENT INTACT, which
    the flattened md_text does not: the reflowed text groups the page by
    column (every township, then every range, then every page number), losing
    which township goes with which page, while the grid keeps them on a row.

    This is free, deterministic, and completely independent of the vision
    read — different mechanism, different failure modes — so it is used to
    corroborate the vision read rather than replace it. It is strictly worse
    on garbled cells (a township digit that OCR dropped is simply absent
    here, where a vision model still reads it off the page), so neither one
    alone is sufficient.

    Returns None when this volume was OCR'd before the `tables` field existed,
    or when no table was detected on the index page — both normal.
    """
    try:
        pages = json.loads(ocr_json.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(pages, list) or index_page >= len(pages):
        return None
    tables = (pages[index_page] or {}).get("tables") or []
    if not tables:
        return None
    tbl = tables[0]
    cells = tbl.get("cells") or []
    if not cells:
        return None

    grid: dict[int, dict[int, str]] = {}
    for c in cells:
        r, col = c.get("start_row"), c.get("start_col")
        if r is None or col is None:
            continue
        grid.setdefault(r, {})[col] = str(c.get("text") or "").strip()

    n_cols = tbl.get("num_cols") or (max((max(r) for r in grid.values() if r), default=0) + 1)

    # Which column holds the page numbers? Named by the header row if there
    # is one; otherwise assume the rightmost column, which is the layout in
    # every index page seen in this corpus.
    page_col = n_cols - 1
    header = grid.get(0, {})
    for col, txt in header.items():
        if "PAGE" in txt.upper():
            page_col = col
            break

    rows = []
    for r in sorted(grid):
        cells_r = grid[r]
        page_txt = cells_r.get(page_col, "")
        m = re.search(r"(\d{1,4})\s*(?:[-–—]\s*(\d{1,4}))?", page_txt)
        if not m:
            continue
        ps = int(m.group(1))
        pe = int(m.group(2)) if m.group(2) else None   # start-only index format
        if ps <= 0 or (pe is not None and pe < ps):
            continue

        # Whatever is left in the Pages cell once the span itself is removed.
        # Column splits vary between volumes: 507_00056 puts the range's
        # direction letter in its own column, while 507_00121 merges it into
        # the Pages cell ("W 1 - 18", "37-63 W"). The residue is where that
        # stranded letter is recovered from. Only a LONE E/W is taken — never
        # a digit — because joining the raw Pages cell into the row text
        # fabricates ranges out of page numbers ("37-63 W" -> range "63W").
        residue = (page_txt[:m.start()] + " " + page_txt[m.end():])
        dir_m = re.search(r"\b([EW])\b", residue, re.IGNORECASE)
        stranded_dir = dir_m.group(1).upper() if dir_m else None
        # Township/range live to the left of the page column, split across a
        # varying number of cells ("2" "N" "3" "E" on one row, "2 N" "3" "E"
        # on the next), so rejoin them and parse the joined string.
        left = " ".join(cells_r.get(c, "") for c in range(page_col) if cells_r.get(c)).strip()
        # REAL DIGITS ONLY here — no _DIGIT_FIX. On a title card the printed
        # format "T<x><N|S>" proves the character before the direction letter
        # is a number slot, which makes undoing a letter-for-digit misread
        # safe. A loose index-table cell proves nothing of the kind, and
        # applying the same fix here fabricated values: 'A E 455' became range
        # "4E" (A->4) and '75 - S E 547' became "5E" (S->5), from rows whose
        # township/range OCR had actually failed. A null is the correct answer
        # for those; the vision read is what fills them in.
        mt = re.search(r"([1-9]\d?)\s*\.?\s*([NS])\b", left, re.IGNORECASE)
        township = f"{int(mt.group(1))}{mt.group(2).upper()}" if mt else None

        mr = re.search(r"([1-9]\d?)\s*\.?\s*([EW])\b", left, re.IGNORECASE)
        if mr:
            # Number and direction sit together in the row's own cells.
            rng = f"{int(mr.group(1))}{mr.group(2).upper()}"
        elif stranded_dir:
            # Direction was stranded in the Pages cell; the number is the first
            # standalone one in the row AFTER the township (or anywhere in the
            # row, when the township digit itself failed to OCR).
            tail = left[mt.end():] if mt else left
            mn = re.search(r"\b([1-9]\d?)\b", tail)
            rng = f"{int(mn.group(1))}{stranded_dir}" if mn else None
        else:
            rng = None
        rows.append({"township": township, "range": rng,
                     "page_start": ps, "page_end": pe, "raw": f"{left} {page_txt}".strip()})

    if not rows:
        return None
    return {"n_rows": len(rows), "rows": rows,
            "grid": f"{tbl.get('num_rows')}x{tbl.get('num_cols')}"}


def _compare_index_reads(vision_rows: list[dict], table_rows: list[dict]) -> dict:
    """Corroborate the vision index read against the table-geometry read.

    Matched on page_start, which is the most robustly recovered field on both
    sides (a bare 2-4 digit number alone in its own column). Disagreements are
    reported, never resolved — same discipline as `conflicts` per page.
    """
    by_page = {r["page_start"]: r for r in table_rows}
    agree = disagree = only_vision = 0
    mismatches = []
    for v in vision_rows:
        t = by_page.get(v["page_start"])
        if not t:
            only_vision += 1
            continue
        # Only compare fields the table read actually recovered.
        fields = [(f, v.get(f), t.get(f)) for f in ("township", "range") if t.get(f)]
        if not fields:
            only_vision += 1
            continue
        bad = [(f, a, b) for f, a, b in fields if a != b]
        if bad:
            disagree += 1
            mismatches.append({"page_start": v["page_start"],
                               "vision_raw": v.get("raw"), "table_raw": t.get("raw"),
                               "fields": [{"field": f, "vision": a, "tables": b}
                                          for f, a, b in bad]})
        else:
            agree += 1
    return {"n_table_rows": len(table_rows), "n_agree": agree, "n_disagree": disagree,
            "n_vision_only": only_vision, "mismatches": mismatches}


def infer_page_ends(rows: list[dict], volume_max: int | None = None) -> int:
    """Fill `page_end_implied` for index rows that give only a start page.

    Two index formats exist in this corpus and they need different treatment:
    507_00615's gives an explicit span per township ("29 - 39"), while
    507_00056's gives only the page each township STARTS on ("139"). A
    start-only row covers exactly one page as written, which would make the
    index match almost nothing.

    The end is therefore inferred as (next distinct start - 1), reading the
    rows in page order — the ordinary meaning of a start-only index. It is
    written to a SEPARATE key, never to `page_end`, and every match made
    through it is marked `span_inferred`, so an inferred boundary can never be
    mistaken for one the index actually states. Rows are sorted by start
    first, because index rows are routinely not in ascending page order.

    The final row has no following start; it takes `volume_max` when one is
    known and is otherwise left open (None) rather than guessed.
    """
    starts = sorted({r["page_start"] for r in rows})
    next_start = {s: (starts[i + 1] if i + 1 < len(starts) else None)
                  for i, s in enumerate(starts)}
    n = 0
    for r in rows:
        if r.get("page_end") is not None:
            continue
        nxt = next_start[r["page_start"]]
        implied = (nxt - 1) if nxt is not None else volume_max
        if implied is not None and implied >= r["page_start"]:
            r["page_end_implied"] = implied
            n += 1
    return n


def _index_lookup(index: dict, vol_page: int | None) -> dict | None:
    """Which index row covers this volume page number?

    Prefers a span the index actually states; falls back to one inferred by
    infer_page_ends(), flagging the match as `span_inferred` when it does.
    """
    if vol_page is None or not index.get("found"):
        return None
    for i, r in enumerate(index["rows"]):
        end, inferred = r.get("page_end"), False
        if end is None:
            end, inferred = r.get("page_end_implied"), True
        if end is None:
            continue
        if r["page_start"] <= vol_page <= end:
            return {"township": r["township"], "range": r["range"], "row": i,
                    "span_inferred": inferred}
    return None


# ---------------------------------------------------------------------------
# pass B — per-page anchors
# ---------------------------------------------------------------------------

def read_page_anchors(pdf_path: Path, pages: list[int], prompt: str, model: str,
                      base_url: str, temperature: float, timeout: float,
                      num_ctx: int, strip_frac: float,
                      batch: int = 25, verbose: bool = True) -> list[dict]:
    """Read card + margin page number from each page's top strip."""
    out: list[dict] = []
    for start in range(0, len(pages), batch):
        chunk = pages[start:start + batch]
        strips = render_page_strips(pdf_path, chunk, frac=strip_frac)
        for page in chunk:
            if page not in strips:
                out.append({"page": page, "_error": "render failed"})
                continue
            res = extract_json_vision(
                system_prompt=prompt, user_content="",
                images=[strips[page]], model=model, base_url=base_url,
                temperature=temperature, timeout=timeout,
                label="", num_ctx=num_ctx,
            )
            if not isinstance(res, dict):
                out.append({"page": page, "_error": "no JSON returned"})
                continue
            rec = {"page": page}
            rec.update(_verify_card(res))
            rec["margin_page_number"] = _clean_page_number(res.get("page_number"))
            out.append(rec)
        if verbose:
            done = min(start + batch, len(pages))
            cards = sum(1 for r in out if r.get("card_text"))
            margins = sum(1 for r in out if r.get("margin_page_number"))
            print(f"    {done}/{len(pages)} pages   cards={cards}  margins={margins}",
                  flush=True)
    return out


# ---------------------------------------------------------------------------
# assembling the evidence map
# ---------------------------------------------------------------------------

def flag_margin_outliers(page_records: list[dict]) -> int:
    """Mark margin page numbers that break the volume's own page sequence.

    The margin number is the linchpin — it is what turns an index page span
    into a PDF page range — so a single misread silently relocates a page to
    the wrong township. Observed on 507_00102: PDF page 7 came back as margin
    "140", which the index then resolved into the 127-188 row, contradicting
    that page's own title card. There it was caught only because a card
    happened to be present; on the 23 cardless volumes nothing would have
    caught it.

    The check uses the OFFSET (margin number - PDF page index), not the raw
    number, because the two volume families behave differently and only the
    offset separates them cleanly:

      - one_to_one volumes hold a near-constant offset throughout.
      - series_numbered volumes have a PIECEWISE-constant offset, jumping
        wherever the scan skips part of the manuscript series.

    So a genuine jump is SHARED by a run of consecutive pages, while a misread
    is an ISOLATED spike whose neighbours agree with each other across it.
    Only the isolated case is flagged, which is what keeps a real subset
    boundary from being mistaken for an error.

    Flags only — nothing is renumbered or dropped. Returns the count flagged.
    """
    seq = [r for r in page_records if r.get("margin_page_number") is not None]
    if len(seq) < 3:
        return 0

    offsets = [r["margin_page_number"] - r["page"] for r in seq]
    n_flagged = 0
    for i, cur in enumerate(offsets):
        # Two nearest neighbours — one either side where possible, otherwise
        # the two on whichever side exists. The endpoints MUST be checked:
        # the first and last pages of a volume are where misreads cluster
        # (title leaves, blank end leaves), and 507_00102's bad margin read
        # was on the last page of the range examined.
        if i == 0:
            neigh = offsets[1:3]
        elif i == len(offsets) - 1:
            neigh = offsets[-3:-1]
        else:
            neigh = [offsets[i - 1], offsets[i + 1]]
        # Flag only when the neighbours agree with EACH OTHER and disagree
        # with this page: that is an isolated spike. A genuine piecewise jump
        # is shared by a run, so its neighbours disagree among themselves and
        # nothing is flagged.
        if len(neigh) == 2 and neigh[0] == neigh[1] and cur != neigh[0]:
            seq[i]["margin_suspect"] = (
                f"offset {cur} breaks the run (neighbours both {neigh[0]}) — "
                f"likely a misread page number")
            n_flagged += 1
    return n_flagged


def build_evidence_map(page_records: list[dict], index: dict) -> tuple[list[dict], list[dict], dict]:
    """Attach index lookups and classify each page's agreement state."""
    pages, conflicts = [], []

    for rec in page_records:
        vol_page = rec.get("margin_page_number")
        match = _index_lookup(index, vol_page)

        evidence = []
        if rec.get("card_text"):
            evidence.append("card")
        if vol_page is not None:
            evidence.append("margin")
        # An index row that covers this page but names no township or range
        # (its own OCR having failed) is not township evidence — count it as
        # span coverage only, so `with_index` stays a count of pages the index
        # actually told us something about.
        if match and (match["township"] or match["range"]):
            evidence.append("index")
        elif match:
            evidence.append("index_span_only")

        card_tr  = (rec.get("township"), rec.get("range"))
        index_tr = (match["township"], match["range"]) if match else (None, None)

        if all(card_tr) and all(index_tr):
            agreement = "corroborated" if card_tr == index_tr else "conflict"
        elif all(card_tr):
            agreement = "card_only"
        elif all(index_tr):
            agreement = "index_only"
        else:
            agreement = "none"

        entry = {
            "page": rec["page"],
            "card_text": rec.get("card_text"),
            "township": rec.get("township"),
            "range": rec.get("range"),
            "district": rec.get("district"),
            "surveyor": rec.get("surveyor"),
            "margin_page_number": vol_page,
            "index_match": match,
            "evidence": evidence,
            "agreement": agreement,
        }
        if match and match.get("span_inferred"):
            entry["index_span_inferred"] = True
        if rec.get("margin_suspect"):
            entry["margin_suspect"] = rec["margin_suspect"]
        if rec.get("card_rejected"):
            entry["card_rejected"] = rec["card_rejected"]
        if rec.get("_error"):
            entry["_error"] = rec["_error"]
        pages.append(entry)

        if agreement == "conflict":
            conflicts.append({
                "page": rec["page"],
                "card_says": f"T{card_tr[0]}_R{card_tr[1]}",
                "index_says": f"T{index_tr[0]}_R{index_tr[1]}",
                "margin_page_number": vol_page,
                "margin_suspect": rec.get("margin_suspect"),
                "card_text": rec.get("card_text"),
            })

    n = len(pages) or 1
    coverage = {
        "n_pages": len(pages),
        "with_card":   sum(1 for p in pages if "card" in p["evidence"]),
        "with_margin": sum(1 for p in pages if "margin" in p["evidence"]),
        "with_index":  sum(1 for p in pages if "index" in p["evidence"]),
        "corroborated": sum(1 for p in pages if p["agreement"] == "corroborated"),
        "conflict":     sum(1 for p in pages if p["agreement"] == "conflict"),
        "card_only":    sum(1 for p in pages if p["agreement"] == "card_only"),
        "index_only":   sum(1 for p in pages if p["agreement"] == "index_only"),
        "no_evidence":  sum(1 for p in pages if p["agreement"] == "none"),
        "margin_suspect": sum(1 for p in pages if p.get("margin_suspect")),
    }
    coverage["pct_any_evidence"] = round(
        100 * sum(1 for p in pages if p["evidence"]) / n, 1)
    return pages, conflicts, coverage


# ---------------------------------------------------------------------------
# survey mode
# ---------------------------------------------------------------------------

def survey_volume(pdf_path: Path, prompt: str, model: str, base_url: str,
                  temperature: float, timeout: float, num_ctx: int,
                  strip_frac: float, n_sample: int, rng: random.Random) -> dict:
    """Sample a few pages and report only whether a title card is present."""
    try:
        import pymupdf as fitz
        doc = fitz.open(str(pdf_path))
        n_pages = len(doc)
        doc.close()
    except Exception as e:
        return {"error": f"could not open PDF: {e}"}

    if n_pages == 0:
        return {"error": "empty PDF"}

    # Sample from the body, skipping front matter (index/cover pages carry no
    # card) and the last few leaves (often blank).
    lo, hi = min(5, n_pages - 1), max(5, n_pages - 3)
    pool = list(range(lo, hi)) or list(range(n_pages))
    sample = sorted(rng.sample(pool, min(n_sample, len(pool))))

    recs = read_page_anchors(pdf_path, sample, prompt, model, base_url,
                             temperature, timeout, num_ctx, strip_frac,
                             batch=len(sample), verbose=False)
    cards   = [r for r in recs if r.get("card_text")]
    shaped  = [r for r in cards if _is_card_shaped(r.get("card_text"))]
    margins = [r for r in recs if r.get("margin_page_number")]

    # Graded on card-SHAPED transcriptions, not on any transcription at all:
    # scoring the latter called volumes carrying only handwritten headers
    # ("Field notes on the Parallel of 31 of North latitude", "Western Land
    # District of the Territory of Orleans") card-bearing, which they are not.
    if len(shaped) >= 3:
        verdict = "solid"
    elif shaped:
        verdict = "weak"
    else:
        verdict = "none"

    return {
        "n_pdf_pages": n_pages,
        "sampled": sample,
        "n_sampled": len(sample),
        "n_with_card": len(cards),
        "n_card_shaped": len(shaped),
        "n_with_margin_number": len(margins),
        "card_verdict": verdict,
        "has_card": verdict == "solid",
        "example_card": (shaped[0]["card_text"] if shaped
                         else cards[0]["card_text"] if cards else None),
        "example_margin_numbers": [r["margin_page_number"] for r in margins][:6],
        "pages": recs,
    }


# ---------------------------------------------------------------------------
# run-folder plumbing (same conventions as the other scripts in this repo)
# ---------------------------------------------------------------------------

def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "nogit"


def _make_run_dir(base: Path) -> Path:
    stamp = f"{datetime.now().strftime('%Y%m%d_%H%M')}_{_git_sha()}"
    base.mkdir(parents=True, exist_ok=True)
    candidate = base / stamp
    n = 0
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            n += 1
            candidate = base / f"{stamp}-{chr(ord('a') + n - 1)}"


def _slug(model: str) -> str:
    return model.replace(":", "_").replace(".", "_")


def _write_anchor_map_md(run_dir: Path, results: list[dict]) -> None:
    """Human-readable review sheet: one block per document, runs of pages."""
    lines = ["# GLO page-anchor evidence map", "",
             "Page numbers are 0-indexed PDF pages, matching the "
             "`=== Page N ===` markers in pdf_ocr's .md/.txt output.", ""]
    for r in results:
        cov = r["coverage"]
        idx = r["index"]
        lines.append(f"## {r['document']}")
        lines.append("")
        lines.append(f"- PDF pages: {r['n_pdf_pages']}")
        if idx.get("found"):
            lines.append(f"- Index table: page {idx['page']}, {len(idx['rows'])} rows, "
                         f"volume pages {idx['page_min']}–{idx['page_max']} "
                         f"(**{idx['index_family']}**)")
        else:
            lines.append("- Index table: **not found**")
        lines.append(f"- Evidence: card {cov['with_card']}/{cov['n_pages']}, "
                     f"margin number {cov['with_margin']}/{cov['n_pages']}, "
                     f"index match {cov['with_index']}/{cov['n_pages']}")
        lines.append(f"- Agreement: corroborated {cov['corroborated']}, "
                     f"card-only {cov['card_only']}, index-only {cov['index_only']}, "
                     f"conflict {cov['conflict']}, none {cov['no_evidence']}")
        if r["conflicts"]:
            lines.append("")
            lines.append("### Conflicts (card vs index) — review these")
            lines.append("")
            lines.append("| page | card says | index says | margin no. |")
            lines.append("|---|---|---|---|")
            for c in r["conflicts"][:40]:
                lines.append(f"| {c['page']} | {c['card_says']} | {c['index_says']} "
                             f"| {c['margin_page_number']} |")
        lines.append("")
    (run_dir / "anchor_map.md").write_text("\n".join(lines), encoding="utf-8")


def _write_survey_md(run_dir: Path, rows: list[dict]) -> None:
    ok = [r for r in rows if not r.get("error")]
    tot_m = sum(r["n_with_margin_number"] for r in ok)
    tot_s = sum(r["n_sampled"] for r in ok)

    lines = ["# GLO title-card survey", "",
             "Which volumes carry the archivist's photographic title card "
             "(white text on black) in the top margin, and which carry a volume "
             "page number there? Sampled pages only.", "",
             f"**Margin page number: {tot_m}/{tot_s} sampled pages "
             f"({100 * tot_m / max(tot_s, 1):.0f}%)** — present across every volume "
             "family, including all of the ones with no card at all. That makes the "
             "margin number the universal anchor and the card a corroborator where "
             "it happens to exist.", "",
             "`solid` = card-shaped text (a township AND a range, in the card's "
             "printed order) on 3+ sampled pages. `weak` = 1-2. `none` = no "
             "card-shaped text; where anything was transcribed it was a handwritten "
             "page header, not a card.", ""]

    for verdict in ("solid", "weak", "none"):
        group = [r for r in ok if r.get("card_verdict") == verdict]
        lines += [f"## {verdict} ({len(group)} volumes)", "",
                  "| volume | PDF pages | card-shaped | any card text | margin no. | example |",
                  "|---|---|---|---|---|---|"]
        for r in group:
            ex = (r.get("example_card") or "").replace("|", "/").replace("\n", " ")[:44]
            lines.append(f"| {r['document']} | {r['n_pdf_pages']} | "
                         f"{r.get('n_card_shaped', 0)}/{r['n_sampled']} | "
                         f"{r['n_with_card']}/{r['n_sampled']} | "
                         f"{r['n_with_margin_number']}/{r['n_sampled']} | {ex} |")
        lines.append("")

    errs = [r for r in rows if r.get("error")]
    if errs:
        lines.append("## errors")
        lines += [f"- {r['document']}: {r['error']}" for r in errs]
    (run_dir / "card_survey.md").write_text("\n".join(lines), encoding="utf-8")


def _pdf_for(doc_name: str, pdf_dir: Path) -> Path | None:
    p = pdf_dir / f"{doc_name}.pdf"
    return p if p.exists() else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, metavar="PATH")
    ap.add_argument("--pdf-dir", default=None, metavar="PATH",
                    help="Directory of ORIGINAL source PDFs (not _ocr.pdf)")
    ap.add_argument("--output-dir", default=None, metavar="PATH")
    ap.add_argument("--report", default=None, metavar="NAME",
                    help="Process only this document (exact stem)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N documents")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Read only the first N pages of each document")
    ap.add_argument("--survey", action="store_true",
                    help="Card-presence survey only; no full per-page pass")
    ap.add_argument("--survey-pages", type=int, default=None,
                    help="Pages sampled per volume in survey mode (default 6)")
    ap.add_argument("--seed", type=int, default=20260914,
                    help="Survey sampling seed, so a survey is reproducible")
    args = ap.parse_args()

    cfg: dict = {}
    cfg_path = None
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            sys.exit(f"Config not found: {cfg_path}")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    pdf_dir_str  = args.pdf_dir    or cfg.get("pdf_dir")
    output_dir   = args.output_dir or cfg.get("output_dir", "runs")
    model        = args.model      or cfg.get("model", DEFAULT_MODEL)
    base_url     = cfg.get("base_url", DEFAULT_BASE_URL)
    temperature  = float(cfg.get("temperature", 0.0))
    timeout      = float(cfg.get("timeout_seconds", 300))
    num_ctx      = int(cfg.get("num_ctx", 8192))
    strip_frac   = float(cfg.get("strip_frac", DEFAULT_STRIP_FRAC))
    scan_pages   = int(cfg.get("index_scan_pages", DEFAULT_INDEX_SCAN_PAGES))
    index_max_dim = int(cfg.get("index_max_dim", DEFAULT_INDEX_MAX_DIM))
    ocr_dir_str   = cfg.get("ocr_dir")
    ocr_dir       = Path(ocr_dir_str) if ocr_dir_str else None
    survey_pages = args.survey_pages or int(cfg.get("survey_pages", 6))
    batch        = int(cfg.get("render_batch", 25))

    if not pdf_dir_str:
        sys.exit("--pdf-dir is required (or set pdf_dir in config)")
    pdf_dir = Path(pdf_dir_str)
    if not pdf_dir.is_dir():
        sys.exit(f"Not a directory: {pdf_dir}")

    for f in (ANCHOR_PROMPT_FILE, INDEX_PROMPT_FILE):
        if not f.exists():
            sys.exit(f"Prompt not found: {f}")
    anchor_prompt = ANCHOR_PROMPT_FILE.read_text(encoding="utf-8")
    index_prompt  = INDEX_PROMPT_FILE.read_text(encoding="utf-8")

    pdfs = sorted(p for p in pdf_dir.glob("*.pdf"))
    if args.report:
        pdfs = [p for p in pdfs if p.stem == args.report]
        if not pdfs:
            sys.exit(f"Document not found: {args.report}")
    if args.limit:
        pdfs = pdfs[:args.limit]
    if not pdfs:
        sys.exit(f"No PDFs found under: {pdf_dir}")

    run_dir    = _make_run_dir(Path(output_dir))
    model_slug = _slug(model)

    if cfg_path and cfg_path.exists():
        shutil.copy(cfg_path, run_dir / cfg_path.name)
    else:
        (run_dir / "config_snapshot.yaml").write_text(
            yaml.dump({"pdf_dir": str(pdf_dir), "output_dir": output_dir,
                       "model": model, "strip_frac": strip_frac,
                       "index_scan_pages": scan_pages}, sort_keys=False),
            encoding="utf-8")

    (run_dir / "prompts.yaml").write_text(
        yaml.dump({
            "method": "vision_page_anchors",
            "model": model,
            "strip_frac": strip_frac,
            "index_scan_pages": scan_pages,
            "index_max_dim": index_max_dim,
            "mode": "survey" if args.survey else "full",
            "segmentation_prompts": {
                "glo_page_anchor": anchor_prompt,
                "glo_index_table": index_prompt,
            },
        }, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8")

    print(f"\nRun dir      : {run_dir}")
    print(f"Mode         : {'survey' if args.survey else 'full evidence map'}")
    print(f"Model        : {model}  (strip {strip_frac:.0%} of page height)")
    print(f"Documents    : {len(pdfs)}\n")

    reset_stats()
    run_started = datetime.now(timezone.utc)
    inventory_rows: list[dict] = []

    # ---------------- survey mode ----------------
    if args.survey:
        rng = random.Random(args.seed)
        rows = []
        for i, pdf in enumerate(pdfs, 1):
            t0 = time.time()
            print(f"[{i}/{len(pdfs)}] {pdf.stem[:60]}", flush=True)
            res = survey_volume(pdf, anchor_prompt, model, base_url, temperature,
                                timeout, num_ctx, strip_frac, survey_pages, rng)
            res["document"] = pdf.stem
            rows.append(res)
            if res.get("error"):
                print(f"    ERROR: {res['error']}")
            else:
                print(f"    card on {res['n_with_card']}/{res['n_sampled']} sampled, "
                      f"margin no. on {res['n_with_margin_number']}/{res['n_sampled']}"
                      f"  -> has_card={res['has_card']}  ({time.time()-t0:.0f}s)")

        out_path = run_dir / f"{model_slug}__card_survey.json"
        out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        _write_survey_md(run_dir, rows)
        inventory_rows.append({
            "run_id": run_dir.name, "tool": "segment_glo_page_anchors.py:survey",
            "model": model, "file_name": out_path.name,
            "file_path": str(out_path), "source_input": str(pdf_dir),
            "prompt_file": str(ANCHOR_PROMPT_FILE),
            "prompt_snapshot_key": "glo_page_anchor",
            "temperature": temperature, "num_ctx": num_ctx,
            "chunk_strategy": f"survey sample of {survey_pages} pages/volume, seed {args.seed}",
            "produced_at": datetime.now(timezone.utc).isoformat(),
            "output_file_path": str(out_path),
        })
        results_summary = {"mode": "survey", "n_documents": len(rows),
                           "n_with_card": sum(1 for r in rows if r.get("has_card"))}

    # ---------------- full evidence map ----------------
    else:
        results = []
        for i, pdf in enumerate(pdfs, 1):
            t0 = time.time()
            print(f"[{i}/{len(pdfs)}] {pdf.stem[:60]}", flush=True)
            try:
                import pymupdf as fitz
                doc = fitz.open(str(pdf))
                n_pdf_pages = len(doc)
                doc.close()
            except Exception as e:
                print(f"    [ERROR] could not open: {e}")
                continue

            index = read_index(pdf, index_prompt, model, base_url, temperature,
                               timeout, num_ctx, scan_pages, n_pdf_pages,
                               max_dim=index_max_dim)

            # Independent second read of the index from OCR table geometry,
            # where this volume has been OCR'd with a pdf_ocr new enough to
            # emit `tables` (2026-09+). Absent for older corpora; that is
            # normal and simply leaves the cross-check unreported.
            if ocr_dir and index.get("found"):
                ocr_json = ocr_dir / pdf.stem / "ocr_docling.json"
                if ocr_json.exists():
                    tbl_read = read_index_from_tables(ocr_json, index["page"])
                    if tbl_read:
                        cmp = _compare_index_reads(index["rows"], tbl_read["rows"])
                        index["table_read"] = tbl_read
                        index["vision_vs_tables"] = cmp
                        print(f"    index cross-check vs OCR table geometry "
                              f"({tbl_read['grid']}): {cmp['n_agree']} agree, "
                              f"{cmp['n_disagree']} disagree, "
                              f"{cmp['n_vision_only']} vision-only")
                    else:
                        index["table_read"] = None

            page_list = list(range(n_pdf_pages))
            if args.max_pages:
                page_list = page_list[:args.max_pages]

            recs = read_page_anchors(pdf, page_list, anchor_prompt, model, base_url,
                                     temperature, timeout, num_ctx, strip_frac,
                                     batch=batch)
            n_sus = flag_margin_outliers(recs)
            if n_sus:
                print(f"    {n_sus} margin page number(s) flagged as out of sequence")
            pages, conflicts, coverage = build_evidence_map(recs, index)

            result = {
                "document": pdf.stem,
                "source_pdf": str(pdf),
                "method": "vision_page_anchors",
                "model": model,
                "read_at": datetime.now(timezone.utc).isoformat(),
                "n_pdf_pages": n_pdf_pages,
                "index": index,
                "coverage": coverage,
                "conflicts": conflicts,
                "pages": pages,
            }
            out_path = run_dir / f"{model_slug}__{pdf.stem}.anchors.json"
            out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            results.append(result)

            print(f"    -> {coverage['pct_any_evidence']}% of pages have evidence; "
                  f"corroborated {coverage['corroborated']}, conflict {coverage['conflict']} "
                  f"({time.time()-t0:.0f}s)")

            inventory_rows.append({
                "run_id": run_dir.name, "tool": "segment_glo_page_anchors.py",
                "model": model, "file_name": out_path.name,
                "file_path": str(out_path), "source_input": str(pdf),
                "prompt_file": f"{ANCHOR_PROMPT_FILE.name}+{INDEX_PROMPT_FILE.name}",
                "prompt_snapshot_key": "glo_page_anchor+glo_index_table",
                "temperature": temperature, "num_ctx": num_ctx,
                "chunk_strategy": f"one call per page top strip (frac {strip_frac})",
                "produced_at": datetime.now(timezone.utc).isoformat(),
                "output_file_path": str(out_path),
            })

        _write_anchor_map_md(run_dir, results)
        results_summary = {
            "mode": "full",
            "n_documents": len(results),
            "n_pages": sum(r["coverage"]["n_pages"] for r in results),
            "n_corroborated": sum(r["coverage"]["corroborated"] for r in results),
            "n_conflict": sum(r["coverage"]["conflict"] for r in results),
        }

    stats = get_stats()
    (run_dir / "run_metadata.json").write_text(json.dumps({
        "run_id": run_dir.name,
        "tool": "segment_glo_page_anchors.py",
        "method": "vision_page_anchors",
        "mode": "survey" if args.survey else "full",
        "model": model,
        "base_url": base_url,
        "temperature": temperature,
        "num_ctx": num_ctx,
        "strip_frac": strip_frac,
        "index_scan_pages": scan_pages,
        "index_max_dim": index_max_dim,
        "pdf_dir": str(pdf_dir),
        "ocr_dir": str(ocr_dir) if ocr_dir else None,
        "started_at": run_started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "token_stats": stats,
        "summary": results_summary,
    }, indent=2), encoding="utf-8")

    write_inventory_csv(run_dir, inventory_rows)

    print(f"\nDone. {results_summary}")
    print(f"Run directory -> {run_dir}")
    print(f"Review        -> {run_dir / ('card_survey.md' if args.survey else 'anchor_map.md')}")


if __name__ == "__main__":
    main()
