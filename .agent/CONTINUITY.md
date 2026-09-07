# Continuity

## [PLANS]

- 2026-09-06T21:35:11-04:00 [USER] Fix only PR #6 high-priority capture-boundary findings: keep long Playwright subresources, refuse to archive resources fetched from private peers, restore redirect-specific errors, add final-URL and redirect tests, update security docs, push without merging or deploying.

## [PROGRESS]

- 2026-09-06T21:35:11-04:00 [TOOL] Five-hour Codex window was 76 percent used and 24 percent remaining. Work resumed only after the user explicitly said to proceed.
- 2026-09-06T21:35:11-04:00 [TOOL] PR #6 is open at commit 4f6576764dd87eb67b8d1abdfed5fb56141943bd. Fable's live review confirms the four user-listed issues. The worktree had no tracked edits; pre-existing `.verify/` and `.verifytestdata/` remain untouched.

## [DECISIONS]

- 2026-09-06T22:02:30-04:00 [CODE] User-submitted and final document URLs keep the 2048-character intake limit. Playwright route validation uses the same scheme, credential, host, and DNS checks without that length limit.
- 2026-09-06T22:02:30-04:00 [CODE] CSS, image, and font responses enter `resource_bodies` only when Playwright's `response.server_addr()` reports an address accepted by `_is_public_ip`. Missing, invalid, and non-public peers fail closed for archival. Redirected browser contact remains possible and is documented.
- 2026-09-06T22:02:30-04:00 [CODE] Final document URL validation wraps `ValueError` as `RuntimeError("Redirected to a blocked address: ...")`; the outer `BaseException` cleanup still closes the browser context.

- Metered / soft paywalls that grant a free read to Google or X visitors get a referrer bounce: the first capture is still a cookieless first-time visitor; if `extract_article` marks the page paywalled, Amber retries in a fresh Playwright context via an allowlisted bounce origin and keeps the higher `word_count`. Allowlist is `google.com`, `news.google.com`, `x.com`, `t.co`. Retries run one origin per family (`google.com`, then `x.com`) and skip any origin whose host matches the article. Bounce visits drop `EXTRA_HEADERS` Referer so the click-through sends the natural origin (x.com is not a duplicate Google visit). The bounce stub is only active until that click — the article document and later bounce-host subresources are not stubbed/aborted. Hard / login-only paywalls stay incomplete + HTML import. Do not clone a Chrome profile, do not use archive.today as a bypass, do not accept a user-supplied Referer.
- Bounce is not enough for Politico-class pages where the full article is already in the DOM. `readability-lxml` prefers a related `article-card` teaser (live Politico.eu: 30 words, `referrer_retried` true, `referrer_bounce` null). `extract_article` strips recirc chrome (`.article-card`, `.content-listing`, `aside`) before readability and prefers the page's own `<article>` / `.article__content` / `[itemprop=articleBody]` only when that body has at least `PAYWALL_WORD_LIMIT` **non-anchor** words. The article-dump path also drops comments / promo / footer / related-stories before scoring, and joins only outer `.article__content` roots so nested wrappers cannot double-count. If content roots existed and chrome strip removes all of them, the dump is discarded rather than widened to the whole `<article>`. Nested `<article>` hosts inside footer/comments are skipped so they cannot re-enter after the outer clone drops them. The dump path also drops nodes whose class+id match readability's `unlikelyCandidatesRe` (unless `okMaybeItsACandidateRe`), so `id="comments"` / `comment-list` / `sponsored` / WordPress `#comments` wrapping `<article class="comment-body">` cannot beat a teaser. Recirc strip stays enumerated — off-list classes like `story-card` can still inflate (pre-existing on main; do not widen casually). A teaser plus comments or a link-dense "Most read" list stays paywalled so the bounce retry can still fire. Bounce fixtures stay for meters that actually need a cross-site referrer.

## [DISCOVERIES]

- 2026-09-06T21:44:00-04:00 [TOOL] Before the fix, the three new browser regressions failed independently: the 2100-character image query was absent from `resource_map`; private CSS and image bodies reached through public redirects were present in `resource_bodies`; and a private final document URL raised the raw `ValueError`.
- 2026-09-06T21:44:00-04:00 [TOOL] Installed Playwright 1.62 reports `Response.server_addr() -> Optional[RemoteAddr]`, with `ipAddress` and `port` when available.

## [OUTCOMES]

- 2026-09-06T22:02:30-04:00 [TOOL] Focused security verification passed: 17 tests in `tests/test_capture_security.py` and `tests/test_security.py`.
- 2026-09-06T22:02:30-04:00 [TOOL] Full repository verification passed: 77 tests in 67.31 seconds. The only output was the same two dependency deprecation warnings reported before this fix. `git diff --check` and targeted `py_compile` also passed.
- 2026-09-07T11:24:26-04:00 [TOOL] Commit `6bc7524a45dadce92705325410a4974c220e6871` is pushed to `codex/capture-boundaries`; PR #6 remains open against `main` with no status checks reported. No merge or deployment was performed.
