# site_form_segmenter

Louisiana archaeological site form PDFs regularly contain multiple investigations in a single file — a 2016 update appended behind a 1996 original, for example. Before those documents can be reliably coded against a standardized codebook, you need to know where each investigation starts and ends, and which pages within each investigation are the structured form, the narrative, and the NRHP eligibility discussion. This tool solves that segmentation problem as a standalone step, separate from any coding pipeline. Its output — a page map showing investigation boundaries and page-type assignments — feeds into [text_coding_program](https://github.com/jnpaige/text_coding_program) for human coding and into downstream extraction and coding tools that need to know which pages to read for which purpose.

This tool takes the output of [pdf_ocr](https://github.com/jnpaige/pdf_ocr) as its input. Every site directory produced by pdf_ocr contains a `text_docling.txt` file with page-indexed OCR text and an `_ocr.pdf` file with a searchable text layer. The segmenter reads both.

---

## What it does

The segmenter runs four focused passes over each document, each handling one narrow question. Pass 1 identifies where each investigation begins and ends and which pages belong to it. If Pass 1 leaves any pages unclaimed — which happens most often with page 0 or multi-page gaps in long documents — a gap-fill step runs a second prompt over just those pages and assigns them to the nearest investigation. Passes 2 through 4 work within each investigation: finding the structured site record form page, the narrative prose pages, and the NRHP eligibility discussion pages respectively.

Dividing segmentation into focused passes lets a small model handle each one reliably. Pass 1 is the hardest because it has to detect where one investigation ends and the next begins, which is where the vision mode option helps. Passes 2–4 are straightforward page-type classification tasks that an 8b text model handles well.

The page types that come out of segmentation — `form_pages`, `narrative_pages`, `nrhp_pages`, and the full `pages` list — are discovered dynamically by downstream tools. Any key ending in `_pages` in the segment JSON is treated as a valid page scope, so the system generalizes to new page types without code changes.

---

## Two modes

In vision mode, a vision model receives a single contact sheet image for Pass 1 — all PDF pages tiled in a grid with each thumbnail labeled with its page number in red. The vision model sees the full visual layout of the document at once and returns investigation boundaries by page number. Passes 2–4 then run on OCR text with a text model. Vision mode requires PyMuPDF and the original PDF file alongside `text_docling.txt`. If vision mode is selected but no PDF is found for a site, that site automatically falls back to text mode for Pass 1.

In text mode, all four passes run on OCR text only — no PDF rendering, no PyMuPDF. This is faster, uses less memory, and is a good baseline for comparing against vision results.

---

## Setup

### 1. Install Ollama and pull models

Install Ollama from [ollama.com](https://ollama.com), then pull the models you plan to use:

```powershell
ollama pull qwen2.5:14b          # recommended text model for all passes
ollama pull llama3.2-vision:11b  # vision pass 1 only (~7.8 GB)
```

### 2. Install uv and sync dependencies

```powershell
# Windows — install uv once per machine
winget install astral-sh.uv

# In the repo directory
uv sync
```

```bash
# Mac/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

If you prefer to activate the venv manually instead of using `uv run`, run `.venv\Scripts\activate` on Windows or `source .venv/bin/activate` on Mac/Linux, then use `python segmenter.py` directly.

### 3. Configure paths

Edit `config.yaml`:

```yaml
input_dir: 'path/to/pdf_ocr/output'
mode: 'text'             # or 'vision'
text_model: 'qwen2.5:14b'
vision_model: 'llama3.2-vision:11b'
```

---

## Usage

```powershell
# All sites, mode from config.yaml
uv run python segmenter.py

# Single site — fastest way to verify segmentation quality before a full run
uv run python segmenter.py --trinomial 16WN385

# Text mode only
uv run python segmenter.py --mode text

# Vision mode
uv run python segmenter.py --mode vision

# Use a different config file
uv run python segmenter.py --config config_test.yaml
```

A good first-run workflow is to process one site in text mode first to confirm the pipeline works end-to-end, then check the `segmentation_map.md` to verify the investigation boundaries look right, then try the same site in vision mode to compare whether vision improves Pass 1 boundary detection.

Every invocation creates its own new run folder — there is no flag to re-run into or resume an existing one. If a run is interrupted partway, re-run with `--trinomial` for the remaining sites; that run's outputs land in a separate folder, and each folder's `run_metadata.json` records which sites it covers so you can cross-reference across folders.

---

## Output

Each run creates its own directory, `runs/<YYYYMMDD_HHMM>_<gitsha>/`, and nothing is ever appended to or overwritten in an existing one. The folder is flat — no per-model subdirectories — and every output filename is prefixed with the model (or model pair) that actually produced it, e.g. `text__qwen2_5_14b__16WN385.segments.json`. This matters because a single run can still mix models: a vision-mode run falls back to text for any site missing a source PDF, and that site's filename gets an explicit `__fallback-from-vision` suffix so it's never mistaken for a real vision-mode result.

The primary output for human review is `segmentation_map.md`, which shows one section per site listing each investigation, its page range, its classified page types, and the model that produced it. The machine-readable equivalent is `segments.csv` with one row per investigation, also carrying a `model` column. Each site gets a `<model_slug>__<trinomial>.segments.json` file with the full structured output, including `segmented_at`, `text_model`, `vision_model`, `effective_mode`, and `fallback_from_vision`. The run folder also contains a `config.yaml` snapshot, a `prompts.yaml` text snapshot of every prompt used, `run_metadata.json` (run-level stats plus the adaptive-context/chunking parameters in effect), and `inventory.csv` — one row per output file summarizing model, prompt, chunking, and file path, generated automatically at the end of every run.

---

## Run metadata standard

Both scripts in this repo — `segmenter.py` (site forms, above) and `segment_reports_pass0.py` (reports, see [Report segmentation](#report-segmentation) below) — follow the same run-metadata convention as the other pdf_ocr downstream tools ([site_coder](), site_vocab_extractor). See [pdf_ocr's README](https://github.com/jnpaige/pdf_ocr#run-metadata-standard) for the full rationale. In short:

- Every invocation gets its own new run folder — `runs/<YYYYMMDD_HHMM>_<gitsha>/` — never reused or appended to.
- The folder is flat; every filename is prefixed with the model(s) that produced it (site-form naming including the `__fallback-from-vision` case is in [Output](#output) above; report naming is in [Report segmentation](#report-segmentation) below).
- Every run folder carries a config snapshot, a `prompts.yaml` text snapshot, `run_metadata.json` (with a `chunking` block), and `inventory.csv`, using the shared column schema: `run_id, tool, model, file_name, file_path, source_input, prompt_file, prompt_snapshot_key, temperature, num_ctx, chunk_strategy, produced_at, output_file_path`. Fields a given row can't determine are written as `not recorded`.
- `generate_inventory.py` backfills `inventory.csv` for runs made before this convention existed (including the old nested `<run>/<model>/<file>` layout — see `--help` for usage), for either script's output.

---

## Configuration reference

```yaml
input_dir:         'path/to/docling_output'
output_dir:        'path/to/output'        # optional; defaults to ./runs if omitted
trinomial_pattern: '(\d{2}[A-Z]{2}\d+)'   # Louisiana format; adjust for other states

mode: 'vision'           # 'vision' or 'text'
page_truncation_chars: 3000  # chars of OCR text per page shown in text passes
pdf_thumb_width:       240   # width in pixels of each page thumbnail in the contact sheet

base_url:        'http://localhost:11434'
vision_model:    'llama3.2-vision:11b'
text_model:      'qwen2.5:14b'
temperature:     0.2
timeout_seconds: 1800
```

---

## Recommended models

The `qwen2.5:14b` model gives the best JSON instruction-following at its size and is the recommended default. The older `llama3.1:8b` works but produces less reliable JSON on ambiguous documents. For text passes 2–4, `phi4:14b` is also a strong option. If boundary detection quality is a priority, `qwen2.5:32b` is noticeably better than 14b at recognizing multi-investigation structure shifts and is worth the extra VRAM.

---

## Troubleshooting

If you see a `500 Internal Server Error` on vision Pass 1, check that Ollama is running and the model is fully loaded with `ollama list`. If the contact sheet image is very large due to a many-page PDF, reduce `pdf_thumb_width` in `config.yaml` to shrink the composite image. The script logs a fallback warning and continues rather than crashing — a 500 on Pass 1 means the whole document is treated as one investigation.

If `PyMuPDF is not installed` appears at startup, the most common cause is running `python segmenter.py` with a different project's venv active. Use `uv run python segmenter.py` instead — uv selects the correct venv automatically.

If the script hangs mid-run, it is most likely waiting for an Ollama response. Text model passes can take 30–120 seconds per investigation depending on page count and model speed. Interrupt with Ctrl+C; sites already written to disk are safe, but re-running the same invocation reprocesses everything in scope from scratch (there is no skip-if-exists check) — use `--trinomial` to target just the remaining sites, which will land in a new run folder.

---

## Report segmentation

Reports are a different document type from site forms and require a different segmentation approach. A single report may be 50 to 800+ pages and discuss anywhere from one to over a hundred sites. The segmenter handles reports through `segment_reports_pass0.py`, a separate script from the 4-pass site form pipeline. A full-text alternative approach is also available for reports where heading detection is sparse or unreliable.

### Why two strategies exist

For site forms, the segmenter sends the full page text of each document. Forms are short enough — typically 2–15 pages — that even a 50-page multi-investigation bundle fits comfortably in a 32k-token context window, and full page content is what makes investigation boundary detection reliable. The `_adaptive_page_trunc` mechanism in `segmenter.py` handles larger site form bundles automatically: it calculates the maximum characters-per-page that keeps the whole document visible in a single call, scaling down proportionally as page count grows. Every page remains visible; only its content is trimmed.

Reports cannot be handled the same way. A 400-page report's OCR output exceeds a 32k-token context window many times over, and even adaptive truncation at 200 characters per page produces such compressed snippets that the model is effectively reading only the first line of each page. Two strategies address this differently:

**Heading-based (pass 0, primary)** — Instead of page text, send only the heading map: the compact list of heading labels and page numbers extracted by pdf_ocr into `headings.json`, typically a few hundred lines regardless of document length. This keeps the prompt small while preserving the structural signal the model needs to identify section boundaries. Section starts are almost always explicitly headed, so heading text alone is usually sufficient. Very large reports with many headings are split into chunks of N headings, each processed in a separate call with results merged afterwards. This is the default for most reports.

**Full-text (pass 1, fallback)** — Send actual page content, augmented with all headings, applying adaptive truncation to fit the entire document in one call. This preserves richer evidence for section inference — body text, transition sentences, artifact descriptions — at the cost of reduced content fidelity on long documents. Intended for reports where pdf_ocr heading detection was sparse or failed, leaving `headings.json` too thin to drive pass 0 reliably. The `report_pass1_sections.txt` prompt is available for this purpose; a script that applies it with adaptive truncation is in development.

The practical trade-off is between coverage and content fidelity. Pass 0 sees the whole heading structure of a long document clearly, but only the headings. Pass 1 sees some body text per page across the whole document, but that content is increasingly truncated as document length grows. For most CRM reports — where section boundaries are explicitly marked with headings — pass 0 is the right choice. For poorly-headed documents (some 1970s–1980s reports use unnumbered all-caps headings that OCR occasionally misses), pass 1's content-based inference can recover what the heading map cannot.

---

### Heading-based approach (pass 0)

`segment_reports_pass0.py` reads `headings.json` from each report directory and sends the heading list to the LLM as a compact page-indexed outline. Each entry optionally includes a short excerpt of the first non-heading body text on the same page. This excerpt resolves a common ambiguity in older reports:

```
p   1  ABSTRACT          |  "The Research Institute, College of Pure..."
p   3  SIGNIFICANCE       |  "Upon completion of the Phase I survey..."
p   4  TABLE OF CONTENTS  |  "...........i   4  ..........ii   4"
```

A real section heading is followed by prose; a TOC listing is followed by dot-leaders and page numbers. This distinction lets the model reliably identify actual section starts even in documents from the early 1980s where the same heading appears on page 3 (TOC) and page 81 (actual section).

**Chunking for large reports.** Reports exceeding `chunk_size_headings` in their heading count are split into sequential heading slices, each processed as a separate LLM call. Results are merged by concatenating the entry lists for each section type. The threshold is set at the heading count of the largest single-call success observed in corpus testing — for the Kisatchie corpus this was 515 headings (Morehead et al. 2003, 591 pages). Reports below that threshold run as a single call; larger reports split automatically with no change in output format.

**Applied to the Kisatchie Phase II corpus.** The Kisatchie National Forest Phase II report corpus spans 44 reports from 1978 to 2025, ranging from 14 to 825 pages. Reports from the 1970s–1980s use short all-caps headings with no numbering; later reports use numbered hierarchical sub-chapters. The `report_pass0_sections.txt` prompt lists recognized labels for each section type across the full date range — both "SIGNIFICANCE" (1978) and "6.6 Recommendations" (2024) map to the same recommendations section. The `--config` flag replaces per-run CLI arguments for corpus-wide runs:

```yaml
# config_reports.yaml
input_dir:            'G:\path\to\reports_ocr_docling\All'
output_dir:           'runs'
model:                'qwen2.5:32b'
chunk_size_headings:  515   # reports exceeding this are split; 0 to disable
temperature:          0.05
timeout_seconds:      1800
num_ctx:              32768
```

```powershell
# Full corpus run
uv run python segment_reports_pass0.py --config config_reports.yaml

# Single report — fastest way to verify output before a corpus run
uv run python segment_reports_pass0.py --config config_reports.yaml --report "22-0479_Hartfield et al. 1978"

# Re-run one report with a bigger model (e.g. a report too large for the default model's context)
uv run python segment_reports_pass0.py --config config_reports.yaml --model qwen2.5:72b --report "22-7597_Zieschang et al. 2024"
```

Every invocation creates its own new, flat run folder — `runs/<YYYYMMDD_HHMM>_<gitsha>/` — and never reuses or appends to a previous one (there used to be a `--run-dir` flag for resuming into an existing folder; it was removed because it let `run_metadata.json` get silently overwritten across invocations). If a corpus run is interrupted, re-run with `--report` for the remaining reports; that invocation's outputs land in a separate folder, and each folder's `run_metadata.json` records exactly which reports it covers.

Output files are named `<model_slug>__<report_name>.segments.json`, so a report reprocessed with a different model (like the `qwen2.5:72b` example above) produces a second, distinct file rather than overwriting the first — both are kept, and the filename alone tells you which model produced which. The format is the same shared segments.json structure used by the site form pipeline, with section types encoded as `<section>_pages` keys rather than investigation-level page types, plus `model`, `segmented_at`, `chunked`, and `n_chunks` fields recording exactly what produced that file:

```json
{
  "report": "22-0479_Hartfield et al. 1978",
  "model": "qwen2.5:32b",
  "segmented_at": "2026-07-07T14:22:03",
  "chunked": false,
  "n_chunks": 1,
  "n_headings": 339,
  "n_pages": 180,
  "segments": [
    {
      "label": "22-0479_Hartfield et al. 1978",
      "pages": [1, 2, 67, 68, 69, 70, 71, 73, 74, 75, 76, 77, 78, 79, 81, 83, 85],
      "executive_summary_pages": [1, 2],
      "methods_pages":           [67, 68, 69, 70, 71],
      "results_pages":           [73, 74, 75, 76, 77, 78, 79],
      "recommendations_pages":   [81, 83, 85],
      "_section_detail": {
        "executive_summary": [{"label": "ABSTRACT", "pages": [1]}, {"label": "MANAGEMENT SUMMARY", "pages": [2]}],
        "recommendations":   [{"label": "RECORDED SITES", "pages": [81]}, {"label": "7. RECOMMENDATIONS", "pages": [85]}]
      }
    }
  ]
}
```

The single segment's `label` is the report directory name. `pages` is the union of all section pages (unclassified pages like front matter and appendices are excluded). `_section_detail` preserves the labeled sub-segments identified by the model for human review; it is ignored by downstream consumers. Because downstream tools discover `*_pages` keys dynamically, they consume this format with no code changes — the same path that reads `form_pages` and `narrative_pages` from site form segments also reads `results_pages` and `recommendations_pages` from report segments.

---

### Full-text approach (pass 1)

`report_pass1_sections.txt` is a prompt for full-text report segmentation. Rather than a heading list, it receives page content assembled from `text_docling.txt` and returns the same section-type page assignments. This is the appropriate approach when `headings.json` is absent or unreliable — for example, reports processed by an older OCR pipeline without heading detection, or documents where headings are too sparse or inconsistently formatted for pass 0 to produce confident results.

The full-text approach benefits from seeing body text — transition sentences, artifact descriptions, site number mentions — that heading text alone cannot capture. The cost is scalability: for documents beyond roughly 30–50 pages, the per-page content must be progressively truncated to fit in context, and at very long documents that truncation can reduce visibility to a line or two per page. In practice this means pass 1 works well for medium-length reports (under ~100 pages) and for reports where the section boundaries are signaled by content patterns rather than heading labels. For long reports, pass 0 with chunking is more reliable because it keeps heading signal sharp regardless of document length.

A script applying pass 1 with adaptive truncation (same mechanism as the site form segmenter) is in development. In the interim, pass 0 with `chunk_size_headings` handles large-corpus runs; pass 1 is available for targeted use on specific documents through direct prompt experimentation.

---

### Trinomial extraction and downstream passes

After section segmentation, [site_vocab_extractor](https://github.com/jnpaige/site_vocab_extractor) scans the relevant pages for site number mentions. The `results_pages` and `recommendations_pages` keys in the segments.json feed directly into the vocab extractor as a page filter, concentrating the search on pages most likely to contain substantive per-site discussion — for the 180-page Hartfield report this reduced the scan from 180 pages to 10. The vocab extractor supports page-count-based chunking so that reports with dense results sections can be processed in smaller batches without losing coverage.

Once section page maps and a trinomial list are in hand, pass 2 identifies which pages within the results and recommendations sections discuss each individual site. It uses `report_pass2_trinomial_pages.txt` and iterates per trinomial, keeping each LLM call short and predictably sized. A script for this pass is in development.

---

## Assemblage segmentation for published journal articles

`pilot_cluster_tagging.py` handles a document type the passes above don't fit: a published lithic journal article, typically 9–32 pages, reporting on one or more excavated assemblages. The corpus this was built for is a set of Stone Age lithic technology papers — African MSA and Oldowan, Levantine, European Palaeolithic, Arctic and Fennoscandian — spanning single-site reports and multi-site syntheses.

The difference from report segmentation is where the unit of interest sits. A Phase II report discusses many sites, but each site's material is confined to its own pages, so page-level segmentation separates them. A journal article discusses its assemblages *interleaved* — the same paragraph compares two layers, and one summary table has a column per assemblage — so the unit sits **below the page** and pages cannot be partitioned between assemblages. Blocks are therefore tagged **multi-label**: a comparative sentence or table row carries every assemblage it discusses, and is never forced into one.

Input is the `.md` from pdf_ocr, split into addressable blocks by `lib/block_parser.py` — paragraphs, headings, and *individual table rows*. Table rows are split separately because comparison tables are where cross-assemblage evidence lives; a row like `Cores | 12 | 8 | 3` needs its own tag set rather than the whole table's. This is also why `.md` is the input rather than `text_docling.txt`, whose flattened text loses table structure.

Three LLM passes run in sequence, all against a local model via Ollama (`qwen2.5:32b` by default for all three).

### pass 0 — section map

Classifies every page of the article into one of nine IMRaD-style categories (`front_matter`, `abstract`, `background`, `site_context`, `methods`, `results`, `discussion`, `conclusions`, `boilerplate_end`), and keeps only the five substantive ones for tagging. This narrows the document before the expensive per-block work, and keeps each later call short.

Its input is a **page inventory** built from the `.md`: one entry per page, carrying that page's headings (each with a snippet of the body text that follows it), a marker for what else is on the page (`[N table rows]`, `[figure/table caption]`), and for pages with no heading, the opening of their caption and prose. Listing every page is what lets the model assign every page; describing what is on a page is what lets it distinguish a full-page plate of artefact photographs from a reference list.

Two properties are load-bearing for this corpus. Data tables are assigned to `results` wherever they are printed — Nature-format "Extended Data Table" pages and author manuscripts routinely place the paper's assemblage counts after the bibliography. And where a document's structure was not recovered by Docling's layout model at all, `lib/heading_recovery.py` asks the model to find the headings in the raw page text, verifies each one verbatim against the page it was claimed for, and injects them as heading blocks so pass 0 can run normally.

### stage 1 — assemblage identification

Identifies the distinct assemblages the paper reports, using variables aligned to the [ROCEEH Out of Africa Database (ROAD)](https://www.roceeh.uni-tuebingen.de/roadweb/), the standard research database for this literature:

| field | meaning |
|---|---|
| `locality` | the specific excavated site |
| `archaeological_layer` | stratigraphic unit defined by cultural content, or null |
| `geological_layer` | unit defined by sediment or deposit, or null |
| `archaeological_stratigraphy` | industry/culture the assemblage is assigned to |
| `n_artefacts` | assemblage size where the paper states it |
| `also_known_as` | spelling variants merged into this assemblage |
| `evidence` | which channels named it |

One assemblage per (locality, layer) the paper reports data for; a paper reporting one undivided collection yields one assemblage with the layer left null.

An industry, culture or technocomplex is recorded as a **field on each assemblage**, not as an assemblage of its own — it classifies assemblages rather than containing them, so a paper framed around one industry across five sites yields five assemblages sharing that value, and the industry-level view is a grouping on that field.

Evidence is gathered from up to six labelled channels — title and abstract, section headings, site/stratigraphy prose, whole tables (header plus rows), and figure captions — each optional, because which one enumerates the assemblages varies by paper. Assemblages arriving under different spellings (numeral style, case, abbreviation, OCR damage) are reconciled into one before the list is emitted.

### stage 2 — block tagging

Tags every block from the retained pages with the assemblage(s) it discusses, against the fixed list stage 1 produced. Blocks that apply to every assemblage equally — general methodology, dating technique, how retouch was scored — are marked `general` rather than assigned to one. Blocks discussing none of the listed assemblages are left with an empty list rather than forced into a tag.

Work is chunked by contiguous page ranges, with the chunk count raised automatically from the block count and a `num_ctx`-derived character budget so the request and its reply both fit the model's context window.

### Output and visual QA

A single `<stem>.cluster_tags.json` per document, carrying the pass-0 section map, the assemblage list, every block with its id/page/type/text, and the per-block tags. `pdf_ocr/annotate_layers.py` renders that output back onto the searchable PDF as toggleable layers — one per assemblage, plus section, block-type and figure-class layers — for visual review.

```bash
uv run python pilot_cluster_tagging.py --md-path "path/to/<paper>.md"
```

---

## Figure classification

`classify_figures.py` flags each figure extracted by [pdf_ocr](https://github.com/jnpaige/pdf_ocr) (`docling.extract_figures: true`) into one or more visual classes using a small local vision model — a separate pipeline stage from page/investigation segmentation above, run over pdf_ocr's `figures/figures.json` output rather than page text. Figures aren't scoped to a trinomial/investigation the way pages are, so it walks pdf_ocr's output directory directly rather than using `grouper.py`'s trinomial grouping.

Classes are config-driven, not hardcoded — `figure_classes` in `config_figures.yaml` is a list of `{key, label, description}` entries rendered into the prompt at runtime, so adding or renaming a class is a config edit, not a code change. Classification is multi-label: a composite figure (e.g. a map and a stratigraphic column side by side) is tagged with every class that applies rather than forced into one bucket, plus a `primary_class` for simple sorting. Each document's output carries a `<class_key>_figures` list per configured class, mirroring the page segmenter's `<type>_pages` convention above so downstream tools can request e.g. `lithic_tool_figures` the same way they already request `narrative_pages`.

When pdf_ocr's figure-context enrichment ran (caption/heading path/surrounding text in `figures.json`), that text is included alongside the image as disambiguating context — the image is still primary evidence.

```powershell
uv run python classify_figures.py                      # all documents, config_figures.yaml
uv run python classify_figures.py --pdf "16WN385"       # single document
uv run python classify_figures.py --model qwen2.5vl:7b  # override vision_model
```

Output follows the same run-metadata standard as the rest of this repo: `runs/<YYYYMMDD_HHMM>_<gitsha>/` containing a config snapshot, `prompts.yaml`, `run_metadata.json`, `inventory.csv`, one `<model_slug>__<pdf_stem>.figure_segments.json` per document, and `figure_classification_map.md`/`figure_classes.csv` for human/machine review (via `lib/figure_reporter.py`, a variant of `lib/reporter.py` shaped for a variable-length multi-label set instead of a handful of fixed page-type columns).

**Model choice.** This is meant to be a cheap, small-model triage pass, not the final trusted classification — validated against `qwen2.5vl:7b` (a 6GB-VRAM-class model): it correctly caught non-figure noise (a journal masthead logo Docling mis-extracted as a "picture" on nearly every page, tagged `other`) and correctly multi-labeled real composite figures, but also showed real errors on nuanced multi-panel figures — e.g. tagging a photo panel as `map` and inferring `artifact_image` from an on-image text label reading "Artefacts in situ" rather than an actual depicted artifact. Treat its output as a routing signal to spot-check, not ground truth — the same caution this repo already gives `qwen2.5:14b` vs `qwen2.5:32b` for page-boundary detection above applies here too.

---

## GLO survey field note segmentation

GLO (General Land Office) survey field-note volumes are a third document shape — 300–430 page manuscript-handwriting volumes, each recording section-line surveys for a dozen or more Township/Range grid cells laid out sequentially (one township's field notes, then the next), further subdivided into individual Sections. Two segmenters produce the identical output schema for this document type; which one to use is a real trade-off, not a strict upgrade path.

### Why two strategies exist

**Regex-based (`segment_glo_township_range.py`, original approach)** — Surveyors marked most pages with a short, repeating running-header stamp ("T. 2N R. 6W S.W.D."). `STAMP_RE` recognizes that stamp (tolerant of common OCR digit/letter confusions), confirms Township/Range directly on pages carrying it, then forward/backward-fills the pages in between from the nearest confirmed neighbors, denoising isolated single-page misreads and splitting genuine transitions at a "Basis Meridian" phrase if one falls in the gap. This is fast (a single regex pass, no LLM call, no GPU) and works well when a volume consistently uses the compact stamp format throughout.

**LLM-based (`segment_glo_township_range_llm.py`)** — A direct read-the-page evaluation of the regex segmenter's output on two real volumes found both switch, partway through, to a second, spelled-out header format ("Field Notes of Township No. 3 North... in Range No. 4 West") that `STAMP_RE` never matches at all. Those pages then get silently absorbed into whichever neighboring block the fill algorithm happens to guess, with zero actual confirmation — on the two volumes checked, this produced segments that were each really 3–4 distinct townships compressed into one label, covering roughly a third of each document. A smaller, separate failure mode: a table-of-contents page listing many townships (assigned to other surveyors, elsewhere in the volume) got matched as if it were a real running-header stamp for the pages that followed it. Because the underlying variation is in how surveyors actually wrote (or how OCR actually misread) a header, no single fixed pattern generalizes — this is a language-understanding problem, not a pattern-matching one, so the fix is an LLM read of the actual page content rather than a better regex.

Architecturally, this is closer to [report pass 1](#full-text-approach-pass-1) than [report pass 2](#trinomial-extraction-and-downstream-passes): GLO township/range blocks are sequential across the document, like report sections, not scattered/interleaved like trinomial mentions — so the right mechanism is one linear pass identifying block boundaries, not a full-document search repeated per known entity. The one thing pass 1 has that GLO documents don't is a cheap, compact heading list to send in a single call; pdf_ocr does not reliably produce `headings.json` for this corpus, so `segment_glo_township_range_llm.py` sends real page text instead, chunked into **overlapping windows** (`window_size` pages per call, advancing by `window_stride`, so consecutive windows share `window_size - window_stride` pages). A page inside the overlap is read independently by two calls — if they agree, that's corroborated evidence; if they disagree, the page is flagged in `_boundary_disagreements` rather than resolved by an arbitrary tie-break (the more-centered read, further from either window's edge, wins, but the disagreement stays visible). Within each window, the model assigns *every* page a Township/Range/Section directly — including pages with no header of their own, using narrative continuity — rather than the regex version's separate detect-then-statistically-fill steps.

Both scripts read either `text_docling.txt` or pdf_ocr's page-scoped `<stem>.md` (set `source: 'txt'` or `source: 'md'` in config) — table structure in `.md` doesn't matter much for detecting a stamp line, but real narrative content matters more for the LLM version's continuity reasoning, so `md` is the recommended `source` for the LLM segmenter where available.

**Practical guidance**: run the regex version first — it's instant and free. If a volume's `township_range_map.md` shows large, single-stamp-confidence blocks spanning 100+ pages, or you're relying on downstream extraction accuracy for a specific volume, re-run that volume through the LLM version and compare; the LLM version's `_boundary_disagreements` and `confidence_counts` (now `multi_window_agreement`/`single_window`/`multi_window_disagreement`/`unassigned`, reflecting genuine cross-window corroboration rather than statistical interpolation) tell you directly which pages are still uncertain, instead of the regex version's "zero unmapped pages" which looked reassuring but really just meant the fill algorithm always produces *an* answer, right or wrong.

### Output schema

Both segmenters write the identical shape, so `site_vocab_extractor` and `locate_and_annotate.py` (in [pdf_ocr](https://github.com/jnpaige/pdf_ocr)) consume either interchangeably with no code changes:

```json
{
  "segments": [
    {
      "label": "T3N_R4W",
      "township": "3N",
      "range": "4W",
      "district": "SWD",
      "pages": [102, 103, 104, "..."],
      "t3n_r4w_pages": [102, 103, 104, "..."],
      "page_span": [102, 145],
      "n_pages": 44,
      "confidence_counts": {"multi_window_agreement": 40, "single_window": 3, "multi_window_disagreement": 1},
      "t3n_r4w_s12_pages": [110, 111],
      "sections": [{"section": 12, "pages": [110, 111]}]
    }
  ]
}
```

`confidence_counts` keys differ by method (`direct`/`interpolated`/`interpolated-boundary`/`edge-extrapolated` for regex, `multi_window_agreement`/`single_window`/`multi_window_disagreement`/`unassigned` for LLM) — both describe *how* each page's assignment was arrived at, but any code reading only `label`/`township`/`range`/`pages`/`sections` (as `site_vocab_extractor` and `locate_and_annotate.py` do) is unaffected either way.

### A third strategy: read the page, not the OCR (`segment_glo_page_anchors.py`)

Both segmenters above read OCR'd **body text** — 1807 manuscript handwriting — which, measured across the 43-volume Evangeline corpus on 2026-09-14, is the weakest evidence the document offers:

- **80% of pages (8,654/10,817)** come back from Docling with no layout structure at all, as raw Surya text.
- **`headings.json` exists for 2 of 43 volumes**, six junk headings each, so the report pass-1 heading mechanism has nothing to work from here. This is not fixable by re-running `extract_headings.py` — that was already backfilled over this corpus in August 2026.

Meanwhile each page physically carries up to three *better* signals, in modern or printed lettering rather than manuscript script, and the text pipeline discards two of them:

| anchor | what it is | recovered by current OCR |
|---|---|---|
| **Index page** | a ruled archival finding aid in the front matter — `Surveyors \| Township \| Range \| Pages`, one page **span** per Township/Range. Present in 35/43 volumes. | Docling shreds its columns into one vertical stream; only 18/43 volumes yield even five parseable spans, and `507_00056` yields **zero** despite having a full index page |
| **Title card** | an archivist's photographic strip laid on nearly every page of the volumes that have one — `T. 2N  R. 4W  N.W.D. / R.B. Paine` | **~9% of pages.** It is white-on-black and Surya is tuned for dark-on-light |
| **Margin page number** | the volume's own page number, top-left/top-right, alternating between facing pages | **not at all** — a consequence of the 80% layout fallback |

`segment_glo_page_anchors.py` reads all three from page imagery with a vision model and writes a per-page **evidence map**, keyed by 0-indexed PDF page so it lines up with the `=== Page N ===` markers in `.md`/`.txt`/`_ocr.pdf`.

**It deliberately does not write `segments.json`.** Producing the evidence and drawing the conclusion are kept as separate steps, because conflating them is exactly what made the regex segmenter's output untrustworthy — its "zero unmapped pages" really meant "the fill algorithm always produces *an* answer, even a wrong one."

**Manuscript page numbers are usually not PDF page indices.** Two families, recorded per volume as `index_family`:
- *`one_to_one`* (e.g. `507_00102`: index max 316, PDF 317 pages) — archivally re-paginated; the index maps almost straight onto PDF page order.
- *`series_numbered`* (e.g. `507_00615`: index max 570, PDF 189 pages) — the index covers a whole manuscript series while this PDF holds only a subset. The offset is **piecewise, not constant**: manuscript page 33 is PDF page 5 (offset 28) while manuscript page 168 is PDF page 60 (offset 108). Never interpolate it; the margin page number is read per page precisely so it doesn't have to be.

**The index gets a second, model-free read where the OCR is new enough.** pdf_ocr's 2026-09 `tables` field records a detected table's grid plus every cell's text and `start_row`/`start_col`. On the GLO index page that grid comes back **with its row alignment intact**, which the flattened `md_text` does not — the reflowed text groups the page by column (every township, then every range, then every page number), losing which township goes with which page. `read_index_from_tables()` parses that grid and `_compare_index_reads()` cross-checks it against the vision read, reporting `n_agree`/`n_disagree`/`mismatches` and never auto-resolving. Set `ocr_dir` in config to enable it; it returns `None` (and the cross-check is silently skipped) on pre-2026-09 corpora.

It is not a replacement for the vision read — cells that OCR dropped are simply absent here, where a vision model still reads them off the page: **16/30 rows on `507_00056`, 31/41 on `507_00121`** recover both township and range. Column splits also vary between volumes (`507_00056` gives the range's direction letter its own column; `507_00121` merges it into the Pages cell as `W 1 - 18` or `37-63 W`), which is why the reader recovers a stranded direction letter from the Pages cell but takes only a lone `E`/`W` from it and never a digit — joining that cell into the row text wholesale turns `37-63 W` into range `63W`.

**Structure is re-derived from verbatim transcription, never taken from the model's own fields.** Both prompts ask for a verbatim string alongside the parsed fields, and the code parses the verbatim itself. This is not belt-and-braces — two distinct failures were observed in testing and are fixed by it:
- Given a prompt whose JSON example held realistic values (`"township": "1N"`, `"surveyor": "R.B. Paine"`), the model correctly reported `card_text: null` on pages with no card and then **filled those fields in anyway, copied out of the example**. Both prompts now use obviously-fake placeholders.
- On an index row it transcribed correctly as `raw='1 N 2 E 9 - 27'`, its own structured fields came back with the columns **shifted by one**. `raw` was right every time the fields were wrong.

**The margin number gets its own guard, because it is the linchpin.** It is what turns an index page span into a PDF page range, so a single misread silently relocates a page to the wrong township. `flag_margin_outliers()` works on the *offset* (margin number − PDF page), which is what separates the two families: `one_to_one` holds a near-constant offset, `series_numbered` a piecewise-constant one. A genuine jump is shared by a run of pages; a misread is an isolated spike whose neighbours agree across it — only the isolated case is flagged, endpoints included. Nothing is renumbered or dropped; the flag rides along on the page entry and on any conflict it explains. On `507_00102` this caught PDF page 7 reading as margin `140`, which the index had resolved into its `127–188` row against that page's own card. **On the 23 cardless volumes this guard is the only check available.**

`agreement` per page is the point of the file: `corroborated` (card and index, reached via the margin page number, independently name the same Township/Range), `card_only`, `index_only`, `conflict`, `none`. Conflicts are listed separately and are genuinely informative — on `507_00615` they caught the index's handwritten `N` being transcribed as `S` on a row the printed card contradicts. **The card is the more reliable of the two where both exist** (printed vs. handwritten), but the conflict is surfaced rather than auto-resolved.

`--survey` samples a few pages per volume and reports only whether a title card is present, so you can find out which volumes this anchor is even available for before committing to a full run. It grades on card-*shaped* text (a township **and** a range, in the card's printed order) rather than on any transcription at all — the looser rule counted volumes carrying only a handwritten page header ("Western Land District of the Territory of Orleans") as card-bearing.

**Survey result, all 43 Evangeline volumes, 6 sampled pages each (2026-09-14, `qwen2.5vl:7b`, seed 20260914):**

| | volumes |
|---|---|
| `solid` — card-shaped text on 3+ sampled pages | **15** |
| `weak` — on 1–2 pages | 5 |
| `none` — no card; the REWRITE books and the colour loose-sheet scans | 23 |

**Margin page number: 245/258 sampled pages (95%), present in every volume family including all 23 with no card.** That is the finding that decides the design: the margin number is the universal anchor and the card is a corroborator where it happens to exist. A card-only approach would cover barely a third of the corpus.

Note this supersedes an earlier estimate made by regex over OCR'd text, which put card presence at ~9% of pages corpus-wide. That measure was wrong in both directions — it missed cards on volumes that have them (the card is white-on-black and Surya rarely reads it) while matching handwritten headers on volumes that don't (it scored `507_00709`, which has no card at all, at 42%).

### Usage

```powershell
# Regex — instant, free, good default first pass
uv run python segment_glo_township_range.py --config config_glo_township_range.yaml
uv run python segment_glo_township_range.py --config config_glo_township_range.yaml --report "507_00056__..."

# LLM — slower (a 32b-model call per ~25-page window), re-run per volume where regex looks weak
uv run python segment_glo_township_range_llm.py --config config_glo_township_range_llm.yaml
uv run python segment_glo_township_range_llm.py --config config_glo_township_range_llm.yaml --report "507_00056__..."

# Vision page anchors — index table + title card + margin page number, per page
uv run python segment_glo_page_anchors.py --config config_glo_page_anchors.yaml
uv run python segment_glo_page_anchors.py --config config_glo_page_anchors.yaml --report "507_00615__..."

# Which volumes even have a title card? Samples 6 pages per volume.
uv run python segment_glo_page_anchors.py --config config_glo_page_anchors.yaml --survey
```

```yaml
# config_glo_township_range_llm.yaml
input_dir:  'path/to/pdf_ocr/output'
output_dir: 'runs'
source:     'md'            # 'txt' or 'md' — both segmenters support either
model:            'qwen2.5:32b'
window_size:      25          # pages per LLM call
window_stride:    20          # advance per call; window_size - window_stride = overlap
```

---

## Segment type prompts

All prompts live in `segment_types/`. Each pass has its own file so prompts can be tuned independently without touching code. The prompts for site forms are prefixed `site_form_`. Report-mode prompts are prefixed `report_`. GLO field-note prompts are prefixed `glo_`:

- `report_pass0_sections.txt` — heading-only section classifier; primary approach for most reports
- `report_pass1_sections.txt` — full-text section classifier; fallback for sparse-heading documents
- `report_pass2_trinomial_pages.txt` — per-trinomial page narrowing within identified sections
- `glo_township_range_pages.txt` — per-page Township/Range/Section classifier for overlapping page windows; used by `segment_glo_township_range_llm.py`
- `glo_page_anchor.txt` — reads an archivist title card and the volume page number off a page's cropped top strip; used by `segment_glo_page_anchors.py`
- `glo_index_table.txt` — transcribes a volume's front-matter index table (Township/Range → page span) from its image; used by `segment_glo_page_anchors.py`
- `report_pass0_lithic_journal_sections.txt` — page-inventory section classifier for published journal articles (pass 0 above)
- `report_pass1_clusters.txt` — ROAD-aligned assemblage identification (stage 1 above)
- `report_pass2_block_tags.txt` — multi-label per-block assemblage tagging (stage 2 above)
- `recover_headings.txt` — recovers section headings from raw page text where the layout model found none

---

## 2026-06-16 evaluation notes

A stratified sample of 20 site forms was manually reviewed against extraction outputs to estimate error rates. Several errors traced back to segmenter page-selection failures rather than extraction model failures. The most impactful errors occurred when neither `narrative_pages` nor `nrhp_pages` were populated for a segment — in those cases, extractors see only the checkbox form page and miss eligibility fields on narrative pages. The 14b text model misses these assignments on a meaningful fraction of terse single-investigation forms. The `qwen2.5:32b` model is substantially better at this and is recommended for production runs where page-selection accuracy matters.

---

## Dependencies

- [Ollama](https://ollama.com) — local LLM server
- [PyMuPDF](https://pymupdf.readthedocs.io) — PDF rendering for vision mode
- [httpx](https://www.python-httpx.org/) — HTTP client for Ollama API
- [PyYAML](https://pyyaml.org/) — config parsing
