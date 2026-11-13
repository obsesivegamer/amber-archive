# Push one snapshot to Dell-PLX

The phone library goes stale because the Mac is the only machine that starts a copy, and that copy is an hourly rsync of a live SQLite file. This slice uploads one snapshot that is already `complete`. An ingest process on Dell-PLX writes `snaps/<id>/` and the `snapshots` row. Capture stays on the Mac. This document is the runbook. Nothing here is applied by the change that added it.

Two things have to change on the reader host before the first ingest commit. The hourly rsync installs a Mac database snapshot over `amber.sqlite3`, which drops `library_revisions` and `library_changes`. The reader opens that file with `immutable=1`, which maps the bytes and does not take a WAL lock, while ingest checkpoints the same live file.

## Where the code lives

Ingest is `app/library_ingest.py` in this repo. The Mac command is `app/library_push.py`.

The writer belongs next to the snapshot schema it commits. `python -m app.library_ingest` loads that module and FastAPI. It does not import Playwright, Chromium, or the capture app. Install `requirements-ingest.txt` on Dell-PLX. The Mac keeps the full capture environment and runs the push command from this same checkout.

amber-reader stays the process that serves archived HTML. It needs the patch in the next section before it shares a data directory with ingest. Ingest is the only writer of `amber.sqlite3` after that cutover.

## Prerequisite: reader open mode

`gareygoosey/amber-reader` `app/index.py` builds this URI:

```text
file:<amber.sqlite3>?mode=ro&immutable=1
```

That open is safe when rsync replaces the database by rename, because the reader keeps the inode it already mapped. Ingest does not replace the file. It commits in WAL mode and runs `wal_checkpoint(TRUNCATE)` on the live file. An immutable reader can see a torn page or `SQLITE_CORRUPT` during that checkpoint.

Apply this change in the amber-reader checkout at `4b985cf` and restart the reader before starting ingest. `docs/ops/amber-reader-mode-ro.patch` is a `git diff` of `app/index.py` (blob `0059481bfc612fd8b059cc55c38f14994be75337`) and of `tests/test_stories.py::test_sqlite_index_is_what_the_list_reads`. That test asserted the list open created no `-wal`. `mode=ro` creates `-wal` and `-shm`, so the patch expects those files after the list call. `git apply` and `patch -p1` both accept it.

```diff
-    uri = "file:{}?mode=ro&immutable=1".format(quote(str(DB_PATH), safe="/:"))
+    # Ingest writes this live file and runs wal_checkpoint(TRUNCATE) into it.
+    # immutable=1 maps the bytes and does not take a WAL lock, so that
+    # checkpoint can tear a read. mode=ro shares the lock. It may create
+    # -shm next to the database, so the reader user has to be the ingest
+    # user (jerem) and the directory has to be a Linux filesystem.
+    uri = "file:{}?mode=ro".format(quote(str(DB_PATH), safe="/:"))
```

`open_readonly`'s docstring changes with it: a `mode=ro` open may create `-wal` and `-shm` beside the database, and the process user has to be allowed to. Both processes run as `jerem`.

```bash
cd "$HOME/apps/amber-reader"
patch -p1 < /path/to/amber-archive/docs/ops/amber-reader-mode-ro.patch
systemctl --user restart amber-reader
curl -sS http://127.0.0.1:8081/healthz
```

`/healthz` should still report `backend=sqlite`.

## Prerequisite: hourly rsync

`scripts/sync-from-mac.sh` in amber-reader excludes `amber.sqlite3` from the tree copy, then rsyncs a Mac `.backup` snapshot onto `DEST_PATH/amber.sqlite3`. That second copy replaces the ingest database. The replacement has no `library_revisions` or `library_changes` table, so the next push of the same id is treated as revision 1 and `change_seq` starts again at 1. A journal left from a crash then looks unpublished. Ingest writes a random id in `library_meta` and in `ingest-store-id`. If that file is present and the database does not contain the same id, startup exits and a commit answers 503, and recovery logs and leaves `snaps/<id>` in place. Do not delete `ingest-store-id` to "clear" that state.

