# Amber

A time capsule for web pages — a self-hosted [archive.today](https://archive.ph/) style snapshotter.

Paste a URL. Amber opens it in Chromium, then stores:

- **Webpage** — HTML after JavaScript has run, with scripts and forms stripped
- **Screenshot** — a graphical copy of the rendered page
- **Text** — a readable article extract for news and blogs

Each snapshot gets a short unchanging link such as `/K7mQp`.

## Run (Windows)

From PowerShell:

```powershell
cd C:\Users\Jerem\OneDrive\Documents\Code\amber
.\run.ps1
```

Then open [http://127.0.0.1:8080](http://127.0.0.1:8080).

`run.ps1` creates `.venv`, installs dependencies, installs Playwright’s Chromium, and starts the server. Use `python -m pip` (never a bare `pip` command) so Windows does not try to “open pip” with another app.

Manual setup:

```powershell
cd C:\Users\Jerem\OneDrive\Documents\Code\amber
C:\Python313\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## Bookmarklet

While Amber is running, bookmark:

```
javascript:void(location.href='http://127.0.0.1:8080/save?url='+encodeURIComponent(location.href))
```

## What it is for

Pages that may change or disappear: news articles, blog posts, price lists, job offers, listings. Amber loads the page as a first-time visitor (fresh browser, no cookies). Soft overlays that hide already-loaded text are removed. Metered paywalls that already grant a free read to Google or X visitors are retried that way in a fresh browser. Login-only pages stay incomplete; import saved HTML for those.

Saved pages have no scripts and no active forms.

## Layout

```
app/            FastAPI app, capture pipeline, HTML freezer
templates/      homepage, saving progress, snapshot viewer
static/         CSS and favicon
data/           SQLite index + snapshot files (gitignored)
```

Snapshots live in `data/snaps/<id>/` as `page.html`, `screenshot.jpg`, `article.txt`, and rewritten CSS/images.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest
```

Unit tests cover URL guards, HTML freezing, and article extraction (including a The Information-style signup wall). `tests/test_e2e_capture.py` runs a real Playwright capture against a local article page.
