# site_form_segmenter

Louisiana archaeological site form PDFs regularly contain multiple investigations in a single file — a 2016 update appended behind a 1996 original, for example. Before those documents can be reliably coded against a standardized codebook, you need to know where each investigation starts and ends, and which pages within each investigation are the structured form, the narrative, and the NRHP eligibility discussion.

This tool solves that segmentation problem as a standalone step, separate from any coding pipeline. The output — a page map showing investigation boundaries and page-type assignments — is designed to feed downstream into `site_coder` or any other tool that needs to know which pages to read for which purpose.

## What it does

A 4-pass approach, each pass handling one narrow question:

| Pass | Model | Task |
|---|---|---|
| 1 | vision or text | Identify each investigation as a contiguous page range |
| 2 | text | Find the structured site record form page within each investigation |
| 3 | text | Find narrative prose pages (excluding the form page) |
| 4 | text | Find NRHP eligibility discussion pages |

Dividing segmentation into focused passes lets a small model handle each one reliably. Pass 1 is the hardest — it has to detect where one investigation ends and the next begins — which is where the vision model option helps. Passes 2–4 are straightforward page-type classification tasks that an 8b text model handles well.

## Two modes

**`vision` (default):** `llama3.2-vision:11b` receives rendered images of all PDF pages for Pass 1. It can see form layouts, logos, headers, and visual structure changes that OCR text may partially lose. Passes 2–4 use `llama3.1:8b` on the existing OCR text. Requires `PyMuPDF` and the PDF file alongside `text_docling.txt`.

**`text`:** `llama3.1:8b` runs all 4 passes on OCR text only. No PDF rendering, no PyMuPDF. Faster, lower memory. Good baseline and useful when PDFs are not available.

If `vision` mode is selected but no PDF is found in a site directory, that site automatically falls back to text mode for Pass 1.

## Input

The same directory structure produced by `pdf_ocr`:

```
<input_dir>/
  16VN1452/
    text_docling.txt       OCR text with === Page N === markers (required)
    16VN1452_ocr.pdf       rendered PDF (required for vision mode)
    ...
  16WN385/
    text_docling.txt
    16WN385_ocr.pdf
    ...
```

## Output

Each run creates a versioned directory:

```
runs/
  20260527_13_abaf0f4/
    config.yaml                        config snapshot for this run
    vision__llama3.2-vision_11b__llama3.1_8b/
      prompts.yaml                     every prompt used in this run
      segmentation_map.md              human-readable page map (appended per site)
      segments.csv                     machine-readable segment table (appended per site)
      16VN1452.segments.json           full structured output for this site
      16WN385.segments.json
      ...
```

### `segmentation_map.md`

The primary output for human review. One section per site:

```markdown
## 16WN385

**2016 Phase I Survey Update** (2016) · *text_docling.txt*
- All pages (8): 0, 1, 2, 3, 4, 5, 6, 7
- Form pages: 0
- Narrative pages: 5
- NRHP pages: 5

**1996 Site Record** (1996) · *text_docling.txt*
- All pages (3): 8, 9, 10
- Form pages: 8
- Narrative pages: 9
- NRHP pages: 9
```

### `segments.csv`

Machine-readable version of the same data. One row per investigation, columns: `trinomial`, `source_file`, `label`, `year`, `pages`, `page_count`, `form_pages`, `narrative_pages`, `nrhp_pages`. Page lists are semicolon-separated.

### `<trinomial>.segments.json`

Structured JSON per site with the same information plus metadata (run timestamp, models used). Intended for downstream consumption by coding pipelines.

## Setup

### 1. Install Ollama and pull models

```powershell
# Install Ollama from https://ollama.com

ollama pull llama3.1:8b                  # text passes (passes 2-4, and all passes in text mode)
ollama pull llama3.2-vision:11b          # vision pass 1 (vision mode only, ~7.8 GB)
```

### 2. Install uv and sync dependencies

```powershell
# Windows — install uv once per machine
winget install astral-sh.uv

# In the repo directory — creates .venv and installs all dependencies including PyMuPDF
cd site_form_segmenter
uv sync
```

```bash
# Mac/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
cd site_form_segmenter
uv sync
```

### 3. Run with uv run

**Always use `uv run` to launch the script.** This ensures the correct virtual environment (the one `uv sync` just built) is used, regardless of what other venvs may be active in your shell:

```powershell
uv run python segmenter.py
```

If you prefer to activate the venv manually instead:

```powershell
# Windows
.venv\Scripts\activate
python segmenter.py

# Mac/Linux
source .venv/bin/activate
python segmenter.py
```

### 4. Configure paths

Edit `config.yaml`:

```yaml
input_dir: 'path/to/pdf_ocr/docling_output'
mode: 'vision'          # or 'text'
text_model:   'llama3.1:8b'
vision_model: 'llama3.2-vision:11b'
```

## Usage

All examples use `uv run python segmenter.py`. If your venv is already activated, `python segmenter.py` works identically.

```powershell
# All sites, mode from config.yaml (default: vision)
uv run python segmenter.py

# Single site — fastest way to verify segmentation quality before a full run
uv run python segmenter.py --trinomial 16WN385

# Text mode only — no PDF rendering, no PyMuPDF, faster
uv run python segmenter.py --mode text

# Single site in text mode
uv run python segmenter.py --trinomial 16WN385 --mode text

# Single site in vision mode
uv run python segmenter.py --trinomial 16WN385 --mode vision

# Re-run everything, ignoring existing output files
uv run python segmenter.py --force

# Use a different config file
uv run python segmenter.py --config config_test.yaml

# Single site with a test config
uv run python segmenter.py --config config_test.yaml --trinomial 16VN1452
```

