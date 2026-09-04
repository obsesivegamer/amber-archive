# Continuity

## [DECISIONS]

- Metered / soft paywalls that grant a free read to Google or X visitors get a referrer bounce: the first capture is still a cookieless first-time visit; if `extract_article` marks the page paywalled, Amber retries in a fresh Playwright context via an allowlisted bounce origin and keeps the higher `word_count`. Allowlist is `google.com`, `news.google.com`, `x.com`, `t.co`. Retries run one origin per family (`google.com`, then `x.com`) so a short real article is not four extra Chromium visits. The bounce lands on a locally fulfilled stub and **clicks through** — `page.goto(url, referer=…)` alone is still `Sec-Fetch-Site: none` (typed URL). `EXTRA_HEADERS` Referer can populate `document.referrer` on some Chromium builds and is not the fix. Hard / login-only paywalls stay incomplete + HTML import. Do not clone a Chrome profile, do not use archive.today as a bypass, do not accept a user-supplied Referer.
