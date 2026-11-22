# Dell-PLX import worker

Runbook for the import worker in `app/library_import.py`. Nothing in the change that added this file is applied by the change. Do not install a token, edit the live Mac LaunchAgent, publish the port, or point Gift Links here until those steps are confirmed one at a time.

The worker accepts the Mac `POST /api/import` body, freezes HTML in a private scratch directory, and commits by calling ingest at `http://127.0.0.1:8082`. Ingest remains the only writer of `/home/jerem/apps/amber-reader/data`. The reader stays on `127.0.0.1:8081` and keeps opening `amber.sqlite3` with `mode=ro`.

Phase 2 pull is not part of this runbook.

## Order

Do these in order. Dell-PLX must not allocate a `Z` id until the Mac app is running the partitioned allocator, and the stranded Mac-only imports have been pushed by backfill. Re-sending those six through this worker would give each a second id.

1. Mac allocator and push-on-save, both checkouts.
2. LaunchAgent reduced to backfill, then a backfill that reports `0 failed`.
3. Dell-PLX worker, loopback only, then `tailscale serve` (never funnel) and a tailnet grant.
4. Gift Links cutover, later, and only while it is keeping the HTML.

## Mac

Two checkouts today: `~/Documents/amber-archive` (the app on `127.0.0.1:8080`) and `~/Documents/amber-archive-ingest` (the hourly push). This commit has to land in both, or the two have to be folded into one. The allocator and the push-on-save run in the app checkout. `--backfill` runs in the ingest checkout.

Pull the commit in each checkout and restart the app so new ids come from the Mac partition. A new snapshot id must not start with `Z`.

Push-on-save reads two variables. Set them on the app's LaunchAgent, not only on the hourly job. The URL is the tailnet ingest port, the same one `--backfill` already uses. `http://127.0.0.1:8082` is the ingest bind on Dell-PLX; it is not reachable from the Mac.

An import's thumbnail can still be rendering when the hourly backfill runs. The app writes `push-hold/<id>` for that wait, and backfill leaves the id for the next run. That skip is not a failure and does not show up in `backfill N ok, 0 failed`. Push-on-save publishes revision 1 after the thumbnail attempt. A second push of an id Dell-PLX already stored is success and does not replace revision 1. The same body is an info log. A different body is a warning.

```bash
AMBER_INGEST_URL=https://dell-plx-reader.tailf9612a.ts.net:8443
AMBER_INGEST_TOKEN_FILE="$HOME/Library/Application Support/amber/ingest.token"
```

Unset, the app still saves locally and the hourly backfill is the only push. A failed push is logged and left for backfill. The import path waits for its thumbnail task, when one was scheduled, before it pushes.

Confirm the live `com.jeremylichtman.amber-reader-sync-dellplx` script before relying on it. It has to be only the backfill below. No rsync of `snaps/`, `amber.sqlite3`, `blobs/`, or the ingest journals, and never `--delete`. An rsync `--delete` would remove Dell-originated snapshot directories the Mac does not have.

```bash
cd "$HOME/Documents/amber-archive-ingest"
AMBER_DATA_DIR="$HOME/Documents/amber-archive/data" \
  .venv/bin/python -m app.library_push --backfill \
  --ingest-url https://dell-plx-reader.tailf9612a.ts.net:8443 \
  --token-file "$HOME/Library/Application Support/amber/ingest.token"
```

Run that by hand once the app is on this commit. The line to require is `backfill N ok, 0 failed`, including the six Gift Links imports that landed on the Mac after the last successful push (Saturday 4:21 AM ET, 21 November 2026). Do not import those six again through the worker.

## Dell-PLX

WSL Ubuntu, user `jerem`, systemd user units. The data directory stays on the ext4 disk under `/home/jerem`, not `/mnt/c`. Ingest stays `amber-ingest.service` on `127.0.0.1:8082`. Restart it after the checkout so it loads this commit. This adds a second unit.

Inside the Ubuntu distro, as `jerem`:

```bash
cd "$HOME/apps/amber-archive"
git fetch origin
git checkout <this-commit>
.venv/bin/pip install -r requirements-import.txt
systemctl --user restart amber-ingest
```

Restart ingest after the checkout. `amber-ingest.service` runs from this same checkout, so the process keeps the previous code until it is restarted. The duplicate warning (`snapshot … repeats an article already stored as …; keeping both`) is in that process.

`requirements-import.txt` pins fastapi, uvicorn, beautifulsoup4, lxml, readability-lxml, pillow and playwright to exact versions. It installs the Playwright Python package because the import contract lives in `app/api.py`. It does not install Chromium. The unit sets `AMBER_IMPORT_THUMBNAILS=0`, so a missing browser does not delay an import. Turn thumbnails on only after `playwright install chromium`, and only then set `AMBER_IMPORT_THUMBNAILS=1`. A thumbnail is taken before the push or skipped. Nothing adds one after the commit.

The ingest token is already at `~/.config/amber/ingest.token`. Do not rotate it for this step. Create the import token only when the file is absent. A second run of this step must leave the existing token in place:

```bash
umask 077
mkdir -p "$HOME/.config/amber" "$HOME/.local/share/amber/import-scratch" "$HOME/.local/share/amber/import-state"
import_token="$HOME/.config/amber/import.token"
if [ -e "$import_token" ]; then
  echo "import.token already exists; leaving it"
else
  python3 -c 'import secrets, pathlib; pathlib.Path.home().joinpath(".config/amber/import.token").write_text(secrets.token_urlsafe(32) + "\n")'
fi
chmod 600 "$import_token" "$HOME/.config/amber/ingest.token"
test -s "$import_token"
```

