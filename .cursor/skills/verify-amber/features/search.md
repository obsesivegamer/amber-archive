# Search the archive

Search lets a user find complete snapshots by URL, title, or site name, open a match, and see a clear empty state when nothing matches.

## Sub-features

- `search-home` runs the homepage search form.
- `search-page` runs the form on `/search`.
- `search-match` lists the imported fixture by title and by token/host.
- `search-open` opens a result and lands on `/{id}`.
- `search-empty` shows `No snapshots matched` for a query with no hits.
- `search-blank` on `/search` with an empty `q` shows the form and no result list.

## How to get to it (user POV)

- On `/`, use the field labeled `I want to search the archive` and choose `search`.
- Open `/search` and submit the same form.
- Open `/search?q=<query>` directly.

## Driving it with browser and HTTP

Preconditions:

- Doctor is exit 0 on the verify instance.
- A fixture snapshot exists (run `scripts/import-fixture.ps1` if `/saved` is empty).
- Note the `snapshot_id` from import.

- **Home entry.** Open `/`. Fill `#q` with `Amber Verification Bridge` and choose `search`. The URL becomes `/search?q=Amber+Verification+Bridge` (encoding may vary) and the results list includes a link whose text is `Amber Verification Bridge`.
- **Search page entry.** Open `/search`. Submit `#q` = `VERIFICATION_TOKEN_AMBER_BRIDGE_2026`. The same snapshot appears. (If the token is only in article text and not in title/url/description, use `Amber Verification Bridge` or `verify.example` — search matches url, title, site name, and description, not the full article body.)
- **Host match.** Search `verify.example`. The fixture row is present with its original URL shown.
- **Open result.** Choose the result link `Amber Verification Bridge`. The viewer at `/{id}` loads.
- **Empty state.** Search `volcano-no-such-snapshot`. The page contains `No snapshots matched “volcano-no-such-snapshot”.`
- **Blank query.** Open `/search` with no `q`. Title is `Search — Amber`. There is no `No snapshots matched` line and no result `<ul>`.
- **HTTP.** `curl.exe -sS "http://127.0.0.1:18080/search?q=Amber%20Verification%20Bridge"` is 200 and contains the fixture title and `/{id}`.
- **Proof.** Save `evidence/search/before.png` on `/search` or home, `evidence/search/after.png` and `after.aria.txt` on the populated results, and `meta.txt` naming which entry point was used.

## Gotchas

- Search does not query `article.txt`. A token that lives only in the body will miss. The fixture puts `VERIFICATION_TOKEN_AMBER_BRIDGE_2026` in the og:description so it is searchable; prefer title or `verify.example` if a future fixture drops that meta tag.
- Only `status = complete` rows appear. A failed or in-flight `/save` will not show up.
- An empty `q` is not an empty-result proof. Submit a string that cannot match.
- Results are capped (50). Do not use search as a complete inventory; use `/saved`.
- Importing twice yields two rows with the same title. Open the id you just created, not "any" match.
