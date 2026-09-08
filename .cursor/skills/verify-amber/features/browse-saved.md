# Browse and delete

Browse saved lists every complete snapshot. Delete removes that snapshot's files and row. The home page also shows a short recent list and a count on the browse button.

## Sub-features

- `browse-saved` opens `/saved` and lists titles, hosts, dates, and `/{id}`. Paywalled rows also say `incomplete · short extract`.
- `browse-empty` shows `Nothing saved yet.` when the scratch archive has no complete snapshots.
- `browse-home-recent` shows `Recently saved` on `/` after at least one complete snapshot.
- `browse-count` shows `browse (N)` on `/` when `N` complete snapshots exist.
- `delete-saved` deletes from `/saved` and the row disappears.
- `delete-viewer` deletes from the snapshot toolbar and returns to `/saved`.

## How to get to it (user POV)

- On `/`, choose `browse` (or `browse (N)`).
- On `/`, under `Recently saved`, choose `all saved`.
- Open `/saved` directly.
- On a snapshot viewer, choose `delete` and confirm.

## Driving it with browser and HTTP

Preconditions:

- Doctor is exit 0 on the verify instance.
- For list proofs, import the fixture first and keep its `snapshot_id`.
- For `browse-empty`, start from a freshly launched scratch dir with no imports.

- **Browse entry.** Open `/` and choose the `browse` link (`href="/saved"`). Title is `Saved — Amber`. The hint mentions the count. A `.saved-row` contains `Amber Verification Bridge`, `/{id}`, and a `delete` button.
- **Recent entry.** After a complete snapshot, `/` includes a `Recently saved` heading and a link to `/{id}`. A paywalled teaser also shows `incomplete · short extract`.
- **HTTP list.** `curl.exe -sS http://127.0.0.1:18080/saved` (or `curl`) is 200 and contains `everything you've saved` plus the fixture title.
- **Empty archive.** On a new scratch instance with zero complete rows, `/saved` contains `Nothing saved yet.` and has no `.saved-row`.
- **Delete from list.** On `/saved`, submit the row's `delete` form. Accept the browser confirm `Delete /{id}? This cannot be undone.` Response is `303` to `/saved`. The id is gone from the HTML.
- **Delete from viewer.** Open `/{id}` and choose toolbar `delete`, confirm. Land on `/saved` without that id.
- **HTTP delete.** `curl.exe -sS -D - -o NUL --max-redirs 0 -X POST http://127.0.0.1:18080/saved/{id}/delete` (Linux/macOS: `curl` and `-o /dev/null`). Status `303`, `Location: /saved`.
- **Side effect.** `{data_dir}/snaps/{id}/` is gone. `GET /{id}` is 404. `/saved` no longer contains the id.
- **Proof.** Save `evidence/browse-saved/before.png` on `/saved` with the row visible, `evidence/browse-saved/after.png` and `after.aria.txt` after delete (or the populated list if you are not proving delete), plus `meta.txt` for the entry point. If you delete, also record that the folder is absent.

## Gotchas

- Delete is permanent for that id. Only delete on the scratch instance. Never send delete to port 8080.
- The confirm dialog is a real `window.confirm`. A browser driver that ignores dialogs will not submit. HTTP POST `/saved/{id}/delete` skips the dialog because that check is client-side only.
- `/saved` lists up to 500 recent complete snapshots. Pending or failed jobs are omitted.
- Home `browse (N)` counts complete snapshots only. A queued `/save` does not increment N until capture finishes.
- After delete, the home recent list must also lose that snapshot. Check `/` as the second view.