`test -s` fails the step when the file is missing or empty. `chmod` does not replace the bytes.

The scratch directory and the budget file are outside `~/apps/amber-reader/data`. The worker opens `amber.sqlite3` there with `mode=ro` only. A `mode=ro` open may create `-wal` and `-shm` beside the database, same as the reader, so this process runs as `jerem`.

```bash
mkdir -p "$HOME/.config/systemd/user"
cp docs/ops/amber-import.service "$HOME/.config/systemd/user/amber-import.service"
systemctl --user daemon-reload
systemctl --user enable --now amber-import
curl -sS http://127.0.0.1:8083/healthz
```

`/healthz` answers `{"ok": true}` and does not create a snapshot. The import route is `POST /api/import` with `Authorization: Bearer` the import token and `Content-Type: application/json`. The ingest token is rejected there. The import token is rejected by ingest on `127.0.0.1:8082`. Cookies are ignored. The process does not send CORS headers.

Publish the loopback port on the tailnet. This is a third HTTPS port on the reader node. Leave 443 on the reader (`8081`) and 8443 on ingest (`8082`).

```bash
set -o pipefail
tailscale serve --bg --https=8444 http://127.0.0.1:8083
tailscale serve status | grep -F '8444' | grep -F '127.0.0.1:8083'
if tailscale funnel status | grep -F '8444'; then
  echo "8444 is on funnel; run: tailscale funnel --https=8444 off" >&2
  exit 1
fi
```

`serve status` has to show `8444` proxying to `127.0.0.1:8083`. If the grep fails, the serve did not apply. Stop. Do not continue to the grant. Use `tailscale serve`. `tailscale funnel` would put the import route on the public internet. The funnel check fails the step when 8444 is listed there.

Then grant that port only to the Grok Bot node. A default allow-all policy, or a rule that already allows `dell-plx-reader:*` or any source to TCP `8444`, does not become narrow because a second grant exists. Read the current policy first. If 8444 is already reachable from more than the Grok Bot node, change that rule before relying on a new one. The shape of the grant, once the wider rule is gone, is a source of the Grok Bot node, a destination of `dell-plx-reader`, and TCP `8444` only.

Verify it. From a tailnet node that is not the Grok Bot, this must fail to connect:

```bash
nc -vz -w 3 dell-plx-reader.tailf9612a.ts.net 8444
```

From the Grok Bot node the same command must succeed. If the non-Grok check connects, the port is still exposed and the step is not done. The bearer token still protects the route, and the reader session cookie is for the same tailnet name on another port. Cookies are not isolated by port, which is why the worker ignores cookies and why the grant has to be checked, not assumed.

Copy the import token to the Grok Bot box the same way the ingest token was copied to the Mac, mode `0600`, and do not put the ingest token on that box. To rotate: write a new `import.token` on Dell-PLX, install that file on the Grok Bot box, restart the worker, then delete the old file. Gift Links retries cover the overlap. Restart the worker after the file changes; it reads the token at startup.

Both units should be enabled so a boot starts ingest and then the worker (`After=amber-ingest.service`). `Linger=yes` for `jerem` is already required by the ingest runbook.

## Gift Links, later

Not this step. When it is cut over, and only after the Mac backfill above has reported `0 failed`:

- Base URL `https://dell-plx-reader.tailf9612a.ts.net:8444`, path `POST /api/import`, bearer import token.
- Same JSON body as today. Do not send `force` on a retry.
- Treat `201` and `409 {"detail": "already archived", "id": ...}` as success. That `409` is a usable snapshot of the URL, or a retry of the same HTML body this worker committed in the last 24 hours. A short Mac teaser is not a `409`. Posting the full article then returns `201` and a new id. Do not treat every complete row as already archived.
- `400`, `415`, and `422` are permanent and get reported. `422` with `Library ingest rejected the snapshot.` means ingest refused the body (its 400, 413, or another 4xx that is not 401, 403, 408, or 429). Retrying it will not succeed. Ingest `401` and `403` arrive as `503` because they mean the worker's ingest token, and the HTML should be kept.
- `503` (`Library ingest is unavailable.`, `Could not allocate a snapshot id.`, `Could not read the library.`) and `429` retry with backoff. Those `503`s do not keep the import budget charge. Client timeout at least 90 s. Network errors retry the same way.
- Keep the HTML until Phase 2 is live, or 14 days, whichever is longer. No fallback to the Mac.
- Alert after 6 hours of failed retries.

A committed import shows on the phone at `https://dell-plx-reader.tailf9612a.ts.net/app/` without restarting the reader. The reader lists `snapshots` rows; a folder with no row stays hidden. Confirm with one frozen page that is not one of the six already on the Mac, then look at the phone. That screenshot is the acceptance check and it is not produced by the code change.

## Check without the phone

On Dell-PLX, after a test import, a read-only open of the library should list the new id. Use the id from the `201` body.

```bash
python3 - <<'PY'
import sqlite3
from pathlib import Path
from urllib.parse import quote
p = Path.home() / "apps/amber-reader/data/amber.sqlite3"
uri = "file:{}?mode=ro".format(quote(str(p), safe="/:"))
conn = sqlite3.connect(uri, uri=True)
print(conn.execute("select id, status, created_at from snapshots order by created_at desc limit 5").fetchall())
conn.close()
PY
```

`created_at` is UTC `YYYY-MM-DDTHH:MM:SSZ`, the same format the Mac writes. The id starts with `Z`.
