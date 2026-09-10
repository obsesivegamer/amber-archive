# View a snapshot

View a snapshot lets a user open a short Amber link and switch between the article extract, the frozen webpage, and the graphical screenshot, then follow the original URL or delete the record.

## Sub-features

- `view-article` opens `/{id}` with the article iframe.
- `view-webpage` opens `/{id}/webpage` with the frozen page iframe.
- `view-screenshot` opens `/{id}/screenshot` with the JPEG.
- `view-files` serves `/{id}/reader`, `/{id}/raw`, `/{id}/image.jpg`, and `/{id}/thumb.jpg`.
- `view-original` exposes the original URL in the toolbar.
- `view-missing` is 404 for an unknown or failed id. Pending or capturing ids redirect to `/saving/{id}` instead of the viewer.
- `view-paywalled` shows a note on `/{id}` (and webpage / screenshot modes) when the snapshot is a paywalled teaser, with a link to homepage Import.

## How to get to it (user POV)

- Choose a snapshot title from `/`, `/saved`, `/search`, or the already-saved page.
- Open `/{id}` directly.
- In the viewer toolbar, choose `article`, `webpage`, or `screenshot`.

## Driving it with browser and HTTP

Preconditions:

- Doctor is exit 0 on the verify instance.
- A complete fixture snapshot exists (`scripts/import-fixture.ps1` or `scripts/import-fixture.sh`). Note `{id}`.

- **Article mode.** Open `/{id}`. Title contains `Amber Verification Bridge — Amber`. Toolbar link `article` has class `on`. An iframe titled `Archived article` has `src="/{id}/reader"`.
- **Webpage mode.** Choose `webpage`. URL is `/{id}/webpage`. Iframe titled `Archived webpage` has `src="/{id}/raw"`.
- **Screenshot mode.** Choose `screenshot`. URL is `/{id}/screenshot`. An image `Graphical copy of Amber Verification Bridge` has `src="/{id}/image.jpg` when a shot exists.
- **Reader HTTP.** `curl.exe -sS http://127.0.0.1:18080/{id}/reader` (or `curl`) is 200 and contains `VERIFICATION_TOKEN_AMBER_BRIDGE_2026`.
- **Readable article.** `GET /{id}/reader` is a single text column. It does not include publisher share / listen / gift toolbar chrome. Images in the reader body are constrained (`max-width: 100%`, no float) with captions under the figure. Webpage mode (`/{id}/raw`) stays the frozen original.
- **Raw HTTP.** `curl.exe -sS http://127.0.0.1:18080/{id}/raw` (or `curl`) is 200, `Content-Type` is HTML, and the body is the frozen page (scripts stripped).
- **Image HTTP.** `curl.exe -sS -D - -o NUL http://127.0.0.1:18080/{id}/image.jpg` (Linux/macOS: `curl` and `-o /dev/null`). If import's Playwright shot succeeded: 200, JPEG. If not: 404 `No screenshot` — record the skip; do not call article mode failed.
- **Original URL.** Toolbar link `.orig` is the fixture URL `https://verify.example/amber-verification-bridge` and opens in a new tab.
- **Text alias.** `GET /{id}/text` is `303` to `/{id}`.
- **Missing.** `GET /zzzzz` (invalid or unused id) is 404.
- **Proof.** Save `evidence/view-snapshot/article.png` and `article.aria.txt` on `/{id}`, `webpage.png` on `/{id}/webpage`, `screenshot.png` on `/{id}/screenshot` when the image exists, and `meta.txt` naming the entry point.

## Gotchas

- Iframe contents are not in the parent ARIA tree. Assert article text via `GET /{id}/reader` or a screenshot, not only `browser_snapshot` on the viewer chrome.
- `/{id}/text` redirects to article mode. Do not expect a plain-text body at that path.
- Incomplete snapshots (`pending` / `capturing`) redirect to `/saving/{id}` instead of the viewer.
- Screenshot mode needs `screenshot.jpg`. Import writes it only after a successful reader screenshot. `/save` writes it from the live capture.
- Resource files live at `/{id}/r/{filename}`. Those are not a user entry point; they are loaded by the frozen webpage.