`--delete` removes names that are not excluded and are absent on the Mac. `blobs/`, `ingest-staging/`, `ingest-previous/`, `ingest-journal/`, and `ingest-store-id` are only on Dell-PLX, so `--delete` removes them. `ingest-store-id` is the sentinel that tells ingest the database was replaced. It has to survive `--delete`.

Do this in `scripts/sync-from-mac.sh` before enabling ingest. Add these excludes next to the existing `--exclude='/homepage.json'` lines. A leading slash anchors at the data root, which is the rule that script already uses, and `--delete` does not remove an excluded name.

```bash
--exclude='/blobs/'
--exclude='/ingest-staging/'
--exclude='/ingest-previous/'
--exclude='/ingest-journal/'
--exclude='/ingest-store-id'
```

There is no `ingest.lock`. Do not add an exclude for it.

Skip the second rsync, the block that installs `$DB_SNAP` as `amber.sqlite3`. A tree rsync of `snaps/<id>/` does not make that id show on the phone. amber-reader lists `snapshots` rows when the database opens (`list_snapshots` uses the SQL backend). A folder with no row stays hidden. In place of the database install, run a backfill from the Mac after ingest is up, and on the same schedule the database copy used to have:

```bash
cd "$HOME/Documents/amber-archive"
AMBER_DATA_DIR="$HOME/Documents/amber-archive/data" \
  .venv/bin/python -m app.library_push --backfill \
  --ingest-url https://dell-plx-reader.tailf9612a.ts.net:8443 \
  --token-file "$HOME/Library/Application Support/amber/ingest.token"
```

`--backfill` asks ingest which ids it has stored and pushes every local `complete` id that is missing. That write is what creates the row the phone lists. `backfill 0` means every complete id is already stored.

The block to leave out is:

```bash
if [[ -n "$DB_SNAP" ]]; then
  DB_RSYNC=(rsync "${RSYNC_COMMON[@]}")
  [[ $DRY_RUN -eq 1 ]] && DB_RSYNC+=(--dry-run)
  "${DB_RSYNC[@]}" "$DB_SNAP" "$TARGET:$DEST_PATH/amber.sqlite3"
fi
```

Leave that block out, or guard it so it does not run once ingest owns the database. The tree copy already excludes `amber.sqlite3`, `amber.sqlite3-wal`, and `amber.sqlite3-shm`. Those excludes stay, so the tree copy does not replace the live database or the reader's `-shm`.

Until both edits are in the script that the Mac LaunchAgent runs, do not let that agent write this data directory.

## Cutover order

1. Confirm the data directory is on the WSL ext4 disk under `/home/jerem`. `/mnt/c` is the Windows drive, and SQLite locking there is a different filesystem.

   ```bash
   df -T /home/jerem/apps/amber-reader/data
   ```

2. Confirm the user manager stays up, and that systemd is pid 1 in this distro.

   ```bash
   loginctl show-user jerem -p Linger
   grep -A2 '^\[boot\]' /etc/wsl.conf
   ```

   `Linger=yes` is required. If it is `no`: `sudo loginctl enable-linger jerem`. `/etc/wsl.conf` needs:

   ```ini
   [boot]
   systemd=true
   ```

   A distro that was started before that line was added needs `wsl --shutdown` from Windows and a new session before `systemctl is-system-running` succeeds.

3. Apply the reader patch above and restart amber-reader.
4. Edit `sync-from-mac.sh` with the excludes and without the `amber.sqlite3` install. Disable the Mac LaunchAgent until that edit is what the agent runs.
5. Install and start ingest, then publish the port, then run the Mac command.

## Layout

Ingest and amber-reader share one data directory. The reader's unit uses `%h/apps/amber-reader/data`, which for user `jerem` is `/home/jerem/apps/amber-reader/data`. If the live reader already uses a different directory on that ext4 disk, point `AMBER_DATA_DIR` at that directory.

