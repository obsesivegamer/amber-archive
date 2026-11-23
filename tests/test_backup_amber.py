"""Local checks for the daily backup. Remote mode goes through a fake ssh."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "scripts" / "backup-amber.sh"
HEALTH = ROOT / "scripts" / "health-amber.sh"
DRILL = ROOT / "scripts" / "restore-drill.sh"
REAL_RSYNC = "/usr/bin/rsync"


def _library_db(path: Path, rows: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE snapshots (id TEXT PRIMARY KEY, title TEXT, status TEXT)")
    for index in range(rows):
        conn.execute(
            "INSERT INTO snapshots VALUES (?, ?, 'complete')", (f"id{index}", "t")
        )
    conn.commit()
    conn.close()


def _layout(tmp_path: Path) -> dict[str, Path]:
    library = tmp_path / "library"
    state = tmp_path / "state"
    _library_db(library / "amber.sqlite3", 2)
    state.mkdir()
    conn = sqlite3.connect(state / "app.sqlite3")
    conn.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO candidates VALUES ('c1')")
    conn.commit()
    conn.close()
    (state / "app.sqlite3-wal").write_bytes(b"not-the-backup")
    (state / "sources.json").write_text('{"sources":[{"id":"one"}]}', encoding="utf-8")
    (state / "homepage.json").write_text(
        '{"code":"x","timestamp":"t","bytes":1}', encoding="utf-8"
    )
    nested = state / "nested"
    nested.mkdir()
    (nested / "note.txt").write_text("nested", encoding="utf-8")
    for sid in ("id0", "id1", "Z2345"):
        snap = library / "snaps" / sid
        snap.mkdir(parents=True)
        (snap / "article.txt").write_text("same bytes\n", encoding="utf-8")
    (library / "blobs").mkdir()
    (library / "blobs" / "aa").write_bytes(b"blob")
    return {
        "library": library,
        "state": state,
        "dest": tmp_path / "backups",
        "stage": tmp_path / "stage",
    }


def _run(env: dict, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BACKUP), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _env(paths: dict[str, Path], **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AMBER_LIBRARY_DIR": str(paths["library"]),
            "AMBER_APP_STATE": str(paths["state"]),
            "AMBER_BACKUP_DEST": str(paths["dest"]),
            "AMBER_BACKUP_STAGING": str(paths["stage"]),
            "AMBER_BACKUP_SSH": "",
            "AMBER_BACKUP_KEEP": "30",
            "AMBER_BACKUP_STAMP": str(paths["stage"].parent / "stamp"),
            "HOME": str(paths["library"].parent),
        }
    )
    env.update(extra)
    return env


def _complete(day: Path) -> None:
    day.mkdir(parents=True, exist_ok=True)
    (day / ".complete").write_text(day.name + "\n", encoding="utf-8")


def _write_ssh(path: Path, *, fail_ls: bool = False) -> None:
    path.write_text(
        f"""#!/bin/bash
set -euo pipefail
printf '%s\\n' "$*" >> "${{SSH_LOG:?}}"
if [[ "${{1:-}}" == "-l" ]]; then
  exit 1
fi
host="$1"
shift
cd "${{BOXJ_ROOT:?}}"
if [[ "$*" == *\\ ls\\ * || "$*" == ls\\ * ]]; then
  {"exit 1" if fail_ls else ":"}
fi
if [[ "${{1:-}}" == rsync || "${{1:-}}" == */rsync ]]; then
  exec "$@"
