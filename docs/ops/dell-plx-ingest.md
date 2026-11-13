# Push one snapshot to Dell-PLX

The phone library goes stale because the Mac is the only machine that starts a copy, and that copy is an hourly rsync of a live SQLite file. This slice is the first path that does not use that job.

A command on the Mac reads one snapshot already marked `complete` and uploads it. An ingest process on Dell-PLX writes `snaps/<id>/` and the `snapshots` row that amber-reader already lists (`status = 'complete'`). Capture and the hourly rsync stay as they are. This document is the runbook. Nothing here is applied by the change that added it.

## Where the code lives

Ingest is `app/library_ingest.py` in this repo. The Mac command is `app/library_push.py`.

Dell-PLX should run this writer, and amber-reader should stay a reader of the same data directory. The writer belongs next to the snapshot schema it commits, in the repo that already owns that schema. `python -m app.library_ingest` loads this module and FastAPI. It does not import Playwright, Chromium, or the capture app. Install `requirements-ingest.txt` on Dell-PLX. The Mac keeps the full capture environment and runs the push command from this same checkout.

amber-reader is unchanged. It opens `amber.sqlite3` read-only and, when that file is usable, lists `status = 'complete'`. Ingest is the only writer of that database. After each commit it checkpoints the WAL so the reader's immutable open sees the row in the main file.

## Layout

Ingest and amber-reader share one data directory on the WSL ext4 disk under `/home/jerem`. The reader's unit uses `%h/apps/amber-reader/data`, which for user `jerem` is `/home/jerem/apps/amber-reader/data`. If the live reader already uses a different directory, point `AMBER_DATA_DIR` at that directory. It has to be a Linux filesystem under `/home/jerem`. `/mnt/c` is the Windows drive, and SQLite locking there is a different filesystem.

```bash
df -T /home/jerem/apps/amber-reader/data
```

The path in that output is under `/home/jerem` and the type is `ext4` (or another Linux filesystem on the WSL disk).

Ingest binds `127.0.0.1:8082`. The reader stays on `127.0.0.1:8081`. The bearer token is a file mode `0600`. It is a different secret from `AMBER_READER_PASSWORD`.

## Dell-PLX

Inside the Ubuntu distro, as `jerem`. The user manager that already runs amber-reader is the one that runs this unit. `loginctl enable-linger` for that user is already part of the reader setup.

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

That serves `https://dell-plx-reader.tailf9612a.ts.net:8443`. Use `tailscale serve`. `tailscale funnel` would publish the write API on the public internet.

Copy the token file to the Mac with `scp` and `chmod 600`. The token is one line. A trailing newline is ignored.

## Mac

From the archive checkout, with the data directory that already holds the snapshot. `K7mQp` is a real id: five characters from the archive alphabet. Use the id of a row whose status is `complete`.

```bash
cd "$HOME/Documents/amber-archive"
install -d -m 700 "$HOME/Library/Application Support/amber"
# ingest.token was copied from Dell-PLX; mode 0600
chmod 600 "$HOME/Library/Application Support/amber/ingest.token"

AMBER_DATA_DIR="$HOME/Documents/amber-archive/data" \
  .venv/bin/python -m app.library_push K7mQp \
  --ingest-url https://dell-plx-reader.tailf9612a.ts.net:8443 \
  --token-file "$HOME/Library/Application Support/amber/ingest.token"
```

A new snapshot prints `201 K7mQp revision=1 change_seq=1`. The same command again prints `200 K7mQp revision=1 change_seq=1` and does not add a second directory or a second row. The phone list is the reader's `/saved` page after the reader password. The reader process does not need a restart.

Revision 1 is the only revision this command sends. A later slice bumps the revision when the files change. Ingest answers 409, with `detail` and the stored revision, when the number is anything other than the stored revision or one past it. The same id and revision, with the same bytes, is 200.

The hourly rsync can keep running. Ingest replaces only the id it was given and leaves every other `snaps/<id>/` directory in place. A second push of an id that is already stored does not rewrite that directory, so a file the rsync added beside the snapshot bytes stays.
