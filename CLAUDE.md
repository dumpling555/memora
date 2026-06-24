# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Intelligent Photo Archive & Natural Language Search System** — a lightweight personal photo management and semantic search platform. Photos on a NAS are analyzed by AI (description, tags, OCR), metadata is stored in SQLite, and a Flask web service provides natural-language search and visual browsing.

## How to Run

```bash
pip install -r requirements.txt      # dependency: flask
python web_app/app.py                 # starts Flask on http://127.0.0.1:5000
```

No build step, no Node.js, no Docker. Frontend is a single HTML file loaded from CDN.

## Architecture

### Core Files

| File | Responsibility |
|---|---|
| `web_app/app.py` | Flask app entry point — auth, photo/folder/timeline/file routes, thumb map, scheduler init |
| `web_app/admin_routes.py` | Admin Blueprint (`/admin`) — media sources CRUD, folder visibility, scan trigger/logs, schedules, scheduler toggle |
| `web_app/scanner.py` | Background directory scanner — incremental (new-only) and full (add+update+delete) modes, parallel `scandir` over SMB |
| `web_app/quick_scanner.py` | QuickScanner — periodic incremental scan of all active sources (runs every N minutes) |
| `web_app/scheduler.py` | ScanScheduler — 24h full-scan cron, delegates to `trigger_scan_fn` |
| `web_app/thumb_daemon.py` | ThumbnailDaemon — persistent background thread, retries failed thumbs, parallel generation via ThreadPoolExecutor |
| `web_app/thumb_generator.py` | Thumbnail generator for on-demand / batch — writes to `web_app/cache/thumbnails/`, updates `thumb_progress` table |
| `web_app/thumb_common.py` | Shared thumb utilities — `try_generate()`, `thumb_filename()`, `existing_thumb_map()`, `find_thumb_file()`, `remove_stale_thumb()` |

### Templates & Static

| Path | Content |
|---|---|
| `web_app/templates/index.html` | Main photo browser — grid view, folder sidebar, timeline, search, image detail modal |
| `web_app/templates/admin.html` | Admin dashboard — sources, folder visibility, scan logs, schedules |
| `web_app/templates/landing.html` | Login / password-change / setup landing page |
| `web_app/static/` | Tailwind CDN, RemixIcon fonts/CSS, theme toggle, i18n |

### Database (res.sqlite)

Key tables (created by `_init_admin_db()` in `app.py`):

- **`image_analysis`** — per-photo metadata (file_path, overview, tags, extracted_text, other_info, dimensions)
- **`media_sources`** — NAS root paths (active/inactive)
- **`folder_visibility`** — per-source hidden folder flags
- **`scan_log`** — scan job history (status, counts)
- **`scan_schedules`** — per-source scan intervals
- **`admin_settings`** — key-value store (credentials, scheduler toggle, secret_key)
- **`thumb_progress`** — thumbnail generation job progress

### Data Flow

```
NAS photos → scanner.py (walk + stat) → image_analysis DB
image_analysis DB → app.py (SQL search) → JSON API → index.html
NAS photos → thumb_daemon/thumb_generator → cache/thumbnails/ → serve_thumbnail()
```

### Auth Flow

- `admin_settings.password_hash` stores bcrypt hash (werkzeug)
- `before_request` middleware checks `session['logged_in']` for all non-public routes
- Default credentials: `admin` / `admin@123` (with `must_change_password` flag)
- Public routes: `/login`, `/api/login`, `/api/logout`, `/api/change-password`, `/static/*`, `/api/folder-visibility-version`

## Key Patterns

- **DB connections**: always use `get_db_connection()` (app.py) or `get_db()` (admin_routes/scanner) — sets `row_factory` and `PRAGMA journal_mode=WAL`
- **Active sources**: `get_active_source_roots()` returns normalized paths with trailing `\`
- **Path separator**: Windows backslash (`\`) — uses `chr(92)` in some places, always normalize with `os.path.normpath()`
- **Background threads**: scanners and thumb daemon run as daemon threads; abort via `threading.Event`
- **Scan modes**: `incremental` (default, only new files) vs `full` (add + update changed + delete removed)
- **Media root**: `MEDIA_ROOT` is hardcoded in app.py line 43 — points to NAS mapped path. Should be configurable.

## Important Notes

- `res.sqlite` is the production database — do not commit or delete it
- `web_app/cache/` (thumbnails) is generated at runtime — do not commit
- `package.json` only has `mermaid` dependency (for architecture diagrams) — not needed to run the app
- NAS path must be locally mapped (e.g., `Z:\`) for the app to access files
- The app uses `send_file()` for photo/thumbnail streaming — no strict directory traversal protection in production
