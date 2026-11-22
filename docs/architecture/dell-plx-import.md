# Dell-PLX import and Mac convergence

Status: implemented in this repo. Not deployed.

This follows the 13 Nov 2026 plan in `cursor/dell-plx-source-of-truth-ecf3`
(`docs/architecture/dell-plx-source-of-truth.md` there) and the ingest journal
landed in #33. Dell-PLX stays the library the phone reads. The Mac stays the
only Playwright capturer. Gift Links, and any other importer, can POST rendered
HTML straight at Dell-PLX.

## Modules

| Piece | Role |
|---|---|
| `POST /v1/import` on `app.library_ingest` | Same JSON as the Mac's `POST /api/import`. Bearer token. |
| `app.library_import` | Validate, extract, freeze. Loaded only when that route runs. |
| `commit_document` / `commit_new_revision` | The journal the hourly push already uses (`ingest-journal`, `ingest-staging`, `ingest-previous`, `library_revisions`, `library_changes`). |
| `GET /v1/changes?after=` | Commits newer than a cursor. Ingest token. |
| `GET /v1/snapshots/{id}/files/{path}` | Committed file bytes. Blobs are deleted after commit, so the pull reads `snaps/`. |
| `app.library_pull` | Mac command. Copies ids this archive does not have. |
| `app.library_push --backfill` | Unchanged document. Skips ids Dell-PLX already lists. |

`amber-reader` is not modified. It still opens `amber.sqlite3` `mode=ro` and serves `snaps/<id>/`. `content_sha256` is a nullable column on `library_revisions` only. The `snapshots` columns and snapshot filenames stay the ones the reader already opens.

## One runtime path

Gift Links has rendered article HTML on its Linux box. The Mac can be asleep.

1. Gift Links `POST`s `https://dell-plx-reader.tailf9612a.ts.net:8443/v1/import` with `Authorization: Bearer` and the same JSON body as `POST /api/import` (`url`, `html`, optional `title`, `final_url`, `force`).
2. Ingest checks the bearer token, validates the URL, and dedupes by `article_match_key`. A new page is extracted and frozen with no network fetch (`scrub_secrets=True`), then `commit_new_revision` stages the directory, fsyncs, renames it into `snaps/<id>/`, and inserts the `snapshots` row and the next `change_seq` in one transaction.
3. The reader, already running, lists that `complete` row. No reader restart and no new reader route.
4. The Mac hourly job runs `python -m app.library_pull`, then `python -m app.library_push --backfill`. Pull writes any Dell-PLX id the Mac does not have into the Mac archive, so local Amber shows the Gift Links import. Backfill then sends Mac captures whose ids Dell-PLX does not list yet. An id pull just copied is already listed, so it is not sent again.

`change_seq` advances only when a new revision commits. A repeated import of the same HTML, a pull, and a second push of bytes Dell-PLX already stored do not add a row to `library_changes`.

## Blocking decisions

1. **Data ownership.** The Dell-PLX data directory the reader opens is the master copy of snapshot bytes. The Mac keeps a replica and remains the only machine that runs Playwright. Anonymous capture does not move to WSL. This is option B of the Nov 13 plan, plus a direct HTML import on the ingest port.

2. **Duplicates.** Identity is `article_match_key` of the final URL and of the requested URL (host without `www.`, tracking and share tokens removed). The same key and the same SHA-256 of the posted HTML return `200` and the existing id, and do not bump `change_seq`. The same key with different HTML returns `409 {"detail":"already archived","id":...}` unless `force` is true, which allocates a new id, matching the Mac. The hourly push stays keyed by snapshot id. It does not drop a distinct Mac id because Dell-PLX already has another id for that URL. It also does not overwrite an id: the same bytes are `200`, and different bytes for that revision are `409`.

3. **Auth.** `POST /v1/import` accepts the ingest bearer token. When `AMBER_IMPORT_TOKEN_FILE` is set, that second token can call only `POST /v1/import`. Push, pull, `/v1/revisions`, `/v1/blobs`, `/v1/changes`, and file reads stay on the ingest token. The reader password is not used. Publish with `tailscale serve`, not `funnel`.

4. **Hourly push.** The revision document `library_push` already sends is unchanged, including revision 1. Import commits through that journal as revision 1. Pull is a separate command run before `--backfill` in the same hourly job. Existing push clients keep working against the new ingest.

## Defaults

- Deletes and tombstones are not in this slice. Pull stops if it sees an op other than `upsert`, so a later tombstone cannot be skipped by accident.
- No Playwright thumbnail on Dell-PLX. The snapshot still has `page.html`, `reader.html`, `article.html`, `article.txt`, and `meta.json`.
- Extraction runs in the ingest process after the same complexity checks the Mac uses before its child. A page that passes those checks can still stall ingest. The import token is what keeps that route off the public internet.
- Pull does not replace an id the Mac already has when the bytes differ. The Mac copy stays, and a push of those different bytes is refused with 409.
- The pull cursor is `library-pull-cursor` in the Mac data directory.
- Importers prefer Dell-PLX and fall back to the Mac's `POST /api/import` when Dell-PLX does not answer. The next pull+push is what removes the gap. The exact request is `docs/ops/dell-plx-import.md`.