fi
bash -c "$*"
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_rsync(path: Path, *, vanish_snaps: bool = False) -> None:
    path.write_text(
        f"""#!/bin/bash
printf '%s\\n' "$*" >> "${{RSYNC_LOG:?}}"
if [[ "{int(vanish_snaps)}" == 1 && "$*" == *snaps* ]]; then
  exit 24
fi
exec {REAL_RSYNC} "$@"
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_backup_copy_keeps_nested_state_and_drops_staging(tmp_path: Path):
    paths = _layout(tmp_path)
    result = _run(_env(paths, AMBER_BACKUP_DATE="2026-04-02"))
    assert result.returncode == 0, result.stderr
    day = paths["dest"] / "2026-04-02"
    copy = sqlite3.connect(day / "amber.sqlite3")
    assert copy.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
    copy.close()
    assert (day / "amber.sqlite3").stat().st_ino != (paths["library"] / "amber.sqlite3").stat().st_ino
    assert (day / "state" / "sources.json").is_file()
    assert (day / "state" / "nested" / "note.txt").read_text() == "nested"
    assert not (day / "state" / "app.sqlite3-wal").exists()
    state_db = sqlite3.connect(day / "state" / "app.sqlite3")
    assert state_db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
    state_db.close()
    assert (day / "snaps" / "Z2345" / "article.txt").read_text() == "same bytes\n"
    assert (day / ".complete").is_file()
    manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshots"] == 2
    assert manifest["candidates"] == 1
    assert manifest["sources"] == 1
    assert manifest["articles"]
    assert not (paths["stage"] / "current").exists()
    assert list(paths["stage"].glob("*")) == []


def test_link_dest_keeps_old_extra_file_off_the_new_day(tmp_path: Path):
    paths = _layout(tmp_path)
    first = _run(_env(paths, AMBER_BACKUP_DATE="2026-01-01"))
    assert first.returncode == 0, first.stderr
    kept = paths["dest"] / "2026-01-01" / "snaps" / "Z2345" / "note.txt"
    kept.write_text("leave this", encoding="utf-8")
    incomplete = paths["dest"] / "2026-01-02" / "snaps" / "Z2345"
    incomplete.mkdir(parents=True)
    (incomplete / "article.txt").write_text("other\n", encoding="utf-8")
    (incomplete.parent.parent / "only-incomplete").write_text("skip", encoding="utf-8")
    second = _run(_env(paths, AMBER_BACKUP_DATE="2026-01-03"))
    assert second.returncode == 0, second.stderr
    old = paths["dest"] / "2026-01-01" / "snaps" / "Z2345" / "article.txt"
    new = paths["dest"] / "2026-01-03" / "snaps" / "Z2345" / "article.txt"
    assert old.stat().st_ino == new.stat().st_ino
    assert kept.read_text() == "leave this"
    assert not (paths["dest"] / "2026-01-03" / "snaps" / "Z2345" / "note.txt").exists()
    assert (paths["dest"] / "2026-01-02" / "only-incomplete").read_text() == "skip"


def test_prune_trusts_only_complete_directories(tmp_path: Path):
    paths = _layout(tmp_path)
    for index in range(31):
        day = paths["dest"] / f"2025-01-{index + 1:02d}"
        _complete(day)
        (day / "marker").write_text("old", encoding="utf-8")
    incomplete = paths["dest"] / "2024-12-31"
    incomplete.mkdir(parents=True)
    (incomplete / "marker").write_text("incomplete", encoding="utf-8")
    result = _run(_env(paths, AMBER_BACKUP_DATE="2026-02-01"))
    assert result.returncode == 0, result.stderr
    names = sorted(path.name for path in paths["dest"].iterdir())
    assert "2026-02-01" in names
    assert "2025-01-01" not in names
    assert "2025-01-02" not in names
    assert (paths["dest"] / "2025-01-03" / "marker").read_text() == "old"
    assert (incomplete / "marker").read_text() == "incomplete"
    assert "pruning 2025-01-01" in result.stderr
    assert len(names) == 31


def test_keep_below_one_does_not_delete(tmp_path: Path):
    paths = _layout(tmp_path)
    old = paths["dest"] / "2020-01-01"
    _complete(old)
    (old / "marker").write_text("stay", encoding="utf-8")
    result = _run(_env(paths, AMBER_BACKUP_KEEP="0"))
    assert result.returncode != 0
    assert "at least 1" in result.stderr
    assert (old / "marker").read_text() == "stay"
    assert [path.name for path in paths["dest"].iterdir()] == ["2020-01-01"]


def test_remote_dest_with_empty_ssh_fails(tmp_path: Path):
    paths = _layout(tmp_path)
    result = _run(_env(paths, AMBER_BACKUP_DEST="boxj:amber-backups", AMBER_BACKUP_SSH=""))
    assert result.returncode != 0
    assert "AMBER_BACKUP_SSH is empty" in result.stderr
    assert not (tmp_path / "boxj:amber-backups").exists()


def test_remote_mkdir_before_rsync_and_rsync_path(tmp_path: Path):
    paths = _layout(tmp_path)
    box = tmp_path / "box"
    box.mkdir()
    ssh_log = tmp_path / "ssh.log"
    rsync_log = tmp_path / "rsync.log"
    ssh = tmp_path / "ssh"
    wrap = tmp_path / "rsync-wrap"
    _write_ssh(ssh)
    _write_rsync(wrap)
    assert not (box / "amber-backups").exists()
    env = _env(
        paths,
        AMBER_BACKUP_DEST="boxj:amber-backups",
        AMBER_BACKUP_SSH=str(ssh),
        AMBER_BACKUP_DATE="2026-04-01",
        AMBER_RSYNC=str(wrap),
        AMBER_RSYNC_PATH="rsync",
    )
    env["BOXJ_ROOT"] = str(box)
    env["SSH_LOG"] = str(ssh_log)
    env["RSYNC_LOG"] = str(rsync_log)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    day = box / "amber-backups" / "2026-04-01"
    assert (day / "amber.sqlite3").is_file()
    assert (day / "manifest.json").is_file()
    assert (day / ".complete").read_text(encoding="utf-8").strip() == "2026-04-01"
    assert (day / "state" / "nested" / "note.txt").read_text() == "nested"
    log = ssh_log.read_text(encoding="utf-8")
    assert log.index("mkdir -p") < log.index("rsync")
    assert log.index("rsync") < log.index(".complete")
    assert "--rsync-path=rsync" in rsync_log.read_text(encoding="utf-8")
    kept = day / "snaps" / "Z2345" / "note.txt"
    kept.write_text("leave this", encoding="utf-8")
    env["AMBER_BACKUP_DATE"] = "2026-04-02"
    second = _run(env)
    assert second.returncode == 0, second.stderr
    new = box / "amber-backups" / "2026-04-02" / "snaps" / "Z2345" / "article.txt"
    assert (day / "snaps" / "Z2345" / "article.txt").stat().st_ino == new.stat().st_ino
    assert kept.read_text() == "leave this"
    assert not (new.parent / "note.txt").exists()


def test_failed_remote_listing_is_not_ignored(tmp_path: Path):
    paths = _layout(tmp_path)
    box = tmp_path / "box"
    box.mkdir()
    ssh_log = tmp_path / "ssh.log"
    ssh = tmp_path / "ssh"
    _write_ssh(ssh, fail_ls=True)
    env = _env(
        paths,
        AMBER_BACKUP_DEST="boxj:amber-backups",
        AMBER_BACKUP_SSH=str(ssh),
        AMBER_BACKUP_DATE="2026-04-01",
        AMBER_RSYNC_PATH="rsync",
    )
    env["BOXJ_ROOT"] = str(box)
    env["SSH_LOG"] = str(ssh_log)
    result = _run(env)
    assert result.returncode != 0
    assert "could not list" in result.stderr
    assert "backup failed:" in result.stderr
    assert not (box / "amber-backups" / "2026-04-01").exists()


def test_vanished_files_are_a_warning(tmp_path: Path):
    paths = _layout(tmp_path)
    wrap = tmp_path / "rsync-wrap"
    rsync_log = tmp_path / "rsync.log"
    _write_rsync(wrap, vanish_snaps=True)
    env = _env(paths, AMBER_BACKUP_DATE="2026-05-01", AMBER_RSYNC=str(wrap))
    env["RSYNC_LOG"] = str(rsync_log)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "vanished files" in result.stderr
    assert (paths["dest"] / "2026-05-01" / ".complete").is_file()


def test_failed_run_keeps_only_the_staging_directory(tmp_path: Path):
    paths = _layout(tmp_path)
    wrap = tmp_path / "rsync-wrap"
    rsync_log = tmp_path / "rsync.log"
    wrap.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    wrap.chmod(0o755)
    env = _env(paths, AMBER_BACKUP_DATE="2026-05-02", AMBER_RSYNC=str(wrap))
    env["RSYNC_LOG"] = str(rsync_log)
    failed = _run(env)
    assert failed.returncode != 0
    assert (paths["stage"] / "current" / "amber.sqlite3").is_file()
    leftover = paths["stage"] / "2020-01-01"
    leftover.mkdir()
    (leftover / "old").write_text("x", encoding="utf-8")
    ok = _run(_env(paths, AMBER_BACKUP_DATE="2026-05-02"))
    assert ok.returncode == 0, ok.stderr
    assert not paths["stage"].exists()


def test_missing_library_fails_loudly(tmp_path: Path):
    paths = _layout(tmp_path)
    (paths["library"] / "amber.sqlite3").unlink()
    result = _run(_env(paths))
    assert result.returncode != 0
    assert "backup failed:" in result.stderr


def test_health_hides_the_password_and_fails_closed(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_log = tmp_path / "curl.log"
    (bin_dir / "curl").write_text(
        f"""#!/bin/sh
printf '%s\\n' "$*" >> "{curl_log}"
for arg in "$@"; do
  case "$arg" in
    *secret*) echo password-on-argv >> "{curl_log}"; exit 2 ;;
  esac
