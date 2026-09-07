# Capture network boundaries

Amber accepts public HTTP(S) URLs without embedded credentials. The validator rejects local, private, reserved, multicast, and unresolved addresses, including hostnames with any non-public DNS result.

In `app/capture.py`, `_capture_visit` checks the initial URL before creating a browser context. Its context-wide route checks requests before continuing them, including subresources and popup navigation. Service workers are blocked so they cannot take requests outside that routing. Allowlisted Google/X bounce documents are fulfilled locally; subsequent article navigation uses Chromium's native click and referrer behavior. The final page URL is checked before extraction.

The crawler fallback, `_http_get`, validates its initial URL and each redirect through `_PublicRedirectHandler` before urllib opens the next request.

## Remaining limits

Playwright routes the first request of a native HTTP redirect chain, but not every later hop. A browser redirect can therefore contact a private destination before the final-URL check rejects it. This also applies to redirecting subresources; the final page URL check does not inspect those destinations. These checks are not a complete browser SSRF boundary.

DNS validation and connection establishment are separate operations. Neither transport pins the validated address, so DNS rebinding remains possible. A network proxy or firewall that rejects private destinations at connection time would be needed to enforce this boundary for arbitrary untrusted pages. Amber's loopback bind limits access to its server; it does not limit the browser's outbound network access.

## Verification and diagnostics

Image and font saving is unchanged: `_should_save` accepts every `image/*` and `font/*` MIME type, plus CSS and the listed legacy application font types. Removing their duplicate entries from `SAVE_TYPES` does not reduce saved resources. The end-to-end capture test checks that image and font responses are stored and served with their original bytes.

`tests/test_capture_security.py` exercises the actual validator with a local publisher fixture. It verifies that forbidden initial URLs never create a context or connection, urllib rejects a private destination after a public redirect, public requests still succeed, and browser subresource and popup requests to private addresses never reach the fixture server. It also checks context cleanup when browser setup fails.

Capture failures log the snapshot ID, exception type, and stack location. Failed bounce attempts log the snapshot ID, allowlisted origin, and exception type, then retain the best available capture. Logs omit exception messages because those can contain publisher URLs, credentials, or headers. The existing job error and snapshot error fields remain available for local debugging.
