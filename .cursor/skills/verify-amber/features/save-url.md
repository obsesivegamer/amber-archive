# Save a URL

Save a URL lets a user archive a public page into a short Amber link, see live capture progress, reopen an existing snapshot instead of silently overwriting it, and get a homepage error when the URL is empty or not public.

## Sub-features

- `save-home` submits the homepage URL form and lands on `/saving/{id}`.
- `save-bookmarklet` starts the same job from `GET /save?url=`.
- `save-progress` shows `#status-line` moving from `Loading` to `Saved` and then redirects to `/{id}`.
- `save-exists` shows `Already saved — Amber` when that URL already has a complete snapshot, unless the user forces a new one.
- `save-reject` returns to `/?error=` for empty, localhost, or private URLs.

## How to get to it (user POV)

- On `/`, paste a URL into the field labeled `My url is alive and I want to archive its content` and choose `save`.
- While Amber is running, use the bookmarklet: `javascript:void(location.href='http://127.0.0.1:18080/save?url='+encodeURIComponent(location.href))` (verify port, not 8080).
- Open `GET /save?url=<encoded-url>` directly.
- On the already-saved page, choose `save a new snapshot anyway` or `cancel`.

## Driving it with browser and HTTP

Preconditions:

- Doctor is exit 0 on the verify instance.
- Scratch archive does not already contain `https://example.com/` unless you are proving `save-exists`.
- The URL is public `http` or `https`. Localhost is rejected on purpose.

- **Home entry.** Open `/`. Fill `#url` with `https://example.com/` and choose `save`. The browser goes to `/saving/{id}` with `#status-line` starting `Loading https://example.com/`.
- **Bookmarklet / GET entry.** `GET /save?url=https%3A%2F%2Fexample.com%2F` (no `force`). Same `/saving/{id}` redirect unless the URL already exists.
- **HTTP form.** `curl.exe -sS -D - -o NUL --max-redirs 0 -X POST -F "url=https://example.com/" http://127.0.0.1:18080/save`. Status `303`, `Location: /saving/{id}`.
- **Progress.** Poll `GET /api/jobs/{id}` until `status` is `complete` or `failed` (allow ~90s). The saving page `#status-line` becomes `Saved …` and the browser replaces location with `/{id}`.
- **Snapshot result.** `GET /{id}` is 200, title contains the page title or URL, and `{data_dir}/snaps/{id}/` has `page.html`, `screenshot.jpg`, and `article.txt`.
- **Reject empty.** Submit `#url` empty (or `POST /save` with `url=`). Land on `/?error=` and a `.flash` containing `Paste a URL first.`
- **Reject localhost.** Submit `http://127.0.0.1/secret`. Flash contains `Local URLs cannot be archived.` or `Private or local network URLs cannot be archived.`
- **Already saved.** Submit `https://example.com/` again after a complete snapshot. Page title is `Already saved — Amber`, existing rows link to `/{id}`, and no new job starts until `save a new snapshot anyway` (`force=1`).
- **Force new.** On that page, choose `save a new snapshot anyway`. A new `/saving/{id}` job runs and `/saved` lists more than one snapshot for the same URL.
- **Proof.** Save `evidence/save-url/before.png` on the homepage with the URL filled, `evidence/save-url/saving.png` on `/saving/{id}`, `evidence/save-url/after.png` plus `after.aria.txt` on `/{id}`, and `meta.txt` naming the entry point used.

## Gotchas

- The user's bookmarklet in the README points at port 8080. Verification must use the verify port.
- Private and loopback hosts are blocked. A local fixture HTTP server cannot be archived unless you change product code; do not treat pytest's `allow_private` monkeypatch as a user path.
- Capture is asynchronous. A 303 to `/saving/{id}` is not proof the page was archived. Wait for `complete` and then open `/{id}`.
- Re-saving the same normalized URL (tracking query params stripped) hits `save-exists`. Normalize before asserting "already saved".
- `force=1` creates a second snapshot on purpose. Delete those ids from the scratch instance if a later recipe assumes a single row.
- Failed captures stay out of the recent/complete lists. Read `/api/jobs/{id}` `error` and the `.flash#err` on the saving page.
