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

**`vision` (default):** `llama3.2-vision:11b` receives rendered images of all PDF pages for Pass 1. It can see form layouts, logos, headers, and visual structure changes that OCR text may partially lose. Passes 2–4 use `llama3.1:8b` on the existing OCR text. Requires `pymupdf` and the PDF file alongside `text_docling.txt`.

**`text`:** `llama3.1:8b` runs all 4 passes on OCR text only. No PDF rendering, no pymupdf. Faster, lower memory, no extra dependency. Good baseline and useful when PDFs are not available.

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
  20260527_10_4f2e3f3/
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
ollama pull llama3.2-vision:11b          # vision pass 1 (vision mode only)
```

### 2. Install Python dependencies

**Recommended — uv:**

```powershell
# Windows
winget install astral-sh.uv
uv sync

# Run
uv run python segmenter.py
# or activate and use python directly:
.venv\Scripts\activate
python segmenter.py
```

```bash
# Mac/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
uv run python segmenter.py
```

`pymupdf` is included in `pyproject.toml` and installed by `uv sync`. It is only used in vision mode — if you only use text mode you can remove it from `pyproject.toml`.

**Alternative — plain pip:**

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install httpx pyyaml pymupdf
```

### 3. Configure paths

Edit `config.yaml`:

```yaml
input_dir: 'path/to/pdf_ocr/docling_output'
mode: 'vision'          # or 'text'
text_model:   'llama3.1:8b'
vision_model: 'llama3.2-vision:11b'
```

## Usage

```powershell
# All sites, mode from config.yaml (default: vision)
python segmenter.py

# Single site — fastest way to verify segmentation quality before a full run
python segmenter.py --trinomial 16WN385

# Text mode only — no PDF rendering, no pymupdf required
python segmenter.py --mode text

# Vision mode — override config
python segmenter.py --mode vision

# Single site in text mode
python segmenter.py --trinomial 16WN385 --mode text

# Single site in vision mode
python segmenter.py --trinomial 16WN385 --mode vision

# Re-run everything, ignoring existing output files
python segmenter.py --force

# Use a different config file
python segmenter.py --config config_test.yaml

# Single site with a test config
python segmenter.py --config config_test.yaml --trinomial 16VN1452
```

**Recommended first-run workflow:**
1. Run one site in vision mode: `python segmenter.py --trinomial 16WN385 --mode vision`
2. Open `runs/<run_id>/.../segmentation_map.md` and check that investigations are split correctly and pages are classified reasonably
3. If vision mode looks good, run all sites: `python segmenter.py`
4. If boundaries are wrong, try adjusting `pdf_render_dpi` upward (200–300) or switch to text mode for comparison

## Configuration reference

```yaml
input_dir:         'path/to/docling_output'
trinomial_pattern: '(\d{2}[A-Z]{2}\d+)'   # Louisiana format; adjust for other states

mode: 'vision'           # 'vision' or 'text'
page_truncation_chars: 500   # chars of OCR text per page shown in text passes
pdf_render_dpi:        150   # image resolution for vision pass 1; 150 is fast and sufficient
                             # raise to 200-300 if boundary detection is unreliable

base_url:        'http://localhost:11434'
vision_model:    'llama3.2-vision:11b'
text_model:      'llama3.1:8b'
temperature:     0.05
timeout_seconds: 1800
```

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
  pyproject.toml         dependencies (httpx, pyyaml, pymupdf)
  lib/
    grouper.py           finds trinomial dirs; returns txt path + pdf path
    page_parser.py       splits text_docling.txt on === Page N === markers
    pdf_renderer.py      renders PDF pages to base64 JPEG via pymupdf
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
- [pymupdf](https://pymupdf.readthedocs.io) — PDF rendering for vision mode
- [httpx](https://www.python-httpx.org/) — HTTP client for Ollama API
- [PyYAML](https://pyyaml.org/) — config parsing
