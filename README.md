# Remedy Drupal

Automated WCAG 2.1 AA accessibility scanning, violation tracing, and LLM-powered remediation for Drupal sites — exposed as an MCP server for use with Claude Code or any MCP-compatible client.

Built for the Los Angeles Community College District (LACCD) to support ADA Title II compliance across its campus websites.

## How It Works

The core pipeline is: **axe-core scanner → violation tracer → LLM remediation → JSON:API write → re-scan verification**.

Claude Code (or any MCP client) is just the caller — it invokes the MCP tools, but the actual scanning, tracing, LLM prompting, and Drupal writes all happen inside this codebase.

### Pipeline

```
1. scan_page
   Launches Playwright, injects axe-core 4.10.2, runs a WCAG 2.1 AA
   audit on the fully rendered page. Returns violations with impact
   level, HTML snippets, and help text.

2. trace_violations
   Takes the axe-core results and maps each violation back to the
   specific Drupal entity — node body field, paragraph row, or media
   entity — using the ViolationTracer. Returns traced violations with
   entity type/ID/field, edit URLs, and suggested fixes.

   Optional: check_content_accuracy runs the ContentAccuracyChecker
   (vision model) over each image on the page to flag captions/alt
   text that do not match what the image actually shows.

3. remediate_page / remediate_traced
   Orchestrates a two-phase fix loop:

   Phase 1 — Vision Alt Text (if image-alt violations exist):
   a. Parses body HTML for <img> tags missing alt attributes
   b. Downloads each image from the Drupal site
   c. Sends to the vision model (kimi-k2.6:cloud) via Ollama's
      native /api/chat with structured JSON output
   d. Vision returns {"alt_text": "...", "decorative": bool}
   e. Inserts generated alt text into the HTML
   f. Detects functional images (inside links) and uses a
      specialized prompt that prevents decorative classification
   g. Retries up to 3 times with seed variation if model returns empty

   Phase 2 — Text LLM Fixes (for remaining violations):
   a. Sends body HTML + violation details to kimi-k2.6:cloud
   b. Gets back corrected HTML (empty links filled, iframe titles
      added, heading hierarchy fixed, etc.)
   c. Writes back via JSON:API PATCH
   d. Re-scans to verify fixes worked
   e. Repeats up to 3 cycles if violations remain
```

### Violation Types

The app can detect, trace, and auto-fix these WCAG 2.1 AA violations:

| Violation | What It Detects | How It Fixes |
|-----------|----------------|--------------|
| color-contrast | Text/background contrast below 4.5:1 | Swaps paragraph theme to a safe alternative |
| image-alt | Images missing alt text | Vision model downloads and analyzes the actual image, returns structured JSON alt text |
| image-redundant-alt | Image alt duplicates surrounding text | LLM rewrites to be descriptive without redundancy |
| empty-heading | Heading elements with no text | LLM adds meaningful heading text |
| heading-order | Skipped heading levels (h2 to h4) | LLM adjusts to sequential hierarchy |
| frame-title | Iframes without title attributes | LLM sets a descriptive title |
| link-name | Links with no accessible text | LLM adds descriptive link text |

All other axe-core violations are detectable and fixable in the body field via `remediate_page`, but only the above are individually traced to specific Drupal entities.

## Architecture

```
┌──────────────────────────────────────────┐
│          MCP Server (server.py)          │
│                                          │
│  Scanning   Reading    Tracing           │
│  Writing    Remediation                  │
└──────────┬───────────────────────────────┘
           │
    ┌──────┴───────┬───────────────┐
    │              │               │
┌───▼────┐   ┌────▼─────┐   ┌────▼──────┐
│ Drupal │   │Remediator│   │ Violation │
│ Client │   │  scan →  │   │  Tracer   │
│        │   │  fix →   │   │  map to   │
│        │   │  verify  │   │  entities │
└─┬────┬─┘   └────┬─────┘   └───────────┘
  │    │          │
┌─▼──┐ ┌▼──────┐ ┌▼───────┐
│Axe │ │JSON:  │ │Text/   │
│Core│ │API    │ │Vision  │
│Scan│ │Client │ │LLM     │
└────┘ └───────┘ └────────┘
```

### Key Files

| File | Purpose |
|------|---------|
| `server.py` | MCP server — all tool definitions, LLM client construction (text + vision) |
| `cli.py` | Click CLI entry points (`scan`, `trace`, `serve`, `serve-http`) |
| `client.py` | DrupalClient facade (composes scanner + JSON:API client) |
| `jsonapi_client.py` | Async HTTP client for Drupal JSON:API reads and writes |
| `scanner.py` | AxeCoreScanner — Playwright + axe-core 4.10.2 injection |
| `tracer.py` | ViolationTracer — maps axe violations to Drupal entities |
| `remediator.py` | Two-phase remediation: vision alt text → text LLM fixes → verify |
| `content_accuracy.py` | ContentAccuracyChecker — vision-model verification of text/image alignment |
| `prompts.py` | LLM prompt templates + `ALT_TEXT_SCHEMA` for structured vision output |
| `config.py` | YAML + env var configuration loading (`LLMConfig` with native API settings) |
| `http_api.py` | FastAPI HTTP service exposing the same operations to the Drupal module |
| `scan_payload.py` | Helpers for expanding axe-core results and filtering template noise |
| `run_store.py` | SQLite-backed run history for the HTTP API |

