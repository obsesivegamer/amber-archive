# Import a rendered page onto Dell-PLX

Gift Links and any other importer should POST here first. The Mac's
`POST /api/import` is the fallback for when this port does not answer. The
hourly Mac job pulls Dell-PLX changes and then pushes Mac captures, so a
fallback import still reaches the phone, and a Dell-PLX import still shows up
in the Mac archive. The request shape and the decisions are in
`docs/architecture/dell-plx-import.md`.

The reader is a different port. Do not POST HTML to `https://dell-plx-reader.tailf9612a.ts.net/` (that is amber-reader on 443). The write origin is the ingest port.

## Request

```text
POST https://dell-plx-reader.tailf9612a.ts.net:8443/v1/import
Authorization: Bearer <importer token>
Content-Type: application/json
```

The token is the file `AMBER_IMPORT_TOKEN_FILE` on Dell-PLX when that file is set, otherwise the ingest token (`AMBER_INGEST_TOKEN_FILE`). Give Gift Links the importer token when it exists. That token cannot push revisions or read the library. `Content-Length` is required. The body cap is 12 MiB, the same cap as the Mac.

```json
{
  "url": "https://www.example.com/story",
  "final_url": "https://www.example.com/story",
  "title": "Headline",
  "html": "<!doctype html><html><head><title>Headline</title></head><body><article><h1>Headline</h1><p>…</p></article></body></html>",
  "force": false
}
```

`url` and `html` are required. `html` is at least 200 characters of the rendered page. `title` is optional and is used only when the page has no title of its own. `final_url`, when set, must be http(s), public, and the same site as `url`. `force` defaults to false.

Nothing is fetched. The HTML is frozen and extracted as posted.

## Answers

| Status | Meaning |
|---|---|
| `201` | Stored. `id`, `status` (`complete`), `usable`, `word_count`, `title`, `revision`, `change_seq`. |
| `200` | This URL and these HTML bytes are already stored. Same body, same `id`, `change_seq` unchanged. |
| `409` | This URL is already archived with different HTML. `{"detail":"already archived","id":"<id>"}`. `force: true` stores a new id instead. |
| `401` | Missing or wrong bearer token. |
| `400` / `413` / `415` | Bad JSON, a URL the Mac would reject, body over 12 MiB, or not JSON. |
| `422` | The HTML was too complex or could not be turned into a snapshot. |

`usable` is true when the extract has at least 120 words and a real title. The Mac's stub check is stricter.

A client should treat `200` and `409` as "the archive already has this article" and read `id`. Retry `201` only when the connection failed before a status.

## Example

```bash
TOKEN_FILE="$HOME/.config/gift-links/dell-plx-import.token"
jq -n \
  --rawfile html page.html \
  --arg url 'https://www.example.com/story' \
  '{url:$url, final_url:$url, title:"Headline", html:$html}' \
| curl -sS -X POST \
  -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
  -H 'Content-Type: application/json' \
  --data-binary @- \
  'https://dell-plx-reader.tailf9612a.ts.net:8443/v1/import'
```

`curl --data-binary` sends `Content-Length`. Do not use chunked transfer.

## Fallback

If that request fails because the connection is refused, times out, or returns `5xx`, POST the same JSON to the Mac:

```text
POST http://127.0.0.1:8080/api/import
Content-Type: application/json
```

The Mac route has no bearer token. It is reachable on the Mac loopback, or on the Mac's own tailnet name when `AMBER_ALLOWED_HOSTS` is set. Do not point the fallback at the reader origin. A `401` from Dell-PLX is a bad token, not a reason to fall back. A `409` or `200` is success and must not be retried against the Mac, or the two copies will each mint an id for one article until someone notices.

The Mac hourly job, pull then backfill, is what makes a fallback import show on the phone and a Dell-PLX import show on the Mac:

```bash
cd "$HOME/Documents/amber-archive"
DATA="$HOME/Documents/amber-archive/data"
URL="https://dell-plx-reader.tailf9612a.ts.net:8443"
TOKEN="$HOME/Library/Application Support/amber/ingest.token"

AMBER_DATA_DIR="$DATA" .venv/bin/python -m app.library_pull \
  --ingest-url "$URL" --token-file "$TOKEN"
AMBER_DATA_DIR="$DATA" .venv/bin/python -m app.library_push --backfill \
  --ingest-url "$URL" --token-file "$TOKEN"
```

Pull uses the ingest token, not the importer token.
