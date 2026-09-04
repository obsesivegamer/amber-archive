# Continuity

## [DECISIONS]

- Metered / soft paywalls that grant a free read to Google or X visitors get a referrer bounce: the first capture is still a cookieless first-time visitor; if `extract_article` marks the page paywalled, Amber retries in a fresh Playwright context via an allowlisted bounce origin and keeps the higher `word_count`. Allowlist is `google.com`, `news.google.com`, `x.com`, `t.co`. Retries run one origin per family (`google.com`, then `x.com`) and skip any origin whose host matches the article. Bounce visits drop `EXTRA_HEADERS` Referer so the click-through sends the natural origin (x.com is not a duplicate Google visit). The bounce stub is only active until that click — the article document and later bounce-host subresources are not stubbed/aborted. Hard / login-only paywalls stay incomplete + HTML import. Do not clone a Chrome profile, do not use archive.today as a bypass, do not accept a user-supplied Referer.
