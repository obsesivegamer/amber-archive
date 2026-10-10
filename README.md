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

No auth, meant for local tools. The `GET` routes are read-only; `POST /api/save` queues a capture. Every route only answers requests whose Host header is `127.0.0.1`, `localhost`, `[::1]` or the configured `AMBER_HOST` (anything else gets 400, which blocks DNS rebinding). If you bind to a non-loopback address (`AMBER_HOST` or `--host`), set `AMBER_HOST` to the name or address clients use, otherwise LAN clients get 400.

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

`GET /api/lookup?url=<article url>` tells a client whether an article is already archived, without saving anything. It matches the normalized saved URL or the final (post-redirect) URL, ignoring tracking parameters, a trailing slash and http vs https. A missing scheme is treated as https; anything other than http(s) answers `400 {"detail": "..."}`. No DNS lookup is made.

```json
{"found": false}
{"found": true, "id": "K7mQp", "status": "complete", "usable": true, "title": "...", "site": "Financial Times",
 "url": "...", "final_url": "...", "created_at": "2026-10-09T19:25:47Z"}
```

The newest `complete` snapshot wins; otherwise the newest of any status is returned. `usable` is true only when `status` is `complete` and the snapshot passes the usable rules above (so `/api/snapshots/{id}/text` will answer 200). A complete but unusable snapshot (paywall stub, empty capture) is `found` with `usable: false`.

`POST /api/save` with `Content-Type: application/json` and body `{"url": "https://...", "force": false}` queues a capture of a public article. The body must be JSON: form posts and other content types get `415`, which stops a cross-site form from triggering a save (the API sends no CORS headers either). URLs are checked with the same rules as the web form (http(s) only, no credentials, no private or local hosts); a rejected URL answers `400 {"detail": "..."}` and never reaches the queue.

| Response | Meaning |
|---|---|
| `202 {"id", "status": "queued"}` | New capture queued. Poll `GET /api/jobs/{id}`. |
| `202 {"id", "status": "queued" or "capturing"}` | The same URL is already being captured; `id` is that job. |
| `200 {"id", "status": "exists", "usable"}` | A complete snapshot already exists and `force` is false. `id` is that snapshot. |
| `400` / `413` / `415` | Bad URL or JSON / body over 16 KB / not `application/json`. |
| `429` | More than 10 captures are already waiting in the queue. |

`GET /api/jobs/{id}` is the polling endpoint (the job id is the snapshot id). Its `status` is always one of `queued`, `capturing`, `complete` or `failed`; `error` is a string when `failed`. Job ids stay valid after the capture finishes: unknown to the in-memory queue, the answer is rebuilt from the database. A snapshot left `pending`/`capturing` by an Amber restart is reported as `failed` ("Capture was interrupted before it finished."), so a client never polls forever. Once `complete`, read the article from `GET /api/snapshots/{id}/text`; it answers 404 if the capture was saved but is not usable (for example a paywall stub).

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
