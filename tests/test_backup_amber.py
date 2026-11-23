"""Local checks for the daily backup. No SSH and no boxj."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "scripts" / "backup-amber.sh"
HEALTH = ROOT / "scripts" / "health-amber.sh"
DRILL = ROOT / "scripts" / "restore-drill.sh"


def _db(path: Path, rows: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE snapshots (id TEXT PRIMARY KEY, title TEXT, status TEXT)"
    )
    for index in range(rows):
        conn.execute(
            "INSERT INTO snapshots VALUES (?, ?, 'complete')", (f"id{index}", "t")
        )
    conn.commit()
    conn.close()


def _layout(tmp_path: Path) -> dict[str, Path]:
    library = tmp_path / "library"
    state = tmp_path / "state"
    _db(library / "amber.sqlite3", 2)
    _db(state / "app.sqlite3", 1)
    conn = sqlite3.connect(state / "app.sqlite3")
    conn.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO candidates VALUES ('c1')")
    conn.commit()
    conn.close()
    (state / "sources.json").write_text('{"sources":[{"id":"one"}]}', encoding="utf-8")
    (state / "homepage.json").write_text('{"code":"x","timestamp":"t","bytes":1}', encoding="utf-8")
    for sid in ("id0", "id1", "Z2345"):
        snap = library / "snaps" / sid
        snap.mkdir(parents=True)
        (snap / "article.txt").write_text("same bytes\n", encoding="utf-8")
    (library / "blobs").mkdir()
    (library / "blobs" / "aa").write_bytes(b"blob")
    return {"library": library, "state": state, "dest": tmp_path / "backups", "stage": tmp_path / "stage"}


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
            "HOME": str(paths["library"].parent),
        }
    )
    env.update(extra)
    return env


def test_staging_backup_is_a_consistent_copy(tmp_path: Path):
    paths = _layout(tmp_path)
    result = _run(_env(paths), )
    assert result.returncode == 0, result.stderr
    staged = paths["stage"]
    day = next(staged.iterdir())
    copy = sqlite3.connect(day / "amber.sqlite3")
    assert copy.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
    copy.close()
    assert (day / "amber.sqlite3").stat().st_ino != (paths["library"] / "amber.sqlite3").stat().st_ino
    assert (day / "state" / "sources.json").is_file()
    assert (paths["dest"] / day.name / "snaps" / "Z2345" / "article.txt").read_text() == "same bytes\n"


def test_same_day_rerun_and_link_dest(tmp_path: Path):
    paths = _layout(tmp_path)
    first = _run(_env(paths, AMBER_BACKUP_DATE="2026-01-01"))
    assert first.returncode == 0, first.stderr
    # A file that must survive inside an older version.
    kept = paths["dest"] / "2026-01-01" / "snaps" / "Z2345" / "note.txt"
    kept.write_text("leave this", encoding="utf-8")
    second = _run(_env(paths, AMBER_BACKUP_DATE="2026-01-02"))
    assert second.returncode == 0, second.stderr
    again = _run(_env(paths, AMBER_BACKUP_DATE="2026-01-02"))
    assert again.returncode == 0, again.stderr
    old = paths["dest"] / "2026-01-01" / "snaps" / "Z2345" / "article.txt"
    new = paths["dest"] / "2026-01-02" / "snaps" / "Z2345" / "article.txt"
    assert old.stat().st_ino == new.stat().st_ino
    assert kept.read_text() == "leave this"
    assert (paths["dest"] / "2026-01-02" / "amber.sqlite3").is_file()


def test_prune_keeps_thirty_whole_directories(tmp_path: Path):
    paths = _layout(tmp_path)
    paths["dest"].mkdir()
    for index in range(31):
        day = paths["dest"] / f"2025-01-{index + 1:02d}"
        day.mkdir()
        (day / "marker").write_text("old", encoding="utf-8")
    result = _run(_env(paths, AMBER_BACKUP_DATE="2026-02-01"))
    assert result.returncode == 0, result.stderr
    names = sorted(path.name for path in paths["dest"].iterdir())
    assert len(names) == 30
    assert "2026-02-01" in names
    assert "2025-01-01" not in names
    assert "2025-01-02" not in names
    assert (paths["dest"] / "2025-01-03" / "marker").read_text() == "old"
    assert "pruning 2025-01-01" in result.stderr


def test_missing_library_fails_loudly(tmp_path: Path):
    paths = _layout(tmp_path)
    (paths["library"] / "amber.sqlite3").unlink()
    result = _run(_env(paths))
    assert result.returncode != 0
    assert "backup failed:" in result.stderr


def test_health_script_and_funnel(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "tailscale").write_text(
        "#!/bin/sh\nif [ \"$1\" = funnel ]; then echo 'No funnel'; exit 0; fi\nexit 1\n",
        encoding="utf-8",
    )
    os.chmod(bin_dir / "curl", 0o755)
    os.chmod(bin_dir / "tailscale", 0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["AMBER_HEALTH_PASSWORD"] = "secret"
    env["AMBER_TAILSCALE"] = str(bin_dir / "tailscale")
    ok = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert ok.returncode == 0, ok.stderr
    (bin_dir / "tailscale").write_text(
        "#!/bin/sh\necho 'Funnel on'; exit 0\n",
        encoding="utf-8",
    )
    os.chmod(bin_dir / "tailscale", 0o755)
    bad = subprocess.run([str(HEALTH)], env=env, text=True, capture_output=True, check=False)
    assert bad.returncode != 0
    assert "funnel is on" in bad.stderr


def test_restore_drill_matches_counts(tmp_path: Path):
    paths = _layout(tmp_path)
    assert _run(_env(paths, AMBER_BACKUP_DATE="2026-03-01")).returncode == 0
    env = _env(paths)
    env["AMBER_RESTORE_SKIP_SERVER"] = "1"
    result = subprocess.run([str(DRILL)], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "snapshot count 2" in result.stderr
    assert "candidates 1 sources 1" in result.stderr
