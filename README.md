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

`GET /api/lookup?url=<article url>` tells a client whether an article is already archived, without saving anything. It compares the article against each snapshot's saved URL and its final (post-redirect) URL, ignoring http vs https, a leading `www.`, default ports, the fragment, a trailing slash, query-parameter order, and share/tracking parameters. Parameters that are tracking tokens everywhere (`utm_*`, `fbclid`, `gclid`, `smid`, `unlocked_article_code`, `accessToken`, `sharetype`, `referringSource` and similar) are dropped on every site; generic names are dropped only on the publisher that uses them as share tokens (`st`, `reflink`, `mod` on wsj.com; `token`, `segmentId` on ft.com; `partner` on nytimes.com; `cmp` on theguardian.com) and stay part of the key elsewhere. Path case is significant. Only the lookup key is loosened; the stored URL is never altered. A missing scheme is treated as https; anything other than http(s) answers `400 {"detail": "..."}`. No DNS lookup is made.

```json
{"found": false}
{"found": true, "id": "K7mQp", "status": "complete", "usable": true, "title": "...", "site": "Financial Times",
 "url": "...", "final_url": "...", "created_at": "2026-10-09T19:25:47Z"}
```

When several snapshots match, the answer prefers a complete and usable one, then one still queued or capturing, then a complete but unusable one, then a failed one; newest first within each group (ties go to the later insert). `usable` is true only when `status` is `complete` and the snapshot passes the usable rules above (so `/api/snapshots/{id}/text` will answer 200). A complete but unusable snapshot (paywall stub, empty capture) is `found` with `usable: false`, unless a recapture is in flight, in which case the in-flight job is returned.

`POST /api/save` with `Content-Type: application/json` and body `{"url": "https://...", "force": false}` queues a capture of a public article. The body must be JSON; other content types get `415`. That check only keeps this endpoint strict about its input. It is not what stops cross-site abuse: a cross-site `fetch()` with a JSON body is stopped by the browser's CORS preflight (the API sends no CORS headers), and a cross-site `<form>` post is stopped by the middleware described under "Cross-site requests" below. URLs are checked with the same rules as the web form (http(s) only, no credentials, no private or local hosts, and numeric hosts only as plain dotted-quad or IPv6 addresses, so `0177.0.0.1`, `0x7f.0.0.1`, `2130706433` and `127.1` are refused); a rejected URL answers `400 {"detail": "..."}` and never reaches the queue.

| Response | Meaning |
|---|---|
| `202 {"id", "status": "queued"}` | New capture queued. Poll `GET /api/jobs/{id}`. |
| `202 {"id", "status": "queued" or "capturing"}` | The same URL is already being captured (even with `force`, and for parallel requests); `id` is that job. |
| `200 {"id", "status": "exists", "usable"}` | A complete snapshot already exists and `force` is false. `id` is that snapshot. |
| `400` / `413` / `415` | Bad URL or JSON / body over 16 KB / not `application/json`. |
| `403` | Cross-site request (see below). |
| `429` | 10 captures are already waiting in the queue. |

`GET /api/jobs/{id}` is the polling endpoint (the job id is the snapshot id). Its `status` is always one of `queued`, `capturing`, `complete` or `failed`; `error` is a string when `failed`. Job ids stay valid after the capture finishes: unknown to the in-memory queue, the answer is rebuilt from the database. A snapshot left `pending`/`capturing` by an Amber restart is reported as `failed` ("Capture was interrupted before it finished."), so a client never polls forever. Once `complete`, read the article from `GET /api/snapshots/{id}/text`; it answers 404 if the capture was saved but is not usable (for example a paywall stub).

### Importing a page from a signed-in browser

`POST /api/import` with `Content-Type: application/json` stores HTML that a browser (typically the Amber extension, in a Chrome where the user is signed in to a publisher) already rendered, so the Reader can show text Amber's own fresh-visitor capture never gets. It queues no capture and fetches nothing: the HTML is parsed, frozen and extracted exactly as posted.

```json
{"url": "https://www.ft.com/content/...", "html": "<!doctype html>...", "title": "optional",
 "final_url": "optional, same site as url", "force": false}
```

