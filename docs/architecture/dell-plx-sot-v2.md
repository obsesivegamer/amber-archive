# Dell-PLX as the Amber source of truth — plan v2

Status: revised after the Claude Opus 5.5 review (verdict **ISSUES**). 22 November 2026, America/New_York. Jeremy accepted every default in §10. This file stays the spec. The code on this branch implements Phase 0, the Mac allocator and push-on-save from Phase 0.5, and the Phase 1 import worker. Phase 2 pull is not in this branch. No deploy, merge, or machine change is authorized by this document.

This supersedes `/workspace/amber-sot/plan.md`. The review is at `/workspace/amber-sot/opus-review.md`. Findings below were checked against `amber-archive` `main` `8c42ec2e`, with one correction noted in §7 (dedup helpers already accept a `conn`).

## 1. Goal

Importers — in particular Gift Links, which already has rendered HTML on Grok Bot's Linux box — archive straight to Dell-PLX without waiting for Jeremy's MacBook to wake. The phone library at `https://dell-plx-reader.tailf9612a.ts.net/app/` shows a newly imported article as soon as Dell-PLX has stored it.

New snapshots converge on both sides. Deletes, and recaptures of ids already pushed, do not propagate until revision bumps and tombstones ship. That gap already exists today; this build does not pretend to close it.

The six Gift Links imports that landed on the Mac after the last successful push (Saturday 4:21 AM ET, 21 November 2026) reach Dell-PLX on the Mac's next `library_push --backfill`. Do not re-send them to Dell-PLX first; that would give each one a second id.

## 2. Recommended end state

**Shape A with B's ownership policy.**

Dell-PLX is the authoritative library the phone already reads. A separate import worker on Dell-PLX accepts the same `POST /api/import` payload Gift Links uses today, freezes HTML in a private scratch directory, and commits through the existing ingest HTTP API. `python -m app.library_ingest` remains the only SQLite writer of the shared data dir. The reader stays read-only.

The Mac stays the capturer for signed-in Chrome and for Playwright saves / gifts redemption. It allocates ids only in its own partition of the existing 5-character alphabet. After a successful local import or save, Mac Amber pushes that id immediately; the hourly `--backfill` is catch-up, not the primary path. The Mac pulls Dell-originated snapshots so the two copies converge. It never changes an existing id across machines in this build.

Gift Links talks only to the Dell-PLX import worker, fails closed when that worker is down, and keeps a durable queue of the HTML until the import is acknowledged (or until the retention window in §10 expires). It does not fall back to the Mac.

A full flip (Mac as a pure client of Dell-PLX for every capture path) is not simpler: Playwright `res/` trees still need a client-chosen id or a new allocate API, and the Mac still needs a queue when Dell-PLX is down. It is a later product, not this build.

## 3. What is true in production today

Unchanged from plan v1, in short:

- Mac Amber at `~/Documents/amber-archive` (`127.0.0.1:8080`) is still the writer of new snapshots, including Gift Links via `POST /api/import`.
- Hourly `python -m app.library_push --backfill` from LaunchAgent `com.jeremylichtman.amber-reader-sync-dellplx`, worktree `~/Documents/amber-archive-ingest`.
- Dell-PLX `python -m app.library_ingest` on `127.0.0.1:8082`, tailnet `https://dell-plx-reader.tailf9612a.ts.net:8443`, bearer token.
- Implemented ingest routes: `GET /healthz`, `GET /v1/revisions`, `PUT /v1/blobs/{sha256}`, `POST /v1/revisions`. Not built: `GET /v1/changes`, `GET /v1/blobs/{sha256}`, any HTML import route, tombstones, revision bumps, or a Mac outbox.
- `library_push` hard-codes revision 1. Backfill skips any id already listed by `GET /v1/revisions`.
- Reader on `127.0.0.1:8081`, `amber.sqlite3` opened `mode=ro`, phone app at `/app/`.

Still unverified, and a Phase 0.5 gate: whether the live LaunchAgent script still rsyncs `snaps/` (with or without `--delete`). After Dell-PLX originates ids the Mac lacks, an rsync with `--delete` would delete those folders; an rsync without `--delete` would still write files outside ingest.

The Mac has two checkouts. Allocator changes land in `~/Documents/amber-archive`. Push and pull changes must land in both that checkout and `~/Documents/amber-archive-ingest`, or the two should be folded into one.

