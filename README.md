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

# Re-run everything, ignoring existing output files
uv run python segmenter.py --force

# Use a different config file
uv run python segmenter.py --config config_test.yaml
```

A good first-run workflow is to process one site in text mode first to confirm the pipeline works end-to-end, then check the `segmentation_map.md` to verify the investigation boundaries look right, then try the same site in vision mode with `--force` to compare whether vision improves Pass 1 boundary detection.

---

## Output

Each run creates a versioned directory. Inside it, one subdirectory is created per model condition. The primary output for human review is `segmentation_map.md`, which shows one section per site listing each investigation, its page range, and its classified page types. The machine-readable equivalent is `segments.csv` with one row per investigation. Each site also gets a `<trinomial>.segments.json` file with the full structured output including run metadata, which is what downstream coding pipelines consume.

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

If the script hangs mid-run, it is most likely waiting for an Ollama response. Text model passes can take 30–120 seconds per investigation depending on page count and model speed. Interrupt with Ctrl+C; completed sites are already saved and will be skipped on the next run.

---

## Segment type prompts

All prompts live in `segment_types/`. Each pass has its own file so prompts can be tuned independently without touching code. The prompts for site forms are prefixed `site_form_`. Report-mode prompts (for longer multi-site documents) are prefixed `report_` and implement a two-stage approach described in the [pdf_ocr README](https://github.com/jnpaige/pdf_ocr).

---

## 2026-06-16 evaluation notes

A stratified sample of 20 site forms was manually reviewed against extraction outputs to estimate error rates. Several errors traced back to segmenter page-selection failures rather than extraction model failures. The most impactful errors occurred when neither `narrative_pages` nor `nrhp_pages` were populated for a segment — in those cases, extractors see only the checkbox form page and miss eligibility fields on narrative pages. The 14b text model misses these assignments on a meaningful fraction of terse single-investigation forms. The `qwen2.5:32b` model is substantially better at this and is recommended for production runs where page-selection accuracy matters.

---

## Dependencies

- [Ollama](https://ollama.com) — local LLM server
- [PyMuPDF](https://pymupdf.readthedocs.io) — PDF rendering for vision mode
- [httpx](https://www.python-httpx.org/) — HTTP client for Ollama API
- [PyYAML](https://pyyaml.org/) — config parsing
