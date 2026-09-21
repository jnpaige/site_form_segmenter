#!/usr/bin/env python3
"""
evaluate_with_opus.py — independent diagnosis of the procedural-unit pipeline,
using Claude Opus 5 to produce the reference answers a human would otherwise
have to code by hand.

WHAT THIS IS FOR
The pipeline's local passes (qwen2.5:32b via Ollama) produce cluster/section
output; nothing independent says whether that output is right. Hand-coding
gold labels for even a dozen papers is a lot of expert time. This asks a
stronger model to read each paper and report what the assemblages actually
are, which tables and figures belong to which, and where the pipeline went
wrong — with a verbatim quote and page number behind every claim, so the
answers can be spot-checked rather than trusted.

  This is a DEVELOPMENT-TIME EVALUATION TOOL, NOT A PIPELINE STAGE.
  The pipeline itself stays local. This grades it.

=============================================================================
 SCOPE RESTRICTION — PUBLISHED LITERATURE ONLY
=============================================================================
This script sends document text and page images to Anthropic's API, i.e. off
this machine. That is acceptable for published, peer-reviewed papers, which
are public documents already.

It must NEVER be run against site forms, Kisatchie/CRS material, or any other
North American raw site data: those record site locations and are not ours to
transmit. That restriction is enforced below by `assert_publishable()` rather
than left to whoever runs it — an allowlist of corpus roots, a check for
sensitive path markers, and a scan for Smithsonian trinomials in the text. The
guard fails closed: anything it cannot confirm is publishable is refused.

If you find yourself wanting to relax the guard, that is the signal to stop
and use the local pipeline instead.
=============================================================================

Usage:
    export ANTHROPIC_API_KEY=...        # or: ant auth login
    uv run python evaluate_with_opus.py --doc-dir "<one document folder>"
    uv run python evaluate_with_opus.py --input-dir "<corpus>" --batch
"""
import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

MODEL = "claude-opus-5"

# Only these corpus roots may be sent. Both hold published journal articles.
ALLOWED_ROOTS = [
    Path(r"C:\Users\jpaige\Box\4. Research\LLM_segmenting_procedural_units\pdfs_ocr_figures_pilot"),
    Path(r"C:\Users\jpaige\Box\4. Research\LLM_segmenting_procedural_units\pdfs_ocr_figures_holdout"),
]

# Path fragments that indicate site data rather than published literature.
FORBIDDEN_PATH_MARKERS = (
    "kisatchie", "site_form", "siteform", "local_repositories",
    "local_research_repositories", "crs_data", "archaeology_site_forms",
    "jblm", "merrit", "piedmont", "usfws",
)

# Smithsonian trinomial (e.g. 16WN385): state number, county letters, site
# number. Its presence in the text means North American site records.
TRINOMIAL_RE = re.compile(r"\b\d{2}[A-Z]{2}\d{1,5}\b")


class ScopeError(RuntimeError):
    pass


def assert_publishable(doc_dir: Path, md_text: str) -> None:
    """Refuse anything not confirmably published literature. Fails closed."""
    resolved = doc_dir.resolve()

    if not any(_is_within(resolved, root.resolve()) for root in ALLOWED_ROOTS):
        raise ScopeError(
            f"{doc_dir.name}: not inside an allowed published-papers corpus.\n"
            "This script may only be run on published, peer-reviewed papers. "
            "Site forms and other North American raw site data must never be "
            "sent off this machine — use the local pipeline for those."
        )

    lowered = str(resolved).lower()
    for marker in FORBIDDEN_PATH_MARKERS:
        if marker in lowered:
            raise ScopeError(f"{doc_dir.name}: path contains '{marker}' — refusing.")

    hits = sorted(set(TRINOMIAL_RE.findall(md_text)))[:5]
    if hits:
        raise ScopeError(
            f"{doc_dir.name}: text contains what look like Smithsonian "
            f"trinomials ({', '.join(hits)}), which indicate North American "
            "site records. Refusing.\n"
            "If this is a published paper that merely cites trinomials, "
            "confirm that by hand and add it to an explicit exception — do not "
            "loosen the pattern."
        )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------

