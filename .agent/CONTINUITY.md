# Continuity

## [DECISIONS]

- Metered / soft paywalls that grant a free read to Google or X visitors get a referrer bounce: the first capture is still a cookieless first-time visit; if `extract_article` marks the page paywalled, Amber retries in a fresh Playwright context via an allowlisted bounce origin (`google.com`, `news.google.com`, `x.com`, `t.co`) and keeps the higher `word_count`. Hard / login-only paywalls stay incomplete + HTML import. Do not clone a Chrome profile, do not use archive.today as a bypass, do not accept a user-supplied Referer.
