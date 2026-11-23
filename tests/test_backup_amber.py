"""Local checks for the daily backup. Remote mode goes through a fake ssh."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "scripts" / "backup-amber.sh"
HEALTH = ROOT / "scripts" / "health-amber.sh"
DRILL = ROOT / "scripts" / "restore-drill.sh"
REAL_RSYNC = shutil.which("rsync")


def _library_db(path: Path, rows: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE snapshots (
            id TEXT PRIMARY KEY,
            url TEXT,
            url_normalized TEXT,
            final_url TEXT,
            title TEXT,
            site_name TEXT,
            created_at TEXT,
            word_count INTEGER,
            status TEXT
        )
        """
    )
    for index in range(rows):
        usable = index == 0
        conn.execute(
            """
            INSERT INTO snapshots
            (id, url, url_normalized, final_url, title, site_name, created_at, word_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'complete')
            """,
            (
                f"id{index}",
                f"https://example.com/{index}",
                f"https://example.com/{index}",
                f"https://example.com/{index}",
                "A real headline" if usable else "t",
                "Example",
                (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                200 if usable else 2,
            ),
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
    long_text = ("alpha " * 120).strip() + "\n"
    for sid in ("id0", "id1", "Z2345"):
        snap = library / "snaps" / sid
        snap.mkdir(parents=True)
        text = long_text if sid == "id0" else "same bytes\n"
        (snap / "article.txt").write_text(text, encoding="utf-8")
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
if [[ "$*" == *for\\ d\\ in* || "$*" == *\\ ls\\ * || "$*" == ls\\ * ]]; then
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
    paths["stage"].mkdir()
    (paths["stage"] / "keep-me").write_text("stay", encoding="utf-8")
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
    assert manifest["library_tables"]["snapshots"] == 2
    assert manifest["app_tables"]["candidates"] == 1
    assert manifest["candidates"] == 1
    assert manifest["sources"] == 1
    assert manifest["snaps_files"] == 3
    assert len(manifest["articles"]) >= 1
    assert "total_usable" not in manifest
    assert not (paths["stage"] / "amber-backup-stage" / "current").exists()
    assert (paths["stage"] / "keep-me").read_text() == "stay"


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
    old_incomplete = paths["dest"] / "2024-12-31"
    old_incomplete.mkdir(parents=True)
    (old_incomplete / "marker").write_text("incomplete", encoding="utf-8")
    recent = paths["dest"] / "2026-01-31"
    recent.mkdir()
    (recent / "marker").write_text("recent", encoding="utf-8")
    stale = paths["dest"] / "2026-01-29"
    stale.mkdir()
    (stale / "marker").write_text("stale", encoding="utf-8")
    result = _run(_env(paths, AMBER_BACKUP_DATE="2026-02-01"))
    assert result.returncode == 0, result.stderr
    names = sorted(path.name for path in paths["dest"].iterdir())
    assert "2026-02-01" in names
    assert "2025-01-01" not in names
    assert "2025-01-02" not in names
    assert "2024-12-31" not in names
    assert "2026-01-29" not in names
    assert (paths["dest"] / "2025-01-03" / "marker").read_text() == "old"
    assert (recent / "marker").read_text() == "recent"
    assert "pruning 2025-01-01" in result.stderr
    assert "pruning incomplete 2024-12-31" in result.stderr


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
    assert log.rindex(".complete") > log.index("rsync")
    recorded = rsync_log.read_text(encoding="utf-8")
    assert "--rsync-path=rsync" in recorded
    assert "--delete" not in recorded
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
    assert (paths["stage"] / "amber-backup-stage" / "current" / "amber.sqlite3").is_file()
    (paths["stage"] / "keep-me").write_text("stay", encoding="utf-8")
    leftover = paths["stage"] / "amber-backup-stage" / "2020-01-01"
    leftover.mkdir()
    (leftover / "old").write_text("x", encoding="utf-8")
    ok = _run(_env(paths, AMBER_BACKUP_DATE="2026-05-02"))
    assert ok.returncode == 0, ok.stderr
    assert paths["stage"].is_dir()
    assert (paths["stage"] / "keep-me").read_text() == "stay"
    assert not (paths["stage"] / "amber-backup-stage" / "current").exists()
    assert not leftover.exists()


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
    password = "secr#et word"
    (bin_dir / "curl").write_text(
        f"""#!/bin/sh
printf '%s\\n' "$*" >> "{curl_log}"
url=
prev=
cfg=
for arg in "$@"; do
  case "$arg" in
    http://*|https://*) url=$arg ;;
  esac
  if [ "$prev" = "-K" ]; then
    cfg=$arg
  fi
  prev=$arg
done
case "$url" in
  *8081*|*8084*)
    [ -n "$cfg" ] || exit 5
    [ "$(stat -c %a "$cfg")" = 600 ] || exit 4
    grep -F -q 'user = "amber:{password}"' "$cfg" || exit 3
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
    env["AMBER_HEALTH_PASSWORD"] = password
    env["AMBER_BACKUP_STAMP"] = str(stamp)
    tailnet = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "dell-plx-reader.tailf9612a.ts.net:443": {
                "Handlers": {"/": {"Proxy": "http://127.0.0.1:8081"}}
            }
        },
    }
    funnel = {
        **tailnet,
        "AllowFunnel": {"dell-plx-reader.tailf9612a.ts.net:443": True},
    }

    def tailscale(body: str, code: int) -> None:
        script = bin_dir / "tailscale"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" != serve ] || [ "$2" != status ] || [ "$3" != --json ]; then exit 2; fi\n'
            f"printf '%s\\n' {body!r}\nexit {code}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        env["AMBER_TAILSCALE"] = str(script)

    tailscale("null", 0)
    empty = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert empty.returncode == 0, empty.stderr
    tailscale(json.dumps(tailnet), 0)
    ok = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert ok.returncode == 0, ok.stderr
    logged = curl_log.read_text(encoding="utf-8")
    assert "-K " in logged
    assert password not in logged

    tailscale(json.dumps(funnel), 0)
    bad = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert bad.returncode != 0
    assert "funnel is on" in bad.stderr

    tailscale("not-json", 0)
    unknown = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert unknown.returncode != 0
    assert "not JSON" in unknown.stderr

    script = bin_dir / "tailscale"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" != serve ] || [ "$2" != status ] || [ "$3" != --json ]; then exit 2; fi\n'
        "echo 'permission denied' >&2\nexit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    env["AMBER_TAILSCALE"] = str(script)
    failed = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert failed.returncode != 0
    assert "serve status failed" in failed.stderr
    assert "permission denied" in failed.stderr

    tailscale("null", 0)
    stamp.unlink()
    missing = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert missing.returncode != 0
    assert "backup stamp" in missing.stderr


def test_restore_drill_uses_manifest_and_reader(tmp_path: Path):
    paths = _layout(tmp_path)
    assert _run(_env(paths, AMBER_BACKUP_DATE="2026-03-01")).returncode == 0
    conn = sqlite3.connect(paths["library"] / "amber.sqlite3")
    conn.execute(
        """
        INSERT INTO snapshots
        (id, url, url_normalized, final_url, title, site_name, created_at, word_count, status)
        VALUES ('id9', 'https://example.com/9', 'https://example.com/9', 'https://example.com/9',
                'later', 'Example', '2026-11-01T00:00:00Z', 2, 'complete')
        """
    )
    conn.commit()
    conn.close()
    server = tmp_path / "reader.py"
    server.write_text(
        """
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
data = Path(os.environ["AMBER_DATA_DIR"])
print("reader-data=" + str(data), flush=True)
print("blobs=" + str(int((data / "blobs").exists())), flush=True)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/healthz"):
            return self._send(200, b"ok")
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
    assert "manifest ok library_snapshots=2 candidates=1 snaps_files=3 articles=2" in result.stdout
    assert "served 2 articles" in result.stdout
    assert "total_usable" not in result.stdout
    assert "live snapshot count (not checked): 3" in result.stderr
    assert "secret" not in result.stdout
    assert "secret" not in result.stderr
    data_line = next(line for line in result.stdout.splitlines() if line.startswith("reader-data="))
    data_dir = Path(data_line.split("=", 1)[1])
    assert "blobs=0" in result.stdout
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


def test_staging_root_is_refused(tmp_path: Path):
    paths = _layout(tmp_path)
    library_db = paths["library"] / "amber.sqlite3"
    original = library_db.read_bytes()
    cases = {
        "": "empty",
        "/": "refusing to use /",
        str(paths["library"].parent): "home directory",
        str(paths["library"]): "the library",
        str(paths["state"]): "app-state",
    }
    for staging, needle in cases.items():
        result = _run(_env(paths, AMBER_BACKUP_STAGING=staging, AMBER_BACKUP_DATE="2026-07-01"))
        assert result.returncode != 0, staging
        assert needle in result.stderr
        assert library_db.read_bytes() == original


def test_failed_rerun_clears_complete_and_requires_an_article(tmp_path: Path):
    paths = _layout(tmp_path)
    assert _run(_env(paths, AMBER_BACKUP_DATE="2026-05-03")).returncode == 0
    day = paths["dest"] / "2026-05-03"
    assert (day / ".complete").is_file()
    wrap = tmp_path / "rsync-fail"
    wrap.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    wrap.chmod(0o755)
    failed = _run(_env(paths, AMBER_BACKUP_DATE="2026-05-03", AMBER_RSYNC=str(wrap)))
    assert failed.returncode != 0
    assert not (day / ".complete").exists()
    for article in (paths["library"] / "snaps").rglob("article.txt"):
        article.unlink()
    missing = _run(_env(paths, AMBER_BACKUP_DATE="2026-05-04"))
    assert missing.returncode != 0
    assert "no article.txt" in missing.stderr


def test_local_drill_reports_a_missing_destination(tmp_path: Path):
    paths = _layout(tmp_path)
    assert _run(_env(paths, AMBER_BACKUP_DATE="2026-03-02")).returncode == 0
    later = paths["dest"] / "2026-09-01"
    later.mkdir()
    (later / "marker").write_text("incomplete", encoding="utf-8")
    env = _env(paths)
    env["AMBER_RESTORE_SKIP_SERVER"] = "1"
    ok = subprocess.run([str(DRILL)], env=env, text=True, capture_output=True, check=False)
    assert ok.returncode == 0, ok.stderr
    assert "manifest ok" in ok.stdout
    missing = _env(paths)
    missing["AMBER_BACKUP_DEST"] = str(tmp_path / "no-such-dest")
    missing["AMBER_RESTORE_SKIP_SERVER"] = "1"
    bad = subprocess.run([str(DRILL)], env=missing, text=True, capture_output=True, check=False)
    assert bad.returncode != 0
    assert "no backup destination" in bad.stderr


def test_reader_count_must_match(tmp_path: Path):
    paths = _layout(tmp_path)
    assert _run(_env(paths, AMBER_BACKUP_DATE="2026-03-03")).returncode == 0
    def serve(payload: str, port: str) -> subprocess.CompletedProcess[str]:
        server = tmp_path / f"reader-{port}.py"
        server.write_text(
            f"""
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PAYLOAD = {payload!r}.encode()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok" if self.path.startswith("/healthz") else PAYLOAD
        self.send_response(200)
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
        env["AMBER_RESTORE_PORT"] = port
        return subprocess.run([str(DRILL)], env=env, text=True, capture_output=True, check=False)

    wrong = serve("not the snapshot", "18762")
    assert wrong.returncode != 0
    assert "did not contain its snapshot text" in wrong.stderr
    empty = serve("", "18763")
    assert empty.returncode != 0
    assert "was empty" in empty.stderr


def test_unit_env_loads_into_app_settings(tmp_path: Path):
    home = tmp_path / "home"
    env = os.environ.copy()
    for line in (ROOT / "docs" / "ops" / "amber-web.service").read_text(encoding="utf-8").splitlines():
        if not line.startswith("Environment="):
            continue
        key, value = line[len("Environment=") :].split("=", 1)
        env[key] = value.replace("%h", str(home))
    env.pop("AMBER_READER_PASSWORD", None)
    password_file = Path(env["AMBER_READER_PASSWORD_FILE"])
    password_file.parent.mkdir(parents=True, exist_ok=True)
    password_file.write_text("unit-secret\n", encoding="utf-8")
    password_file.chmod(0o600)
    token_file = Path(env["AMBER_INGEST_TOKEN_FILE"])
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("ingest-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    (home / "apps" / "amber-reader" / "data").mkdir(parents=True)
    script = tmp_path / "check_unit_env.py"
    script.write_text(
        """
import app.auth as auth
import app.config as config
from app.main import ALLOWED_HOSTS

import os
from pathlib import Path

from app.library_push import _push_settings

assert config.ingest_library()
assert config.HOST == "127.0.0.1"
assert config.PORT == 8084
assert "dell-plx-reader.tailf9612a.ts.net" in config.EXTRA_HOSTS
assert "127.0.0.1" in config.EXTRA_HOSTS
assert config.app_state_dir() == Path(os.environ["AMBER_APP_STATE"]).resolve()
assert config.scratch_path() == Path(os.environ["AMBER_SCRATCH_DIR"]).resolve()
assert os.environ["AMBER_INGEST_URL"] == "http://127.0.0.1:8082"
assert os.environ["AMBER_INGEST_TOKEN_FILE"].endswith("/.config/amber/ingest.token")
assert _push_settings() == ("http://127.0.0.1:8082", "ingest-token")
assert auth.USERNAME == "amber"
assert auth.required_password() == "unit-secret"
assert auth.CHALLENGE == 'Basic realm="amber-reader", charset="UTF-8"'
assert "/healthz" in auth.OPEN_PATHS
assert "dell-plx-reader.tailf9612a.ts.net" in ALLOWED_HOSTS
assert "127.0.0.1" in ALLOWED_HOSTS
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["python3", str(script)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr


def test_units_and_docs_keep_bind_and_cold_boot_order():
    unit = (ROOT / "docs" / "ops" / "amber-web.service").read_text(encoding="utf-8")
    assert "amber-reader.env" not in unit
    assert "EnvironmentFile=" not in unit
    assert "AMBER_READER_PASSWORD_FILE=%h/.config/amber/reader.password" in unit
    example = (ROOT / "docs" / "ops" / "amber-web.env.example").read_text(encoding="utf-8")
    assert "reader.password" in example
    assert "only the password" in example
    assert not [line for line in example.splitlines() if line and not line.startswith("#")]
    assert "AMBER_ALLOWED_HOSTS=dell-plx-reader.tailf9612a.ts.net,127.0.0.1:8084" in unit
    assert "AMBER_HOST=127.0.0.1" in unit
    assert "--host 127.0.0.1 --port 8084" in unit
    backup_unit = (ROOT / "docs" / "ops" / "amber-backup.service").read_text(encoding="utf-8")
    assert "AMBER_RSYNC_PATH=" in backup_unit
    assert "OnFailure=amber-backup-failed.service" in backup_unit
    cutover = (ROOT / "docs" / "ops" / "dell-plx-cutover.md").read_text(encoding="utf-8")
    assert cutover.index("http://127.0.0.1:8081") < cutover.index("enable --now amber-web.service")
    assert cutover.index("enable --now amber-web.service") < cutover.index(
        "tailscale serve --bg --https=443 http://127.0.0.1:8084"
    )
    assert "18081" in cutover
    assert "rollback" in cutover.lower()
    cold = (ROOT / "docs" / "ops" / "dell-plx-cold-boot.md").read_text(encoding="utf-8")
    assert "https://dell-plx-reader.tailf9612a.ts.net/healthz" in cold
    assert "https://dell-plx-reader.tailf9612a.ts.net:8443/healthz" in cold
    assert "query.exe user" in cold
    assert "No User exists for *" in cold
    assert "tasklist.exe" in cold
    assert "explorer.exe" in cold
    assert "ToUniversalTime()" in cold
    assert "dell-plx-reader" in cold
    assert "Do not sign in at the desktop" in cold
    assert "Mac" in cold
    assert "screenshot" in cold
    assert "phone" in cold
    assert "S4U" in cold
    assert "127.0.0.1:8084/healthz" in cold
    backup_doc = (ROOT / "docs" / "ops" / "dell-plx-backup.md").read_text(encoding="utf-8")
    assert "ssh -p 3003 boxj '$HOME/bin/rsync --version'" in backup_doc
    assert "scripts/sync-from-mac.sh" in backup_doc
