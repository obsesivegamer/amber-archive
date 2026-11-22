# Dell-PLX source of truth

Locked 22 November 2026. Jeremy accepted every default in section 10 of `docs/architecture/dell-plx-sot-v2.md`. That file is the spec. This note records the decisions, the gate on Mac import today, and what the code in this repository does. It does not deploy anything.

## Decisions

1. **Leading id character.** Dell-PLX snapshot ids start with `Z`. The alphabet stays `23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz`, length 5. Mac allocation never emits that prefix. The Dell allocator still checks the live store, because a legacy Mac id may already start with `Z`. A `409` from ingest on a new id is a retry (reallocate), not a skip. Origin is not inferred from the prefix.
2. **Duplicates.** One URL with two ids is cosmetic. Push it and log it. `--backfill` does not refuse a Mac id whose `match_key` is already stored. Ingest logs a warning and keeps both rows.
3. **Tokens.** The import token can only create snapshots, and only through the import worker (freezer, caps, the 300 MiB / 24 h budget). The ingest token, which can replace files on an existing id, stays on Dell-PLX and the Mac. The worker holds the ingest token so it can call `127.0.0.1:8082`. The two tokens must differ. Rotation: write a new import token on Dell-PLX, install it on the Grok Bot box, then retire the old one. Gift Links retries cover the gap.
4. **HTML retention.** Gift Links keeps the HTML until Phase 2 is live, or 14 days, whichever is longer. It fails closed. There is no Mac fallback. `409 {"detail": "already archived", "id": ...}` is success. `201` is success. `400`, `415`, and `422` are permanent. Network errors, `5xx`, and `429` retry. The client timeout is longer than the worker's child deadline (45 s) plus the thumbnail timeout (20 s); use at least 90 s.
5. **Ownership.** Rows and files belong to Dell-PLX once committed. Each id belongs to the allocator whose partition it is in. Nothing in this build changes an existing id across machines. Gifts candidates, `homepage.json`, `sources.json`, and recapture leases stay on the Mac. Gift candidate ids are 8 characters and are not snapshot ids; the snapshot created when a gift is redeemed goes through the Mac allocator.

Phase 2 (Mac pull: `GET /v1/changes`, `GET /v1/blobs/{sha256}`, the `(ingest-store-id, change_seq)` cursor, resync) is not in this branch. It is a follow-up. Tombstones, revision bumps, and retargeting the Chrome extension are not in this build either.

## How Gift Links reaches Mac import today

`POST /api/import` has no bearer token and no session of its own. `app.main` is the gate:

- The process binds `127.0.0.1:8080` unless `AMBER_HOST` / `AMBER_PORT` say otherwise.
- `TrustedHostMiddleware` answers only `127.0.0.1`, `localhost`, `[::1]`, `AMBER_HOST`, and names in `AMBER_ALLOWED_HOSTS`.
- `reject_cross_site_writes` refuses a browser write that carries `Origin` or `Sec-Fetch-Site: cross-site` / `same-site`, unless the origin is this server or a `chrome-extension://` origin. `AMBER_EXTENSION_IDS`, when set, limits which extension ids pass. A client that sends neither `Origin` nor `Sec-Fetch-Site` (curl, a server-side poster) is not treated as a cross-site browser and is allowed through.

There is no import token on this path, and this repository does not record a tailnet ACL in front of it. If Gift Links runs off the Mac, whatever carries the request to that socket (a tailnet proxy, a tunnel, or a bind change) is outside this code. The application gate is the bind address plus the Host allow-list.

## What the code does

- **Mac ids.** `db.allocate_id` is the allocator for `POST /api/import`, the homepage HTML import, `ingest_html`, and Playwright saves (`enqueue_capture`, including a gift redemption). The first character is drawn from the alphabet with `Z` removed.
- **Mac push.** After a successful import or Playwright save, if `AMBER_INGEST_URL` and `AMBER_INGEST_TOKEN_FILE` are set, the app pushes that id. An import waits for its thumbnail task first, when one was scheduled, so `screenshot.jpg` is in the tree before revision 1 is published. Failures are logged. Hourly `python -m app.library_push --backfill` stays the catch-up and does not grow a `match_key` refusal rule.
- **Import worker.** `python -m app.library_import` listens on `127.0.0.1:8083` (not 8081 or 8082). It freezes into `AMBER_IMPORT_SCRATCH_DIR`, dedups against `AMBER_LIBRARY_DIR` opened `mode=ro`, and commits with `library_push.push_snapshot` to ingest. The budget ledger is `AMBER_IMPORT_STATE_DIR/budget.json`. Scratch, state, and the library must be separate directories. A crash before the ingest commit leaves no snapshot row and no `snaps/<id>/` in the library; the client retries. Two concurrent posts of one URL produce one id.

`python -m app.library_ingest` does not import the capture stack and does not import the import worker. It remains the only process that writes the shared SQLite file. The reader stays `mode=ro`. The snapshot schema and `snaps/<id>/` names are unchanged.

The runbook for the machines is `docs/ops/dell-plx-import.md`. Applying it is a later step. This repository does not install tokens, edit the live Mac LaunchAgent, or point Gift Links at Dell-PLX.
