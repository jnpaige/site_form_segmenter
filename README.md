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

## Segment type prompts

All prompts live in `segment_types/`. Each pass has its own file so prompts can be tuned independently without touching code. The prompts for site forms are prefixed `site_form_`. Report-mode prompts are prefixed `report_`:

- `report_pass0_sections.txt` — heading-only section classifier; primary approach for most reports
- `report_pass1_sections.txt` — full-text section classifier; fallback for sparse-heading documents
- `report_pass2_trinomial_pages.txt` — per-trinomial page narrowing within identified sections

---

## 2026-06-16 evaluation notes

A stratified sample of 20 site forms was manually reviewed against extraction outputs to estimate error rates. Several errors traced back to segmenter page-selection failures rather than extraction model failures. The most impactful errors occurred when neither `narrative_pages` nor `nrhp_pages` were populated for a segment — in those cases, extractors see only the checkbox form page and miss eligibility fields on narrative pages. The 14b text model misses these assignments on a meaningful fraction of terse single-investigation forms. The `qwen2.5:32b` model is substantially better at this and is recommended for production runs where page-selection accuracy matters.

---

## Dependencies

- [Ollama](https://ollama.com) — local LLM server
- [PyMuPDF](https://pymupdf.readthedocs.io) — PDF rendering for vision mode
- [httpx](https://www.python-httpx.org/) — HTTP client for Ollama API
- [PyYAML](https://pyyaml.org/) — config parsing