## Setup

### Prerequisites

- Python 3.11+
- Playwright (`playwright install chromium`)
- Access to a Drupal site with JSON:API enabled
- An Ollama Cloud account (or local Ollama instance) with vision-capable model

### Install

```bash
pip install -e ".[scanner,llm,dev]"
playwright install chromium
```

### Configuration

**config.yaml** — define your Drupal sites and LLM backend:

```yaml
sites:
  - campus_code: ELAC
    name: East Los Angeles College
    base_url: "https://elac-dev.laccd.edu"
    auth_type: cookie
    http_auth_username: "ELAC"    # Acquia dev-shield (optional)
    http_auth_password: "ELAC"
    headless: true

llm:
  backend: ollama
  api_mode: native              # "native" for /api/chat, "openai_compat" for /v1/chat/completions
  base_url: "https://ollama.com/api"
  text_model: "kimi-k2.6:cloud"
  vision_model: "kimi-k2.6:cloud"
  max_concurrent: 5
  temperature: 0.0               # deterministic output
  top_p: 1.0
  seed: 42                       # reproducible results (varied per retry)
  disable_thinking: true          # prevent thinking models from wasting tokens on reasoning
  text_max_tokens: 8192
  vision_max_tokens: 256
```

**.env** — credentials (not committed):

```
DRUPAL_ELAC_USERNAME=your_drupal_user
DRUPAL_ELAC_PASSWORD=your_drupal_password
OLLAMA_API_KEY=your_ollama_api_key
OLLAMA_BASE_URL=https://ollama.com/api
VISION_MODEL=kimi-k2.6:cloud
TEXT_MODEL=kimi-k2.6:cloud
```

The vision model uses Ollama's native `/api/chat` endpoint with structured JSON outputs. Images are sent as raw base64 in `messages[].images`, and the model returns `{"alt_text": "...", "decorative": bool}` enforced by a JSON schema in the `format` parameter. The `disable_thinking: true` setting is important for thinking-capable models like kimi-k2.6 that would otherwise consume tokens on reasoning traces without producing useful content.

### Run the MCP Server

```bash
remedy-drupal serve --config config.yaml --env .env
```

Or configure it in your Claude Code MCP settings to start automatically.

### Run the HTTP API

The HTTP API is the production integration point for the Drupal module:

```bash
export REMEDY_DRUPAL_API_TOKEN=change-me
remedy-drupal serve-http --config config.yaml --env .env --host 0.0.0.0 --port 8787
```

See `docs/production-deployment.md` for the Docker build, required environment variables, and deployment controls.

## MCP Tools

### Scanning
- **scan_page**(page_url, campus_code) — single page WCAG 2.1 AA scan
- **scan_batch**(urls, campus_code) — scan multiple pages concurrently
- **scan_content_list**(campus_code, content_type, max_pages) — crawl and scan all content of a type

### Reading
- **get_page_info**(nid, campus_code) — title, type, moderation state, paragraph count
- **get_page_body**(nid, campus_code) — extract body HTML
- **get_paragraph_fields**(nid, campus_code, row_index) — all fields for a paragraph row
- **get_media_info**(mid, campus_code) — media entity fields

### Tracing
- **trace_violations**(nid, campus_code) — scan + trace each violation to its Drupal source entity

### Writing
- **update_page_body**(nid, campus_code, html, revision_message) — update node body via JSON:API
- **update_paragraph_fields**(nid, campus_code, row_index, field_values) — update paragraph fields
- **update_media_fields**(mid, campus_code, fields) — update media alt text, title, etc.
- **replace_media_file**(mid, campus_code, file_path) — upload a replacement file

