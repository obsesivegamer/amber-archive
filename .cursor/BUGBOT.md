# Amber review rules

Amber is a self-hosted FastAPI page archiver (`app/`). It fetches arbitrary third-party URLs, stores their bytes on disk and in SQLite, and serves them back on its own origin. Flag real bugs and security problems first.

## Priority 1: SQLite connections (`app/db.py`)
- `db.connect()` returns a `_ClosingConnection`: `with connect() as db:` commits or rolls back, then closes. A leak of 149 handles was fixed in PR #30; do not reintroduce it.
- Flag any `sqlite3.connect(...)` outside `db.connect()`, any `connect()` result that is never used in a `with` block or closed on every path (including exceptions and early returns), and any use of `db` or a cursor after its `with` block (copy rows out first).
- Flag `with conn:` used as a transaction scope on a connection the code keeps using afterwards: leaving the block now closes it.
- Candidate polling (`GET /api/candidates`, `app/candidates.py`) runs often, so a per-call leak there exhausts the open-file limit.

## Priority 1: SSRF and pinned addresses
- Every user- or page-supplied URL must pass `security.validate_public_http_url` (or `validate_public_http_request_url` for browser subresources) before any connection. Check new fetch paths in `app/api.py` (`/api/save`, `/api/import` `url` and `final_url`), `app/main.py` (`/save`, `/import`), `app/candidates.py` and `app/recapture.py`.
- Feed proxy (`GET /api/feedproxy`, `app/web.py`): the connection must stay pinned to the address `_public_peer_ip` chose. The request URL host and the `CURLOPT_RESOLVE` pin must be the same ASCII form (`_ascii_http_host`). The connected peer must be re-checked with `_same_pinned_peer` before the body is kept. Each redirect hop (up to `FEED_MAX_REDIRECTS`) needs the same validation and pinning. Never fall back to an unpinned client or a second DNS lookup.
- Capture (`app/capture.py`): `_http_get` and `_PublicRedirectHandler` must validate every redirect. Browser routes must keep rejecting non-public hosts. Saved CSS, image and font responses must keep the `response.server_addr()` public-peer check. Do not add a path that follows redirects or opens sockets without these checks. Known gaps are listed in `docs/security.md`; do not flag those, but do flag changes that widen them.
- Host parsing: numeric-like hosts (`0177.0.0.1`, `2130706433`) and non-public IPv6 forms must stay rejected.

## Priority 1: Access control and cross-site writes
- Amber has no login. Access control is `TrustedHostMiddleware` (Host allow-list, `AMBER_HOST`, `AMBER_ALLOWED_HOSTS`) plus `reject_cross_site_writes` in `app/main.py`. Flag a new route, `Sec-Fetch-*` or Origin exception, or CORS header that bypasses either one. Flag any change that widens the allow-list or the loopback default (`AMBER_HOST`, `HOST`).
- Every state-changing route (`POST /save`, `/import`, `/saved/{sid}/delete`, `/recapture/run`, `/api/candidates/...`, `PUT /api/homepage`, `PUT /api/sources`) must require `application/json` or go through the cross-site middleware. `GET /save` must stay top-level-navigation only.
- `GET /api/feedproxy` must keep `_refuse_non_app_fetch`.

## Priority 1: Secrets and logging
- Never log or return credentials, cookies, tokens, `Authorization` or `Cookie` headers, publisher URLs with tokens, or raw exception messages that may contain them. Existing code logs only snapshot ID and exception type; keep it that way. Flag `log.exception` or `str(exc)` that reaches a response or log on a fetch path.
- Captures start in a fresh browser with no cookies; flag code that adds user cookies or auth headers to a capture or feed request. URLs with embedded credentials must stay rejected. Imported HTML must keep `scrub_secrets=True` (`freeze.strip_secrets`).

## Priority 1: Paths and stored bytes
- Snapshot IDs must match `ID_RE` before `db.snap_dir(sid)` builds a path. Flag any new route or file write that joins a user-supplied name to `SNAPS_DIR` or `WEBAPP_DIR` without that check, a `..`/absolute-path check, or `_is_unsafe_path`/`_webapp_file`.
- `GET /{sid}/r/{filename}` must keep rejecting `/`, `\` and leading dots, serving only passive types inline, and sending `RESOURCE_CSP` and `nosniff`.
- Captured HTML is third-party and served on Amber's origin: keep CSP and `DOCUMENT_SANDBOX` on `/raw` and `/reader`, and keep scripts and forms stripped in `freeze.py`.
- Flag writes that are not atomic or leave temp files on failure (`recapture.py`, `web.py` stores), and `shutil.rmtree` on a path not derived from a validated ID.

## Priority 2: Resource limits
- Request bodies go through `_read_body_capped`. Flag unbounded reads, missing size or time caps on fetched bodies (`FEED_MAX_BYTES`, `MAX_RESOURCE_BYTES`), and Playwright `context` or `browser` objects not closed on every path.

## Low priority / ignore
- Style, naming, formatting, import order, line length, docstring wording, and comment density.
- Missing type hints or tests for UI, glue code, or scripts.
- Root `_probe_*.py`, `_retrieve_full.py`, and `scripts/` one-off tooling.
- Per-publisher heuristics in `extract.py` and `quality.py` unless they crash or hang.
