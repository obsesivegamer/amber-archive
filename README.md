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

## Run (macOS)

From Terminal, in the repo folder:

```bash
./run.sh
```

Then open [http://127.0.0.1:8080](http://127.0.0.1:8080). Press `Ctrl+C` to stop.

`run.sh` does what `run.ps1` does: creates `.venv` if missing, installs dependencies, installs Playwright's Chromium, and starts the server. The first run downloads Chromium, so it takes a minute or two. If `run.sh` is not executable, run `chmod +x run.sh` once.

## Bookmarklet

While Amber is running, bookmark:

```
javascript:void(location.href='http://127.0.0.1:8080/save?url='+encodeURIComponent(location.href))
```

## What it is for

Pages that may change or disappear: news articles, blog posts, price lists, job offers, listings. Amber loads the page as a first-time visitor (fresh browser, no cookies). Soft overlays that hide already-loaded text are removed. Metered paywalls that already grant a free read to Google or X visitors are retried that way in a fresh browser. Extract prefers the article body over a related-story card that readability sometimes scores as the story. Login-only pages stay incomplete; import saved HTML for those.

Saved pages have no scripts and no active forms.

## Layout

```
app/            FastAPI app, capture pipeline, HTML freezer
templates/      homepage, saving progress, snapshot viewer
static/         CSS and favicon
data/           SQLite index + snapshot files (gitignored)
```

Snapshots live in `data/snaps/<id>/` as `page.html`, `screenshot.jpg`, `article.txt`, and rewritten CSS/images.

## JSON API

Read-only, no auth, meant for local tools. Every route only answers requests whose Host header is `127.0.0.1`, `localhost`, `[::1]` or the configured `AMBER_HOST` (anything else gets 400, which blocks DNS rebinding). If you bind to a non-loopback address (`AMBER_HOST` or `--host`), set `AMBER_HOST` to the name or address clients use, otherwise LAN clients get 400.

Only *usable* snapshots appear: status `complete`, a stored `word_count` of at least 120, a title that is not just the site or host name, and article text of at least 120 words after extraction (see `/text` below). Paywall stubs are excluded too: text under 400 words that opens with a "Subscribe to unlock/read" or "Already a subscriber" notice. Failed paywall captures are excluded by these rules.

`GET /api/snapshots?since_days=14&limit=60` lists usable snapshots saved in the last `since_days` days (default 14, clamped to 1-365), newest first, at most `limit` (default 60, clamped to 1-200):

```json
{
  "total_usable": 42,
  "sources": [{"site": "Financial Times", "count": 20, "latest": "2026-10-09T19:25:47Z"}],
  "snapshots": [{"id": "K7mQp", "url": "...", "final_url": "...", "title": "...", "site": "Financial Times",
                 "author": null, "published_at": null, "created_at": "2026-10-09T19:25:47Z",
                 "word_count": 1200, "paywalled": false}]
}
```

`total_usable` and `sources` cover the whole window, before `limit`. When the same URL was saved more than once, only the newest usable copy is listed. `site` is a normalized display name: the stored site name, or for bare hostnames such as `nytimes.com` the publication's name.

`GET /api/snapshots/{id}/text` returns the same fields plus `text`: the article body with paragraphs separated by blank lines, rebuilt from `article.html` (only the marked article body when the page has one; menus, related-story lists and newsletter filler dropped) and falling back to `article.txt`. Extracted text is cached per snapshot until its files change, so the first list call after a restart is slower than later ones. It answers `404 {"detail": "..."}` for unknown, malformed, incomplete or unusable ids.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest
```

Unit tests cover URL guards, HTML freezing, and article extraction (including a The Information-style signup wall). `tests/test_e2e_capture.py` runs a real Playwright capture against a local article page.

For container verification, run `docker build -t amber-tests .` followed by
`docker run --rm amber-tests`. See [AGENTS.md](AGENTS.md) for lint and compile commands.

FT imports and captures select the article's `o-topper__headline` heading before
other page headings and exclude `o-banner` promotional headings from fallback
selection. Existing snapshots keep their stored titles; this change does not
rewrite saved articles or change gift-link acquisition.