`url` follows the same rules as `/api/save` (public http(s), no credentials, no private or local hosts, no numeric-host tricks, at most 2048 characters). The fragment is dropped and a missing scheme becomes `https://`; the stored `url` is that value. `html` is the page's HTML and must be a string of at least 200 characters. `title` is optional and used only when the page itself has no title (or only its host name); it never overrides a title the page declares. `final_url` is optional (the address the browser ended up on after redirects). It goes through the same public-address checks as `url` (a rejected one is a 400, DNS included) and must be on the same site: the same host or the same registrable domain, otherwise `400 {"detail": "final_url is a different site"}`. Registrable domains use a built-in suffix table (last two labels, last three under suffixes such as `co.uk`, and shared-hosting suffixes such as `github.io`, `blogspot.com`, `herokuapp.com`, `vercel.app`, `netlify.app`, `pages.dev`, `workers.dev`, `web.app`, `s3.amazonaws.com`, `appspot.com`, `wordpress.com`, so two subdomains of those are different sites). Hosts are compared after NFKC and IDNA 2008 normalisation (the `idna` package when installed, otherwise a punycode fallback; either way `ß.de` and `ss.de` are different sites), and the port must be the same or both default (80/443). The scheme is not compared: `http://host/` and `https://host/` are the same site when the host matches. `*.s3.amazonaws.com`, `*.s3.<region>.amazonaws.com` and `*.s3-<region>.amazonaws.com` are each a separate owner per bucket host, as are subdomains of `substack.com`, `medium.com`, `wixsite.com`, `myshopify.com`, `tumblr.com`, `squarespace.com`, `ghost.io` and `notion.site`. It is stored as the snapshot's `final_url`, and lookups match on it; without it `final_url` equals `url`. The usable-copy check for `409` runs on both `url` and `final_url`.

