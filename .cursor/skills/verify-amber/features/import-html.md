# Import HTML

Import HTML lets a user turn a file they already saved (browser Save Page, or similar) into an Amber snapshot without fetching the live URL, then opens that snapshot.

## Sub-features

- `import-home` uploads a file from the homepage form and redirects to `/{id}`.
- `import-empty` rejects a missing or empty file and returns to `/?error=`.
- `import-article` shows the extracted title and body on the article view.
- `import-files` writes `page.html`, `reader.html`, and `article.txt` under the snapshot folder.

## How to get to it (user POV)

- On `/`, choose a `.html` file in the field labeled `I already saved the page as HTML` and choose `import`.

## Driving it with browser and HTTP

Preconditions:

- Doctor is exit 0 on the verify instance.
- The file is `.cursor/skills/verify-amber/fixtures/article.html`.

- **Home entry.** Open `/`. Set `#file` to `fixtures/article.html` and choose `import`. The response is `303` to `/{id}` (5-character id).
- **HTTP form.** From the repo root, run `powershell -NoProfile -File .cursor\skills\verify-amber\scripts\import-fixture.ps1` or `bash .cursor/skills/verify-amber/scripts/import-fixture.sh`. Stdout is `snapshot_id=<id>`. This posts the same multipart field `file` as the homepage form.
- **Manual HTTP.** `curl.exe -sS -D - -o NUL --max-redirs 0 -F "file=@.cursor/skills/verify-amber/fixtures/article.html;filename=article.html;type=text/html" http://127.0.0.1:18080/import` (Linux/macOS: `curl` and `-o /dev/null`). Status `303`, `Location: /{id}`.
- **Viewer.** Open `/{id}`. The toolbar `article` link is current, the heading/title includes `Amber Verification Bridge`, and the iframe `Archived article` is `/{id}/reader`.
- **Reader body.** `GET /{id}/reader` is 200 and contains `VERIFICATION_TOKEN_AMBER_BRIDGE_2026` and `Casey Prover`.
- **Missing file.** POST `/import` with no file. Land on `/?error=` with flash `Choose an HTML file to import.`
- **Empty file.** POST `/import` with an empty upload. Flash is `That file was empty.`
- **Disk.** `{data_dir}/snaps/{id}/page.html`, `reader.html`, and `article.txt` exist. `article.txt` contains the token. `screenshot.jpg` may appear after Playwright renders the reader; if it is missing, record that and still treat the article files as the import proof.
- **Proof.** Save `evidence/import-html/before.png` on the homepage, `evidence/import-html/after.png` and `after.aria.txt` on `/{id}`, `http.txt` with the `303` `Location`, and `disk.txt` listing `{data_dir}/snaps/{id}`. `meta.txt` names `import-home` or `import-http`.

## Gotchas

- Import does not run the live Chromium capture pipeline. Do not treat a successful import as proof of `/save`.
- The homepage file input is `required`. Browser automation that clicks `import` without a file will be blocked by the browser, not by Amber's empty-file flash. Use HTTP to prove the server-side empty/missing errors.
- Import may take several seconds while it screenshots `reader.html`. Wait for the `303` to settle on `/{id}` with status 200.
- If the HTML has no extractable original URL, Amber stores `https://example.com/`. The fixture includes a real-looking URL so search-by-host stays meaningful.
- Keep the fixture body above Amber's short-extract threshold (80 words). A shorter file is treated as paywalled and shows `incomplete · short extract` plus the viewer note — that is not the happy-path import proof.
- A second import of the same file creates a second id. Use the id from the latest `Location` header.
