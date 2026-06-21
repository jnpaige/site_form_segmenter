For session management instructions:
`C:\Users\jpaige\Desktop\Research_repositories\Context_instructions\universal-context-management.md`

## What this repo does

Segments multi-investigation Louisiana site form PDFs into investigation boundaries with page-type assignments (form, narrative, NRHP). Uses a 4-pass LLM approach via Ollama: Pass 1 detects investigation boundaries (vision or text), Pass 1b gap-fills unclaimed pages, Passes 2-4 classify page types within each investigation. Output is a page map that downstream tools (`site_coder`, `site_attribute_extractor`) use to route coding/extraction calls to the right pages.

## Structure

```
segmenter.py             main script — orchestrates all passes, writes output
config.yaml              runtime config (input/output dirs, models, mode)
lib/
  grouper.py             finds trinomial directories, returns txt + pdf paths
  page_parser.py         splits text_docling.txt on === Page N === markers
  pdf_renderer.py        renders all PDF pages as a single contact sheet JPEG
  ollama_client.py       Ollama REST wrapper: text + vision, token stats
  reporter.py            writes segmentation_map.md and segments.csv
segment_types/           prompt files for each pass (one per .txt file)
runs/                    gitignored — versioned run outputs
```

## Dependencies and setup

```powershell
uv sync
uv run python segmenter.py
```

Requires Ollama with models pulled:
```powershell
ollama pull qwen2.5:14b          # text passes (recommended over llama3.1:8b)
ollama pull llama3.2-vision:11b  # vision pass 1 (vision mode only)
```

## Run command

```powershell
uv run python segmenter.py                              # all sites
uv run python segmenter.py --trinomial 16WN385          # single site
uv run python segmenter.py --mode text                  # text mode only
uv run python segmenter.py --force                      # re-process existing
uv run python segmenter.py --config config_chunk1.yaml  # alternate config
```

## Key config settings

| Key | Purpose |
|---|---|
| `input_dir` | Root of pdf_ocr output containing `<trinomial>/` dirs |
| `output_dir` | Output directory (defaults to `./runs`) |
| `mode` | `vision` (default) or `text` |
| `text_model` | Model for text passes 2-4 (recommended: `qwen2.5:14b`) |
| `vision_model` | Model for vision pass 1 (`llama3.2-vision:11b`) |
| `page_truncation_chars` | Chars of OCR text per page in text passes |
| `pdf_thumb_width` | Width of page thumbnails in vision contact sheet |

## Output per site

- `<trinomial>.segments.json` — structured JSON with investigation boundaries and page types
- `segmentation_map.md` — human-readable page map (appended per site)
- `segments.csv` — machine-readable segment table

## Known issues

- The 14b text model misses narrative/NRHP page assignments on terse forms. Recommended upgrade: `qwen2.5:32b` for text passes 2-4.
- Vision pass 1 sends a single contact sheet image (all pages tiled in a grid). Very large PDFs may need reduced `pdf_thumb_width`.
- If vision mode finds no PDF for a site, it falls back to text mode automatically.