How Gift Links reaches Mac `POST /api/import` today, and what authenticates that path, is not documented in the code excerpts. The excerpted `import_page` and `APIRouter(prefix="/api")` have no auth of their own. Phase 0 should state the actual gate (tailnet ACL, bind address, something in `app.main`, or none).

## 4. Locked design

### Id allocation

Keep `ID_ALPHABET` (`23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz`), `ID_LENGTH` (5), and `ID_RE`. Reserve one leading character for Dell-PLX. Every Mac allocation site — imports, anonymous Playwright saves, gifts redemption, and any other allocator — stops emitting it. This change ships on the Mac, in the app checkout, during one awake session, **before** Dell-PLX allocates anything.

The Dell allocator also checks the Dell store, because legacy Mac ids may already start with the reserved character. A `409` from ingest on a new id is a retryable failure (reallocate), not a skip.

Do not infer origin from the leading character at pull time; record origin explicitly, because legacy Mac ids can start with the reserved character.

### Import worker (Shape A, tightened)

A named process, separate from `python -m app.library_ingest`, with its own loopback port.

- Run `ingest_in_child` into a private scratch data dir, not the shared library dir.
- Commit with the shipped `library_push.push_snapshot(scratch, sid, "http://127.0.0.1:8082", ingest_token)`, then delete the scratch tree.
- Do not call `library_ingest.commit_document` in-process. That would be a second SQLite writer and would race ingest's journal recovery.
- Dedup opens the shared `amber.sqlite3` as `file:…?mode=ro`, same as the reader, so the worker runs as `jerem`.
- `_find_matches` already accepts `conn` and defaults to the module-level store via `db.find_candidates`. The worker must pass a `mode=ro` connection to the shared database. Using the default against a scratch `AMBER_DATA_DIR` would make `409 already archived` never fire. Helpers may need to be callable with only columns Dell-PLX has: `url`, `url_normalized`, `final_url`, and `library_revisions.match_key`.
- One worker process. Hold a lock per `match_key` from the dedup check through the ingest commit, so a Gift Links timeout-and-retry cannot allocate two ids for one URL.
- Budget ledger and scratch dir live outside the shared data dir.
- Serve the exact Mac `POST /api/import` contract (body, status codes, `detail` strings), so Gift Links changes only its base URL and auth header.
- Thumbnails are taken in the worker before the push, with a timeout, or skipped. Never after commit: adding a file later would need revision 2, and nothing produces revision 2.

### Dedup and backfill

Duplicates across the two stores (one URL, two ids, both present on both machines) are cosmetic. They are pushed and logged, never refused. Ingest logs a warning when a new id's `match_key` already exists in `library_revisions`.

The v1 idea of having `--backfill` refuse a Mac id whose URL is already on Dell-PLX is rejected. `GET /v1/revisions` does not return URLs, a refused id stays Mac-only forever, every hourly run exits 1 and hides real failures, and Mac `force` re-imports would be refused forever.

Gift Links treats `409 {"detail": "already archived", "id": …}` as success and never sends `force` on a retry. `201` is also success. 400, 415 and 422 are permanent and get reported. Network errors, 5xx and 429 retry with backoff. The client timeout is longer than the worker's child deadline.

### Auth

A separate import token, held by the Grok Bot box and the worker, can only create new snapshots, and only through the freezer, caps and budget. The ingest token (which can post `revision = stored + 1` for any existing id and replace its files) never leaves Dell-PLX and the Mac. The worker holds the ingest token in order to call `127.0.0.1:8082`.

The worker port is published with `tailscale serve` only, never funnel, plus a tailnet policy that limits that port to the Grok Bot box. Bearer-only, `application/json`-only, no CORS, cookies ignored. Cookies are not isolated by port, so the reader session cookie on `dell-plx-reader.tailf9612a.ts.net` would otherwise reach the worker port.

### Pull and `change_seq`

Implement `GET /v1/changes?after=…` and `GET /v1/blobs/{sha256}`. The Mac cursor is `(ingest-store-id, change_seq)`, not `change_seq` alone. Every changes response carries the store id. On a mismatch, the Mac runs an idempotent full resync that never deletes Mac-only snapshots. Any restore of the Dell-PLX database rotates `ingest-store-id`. The Mac advances its cursor only after the local apply is durable. `change_seq` is assigned inside the commit's write transaction.

Pull never overwrites a local id whose bytes differ; it logs a conflict. Pulled rows go through Mac Amber's own insert path, so the Mac keeps one writer. The Mac refuses to modify ids it pulled. Per-id Mac state: `(revision, origin, change_seq)`, plus the durable cursor and a conflict log.