### Remediation
- **remediate_page**(nid, campus_code, max_cycles) — scan + LLM-fix body field (up to 3 cycles)
- **remediate_traced**(nid, campus_code, max_cycles) — trace violations + apply targeted entity fixes

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/pii_scanner.py` | Scan downloaded documents (PDF, DOCX, XLSX, etc.) for PII patterns |
| `scripts/media_audit.py` | Audit media usage — find referenced vs unreferenced media entities |
| `scripts/export_site_config.py` | Export site structure (content types, paragraph types, media types) |

## Drupal Module (`drupal_module/remedy/`)

The companion Drupal module that talks to this backend lives in `drupal_module/remedy/`. It exposes the admin UI at `/admin/config/content/remedy/settings`, posts to the HTTP API using the bearer token configured via `REMEDY_DRUPAL_API_TOKEN`, and stores remediation issues via its `IssueRepository` service.

Drupal machine names (`remedy.*` info/services/routing/permissions, `Drupal\remedy\*` namespace) are intentionally kept stable so existing site config and module enable state continue to work. Only the human-facing display name is `Remedy Drupal`.

---

# YouTube Caption Overlay

A companion Drupal custom field module that overlays synchronized SRT/VTT captions on embedded YouTube videos for ADA Title II / WCAG 2.1 AA compliance.

**Problem:** LACCD campus sites embed YouTube videos they don't own and can't control the captions for. YouTube's auto-captions are often inaccurate, and there's no way to add corrected captions to someone else's video.

**Solution:** This module renders a caption overlay on top of the YouTube iframe, driven by an uploaded SRT or VTT file, giving content editors full control over caption accuracy.

Source lives in the sibling suite project at `../remedy-youtube_caption_overlay` and is installed as a custom module on LACCD Drupal sites. The section below is retained here for reference; see that project's README for current details.

## How It Works

### Field Architecture

The module provides a custom Drupal field type with three plugin components:

**FieldType** (`YoutubeCaptionOverlayItem`) — stores per-field-instance data:
- `youtube_video_id` — the 11-character YouTube video ID
- `caption_fid` — file entity reference to the uploaded SRT/VTT file
- `caption_lang` — ISO 639-1 language code (e.g., "en", "es")
- `caption_label` — human-readable label (e.g., "English")

**FieldWidget** (`YoutubeCaptionOverlayWidget`) — the edit form:
- Text input for the YouTube video ID
- Managed file upload accepting `.srt` and `.vtt` files (5MB max)
- Language code and label fields

**FieldFormatter** (`YoutubeCaptionOverlayFormatter`) — renders the output:
- Generates a YouTube iframe with `enablejsapi=1` for API control
- Attaches data attributes for the JS behavior (video ID, caption URL, format)
- Configurable caption styling (font size, background color, text color)
- Optional transcript panel toggle

### Caption Rendering Flow

```
1. Page loads → Drupal renders the field as an iframe + data attributes
2. JS behavior attaches (via Drupal.behaviors)
3. YouTube IFrame API loaded lazily
4. Caption file fetched via XHR
5. SRT/VTT parser converts to timed cue array: [{start, end, text}, ...]
6. YT.Player created from the iframe
7. On PLAYING state → sync loop starts (100ms interval)
8. Each tick: find active cue by currentTime, update caption overlay text
9. On PAUSE/END → sync loop stops
```

### Dual Strategy

The module handles two common embedding patterns on LACCD sites:

**Strategy 1: Direct iframes** (primary)
- The formatter renders a YouTube iframe directly in the field wrapper
- JS finds the iframe by matching the video ID in the src URL
- Wraps it with the caption overlay DOM and starts sync

**Strategy 2: Bootstrap modal lightboxes** (secondary)
- Many LACCD pages use oEmbed-based media embeds that open in Bootstrap modals
- When a modal opens (`shown.bs.modal` event), the JS:
  - Finds the oEmbed proxy iframe inside the modal
  - Extracts the video ID from the proxy URL
  - Replaces the proxy iframe with a direct YouTube embed
  - Wraps with caption overlay and starts sync
  - Cleans up intervals on modal close to prevent memory leaks

### Caption Overlay UI

```
┌─────────────────────────────────┐
│                                 │
│         YouTube Video           │
│                                 │
│  ┌───────────────────────────┐  │
│  │  Caption text appears here│  │
│  └───────────────────────────┘  │
└─────────────────────────────────┘
  [CC]  [⛶ Fullscreen]  [Transcript]
```

- **CC button** — toggles caption visibility
- **Fullscreen button** — custom implementation (YouTube's native fullscreen disabled via `fs=0` so captions remain visible)
- **Transcript button** — toggles a scrollable transcript panel below the video with the current cue highlighted

### Caption Parsers

**SRT Parser** (`srt-parser.js`):
- Parses SubRip format (block number, `HH:MM:SS,mmm --> HH:MM:SS,mmm`, text)
- Handles both comma and period as millisecond separators
- Supports multi-line captions

**VTT Parser** (`vtt-parser.js`):
- Parses WebVTT format (strips WEBVTT header, handles cue IDs)
- Strips VTT formatting tags (`<v>`, `<c>`, `<b>`, `<i>`)
- Supports both `MM:SS.mmm` and `HH:MM:SS.mmm` timestamps

Both parsers output the same format: `[{start: float, end: float, text: string}, ...]` sorted by start time.

### Fullscreen Behavior

YouTube's native fullscreen hides all page-level overlays, so the module:
1. Disables YouTube's fullscreen button (`fs=0` in iframe params)
2. Provides a custom fullscreen button that fullscreens the `.yco-field-wrapper` container
3. In fullscreen mode, captions scale up to `1.5em` and reposition above the controls area
4. The responsive wrapper switches from padding-based aspect ratio to flexbox fill

### Accessibility

- Caption display uses `role="status"` and `aria-live="polite"` for screen reader announcements
- All control buttons have `aria-label` and keyboard focus support
- Transcript panel uses `role="log"` with `aria-label`
- High contrast mode support via `prefers-contrast: more` media query
- Reduced motion support disables transcript scroll animations
- HTML in captions is escaped to prevent XSS from untrusted caption files
