# Daily backup

Code and units only. Nothing here is installed or started by the change that added it.

boxj is an armv7 Synology on DSM 7.1. SSH is port 3003. The copy uses the user-space rsync on that box, over SSH, the same way `sync-boxj.sh` does. It does not use SMB.

## What is copied

1. `sqlite3 .backup` of `/home/jerem/apps/amber-reader/data/amber.sqlite3` and of the amber-web `app.sqlite3` into a staging directory.
2. `sources.json`, `homepage.json`, and the other files in the app-state directory (`%h/.local/share/amber/web-state`). The live `app.sqlite3` is not copied over the backup.
3. `rsync -a --link-dest` of the staged databases, `snaps/`, `blobs/`, and `state/` to `boxj:amber-backups/YYYY-MM-DD/`.

`--link-dest` points at the previous dated directory. A re-run on the same UTC day updates that day's directory and does not touch older days. `AMBER_BACKUP_KEEP` is 30. Pruning deletes whole older dated directories. It never runs `--delete` inside an older version.

A failure exits nonzero and writes a `backup failed:` line to stderr.

`AMBER_RSYNC` defaults to `rsync`. On Dell-PLX set it to the user-space rsync binary the boxj sync already uses (`%h/bin/rsync` in the unit). `AMBER_BACKUP_SSH` defaults to `ssh -p 3003 -o BatchMode=yes`.

## Units

`docs/ops/amber-backup.service` and `docs/ops/amber-backup.timer`. Enable the timer when the backup is accepted. Do not enable `amber-web.service` until cutover.

## Health

`scripts/health-amber.sh` curls reader `8081`, ingest `8082`, import `8083`, and amber-web `8084`. Reader and amber-web use Basic auth. It then runs `tailscale funnel status` and fails if funnel is on.

## Restore drill

`scripts/restore-drill.sh` reads the latest dated directory. It opens the live databases with `mode=ro`. It compares snapshot counts, the sha256 of up to five `article.txt` files, candidate counts, and source counts. With `AMBER_READER_DIR` set it starts amber-reader against a temp copy of the backup on `AMBER_RESTORE_PORT` (default 18081) and curls `/healthz`. It does not write the live data directory.