done
url=
prev=
netrc=
for arg in "$@"; do
  case "$arg" in
    http://*|https://*) url=$arg ;;
  esac
  if [ "$prev" = "--netrc-file" ]; then
    netrc=$arg
  fi
  prev=$arg
done
case "$url" in
  *8081*|*8084*)
    [ -n "$netrc" ] || exit 5
    [ "$(stat -c %a "$netrc")" = 600 ] || exit 4
    grep -q secret "$netrc" || exit 3
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    (bin_dir / "curl").chmod(0o755)
    stamp = tmp_path / "stamp"
    stamp.write_text(subprocess.check_output(["date", "-u", "+%s"], text=True), encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["AMBER_HEALTH_PASSWORD"] = "secret"
    env["AMBER_BACKUP_STAMP"] = str(stamp)
    env["CURL_LOG"] = str(curl_log)

    def tailscale(body: str, code: int) -> None:
        script = bin_dir / "tailscale"
        script.write_text(f"#!/bin/sh\nprintf '%s\\n' {body!r}\nexit {code}\n", encoding="utf-8")
        script.chmod(0o755)
        env["AMBER_TAILSCALE"] = str(script)

    tailscale("Funnel is disabled", 0)
    disabled = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert disabled.returncode == 0, disabled.stderr
    tailscale("No funnel", 0)
    ok = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert ok.returncode == 0, ok.stderr
    logged = curl_log.read_text(encoding="utf-8")
    assert "--netrc-file" in logged
    assert "secret" not in logged
    assert "password-on-argv" not in logged

    tailscale("Funnel on", 0)
    bad = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert bad.returncode != 0
    assert "funnel is on" in bad.stderr

    tailscale("status unknown", 0)
    unknown = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert unknown.returncode != 0
    assert "not a known off state" in unknown.stderr

    tailscale("", 1)
    failed = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert failed.returncode != 0
    assert "funnel status failed" in failed.stderr

    tailscale("", 0)
    empty = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert empty.returncode == 0, empty.stderr

    stamp.unlink()
    missing = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert missing.returncode != 0
    assert "backup stamp" in missing.stderr


def test_restore_drill_uses_manifest_and_reader(tmp_path: Path):
    paths = _layout(tmp_path)
    assert _run(_env(paths, AMBER_BACKUP_DATE="2026-03-01")).returncode == 0
    conn = sqlite3.connect(paths["library"] / "amber.sqlite3")
    conn.execute("INSERT INTO snapshots VALUES ('id9', 'later', 'complete')")
    conn.commit()
    conn.close()
    server = tmp_path / "reader.py"
    server.write_text(
        """
import json, os, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
data = Path(os.environ["AMBER_DATA_DIR"])
print(f"reader-data={data}", flush=True)
print(f"blobs={int((data / 'blobs').exists())}", flush=True)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/healthz"):
            return self._send(200, b"ok")
        if self.path.startswith("/api/snapshots"):
            db = sqlite3.connect(data / "amber.sqlite3")
            count = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            return self._send(200, json.dumps({"count": count}).encode(), "application/json")
        if self.path.endswith("/article.txt"):
            sid = self.path.strip("/").split("/")[0]
            path = data / "snaps" / sid / "article.txt"
            if not path.is_file():
                return self._send(404, b"missing")
            return self._send(200, path.read_bytes(), "text/plain")
        return self._send(404, b"no")
    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, fmt, *args):
        return
ThreadingHTTPServer(("127.0.0.1", int(os.environ["AMBER_PORT"])), Handler).serve_forever()
""",
        encoding="utf-8",
    )
    env = _env(paths)
    env["AMBER_READER_CMD"] = f"python3 {server}"
    env["AMBER_RESTORE_PORT"] = "18761"
    env["AMBER_READER_PASSWORD"] = "secret"
    result = subprocess.run([str(DRILL)], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "manifest ok snapshots=2" in result.stdout
    assert "reader count 2" in result.stdout
    assert "served 2 articles" in result.stdout
    assert "live snapshot count (not checked): 3" in result.stderr
    assert result.args == [str(DRILL)]
    data_line = next(line for line in result.stdout.splitlines() if line.startswith("reader-data="))
    data_dir = Path(data_line.split("=", 1)[1])
    assert f"blobs=0" in result.stdout
    assert not data_dir.exists()
    assert not data_dir.parent.exists()


def test_restore_drill_refuses_live_paths_and_remote_blobs(tmp_path: Path):
    paths = _layout(tmp_path)
    box = tmp_path / "box"
    box.mkdir()
    ssh = tmp_path / "ssh"
    ssh_log = tmp_path / "ssh.log"
    wrap = tmp_path / "rsync-wrap"
    rsync_log = tmp_path / "rsync.log"
    _write_ssh(ssh)
    _write_rsync(wrap)
    env = _env(
        paths,
        AMBER_BACKUP_DEST="boxj:amber-backups",
        AMBER_BACKUP_SSH=str(ssh),
        AMBER_BACKUP_DATE="2026-06-01",
        AMBER_RSYNC=str(wrap),
        AMBER_RSYNC_PATH="rsync",
    )
    env["BOXJ_ROOT"] = str(box)
    env["SSH_LOG"] = str(ssh_log)
    env["RSYNC_LOG"] = str(rsync_log)
    assert _run(env).returncode == 0
    work = tmp_path / "drill-work"
    drill_env = env.copy()
    drill_env["AMBER_RESTORE_WORK"] = str(work)
    drill_env["AMBER_RESTORE_SKIP_SERVER"] = "1"
    rsync_log.write_text("", encoding="utf-8")
    result = subprocess.run([str(DRILL)], env=drill_env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (work / "nas" / "snaps" / "id0" / "article.txt").is_file()
    assert not (work / "nas" / "blobs").exists()
    logged = rsync_log.read_text(encoding="utf-8")
    assert "--rsync-path=rsync" in logged
    assert "--exclude" in logged
    live = _env(paths)
    live["AMBER_BACKUP_DEST"] = str(paths["library"] / "backups")
    (paths["library"] / "backups" / "2026-01-01").mkdir(parents=True)
    (paths["library"] / "backups" / "2026-01-01" / ".complete").write_text("x\n", encoding="utf-8")
    refused = subprocess.run([str(DRILL)], env=live, text=True, capture_output=True, check=False)
    assert refused.returncode != 0
    assert "refusing to use live path" in refused.stderr
    empty = _env(paths, AMBER_BACKUP_DEST="boxj:amber-backups", AMBER_BACKUP_SSH="")
    empty["AMBER_RESTORE_SKIP_SERVER"] = "1"
    missing_ssh = subprocess.run([str(DRILL)], env=empty, text=True, capture_output=True, check=False)
    assert missing_ssh.returncode != 0
    assert "AMBER_BACKUP_SSH is empty" in missing_ssh.stderr


def test_units_and_docs_keep_bind_and_cold_boot_order():
    unit = (ROOT / "docs" / "ops" / "amber-web.service").read_text(encoding="utf-8")
    assert "amber-reader.env" not in unit
    assert "amber-web.env" in unit
    assert unit.index("EnvironmentFile=") < unit.index("AMBER_LIBRARY_MODE=ingest")
    assert "AMBER_ALLOWED_HOSTS=dell-plx-reader.tailf9612a.ts.net,127.0.0.1:8084" in unit
    assert "AMBER_HOST=127.0.0.1" in unit
    assert "--host 127.0.0.1 --port 8084" in unit
    backup_unit = (ROOT / "docs" / "ops" / "amber-backup.service").read_text(encoding="utf-8")
    assert "AMBER_RSYNC_PATH=" in backup_unit
    assert "OnFailure=amber-backup-failed.service" in backup_unit
    cutover = (ROOT / "docs" / "ops" / "dell-plx-cutover.md").read_text(encoding="utf-8")
    assert cutover.index("enable --now amber-web.service") < cutover.index(
        "tailscale serve --bg --https=443 http://127.0.0.1:8084"
    )
    assert "18081" in cutover
    cold = (ROOT / "docs" / "ops" / "dell-plx-cold-boot.md").read_text(encoding="utf-8")
    assert "https://dell-plx-reader.tailf9612a.ts.net/healthz" in cold
    assert "https://dell-plx-reader.tailf9612a.ts.net:8443/healthz" in cold
    assert "query.exe user" in cold
    assert "No User exists for *" in cold
    assert "tasklist.exe" in cold
    assert "explorer.exe" in cold
    assert "LastBootUpTime" in cold
    assert "S4U" in cold
    assert "127.0.0.1:8084/healthz" in cold
