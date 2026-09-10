---
name: verify-amber
description: Verify Amber's web UI (save URL, import HTML, search, browse/delete, snapshot viewer) by launching an isolated FastAPI instance and driving it with a browser plus HTTP. Use when proving Amber behavior after a change, checking a mapped feature, or when asked to verify the archive app.
---

# Verify Amber

Amber is a self-hosted web-page archive. A user pastes a public URL (or imports saved HTML). The app stores a frozen webpage, a screenshot, and an article extract at a short link such as `/K7mQp`.

Primary surface: the FastAPI HTML UI at the instance URL. Secondary: the same routes over HTTP (forms, `GET /api/jobs/{id}`, snapshot files). There is no CLI. `pytest` is complementary and is not this harness.

Read `features/README.md` before driving. Use the matching feature file. A proof that takes one convenient entry point is incomplete when that file lists others.

## Launch

Never drive the user's live instance. Live Amber is `http://127.0.0.1:8080` with SQLite and files in repo `data/`. Verification uses port **18080** and a disposable data dir under this skill's `.scratch/`.

From the repo root, Windows:

```powershell
powershell -NoProfile -File .cursor\skills\verify-amber\scripts\launch.ps1
```

Linux / macOS (this skill's bash helpers; same isolation rules):

```bash
bash .cursor/skills/verify-amber/scripts/launch.sh
```

Ready when stdout contains `Amber verify instance ready at http://127.0.0.1:18080` and `GET http://127.0.0.1:18080/` returns 200 with title `Amber — a time capsule for web pages`.

`launch.ps1` / `launch.sh` set `AMBER_DATA_DIR` to `.cursor/skills/verify-amber/.scratch/data`, start `.venv` Python as `-m uvicorn app.main:app --host 127.0.0.1 --port 18080`, and write `.cursor/skills/verify-amber/.scratch/instance.json`. They reuse a healthy existing verify instance instead of starting a second one on the same port.

Override port with `$env:AMBER_VERIFY_PORT` / `AMBER_VERIFY_PORT` (must still not be 8080). Two verify instances may run only if they have different ports **and** different `AMBER_DATA_DIR` values. If 18080 is already taken by something that is not this skill's `instance.json`, refuse and stop.

Python is `.venv\Scripts\python.exe` on Windows and `.venv/bin/python` on Linux/macOS. If that interpreter is missing, run the README setup first (`.\run.ps1` or the manual venv + `pip install` + `playwright install chromium` steps), then launch again. Do not install into the user's live `data/` tree.

Teardown:

```powershell
powershell -NoProfile -File .cursor\skills\verify-amber\scripts\cleanup.ps1
```

```bash
bash .cursor/skills/verify-amber/scripts/cleanup.sh
```

## Doctor

Read-only. Run this first whenever anything looks off:

```powershell
powershell -NoProfile -File .cursor\skills\verify-amber\scripts\doctor.ps1
```

```bash
bash .cursor/skills/verify-amber/scripts/doctor.sh
```

Doctor is worth driving only when all of these hold:

- `.scratch/instance.json` exists and its `pid` is still alive
- `url` is `http://127.0.0.1:<verify-port>` and the port is **not** 8080
- `data_dir` is under this skill's `.scratch/` and is **not** the repo `data/` directory
- `GET {url}/` is 200 and the body contains `time capsule for web pages`
- `GET {url}/about` is 200
- `{data_dir}/amber.sqlite3` exists

If doctor fails, do not continue. Fix launch or clean up and relaunch. If you find a server on 8080, leave it alone — that is the user's archive.

## Drive

Harness: **cursor-ide-browser** for the user path when that MCP is available; otherwise drive the same verify URL with the available browser (or HTTP). Plus **HTTP** for the same form actions and for job polling. Prefer accessible names and element ids from the templates over coordinates. Never fall back to port 8080.

Base URL is the verify instance from `instance.json` (default `http://127.0.0.1:18080`).

Stable handles (from `templates/`):

| Control | Handle |
|---|---|
| Home | `GET /` or link named `Amber` (wordmark, `href="/"`) |
| URL field | textbox `#url`, name `url`, labeled `My url is alive and I want to archive its content` |
| Save | button `save` (POST `/save`) |
| HTML file | file input `#file`, name `file`, labeled `I already saved the page as HTML` |
| Import | button `import` (POST `/import`) |
| Search field | searchbox `#q`, name `q`, labeled `I want to search the archive` |
| Search | button `search` (GET `/search?q=`) |
| Browse saved | link `browse` (`href="/saved"`), also `all saved` on the home recent list |
| About | link `How it works` (`href="/about"`) |
| Snapshot modes | links `article` (`/{id}`), `webpage` (`/{id}/webpage`), `screenshot` (`/{id}/screenshot`) |
| Delete | button `delete` (POST `/saved/{id}/delete`); browser `confirm` text is `Delete /{id}? This cannot be undone.` |
| Already-saved | button `save a new snapshot anyway` (hidden `force=1`) and link `cancel` |
| Saving status | `#status-line` (`Loading …` then `Saved …`); poll `GET /api/jobs/{id}` |

Browser recipe:

1. `browser_tabs` list, then `browser_navigate` to the verify URL (not 8080).
2. `browser_lock` with `action: "lock"` before a sequence of interactions.
3. `browser_snapshot` for refs. Click and fill by role/name or `#id`.
4. `browser_take_screenshot` for visual proof.
5. `browser_lock` with `action: "unlock"` when the run is done.

HTTP equivalents (same user routes, no test-only endpoints):

```powershell
curl.exe -sS "http://127.0.0.1:18080/"
curl.exe -sS -D - -o NUL --max-redirs 0 -X POST -F "url=https://example.com/" "http://127.0.0.1:18080/save"
curl.exe -sS -D - -o NUL --max-redirs 0 -F "file=@.cursor/skills/verify-amber/fixtures/article.html;filename=article.html;type=text/html" "http://127.0.0.1:18080/import"
curl.exe -sS "http://127.0.0.1:18080/search?q=VERIFICATION_TOKEN_AMBER_BRIDGE_2026"
curl.exe -sS "http://127.0.0.1:18080/api/jobs/JOBID"
```

Linux / macOS (`curl`; `-o /dev/null` instead of `NUL`):

```bash
curl -sS "http://127.0.0.1:18080/"
curl -sS -D - -o /dev/null --max-redirs 0 -X POST -F "url=https://example.com/" "http://127.0.0.1:18080/save"
curl -sS -D - -o /dev/null --max-redirs 0 -F "file=@.cursor/skills/verify-amber/fixtures/article.html;filename=article.html;type=text/html" "http://127.0.0.1:18080/import"
curl -sS "http://127.0.0.1:18080/search?q=VERIFICATION_TOKEN_AMBER_BRIDGE_2026"
curl -sS "http://127.0.0.1:18080/api/jobs/JOBID"
```

Seed a snapshot without the file picker (same `/import` form the homepage posts):

```powershell
powershell -NoProfile -File .cursor\skills\verify-amber\scripts\import-fixture.ps1
```

```bash
bash .cursor/skills/verify-amber/scripts/import-fixture.sh
```

Stdout is `snapshot_id=<id>`. Use that id for search, browse, and viewer recipes.

Saving a live URL is blocked for localhost and private hosts (`app/security.py`). Do not try to archive `127.0.0.1`. A real `/save` proof needs a public `http(s)` URL (or accept the flash error for a rejected URL). Capture waits up to ~45s navigation + render; poll `/api/jobs/{id}` until `status` is `complete` or `failed`. Do not sleep a fixed time and declare success.

## Evidence

Write proof under `.cursor/skills/verify-amber/evidence/<feature-id>/`. Cleanup must not delete this tree.

Proof standards:

- Drive the real user path (homepage forms, `/save`, `/import`, `/search`, `/saved`, `/{id}` modes). Do not call `ingest_html` or patch `app.db` as a substitute for the UI.
- Capture the action and the resulting state: homepage or form before submit, then the saving page or snapshot viewer after.
- Pair the visible result with a side effect: a row in `{data_dir}/amber.sqlite3`, files under `{data_dir}/snaps/<id>/` (`page.html`, `reader.html`, `article.txt`; `screenshot.jpg` after a successful Playwright shot).
- UI proof: an ARIA snapshot (`.aria.txt`) and a screenshot (`.png`) that show the Amber wordmark and the feature-specific text (title, token, mode, or error).
- HTTP proof: status, `Location` if any, and a body excerpt saved as `.http.txt`.
- Record the feature id and entry point in `evidence/<feature-id>/meta.txt`.
- Mocks only at the production boundary: `/save` hitting the public internet is real; do not stub Playwright. Import may skip a screenshot if Chromium fails — say so, and still prove `reader.html` + article text.

Minimum files for a feature proof:

```
evidence/<feature-id>/meta.txt
evidence/<feature-id>/before.png
evidence/<feature-id>/after.png
evidence/<feature-id>/after.aria.txt
```

Plus whatever disk or HTTP dump the feature file names.

## Cleanup

```powershell
powershell -NoProfile -File .cursor\skills\verify-amber\scripts\cleanup.ps1
```

```bash
bash .cursor/skills/verify-amber/scripts/cleanup.sh
```

Kills only the PID in `.scratch/instance.json` (and that PID's remaining children on Unix), then removes `.scratch/` (data dir, logs, instance file). Does not kill by process name. Does not touch repo `data/`, port 8080, or `evidence/`.

After a failed iteration, run cleanup before the next launch so port 18080 and the scratch DB are not stranded.

## Helpers

All scripts are launched from the repo root as shown.

| Script | Purpose |
|---|---|
| `scripts/launch.ps1` / `scripts/launch.sh` | Start or reuse the isolated verify instance |
| `scripts/doctor.ps1` / `scripts/doctor.sh` | Read-only health check; exit 0 only if worth driving |
| `scripts/import-fixture.ps1` / `scripts/import-fixture.sh` | POST `fixtures/article.html` to `/import`; print `snapshot_id=` |
| `scripts/cleanup.ps1` / `scripts/cleanup.sh` | Stop the launched PID; delete scratch only |

Use the `.ps1` files from PowerShell on Windows. Use the `.sh` files from bash on Linux/macOS. Do not mix a Windows launch with a Unix cleanup (or the reverse): each pair writes and reads the same `.scratch/instance.json`.

Fixture `fixtures/article.html` is a news-shaped page titled **Amber Verification Bridge** whose body contains the unique token `VERIFICATION_TOKEN_AMBER_BRIDGE_2026` and byline Casey Prover. Search, browse, and viewer proofs assert that token or title.