**Recommended first-run workflow:**
1. Run one site in text mode first to confirm the pipeline works end-to-end: `uv run python segmenter.py --trinomial 16WN385 --mode text`
2. Check `runs/<run_id>/.../segmentation_map.md` — verify the investigation boundaries look right
3. Run the same site in vision mode: `uv run python segmenter.py --trinomial 16WN385 --mode vision --force`
4. Compare the two maps to see whether vision improves Pass 1 boundary detection
5. If vision mode looks good, run all sites: `uv run python segmenter.py`

## Configuration reference

```yaml
input_dir:         'path/to/docling_output'
trinomial_pattern: '(\d{2}[A-Z]{2}\d+)'   # Louisiana format; adjust for other states

mode: 'vision'           # 'vision' or 'text'
page_truncation_chars: 500   # chars of OCR text per page shown in text passes
pdf_render_dpi:        96    # render resolution for vision pass 1
                             # images are also hard-capped at 1024px on the longest edge,
                             # so raising DPI beyond ~150 has no effect on payload size

base_url:        'http://localhost:11434'
vision_model:    'llama3.2-vision:11b'
text_model:      'llama3.1:8b'
temperature:     0.05
timeout_seconds: 1800
```

## Troubleshooting

### `500 Internal Server Error` on vision pass 1

Ollama returns a 500 when the image payload is too large for the model to process. The script renders each page as a JPEG and sends all pages in a single request — more pages or higher DPI means a larger payload.

The renderer caps images at 1024px on the longest edge regardless of DPI, which keeps payloads manageable for typical site forms (3–15 pages). If you still see 500 errors:

- Lower `pdf_render_dpi` in `config.yaml` (try `72`)
- Check how many pages the failing site has — `[render] 16VN1451: rendering 6 pages` is printed before the call
- Run that site in text mode as a workaround: `uv run python segmenter.py --trinomial 16VN1451 --mode text`

The script logs `[warn] pass1 failed — single segment fallback` and continues rather than crashing; a 500 on Pass 1 means the whole document is treated as one investigation, which is safe but loses boundary detection for multi-investigation PDFs.

### `PyMuPDF is not installed in the current Python environment`

The script checks for PyMuPDF at startup and exits immediately with the Python path if it's missing. The most common cause is running `python segmenter.py` with a shell that has a different project's venv active.

Fix: use `uv run python segmenter.py` instead — uv selects the correct venv automatically. If the `.venv` doesn't exist yet, run `uv sync` first.

### Script hangs mid-run

The text model passes (2–4) can take 30–120 seconds per investigation depending on page count and model speed. The script prints `[done] <trinomial>` when each site finishes. If it appears to hang, it is likely waiting for an Ollama response — check that Ollama is running and not already processing another request. Interrupt with Ctrl+C; completed sites are already saved and will be skipped on the next run.

## Segment type prompts

All prompts live in `segment_types/`. Each pass has its own file so prompts can be tuned independently without touching code.

| File | Mode | Pass | Task |
|---|---|---|---|
| `site_form_pass1_vision.txt` | vision | 1 | Boundary detection from page images |
| `site_form_pass1_boundaries.txt` | text | 1 | Boundary detection from OCR text |
| `site_form_pass2_form_page.txt` | both | 2 | Identify structured site record form page |
| `site_form_pass3_narrative.txt` | both | 3 | Identify narrative prose pages |
| `site_form_pass4_nrhp.txt` | both | 4 | Identify NRHP eligibility discussion pages |

Pass 3 receives the Pass 2 result injected via `{form_pages}` in the prompt template so the model knows which page to exclude.

## Relationship to site_coder

`site_form_segmenter` is upstream of `site_coder`. It focuses entirely on segmentation quality and produces a page map that can be used to route coding calls to the right pages. `site_coder` currently runs its own internal segmentation as part of the coding pipeline; the intent is that a verified segmentation map from this tool can eventually replace that step, ensuring coding always works from correctly identified pages.

## Project structure

```
site_form_segmenter/
  segmenter.py           main script
  config.yaml            configuration
  pyproject.toml         dependencies (httpx, pyyaml, PyMuPDF)
  lib/
    grouper.py           finds trinomial dirs; returns txt path + pdf path
    page_parser.py       splits text_docling.txt on === Page N === markers
    pdf_renderer.py      renders PDF pages to base64 JPEG via PyMuPDF
    ollama_client.py     Ollama REST wrapper: text + vision, token stats
    reporter.py          writes segmentation_map.md and segments.csv
  segment_types/
    site_form_pass1_vision.txt
    site_form_pass1_boundaries.txt
    site_form_pass2_form_page.txt
    site_form_pass3_narrative.txt
    site_form_pass4_nrhp.txt
  runs/                  gitignored — versioned run outputs
```

## Dependencies

- [Ollama](https://ollama.com) — local LLM server
- [PyMuPDF](https://pymupdf.readthedocs.io) — PDF rendering for vision mode
- [httpx](https://www.python-httpx.org/) — HTTP client for Ollama API
- [PyYAML](https://pyyaml.org/) — config parsing