SYSTEM = """\
You are auditing an automated pipeline that reads archaeology journal articles
and identifies which assemblage/layer contexts each paper reports, then tags
every paragraph, table row and figure with the context(s) it concerns.

HOW TO THINK ABOUT "CONTEXT" IN THIS DOMAIN
Archaeological papers organize their material along two independent axes, and
conflating them is the main way this analysis goes wrong.

1. CONTAINMENT — a nesting hierarchy, coarse to fine:
      industry_or_technocomplex   e.g. "the Makgadikgadi MSA industry", "Aurignacian"
      region_or_basin             e.g. "Ntwetwe Pan", "Kapthurin Formation"
      site                        e.g. MAK33, Koimilot, GhGk-63
      locus_or_area               e.g. "Locus 1", "Trench 2", "Area A"
      layer_or_unit               e.g. "Layer 3a", "Bed C", "levels IId-IIf"
      sub_assemblage              a partition within a layer
      artifact_set                a refit set, a raw material group's pieces
      individual_artifact         a single specimen
   Papers populate these levels inconsistently. Some report only at industry
   level; some go site -> locus; some site -> layer; some only ever describe
   one undivided assemblage. Record only the levels the paper actually uses.

2. CROSS-CUTTING FACETS — dimensions that slice the material instead of
   nesting inside it: raw_material, artefact_class, reduction_stage,
   technique, chronological_phase, refit_set.
   A table of "core stage x reduction approach" is NOT about any particular
   assemblage — it describes a procedural dimension that applies across units.
   Recording such a table as belonging to no context is wrong; it belongs to
   a facet. This distinction matters more than any other in this audit,
   because procedural/technological facets are what the pipeline ultimately
   exists to capture.

A single item can carry both: "silcrete points from MAK33" is unit=MAK33 plus
facets raw_material=silcrete, artefact_class=points.

GRANULARITY IS OFTEN GENUINELY AMBIGUOUS - SAY SO
A paper describing five sites as one industry can legitimately be read as one
context or as six (the industry plus each site). Do not force a verdict.
Report each defensible reading, say which the paper's own framing favours, and
say what each preserves or loses. Note that finer readings are
information-preserving: a block tagged at site level still rolls up to the
industry, while a block tagged only at industry level can never be recovered
to site level. Where the pipeline's output matches ANY defensible reading,
that is not a failure - mark failure_kind "ambiguous_not_a_failure".

You are given, for one paper:
  1. The original PDF.
  2. The OCR'd markdown the pipeline actually read (this may be degraded — the
     layout model sometimes loses headings or table structure entirely).
  3. The pipeline's own output for that paper.

Your job is twofold.

FIRST, report the ground truth from the PDF: what assemblages/contexts does
this paper actually report? Which tables and figures relate to which? A paper
reporting a single undivided assemblage is common and correct — say so plainly
rather than inventing subdivisions.

SECOND, assess the pipeline's output against that truth, and — this is the
part that matters most — distinguish two kinds of failure:
  - OCR/extraction failures: the information never reached the pipeline
    because the markdown lost it (missing headings, unparsed tables).
  - Logic failures: the information was present in the markdown and the
    pipeline still got it wrong.
Compare the PDF against the markdown to tell these apart. Saying which kind a
failure is determines whether it gets fixed upstream or in the prompts.

Every factual claim you make must carry a short verbatim quote from the paper
and the page it appears on, so a human can check you without re-reading the
article. Quotes are checked programmatically against the source; do not
paraphrase them. If you are unsure about something, say so in `confidence`
rather than guessing — an honest "unclear" is more useful here than a
confident answer that has to be independently re-verified anyway.
"""

LEVELS = ["industry_or_technocomplex", "region_or_basin", "site", "locus_or_area",
          "layer_or_unit", "sub_assemblage", "artifact_set", "individual_artifact"]
FACETS = ["raw_material", "artefact_class", "reduction_stage", "technique",
          "chronological_phase", "refit_set", "other"]

_UNIT_REF = {"type": "array", "items": {"type": "string"},
             "description": "unit_id values from `units`"}


def _evidence(props: dict) -> dict:
    props.update({
        "evidence_quote": {"type": "string"},
        "evidence_page": {"type": "integer"},
    })
    return props