**Same-site rule for `final_url`** (the extension's `sameSite` mirrors this exactly). Two URLs are the same site when their hosts, after NFKC and IDNA 2008 normalisation and lower-casing without a trailing dot, are equal, or have the same registrable domain, and their ports are equal or both default (80/443). The scheme is not compared. IP literals are compared exactly. The registrable domain is the longest matching suffix from the list below plus one label, otherwise the last two labels:

- country-style two-label suffixes: `co.uk org.uk ac.uk gov.uk me.uk ltd.uk plc.uk com.au net.au org.au edu.au gov.au co.nz org.nz govt.nz co.jp or.jp ne.jp ac.jp go.jp com.br net.br org.br com.cn net.cn org.cn com.hk com.sg com.tw com.mx com.ar com.tr com.ua co.in net.in org.in co.za co.kr or.kr co.il co.id com.my com.ph com.pk com.eg com.sa com.ng`
- shared hosting, where each subdomain has a different owner: `github.io gitlab.io blogspot.com herokuapp.com vercel.app netlify.app pages.dev workers.dev web.app firebaseapp.com azurewebsites.net cloudfront.net s3.amazonaws.com appspot.com wordpress.com substack.com medium.com wixsite.com myshopify.com tumblr.com squarespace.com ghost.io notion.site`
- any `*.amazonaws.com` host with a label that is `s3` or starts with `s3-` (`bucket.s3.us-west-2.amazonaws.com`, `bucket.s3-eu-west-1.amazonaws.com`): the whole host is the registrable domain.

A `final_url` that fails is `400` with exactly `{"detail": "final_url is a different site"}` (other site) or `{"detail": "final_url is a private or local address"}` (private, loopback, link-local, local or numeric-trick host); other rejections keep `final_url: <reason>`.

| Response | Meaning |
|---|---|
| `201 {"id": "K7mQp", "status": "complete", "usable": true, "word_count": 1200, "title": "..."}` | Stored as a new snapshot. `title` is `null` when neither the page nor the request had one. |
| `409 {"detail": "already archived", "id": "..."}` | A complete, usable snapshot of this article already exists and `force` is not `true`. Nothing was stored. |
| `400 {"detail"}` | Rejected URL or `final_url`, invalid JSON, missing or too-short `html`, non-string `title`/`final_url`, non-boolean `force`. |
| `413` | Body over 12 MiB. The body is read in chunks and refused as soon as it passes the limit. |
| `415` | Not `application/json`. |
| `403` | Cross-site request, or an extension not on the allowlist (see below). |
| `422` | The HTML could not be processed, or is too complex (see below); nothing is left behind. |
| `408` | The body took more than 60 s to arrive. |
| `429` | Two imports are already running; 300 MB of HTML was already imported in the last 24 hours (counted in memory, so a restart resets it; the page's bytes are charged as soon as the import is accepted, before the complexity pre-check and the parse, and stay charged if it then fails or is killed; a `409` is not charged); or three imports failed or were killed within 60 s, which pauses imports for 60 s (`Retry-After` is set). |

An import never displaces a usable copy: with an existing usable snapshot of the same article (matched like `/api/lookup`), the answer is `409` unless the body says `"force": true`, which stores a new snapshot that then becomes the newest. Otherwise (nothing archived, or only stubs, failures or in-flight captures) the import creates a new snapshot. `usable` applies the same rules as `/api/snapshots` (complete, at least 120 words, a real title, not a paywall stub). `GET /api/lookup` and `POST /api/save` prefer the newest complete and usable copy, so a usable import wins immediately, while an unusable one never displaces an older usable snapshot. Once `usable` is true, `GET /api/snapshots/{id}/text` answers 200 with the paragraphs. The snapshot is marked `"imported": true` in its `meta.json`.

An imported snapshot has no stored resources, so its Webpage view shows the page without remote images or stylesheets (the page's own CSP blocks them); the Reader view carries the full text and, under its CSP, shows `https:` images from the publisher. Form values, CSRF/token `<meta>` tags and long opaque-token `data-*` values are dropped from imported pages (not from captures). The thumbnail is rendered in the background after the response, offline and without JavaScript, only when fewer than 3 thumbnails are waiting, and clipped to the top 8000 px; a failure there is logged and never affects the import.

#### Page limits

Pasted HTML can be shaped to make a parser spend minutes or gigabytes, so before parsing the page is refused with `422 {"detail": "Page too complex to import: ..."}` when it has more than 60,000 `<` characters, nests deeper than 400 elements, or has a tag with more than 1,000 attributes (counted as written, including attributes without `=`). That text scan is only a fast reject: the same limits (elements, nesting depth, attributes per element and 400,000 attributes in total) are measured again inside the child on the tree the parser actually built, which is the authoritative check. The child is killed after 45 seconds, and its memory is capped at about 1.5 GB. The memory cap is best-effort: where `RLIMIT_AS` cannot be set (it is rejected or ignored on macOS) only a 100 ms resident-size watchdog in the child enforces it, so a single huge allocation can briefly exceed 1.5 GB; the wall-clock kill and the page limits above are the dependable bounds. Violations answer `422`, so the server stays responsive. While a child runs, the reserved snapshot reports `capturing` to `/api/lookup` and `/api/jobs/{id}`, never `failed`. If the import then fails, `/api/jobs/{id}` answers `failed` with the reason for 60 seconds instead of `404`. The child also ends itself (`SIGALRM`) shortly after the parent's own deadline, so a killed server leaves no runaway worker, and at startup any snapshot left `pending` or `capturing` by a previous process is marked `failed`, but only by the process that holds an exclusive lock on `data/amber.lock`: a second Amber started on the same data directory logs that it is leaving the first one's jobs alone. Ordinary articles import in well under a second. The homepage's HTML-file form (`POST /import`) goes through the same pre-check, child process, 12 MiB streaming body cap and in-flight and daily limits; it keeps its redirect behaviour and reports a refusal as the usual `/?error=...` redirect.

#### Which extensions may import

Chrome sends `Origin: chrome-extension://<id>` for an extension's requests, and by default any installed extension that holds host access to Amber's address may POST JSON (`/api/save`, `/api/import`, ...). Amber logs a warning at startup while that is the case. To allow only your own extension, set `AMBER_EXTENSION_IDS` to its id (comma-separated for several): other extension origins then get `403` on every non-GET request. Find the id at `chrome://extensions` with Developer mode on (the "ID" line under the extension's name; unpacked extensions keep a stable id as long as the folder path stays the same). With launchd, add it to the job's plist and reload:

```xml
<key>EnvironmentVariables</key>
<dict>
  <key>AMBER_EXTENSION_IDS</key>
  <string>abcdefghijklmnopabcdefghijklmnop</string>
</dict>
```

`launchctl kickstart -k gui/$(id -u)/<your job label>` restarts it. The allowlist only gates requests that carry a `chrome-extension://` Origin; `GET`s are read-only and unaffected, and curl or scripts (no Origin) are unchanged.

### Cross-site requests

Every request other than `GET`, `HEAD` and `OPTIONS` is refused with `403 {"detail": ...}` when a web page on another site started it: when the browser sent `Sec-Fetch-Site: cross-site` or `same-site`, or an `Origin` header that is neither this server's own origin nor `chrome-extension://...`. This covers `POST /save`, `/import`, `/saved/{id}/delete`, `/api/save` and `/api/import`. Requests with neither header (curl, scripts) pass.

The browser extension is allowed, but only with `Content-Type: application/json`: Chrome sends `Origin: chrome-extension://<id>` and `Sec-Fetch-Site: none` for its fetches (observed with a real MV3 service worker), and a JSON body forces a CORS preflight unless the extension holds host permission for `127.0.0.1`. A form-encoded or `text/plain` post from an extension origin gets 403. Any installed extension with host access to `127.0.0.1` can therefore post JSON, which is the intended trust boundary.

`GET /save?url=...` is the bookmarklet's endpoint, so it is not blocked outright. Whenever the browser sends any `Sec-Fetch-*` header, the request must be a top-level navigation (`Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`), which is what `location.href = ...` (the bookmarklet) and the address bar are (the homepage's save form is a `POST` and is covered by the cross-site rules above, not by this one), and a request whose `Sec-Fetch-Site` is `same-origin`, `same-site` or `cross-site` must also have `Dest: document`: an `<img>`, `<link>`/CSS `url()`, `fetch()` or `<iframe>` GET gets 403 even from a snapshot that Amber itself serves on this origin. curl and scripts (no `Sec-Fetch-*` headers) are unchanged. The header rule is the guarantee. As defence in depth, when a page is frozen or extracted, subresource URLs (`src`, `srcset`, `poster`, `background`, `<link href>`, `<use>`/`<image>` href, CSS `url()` and `@import "..."`) that Amber did not capture are resolved against the page's own address, keeping the page's scheme (`https://<original host>/...`), and dropped when they are not http(s), when the host is this machine, a private, LAN or CGNAT address or a numeric-trick host (`2130706433`, `0x7f.0.0.1`, `127.1`), or when the authority contains a backslash (`\\host`, `/\host`, `https:\\host`). Saved-file paths of the form `/<id>/r/<file>` are left alone. The reader extract gets the same rewrite, including `srcset` (parsed the way browsers parse it, so commas inside a URL such as `w_120,c_limit/a.jpg`, `width=1024,quality=80/...` or a `data:` URI are kept and a candidate with no descriptor is fine). It is not a complete filter: it does not cover CSS escapes (`u\72l(...)`), `image-set()`, SVG `<feImage>` or other attributes it does not list, so the `Sec-Fetch-*` rule above is what actually stops a snapshot from starting a capture. CSS rewriting is bounded: a stylesheet gets at most 20,000 distinct targets resolved, the rest are dropped unresolved, so a hostile 12 MB stylesheet costs well under two seconds. Chromium 151 also blocks web pages from reaching loopback subresources on its own (Local Network Access), so this is a second layer. A page can still make a browser navigate to `/save?url=...` and start a capture. The damage is bounded instead: at most 10 captures wait in the queue (`/save` answers with the usual `/?error=...` redirect, `/api/save` with `429`), and a URL already queued or capturing is returned as the same job rather than queued again. The URL still has to pass the public-address checks.

### Captured content

Everything stored under a snapshot is third-party bytes served on Amber's origin, so it is never allowed to run as a document there.

- Stored resources get their file extension from the response MIME type only (a fixed table of image, font and CSS types, anything else `.bin`), never from the URL. A response typed `image/x-evil` from `/a.html` is stored as `<hash>.bin`.
- `/{id}/r/<file>` serves a fixed set of passive types inline (CSS, images including SVG, fonts); everything else, including `.html`, `.js` or `.xml` files that older captures stored by URL suffix, is `application/octet-stream` with `Content-Disposition: attachment`. All of them carry `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox; default-src 'none'; ...`, so even an SVG with script opened directly cannot run script or reach Amber's origin.
- `/{id}/raw` and `/{id}/reader` carry a CSP with `script-src 'none'` and the same `sandbox` flags the viewer iframes use (no scripts, `allow-same-origin` so fonts and images load), so captured markup cannot execute script when opened as a top-level page either. `/raw` restricts images to `'self'` and `data:`; the reader also allows `https:` images (captured readers keep their publisher image URLs).

### Imported pages

The posted HTML is untrusted third-party content, handled like a captured page, and more strictly where it is cheap to be.

- Nothing is fetched while importing. `freeze_html` is given an empty resource map and extraction is DOM-only; verified by running the import path with a socket guard and a listening canary against a page full of external `<img>`, `<link>`, `@import`, `<iframe>`, `<!DOCTYPE ... SYSTEM>` and XML entity references: no connection was attempted.
- `freeze_html` removes `<script>`, `<noscript>` and `<template>`, replaces `<iframe>`, `<object>`, `<embed>` with a note, strips every `on*` attribute and `javascript:` URL, disables forms and drops `<meta http-equiv=refresh>`. The reader extract drops `script`, `style`, `link`, `meta`, `base`, `form`, `iframe`, `object` and `embed` elements, every `on*` attribute, `srcdoc`, `formaction` and `javascript:`/`vbscript:`/`data:text/html` URLs, escapes the text it builds from `og:description`, and escapes title, author and site name.
- `/{id}/raw` carries `default-src 'none'` with `img-src 'self' data:`, `script-src 'none'` and the viewer's `sandbox` flags: no script and no remote subresource. `/{id}/reader` carries `script-src 'none'`, the same `sandbox` flags and `img-src 'self' data: https:` (and the same for media), with `default-src 'none'` and `connect-src 'none'` (so `<a ping>`, which would otherwise POST from the viewer's iframe, and every other fetch other than `https:` images and media, `data:` and same-origin images, is blocked), `style-src 'unsafe-inline'` and `font-src 'self' data:`. Publisher images show; `http:` addresses, including loopback ones, are not fetched. In `/raw`, `'self'` is Amber's own origin, so the URL rewrite above (and the `/save` navigation rule) is what keeps an image or stylesheet from reaching Amber's own routes. `ping` and `attributionsrc` attributes are also removed when a page is frozen or an extract is sanitised, and a non-GET request carrying `Content-Type: text/ping` or a `Ping-To` header is refused with 403. An import stores no files under `/{id}/r/`.
- Every Amber-side rendering of third-party markup (the reader thumbnails of imports and HTML imports, and the paywalled-capture reader screenshot) uses a Chromium context with JavaScript off, service workers blocked and every request except `data:`/`about:` aborted at context level, so remote images are blank in those screenshots.
- The body is capped at 12 MiB; page complexity, wall-clock and memory are capped as described above; at most two imports run at once; 300 MB of HTML per rolling day. The existing cross-site rules apply (an extension origin is accepted only for `application/json`, and only for listed ids when `AMBER_EXTENSION_IDS` is set; Host is checked first).
- JSON bodies nested deep enough to exhaust the parser's recursion answer `400`, not `500`.

### Capture-time protections

- The crawler fetch used to recover paywalled text checks the connected peer address before sending anything (HTTP and HTTPS, redirects included), so a hostname that resolves to a public address when validated and a private one when connected is refused. It reads at most 8 MiB, and one 30 s wall-clock budget covers connect, TLS, headers and body for both user-agent attempts together; a timer shuts the socket down at the deadline, so a one-byte-per-second drip cannot hold it open. Proxy environment variables are ignored for this fetch.
- Chromium only reads a subresource body when the response is a type that is stored, and only after the browser reports its real transferred size; a response over 8 MiB, whether it declared `Content-Length` or was chunked, is never read into Amber's memory.
- Private-address checks treat IPv6 site-local `fec0::/10` as non-public.
- Chromium's Local Network Access checks (a page it treats as public cannot reach loopback or private addresses) are on by default in the Chromium Playwright 1.62 ships (151); this was verified by marking `127.0.0.1:PORT` public with `--ip-address-space-overrides=127.0.0.1:PORT=public` and loading a page that fetches another loopback port: blocked by default, allowed with `--disable-features=LocalNetworkAccessChecks`. Amber does not pass its own `--enable-features`, because that would replace Playwright's `--enable-features=CDPScreenshotNewSurface`. The protection therefore relies on Chromium's default and must be re-verified on Playwright upgrades.

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