Ingest binds `127.0.0.1:8082`. The reader stays on `127.0.0.1:8081`. The bearer token is a file mode `0600`. It is a different secret from `AMBER_READER_PASSWORD`.

## Dell-PLX

Inside the Ubuntu distro, as `jerem`, after the prerequisites above.

```bash
mkdir -p "$HOME/apps"
cd "$HOME/apps"
git clone https://github.com/obsesivegamer/amber-archive.git
cd amber-archive
python3 -m venv .venv
.venv/bin/pip install -r requirements-ingest.txt

umask 077
mkdir -p "$HOME/.config/amber"
python3 -c 'import secrets, pathlib; pathlib.Path.home().joinpath(".config/amber/ingest.token").write_text(secrets.token_urlsafe(32) + "\n")'
chmod 600 "$HOME/.config/amber/ingest.token"

mkdir -p "$HOME/.config/systemd/user"
cp docs/ops/amber-ingest.service "$HOME/.config/systemd/user/amber-ingest.service"
systemctl --user daemon-reload
systemctl --user enable --now amber-ingest
curl -sS http://127.0.0.1:8082/healthz
```

`/healthz` answers `{"ok": true}`. The unit is:

```ini
[Unit]
Description=Amber library ingest

[Service]
WorkingDirectory=%h/apps/amber-archive
Environment=PYTHONUNBUFFERED=1
Environment=AMBER_DATA_DIR=%h/apps/amber-reader/data
Environment=AMBER_INGEST_TOKEN_FILE=%h/.config/amber/ingest.token
Environment=AMBER_INGEST_HOST=127.0.0.1
Environment=AMBER_INGEST_PORT=8082
ExecStart=%h/apps/amber-archive/.venv/bin/python -m app.library_ingest
Restart=on-failure

[Install]
WantedBy=default.target
```

Publish that loopback port on the tailnet with `tailscale serve`. This is a second HTTPS port on the reader node so the write origin stays off the origin that serves archived HTML. Leave the reader's `tailscale serve` on 443 pointed at port 8081.

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8082
```

That serves `https://dell-plx-reader.tailf9612a.ts.net:8443`. Use `tailscale serve`. `tailscale funnel` publishes the write API on the public internet, so it is the wrong command here.

Copy the token to the Mac, then set the mode again on the Mac side. A trailing newline is ignored.

```bash
install -d -m 700 "$HOME/Library/Application Support/amber"
scp jerem@dell-plx-reader:.config/amber/ingest.token \
  "$HOME/Library/Application Support/amber/ingest.token"
chmod 600 "$HOME/Library/Application Support/amber/ingest.token"
```

## Mac

From the archive checkout, with the data directory that already holds the snapshot. `K7mQp` is a real id: five characters from the archive alphabet. Use the id of a row whose status is `complete`.

```bash
cd "$HOME/Documents/amber-archive"
AMBER_DATA_DIR="$HOME/Documents/amber-archive/data" \
  .venv/bin/python -m app.library_push K7mQp \
  --ingest-url https://dell-plx-reader.tailf9612a.ts.net:8443 \
  --token-file "$HOME/Library/Application Support/amber/ingest.token"
```

A new snapshot prints `201 K7mQp revision=1 change_seq=1`. The same command again prints `200 K7mQp revision=1 change_seq=1` and does not add a second directory or a second row. The phone list is the reader's `/saved` page after the reader password. Restart the reader after the mode=ro patch. A later push does not need another restart.

Revision 1 is the only revision this command sends. A later slice bumps the revision when the files change. Ingest answers 409, with `detail` and the stored revision, when the number is anything other than the stored revision or one past it. The same id and revision, with the same bytes, is 200.

The two renames that publish an existing id leave a gap where `snaps/<id>` is missing. The journal is written first. Startup moves `ingest-previous/<id>` back if the SQL transaction did not commit. `renameat2(RENAME_EXCHANGE)` would close that gap in one syscall. Recovery is written for the two-step order, so that is the order the code uses.
