# Daily backup

Code and units only. Nothing here is installed or started by the change that added it.

boxj is an armv7 Synology on DSM 7.1. SSH is port 3003. The copy uses a user-space rsync on that box, over SSH. It does not use SMB. `sync-boxj.sh` is not in this repository. The Mac sync that is, amber-reader `scripts/sync-from-mac.sh`, invokes whatever `rsync` is on the remote `PATH` and does not pass `--rsync-path`. Before enabling the timer, confirm the user-space binary and compare it with `sync-boxj.sh` if that script is on the Mac:

```bash
ssh -p 3003 boxj '$HOME/bin/rsync --version'
```

If `sync-boxj.sh` names a different remote path, set `AMBER_RSYNC_PATH` to it.

## What is copied

1. `sqlite3 .backup` of `/home/jerem/apps/amber-reader/data/amber.sqlite3` and of the amber-web `app.sqlite3` into a staging directory.
2. `sources.json`, `homepage.json`, and the rest of the app-state tree (`%h/.local/share/amber/web-state`), including nested files. The live `app.sqlite3` and its sidecars are not copied over the backup.
3. `rsync -a --link-dest` of the staged databases, `snaps/`, `blobs/`, and `state/` to `boxj:amber-backups/YYYY-MM-DD/`.

`--link-dest` points at the previous dated directory. A re-run on the same UTC day updates that day's directory and does not touch older days. `AMBER_BACKUP_KEEP` is 30. Pruning deletes whole older dated directories. It never runs `--delete` inside an older version.

A failure exits nonzero and writes a `backup failed:` line to stderr.

Dell's backup unit runs `rsync` from `PATH`. Set `AMBER_RSYNC=%h/bin/rsync` in the unit when that user-space binary should be used instead. The unit's `WorkingDirectory` and `ExecStart` are `~/apps/amber-web`, the deploy checkout, not `~/apps/amber-archive`. boxj's shell runs `AMBER_RSYNC_PATH`, passed as `--rsync-path`. The default is `$HOME/bin/rsync` on boxj. DSM's `rsync` on `PATH` is not used. `AMBER_BACKUP_SSH` defaults to `ssh -p 3003 -o BatchMode=yes`. An empty SSH command with a `host:path` destination is an error.

The script creates `YYYY-MM-DD` on boxj with `mkdir -p` before the first rsync, and removes `.complete` before that rsync so a failed re-run is not trusted. It writes `manifest.json` from the staged database copies: raw row counts for every table in the library and in `app.sqlite3`, sha256 and a short sample of up to five `article.txt` files, the `snaps/` file count, and the source and candidate counts. It does not copy the reader's usable-snapshot rules. `.complete` is written last. Prune and the restore drill ignore a directory without `.complete`. Unmarked dated directories older than the oldest kept complete backup, or older than two days, are removed whole. Staging writes only under `backup-staging/amber-backup-stage/`. It deletes `current` there, and dated directories of that same shape, and refuses an empty staging root, `/`, the home directory, the library, or app-state. `AMBER_BACKUP_KEEP` must be at least 1. A vanished-file rsync status (24) is a warning. Any other rsync or ssh failure exits nonzero with a `backup failed:` line. The success timestamp is `~/.local/share/amber/backup-last-success`. `amber-backup.service` has `OnFailure=amber-backup-failed.service`, which logs the same phrase.

## Units

`docs/ops/amber-backup.service` and `docs/ops/amber-backup.timer`. Enable the timer when the backup is accepted. Enable `amber-web.service` only in the order `docs/ops/dell-plx-cutover.md` gives, before the cold-boot check of port 8084.

## Health

`scripts/health-amber.sh` curls reader `8081`, ingest `8082`, import `8083`, and amber-web `8084`. The reader password is passed with curl `-K` from a mode `0600` temp file (`user = "amber:…"`), not on the curl command line. It then runs `tailscale serve status --json`. Before this check is enabled, confirm user `jerem` can run that command without root. A failed command includes tailscale's own error text. Unparseable output, or any `AllowFunnel` value of true, is a failure. Empty config, `null`, or no `AllowFunnel` is off. It also fails when `~/.local/share/amber/backup-last-success` is missing or older than 26 hours.

## Restore drill

`scripts/restore-drill.sh` uses the latest directory that has `.complete`. A missing destination, or a destination with no complete backup, exits with `restore drill failed:`. It checks that copy against `manifest.json` with `sqlite3` and `sha256sum`: raw table counts, the sha256 of the recorded `article.txt` files, the `snaps/` file count, and the source and candidate counts. A manifest with no articles fails. It does not compare a reader-derived count, and it does not fail because the live library has changed since the backup. A live snapshot count, when the live database can be opened read-only, is logged and is not a pass/fail check. It refuses to use a work directory inside the live library or app-state, and it refuses a symlink in the backup that dangles, points outside that backup, or resolves into the live library or app-state. The reader copy is made with `rsync -aL`, so the process opens regular files. With `AMBER_READER_DIR` set to the amber-reader checkout it starts that checkout's `python -m uvicorn app.main:app` on a temp copy (no blobs) on `AMBER_RESTORE_PORT` (default 18081), with `AMBER_ALLOWED_HOSTS` set to that loopback port. It curls `/healthz`, then fetches each recorded article with Basic auth and requires 200, non-empty text, and the sample stored from that file. The article request waits up to 60 seconds. The server process is killed and the temp copy removed on exit. `AMBER_RESTORE_SKIP_SERVER=1` skips the server. The remote copy uses the same `AMBER_RSYNC` and `AMBER_RSYNC_PATH` as the backup.