SCHEMA = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "description": ("Every context the paper reports, at whatever levels it "
                            "actually uses. Nest them with parent_unit_id."),
            "items": {
                "type": "object",
                "properties": _evidence({
                    "unit_id": {"type": "string"},
                    "label": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "level": {"type": "string", "enum": LEVELS},
                    "parent_unit_id": {"type": ["string", "null"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                }),
                "required": ["unit_id", "label", "aliases", "level", "parent_unit_id",
                             "confidence", "evidence_quote", "evidence_page"],
                "additionalProperties": False,
            },
        },
        "levels_with_data": {
            "type": "array", "items": {"type": "string", "enum": LEVELS},
            "description": ("Levels the paper actually breaks its data out by, as "
                            "opposed to merely mentioning in passing."),
        },
        "facets": {
            "type": "array",
            "description": ("Cross-cutting dimensions, which slice the material rather "
                            "than nesting inside the containment hierarchy."),
            "items": {
                "type": "object",
                "properties": _evidence({
                    "facet": {"type": "string", "enum": FACETS},
                    "values": {"type": "array", "items": {"type": "string"}},
                    "cuts_across_level": {"type": "string", "enum": LEVELS},
                }),
                "required": ["facet", "values", "cuts_across_level",
                             "evidence_quote", "evidence_page"],
                "additionalProperties": False,
            },
        },
        "defensible_readings": {
            "type": "array",
            "description": ("Each legitimate way to carve this paper into contexts. More "
                            "than one entry means granularity is genuinely ambiguous and "
                            "no single answer is correct."),
            "items": {
                "type": "object",
                "properties": {
                    "reading_id": {"type": "string"},
                    "level": {"type": "string", "enum": LEVELS},
                    "unit_ids": _UNIT_REF,
                    "favoured_by_paper_framing": {"type": "boolean"},
                    "preserves": {"type": "string"},
                    "loses": {"type": "string"},
                },
                "required": ["reading_id", "level", "unit_ids",
                             "favoured_by_paper_framing", "preserves", "loses"],
                "additionalProperties": False,
            },
        },
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "caption": {"type": "string"},
                    "page": {"type": "integer"},
                    "unit_ids": _UNIT_REF,
                    "facets": {"type": "array", "items": {"type": "string", "enum": FACETS}},
                    "is_cross_cutting": {
                        "type": "boolean",
                        "description": ("True when the table describes a facet (e.g. "
                                        "reduction stages) rather than belonging to "
                                        "particular units. Such a table must never be "
                                        "recorded as simply contextless."),
                    },
                    "why": {"type": "string"},
                },
                "required": ["caption", "page", "unit_ids", "facets",
                             "is_cross_cutting", "why"],
                "additionalProperties": False,
            },
        },
        "figures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "caption": {"type": "string"},
                    "page": {"type": "integer"},
                    "unit_ids": _UNIT_REF,
                    "facets": {"type": "array", "items": {"type": "string", "enum": FACETS}},
                    "is_cross_cutting": {"type": "boolean"},
                    "why": {"type": "string"},
                },
                "required": ["caption", "page", "unit_ids", "facets",
                             "is_cross_cutting", "why"],
                "additionalProperties": False,
            },
        },
        "pipeline_assessment": {
            "type": "object",
            "properties": {
                "pipeline_level": {
                    "type": "string",
                    "description": "Containment level the pipeline's clusters sit at, or 'mixed'.",
                },
                "matches_reading_id": {
                    "type": ["string", "null"],
                    "description": ("The defensible reading the pipeline's output matches, "
                                    "or null if it matches none."),
                },
                "finer_level_available": {
                    "type": "boolean",
                    "description": ("True if the paper supports a finer level than the "
                                    "pipeline used, i.e. detail was discarded that cannot "
                                    "be recovered by rolling up."),
                },
                "units_missing": _UNIT_REF,
                "spurious_units": {"type": "array", "items": {"type": "string"}},
                "unmerged_duplicates": {"type": "array", "items": {"type": "string"}},
                "pages_wrongly_dropped": {"type": "array", "items": {"type": "integer"}},
                "cross_cutting_items_left_untagged": {
                    "type": "array", "items": {"type": "string"},
                    "description": ("Tables or figures describing a facet that the pipeline "
                                    "left untagged because it had no way to express one."),
                },
                "failure_kind": {
                    "type": "string",
                    "enum": ["none", "ambiguous_not_a_failure", "ocr_extraction",
                             "pipeline_logic", "both"],
                },
                "notes": {"type": "string"},
            },
            "required": ["pipeline_level", "matches_reading_id", "finer_level_available",
                         "units_missing", "spurious_units", "unmerged_duplicates",
                         "pages_wrongly_dropped", "cross_cutting_items_left_untagged",
                         "failure_kind", "notes"],
            "additionalProperties": False,
        },
    },
    "required": ["units", "levels_with_data", "facets", "defensible_readings",
                 "tables", "figures", "pipeline_assessment"],
    "additionalProperties": False,
}