Ingest already rejects a non-identical revision 1 with `409`. The remaining gap is that Mac recaptures never propagate. Out of scope for this build, same as tombstones.

Blob responses on `GET /v1/blobs/{sha256}` should be `application/octet-stream` with `X-Content-Type-Options: nosniff`.

### Offline behaviour

Gift Links fails closed. No Mac fallback. Gift Links already holds the HTML and already waits for the Mac today, so waiting for Dell-PLX costs nothing. A Mac fallback would recreate the 22 November failure and would duplicate an article if Dell-PLX committed but the response was lost.

Between Phase 1 and Phase 2, a Gift Links article exists only on Dell-PLX. Gift Links keeps the HTML until Phase 2 is live, or for the retention window in §10, whichever is longer.

Chrome imports are not retargeted in this build. The Mac is awake whenever the extension imports, so Mac Amber runs `python -m app.library_push <id>` right after each successful `POST /api/import` and after Playwright saves. If the thumbnail lives in `snaps/<id>/`, wait for the thumbnail task first. Hourly `--backfill` remains the catch-up. Retarget the extension later only if cross-store duplicates actually show up.

## 5. Phases

### Phase 0 — Lock the shape

PR description plus a docs note (update or replace `docs/architecture/dell-plx-source-of-truth.md`; PR #32 need not be merged as-is, its "current state" is stale). Record the locked design in §4 and the §10 decisions. Confirm how Gift Links reaches Mac import today. No code. The builder does not merge or deploy. Jeremy confirms any production deploy.

### Phase 0.5 — Mac gate, before Dell-PLX allocates

Needs one session with the Mac awake. Phase 1 does not go live until this is done.

1. Partitioned allocator at every Mac allocation site in `~/Documents/amber-archive`.
2. Confirm the live LaunchAgent script, and reduce it to `python -m app.library_push --backfill` (plus pull later). No rsync of `snaps/`, `amber.sqlite3`, `blobs/`, or ingest journals, and never `--delete`.
3. Drain the six stranded Mac-only imports (and any others waiting). Backfill reports `backfill N ok, 0 failed`.
4. Push/pull code changes, when they land, go to both Mac checkouts or the two are folded together.

### Phase 1 — Gift Links can commit on Dell-PLX

Build the import worker as in §4. Install the import token, publish the port, add the tailnet policy, and document install and rotation on the Grok Bot box. Gift Links points at the new URL, uses the import bearer token, keeps a durable queue, and follows the client contract in §4.

Fix the `_connect` leak in `library_ingest.py` in this phase.

Tests (each crash-window retry must end with exactly one committed id):

- After `ingest_in_child`, before the push.
- After the blob PUTs, before `POST /v1/revisions`.
- After the commit, before the response.
- Two concurrent POSTs of one URL produce one id.
- A URL already pushed from the Mac returns `409` with the Mac's id (proves dedup reads the shared database, not scratch).
- `created_at` format and time zone match a Mac import of the same fixture (the reader orders by it).
- Status codes and `detail` strings are byte-identical to `app/api.py`, including `"already archived"`, `"final_url is a private or local address"`, and `"final_url is a different site"`.
- Real Gift Links pages (the stranded six are a ready fixture set, used as frozen HTML, not re-imported live in a way that creates second ids) freeze on Dell-PLX without hitting the child's deadline or memory cap, and produce the same row shape as on the Mac. Worker dependencies are pinned to the Mac's versions.
- The worker only ever opens the shared database `mode=ro`.
- Ingest still does not load the capture stack.

Do not test "crash mid-commit leaves the previous library in place" as a new ingest requirement; that recovery shipped in PR #33.

Acceptance: with the Mac asleep, a Gift Links import shows at `https://dell-plx-reader.tailf9612a.ts.net/app/` without restarting the reader, with a screenshot on the PR. A unit test that a row is listable inside amber-archive does not prove this; amber-reader is a separate repo.

Phase 1 may go live before Phase 2 only if Phase 0.5 is done and Gift Links is keeping the HTML.

Start ingest and the worker on Dell-PLX boot. Gift Links should alert after a stretch of failed retries (default 6 hours); the 22 November gap was only noticed by looking at the phone.

### Phase 2 — Mac convergence

Build the pull contract in §4. Add Mac Amber push-on-import and push-on-save (see §4, Chrome path). Hourly `--backfill` remains catch-up, with no `match_key` refusal rule.

Tests:

- Same snapshot set after a Gift Links import while the Mac is offline, then a Mac wake and pull.
- Same snapshot set after a Mac Chrome import, then a push, then a pull (no second id).
- A killed pull or push does not fork the libraries.
- A store-id change triggers a resync with no duplicates and no deletes.
- Pull racing a Mac import.
- A restore while the Mac holds an old cursor.

### Phase 3 — Importer docs and client policy

Document the Dell-PLX import URL, token, payload, and error codes. Gift Links fails closed with a durable queue; there is no Mac fallback. Push-on-import is already in Phase 2; this phase is docs and any remaining client polish. Retargeting the Chrome extension is not in this build.

### Later, not in this build

- Tombstones and delete propagation. When they land, `GET /v1/revisions` must keep listing tombstoned ids, or `--backfill` will revive deleted articles from the Mac copy. Tombstones are more urgent now that a bot can write; a bad Gift Links run can add up to the 300 MiB / 24 h budget of junk, and there is no clean delete.
- Revision bumps so Mac recaptures reach Dell-PLX.
- A durable Mac outbox as a replacement for hourly backfill.
- Retiring the LaunchAgent once pull and push-on-import are proven.
- Blob GC for PUTs that never reach `POST /v1/revisions`.
- Moving Gifts candidates, `homepage.json`, `sources.json`, feed proxy, or Playwright capture onto Dell-PLX.

## 6. Constraints

- Do not break the reader data layout (`snapshots` schema, `snaps/<id>/` names, 5-character ids).
- Only `python -m app.library_ingest` writes under the shared Dell-PLX data dir, including when the import worker is running. The reader stays `mode=ro`.
- The ingest token never leaves Dell-PLX and the Mac.
- No rsync into the Dell-PLX data dir in any form. Do not reintroduce `immutable=1`, database-file rsync, or `--delete` of ingest journals / `blobs/` / `ingest-store-id`.
- The builder does not merge or deploy. Jeremy confirms any production deploy.

## 7. Conditions for leaving hourly `--backfill` in place

1. Mac Amber cannot allocate an id in Dell-PLX's partition.
2. The LaunchAgent runs only `python -m app.library_push --backfill`, plus pull later. No rsync, no `--delete`.
3. The stranded Mac-only imports are pushed before Dell-PLX starts allocating.
4. `--backfill` gets no `match_key` refusal rule.

With those true, `if sid in have: continue` is correct again. If Dell-PLX is ever restored (and `ingest-store-id` is rotated), backfill can refill it from the Mac, including ids the Mac pulled.

## 8. Sources

Same as plan v1: the 22 November brief; PR #32 docs plan (`docs/architecture/dell-plx-source-of-truth.md`); PR #33 / `docs/ops/dell-plx-ingest.md`; `app/library_push.py`, `app/library_ingest.py`, `app/library_contract.py`, `app/api.py` on `main` `8c42ec2e`; and the Opus 5.5 review in `/workspace/amber-sot/opus-review.md`.

## 9. What a builder must not do

No production deploy, no merge, no LaunchAgent edit on the live Mac, no token install on the Grok Bot box, and no cutover of Gift Links, until Jeremy confirms those specific steps. Phase 0 docs and a PR are in scope after he signs off on §10.

## 10. Decisions for Jeremy, with defaults

These can be accepted as-is. Only the reserved character really needs a look.

1. **Leading id character reserved for Dell-PLX.** Default: **`Z`**. The alphabet is `23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz`. The Dell allocator still checks the live store, because some existing Mac ids may already start with `Z`.
2. **Duplicate policy.** Default: **accept cosmetic duplicates** (one URL, two ids, both kept). Push and log them; never refuse a Mac id from backfill. Routing every import through one endpoint is a later change, not a requirement for this build.
3. **Import token.** Default: **a separate import token** that can only create new snapshots through the freezer, caps and budget. The ingest token stays on Dell-PLX and the Mac. Rotation: write a new import token on Dell-PLX, install it on the Grok Bot box, then retire the old one; Gift Links retries cover the gap.
4. **How long Gift Links keeps the HTML.** Default: **until Phase 2 is live, or 14 days, whichever is longer.**
5. **Ownership.** Default:
   - Rows and files belong to Dell-PLX once committed.
   - Each id belongs to the allocator whose partition it is in.
   - Nothing in this build changes an existing id across machines.
   - Gifts candidates, `homepage.json`, `sources.json`, and recapture leases stay on the Mac.

Two of these were the review's suggested "answer first" questions: (1) and (2). The defaults above are the recommended answers for all five.
