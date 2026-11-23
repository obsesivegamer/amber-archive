# Daily backup

Code and units only. Nothing here is installed or started by the change that added it.

boxj is an armv7 Synology on DSM 7.1. SSH is port 3003. The copy uses the user-space rsync on that box, over SSH, the same way `sync-boxj.sh` does. It does not use SMB.

## What is copied

1. `sqlite3 .backup` of `/home/jerem/apps/amber-reader/data/amber.sqlite3` and of the amber-web `app.sqlite3` into a staging directory.
2. `sources.json`, `homepage.json`, and the rest of the app-state tree (`%h/.local/share/amber/web-state`), including nested files. The live `app.sqlite3` and its sidecars are not copied over the backup.
3. `rsync -a --link-dest` of the staged databases, `snaps/`, `blobs/`, and `state/` to `boxj:amber-backups/YYYY-MM-DD/`.

`--link-dest` points at the previous dated directory. A re-run on the same UTC day updates that day's directory and does not touch older days. `AMBER_BACKUP_KEEP` is 30. Pruning deletes whole older dated directories. It never runs `--delete` inside an older version.

A failure exits nonzero and writes a `backup failed:` line to stderr.

Dell runs `AMBER_RSYNC` (`%h/bin/rsync` in the unit). boxj runs `AMBER_RSYNC_PATH`, passed as `--rsync-path`. The default is `$HOME/bin/rsync` on boxj, the user-space binary `sync-boxj.sh` invokes there. `sync-boxj.sh` is not in this repository; if that script names a different remote path, set `AMBER_RSYNC_PATH` to it. DSM's `rsync` on `PATH` is not used. `AMBER_BACKUP_SSH` defaults to `ssh -p 3003 -o BatchMode=yes`. An empty SSH command with a `host:path` destination is an error.

The script creates `YYYY-MM-DD` on boxj with `mkdir -p` before the first rsync. It writes `manifest.json` and then `.complete`. Prune and the restore drill ignore a directory without `.complete`. Staging is one directory, `backup-staging/current`, removed when the script exits. `AMBER_BACKUP_KEEP` must be at least 1. A vanished-file rsync status (24) is a warning. Any other rsync or ssh failure exits nonzero with a `backup failed:` line. The success timestamp is `~/.local/share/amber/backup-last-success`. `amber-backup.service` has `OnFailure=amber-backup-failed.service`, which logs the same phrase.

## Units

`docs/ops/amber-backup.service` and `docs/ops/amber-backup.timer`. Enable the timer when the backup is accepted. Enable `amber-web.service` only in the order `docs/ops/dell-plx-cutover.md` gives, before the cold-boot check of port 8084.

## Health

`scripts/health-amber.sh` curls reader `8081`, ingest `8082`, import `8083`, and amber-web `8084`. The reader password is passed with `--netrc-file` from a mode `0600` temp file, not on the curl command line. It then runs `tailscale funnel status`. A failed command, empty-looking success that is not a known off state, or any output other than empty, `no funnel`, or `Funnel is disabled` is a failure. It also fails when `~/.local/share/amber/backup-last-success` is missing or older than 26 hours.

## Restore drill

`scripts/restore-drill.sh` uses the latest directory that has `.complete`. It checks that copy against `manifest.json` from backup time: snapshot count, candidate count, source count, and the sha256 of up to five `article.txt` files. It does not fail because the live library has changed since the backup. A live count, when the live database can be opened read-only, is logged and is not a pass/fail check. It refuses to use a work directory inside the live library or app-state. With `AMBER_READER_DIR` set it starts amber-reader on a temp copy (no blobs) on `AMBER_RESTORE_PORT` (default 18081), with `AMBER_ALLOWED_HOSTS` set to that loopback port, queries `/api/snapshots` for a count, and fetches those articles and hashes them. The server process is killed and the temp copy removed on exit. `AMBER_RESTORE_SKIP_SERVER=1` skips the server. The remote copy uses the same `AMBER_RSYNC` and `AMBER_RSYNC_PATH` as the backup.