def build_content(doc_dir: Path, md_text: str, with_pdf: bool) -> list[dict]:
    content: list[dict] = []

    pdf = next(doc_dir.glob("*_ocr.pdf"), None)
    if with_pdf and pdf and pdf.stat().st_size < 30 * 1024 * 1024:
        content.append({
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(pdf.read_bytes()).decode(),
            },
        })

    tags_path = next(doc_dir.glob("*.cluster_tags.json"), None)
    pipeline_output = "(the pipeline has not been run on this document)"
    if tags_path:
        data = json.loads(tags_path.read_text(encoding="utf-8"))
        pipeline_output = json.dumps({
            "clusters": data.get("clusters"),
            "pass0_section_map": data.get("pass0_result"),
            "n_blocks": data.get("n_blocks"),
            "headings_recovered": data.get("n_headings_recovered"),
            "block_tags_sample": (data.get("block_tags") or [])[:400],
        }, ensure_ascii=False, indent=1)

    content.append({"type": "text", "text": (
        "=== OCR'D MARKDOWN THE PIPELINE READ ===\n"
        f"{md_text}\n\n"
        "=== THE PIPELINE'S OUTPUT FOR THIS PAPER ===\n"
        f"{pipeline_output}\n\n"
        "Report the ground truth from the PDF, then assess the pipeline's "
        "output against it, distinguishing OCR/extraction failures from "
        "pipeline logic failures."
    )})
    return content


def verify_quotes(result: dict, md_text: str, pdf_present: bool) -> list[str]:
    """Flag evidence quotes that don't occur in the OCR text.

    Not a hard failure: a quote may be correct against the PDF and absent from
    the markdown precisely because OCR dropped it — which is itself a finding.
    Reported so a human knows which claims to check against the PDF directly.
    """
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s or "").strip().lower()

    haystack = norm(md_text)
    unverified = []
    for a in result.get("units", []):
        q = norm(a.get("evidence_quote", ""))
        if q and q not in haystack:
            unverified.append(f"{a.get('label')}: {a.get('evidence_quote', '')[:60]}")
    return unverified


def evaluate(doc_dir: Path, client, with_pdf: bool) -> dict | None:
    md = next(doc_dir.glob("*.md"), None)
    if md is None:
        print(f"  SKIP {doc_dir.name} — no markdown")
        return None
    md_text = md.read_text(encoding="utf-8")

    assert_publishable(doc_dir, md_text)

    # Citations (which would attach page locations automatically) are
    # incompatible with structured outputs and return a 400, so the schema
    # carries explicit quote+page fields instead and they are verified here.
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": build_content(doc_dir, md_text, with_pdf)}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        print(f"  [warn] {doc_dir.name}: request refused "
              f"({getattr(message.stop_details, 'category', None)})")
        return None

    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        print(f"  [warn] {doc_dir.name}: response was not valid JSON")
        return None

    result["_document"] = doc_dir.name
    result["_unverified_quotes"] = verify_quotes(result, md_text, with_pdf)
    result["_usage"] = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }
    return result


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--doc-dir")
    g.add_argument("--input-dir")
    ap.add_argument("--out", default="opus_evaluation.json")
    ap.add_argument("--no-pdf", action="store_true",
                    help="Send only the OCR markdown (cheaper; loses the ability "
                         "to tell OCR failures from logic failures)")
    args = ap.parse_args()

    try:
        import anthropic
    except ImportError:
        sys.exit("pip install anthropic  (or: uv run --with anthropic ...)")

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("[note] no ANTHROPIC_API_KEY in the environment — the SDK will try "
              "an `ant auth login` profile, and fail if there isn't one.")
    client = anthropic.Anthropic()

    dirs = ([Path(args.doc_dir)] if args.doc_dir else
            sorted(d for d in Path(args.input_dir).iterdir()
                   if d.is_dir() and not d.name[0].isdigit()))

    results, refused = [], []
    for d in dirs:
        print(f"  {d.name}")
        try:
            result = evaluate(d, client, with_pdf=not args.no_pdf)
        except ScopeError as e:
            print(f"    REFUSED: {e}")
            refused.append(d.name)
            continue
        if result:
            results.append(result)
            pa = result["pipeline_assessment"]
            print(f"    assemblages={len(result['assemblages'])} "
                  f"reading={pa['matches_reading_id']} "
                  f"failure_kind={pa['failure_kind']}")
            if result["_unverified_quotes"]:
                print(f"    [check] {len(result['_unverified_quotes'])} quote(s) "
                      "not found in the OCR text — verify against the PDF")

    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    total_in = sum(r["_usage"]["input_tokens"] for r in results)
    total_out = sum(r["_usage"]["output_tokens"] for r in results)
    print(f"\n{len(results)} evaluated, {len(refused)} refused -> {args.out}")
    print(f"tokens: {total_in:,} in / {total_out:,} out "
          f"(~${total_in/1e6*5 + total_out/1e6*25:.2f})")


if __name__ == "__main__":
    main()
