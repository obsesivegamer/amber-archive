"""Copy Dell-PLX snapshots this archive does not already have.

Reads ``GET /v1/changes`` and the committed files, then inserts the snapshot
row and ``snaps/<id>/``. An id that is already here with the same bytes is
skipped. An id that is already here with different bytes is left alone, so a
later push of those bytes gets 409 instead of replacing either copy.

Run it before ``python -m app.library_push --backfill``. The cursor is
``library-pull-cursor`` in the archive data directory.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .config import DATA_DIR
from .library_contract import (
    ID_RE,
    SHA256_RE,
    SNAPSHOTS_DDL,
    SNAPSHOT_COLUMNS,
    TokenFileError,
    read_token_file,
    sha256_hex,
    snapshot_relpath,
)
from .library_push import UrllibTransport


class PullError(Exception):
    def __init__(self, detail: str, status: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class PullItem:
    sid: str
    action: str
    change_seq: int


@dataclass(frozen=True)
class PullResult:
    items: tuple[PullItem, ...]
    cursor: int


_CURSOR = "library-pull-cursor"


def _cursor_path(source: Path) -> Path:
    return source / _CURSOR


def _read_cursor(source: Path) -> int:
    path = _cursor_path(source)
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8").strip()
    if not text.isdecimal():
        raise PullError("library-pull-cursor is not an integer")
    return int(text)


def _write_cursor(source: Path, cursor: int) -> None:
    source.mkdir(parents=True, exist_ok=True)
    tmp = source / ".library-pull-cursor.tmp"
    tmp.write_text(f"{cursor}\n", encoding="utf-8")
    os.replace(tmp, _cursor_path(source))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _remote_files(sid: str, files: list) -> list[tuple[str, str, int]]:
    listed: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise PullError(f"{sid} file entry is not an object")
        rel = snapshot_relpath(item.get("path"))
        if rel is None or rel in seen:
            raise PullError(f"{sid} has a file outside the snapshot")
        seen.add(rel)
        sha = item.get("sha256")
        size = item.get("size")
        if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha.lower()):
            raise PullError(f"{sid} file hash is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PullError(f"{sid} file size is invalid")
        listed.append((rel, sha.lower(), size))
    if "meta.json" not in seen:
        raise PullError(f"{sid} is missing meta.json")
    listed.sort()
    return listed


def _local_files(snap: Path) -> list[tuple[str, str, int]] | None:
    if snap.is_symlink() or not snap.exists():
        return None
    if not snap.is_dir():
        raise PullError(f"{snap.name} snapshot path is not a directory")
    listed: list[tuple[str, str, int]] = []
    for path in sorted(snap.rglob("*")):
        rel = path.relative_to(snap).as_posix()
        if path.is_symlink():
            raise PullError(f"{snap.name} {rel} is a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PullError(f"{snap.name} {rel} is not a regular file")
        if snapshot_relpath(rel) != rel:
            raise PullError(f"{snap.name} {rel} is outside the snapshot layout")
        data = path.read_bytes()
        listed.append((rel, sha256_hex(data), len(data)))
    listed.sort()
    return listed


def _has_row(source: Path, sid: str) -> bool:
    db_path = source / "amber.sqlite3"
    if not db_path.is_file():
        return False
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'snapshots'"
        ).fetchone()
        if row is None:
            return False
        found = conn.execute("SELECT 1 FROM snapshots WHERE id = ?", (sid,)).fetchone()
        return found is not None
    except sqlite3.Error as exc:
        raise PullError("could not read the local snapshot row") from exc
    finally:
        conn.close()


def _download(client, base: str, token: str, sid: str, rel: str) -> bytes:
    parts = "/".join(quote(part, safe="") for part in rel.split("/"))
    status, data = client.round_trip_bytes(
        "GET",
        f"{base}/v1/snapshots/{sid}/files/{parts}",
        None,
        _auth(token),
    )
    if status != 200:
        raise PullError(f"{sid} file {rel} was not stored", status=status)
    return data


def _materialize(source: Path, client, base: str, token: str, sid: str, files: list) -> None:
    staging_root = source / ".pull-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / sid
    if staging.exists() or staging.is_symlink():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        for item in files:
            rel = snapshot_relpath(item.get("path"))
            if rel is None:
                raise PullError(f"{sid} has a file outside the snapshot")
            sha = str(item.get("sha256", "")).lower()
            size = item.get("size")
            data = _download(client, base, token, sid, rel)
            if sha256_hex(data) != sha or not isinstance(size, int) or len(data) != size:
                raise PullError(f"{sid} file {rel} does not match the change")
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        final = source / "snaps" / sid
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.is_symlink() or (final.exists() and not final.is_dir()):
            raise PullError(f"{sid} snapshot path is not a directory")
        if final.exists():
            shutil.rmtree(final)
        os.rename(staging, final)
    except Exception:
        if staging.exists() or staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _required_text(sid: str, row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or value == "":
        raise PullError(f"{sid} row is missing {key}")
    return value


def _optional_text(sid: str, row: dict, key: str) -> str | None:
    if key not in row or row[key] is None:
        return None
    value = row[key]
    if not isinstance(value, str):
        raise PullError(f"{sid} row {key} is not text")
    return value


def _optional_int(sid: str, row: dict, key: str) -> int | None:
    if key not in row or row[key] is None:
        return None
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PullError(f"{sid} row {key} is not an integer")
    return value


def _insert_row(source: Path, sid: str, row: dict) -> None:
    if row.get("status") != "complete":
        raise PullError(f"{sid} status is not complete")
    paywalled = row.get("paywalled")
    if isinstance(paywalled, bool):
        flag = 1 if paywalled else 0
    elif isinstance(paywalled, int) and not isinstance(paywalled, bool):
        flag = 1 if paywalled else 0
    else:
        raise PullError(f"{sid} row paywalled is not a boolean")
    values = {
        "id": sid,
        "url": _required_text(sid, row, "url"),
        "url_normalized": _required_text(sid, row, "url_normalized"),
        "final_url": _optional_text(sid, row, "final_url"),
        "title": _optional_text(sid, row, "title"),
        "site_name": _optional_text(sid, row, "site_name"),
        "author": _optional_text(sid, row, "author"),
        "published_at": _optional_text(sid, row, "published_at"),
        "description": _optional_text(sid, row, "description"),
        "created_at": _required_text(sid, row, "created_at"),
        "http_status": _optional_int(sid, row, "http_status"),
        "word_count": _optional_int(sid, row, "word_count"),
        "paywalled": flag,
        "status": "complete",
        "error": _optional_text(sid, row, "error"),
    }
    db_path = source / "amber.sqlite3"
    source.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SNAPSHOTS_DDL)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT 1 FROM snapshots WHERE id = ?", (sid,)).fetchone()
        if existing is not None:
            conn.rollback()
            return
        columns = SNAPSHOT_COLUMNS
        conn.execute(
            f"""
            INSERT INTO snapshots ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            """,
            [values[column] for column in columns],
        )
        conn.commit()
    except PullError:
        raise
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise PullError("could not store the snapshot row") from exc
    finally:
        conn.close()


def _apply_one(source: Path, client, base: str, token: str, change: dict) -> PullItem:
    if not isinstance(change, dict):
        raise PullError("change is not an object")
    sid = change.get("id")
    if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
        raise PullError("change is missing a snapshot id")
    if change.get("op") != "upsert":
        raise PullError(f"{sid} op is not upsert")
    seq = change.get("change_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise PullError(f"{sid} change_seq is missing")
    files = change.get("files")
    row = change.get("row")
    if not isinstance(files, list) or not isinstance(row, dict):
        raise PullError(f"{sid} change is missing files")
    remote = _remote_files(sid, files)
    local = _local_files(source / "snaps" / sid)
    exists = _has_row(source, sid)
    if exists and local == remote:
        return PullItem(sid, "skipped", seq)
    if exists and local is not None and local != remote:
        return PullItem(sid, "kept", seq)
    if local != remote:
        _materialize(source, client, base, token, sid, files)
    if not exists:
        _insert_row(source, sid, row)
    return PullItem(sid, "stored", seq)


def pull(
    source: Path,
    ingest_url: str,
    token: str,
    *,
    transport: UrllibTransport | None = None,
) -> PullResult:
    """Bring in every committed snapshot this archive does not already match."""
    client = transport or UrllibTransport()
    base = ingest_url.rstrip("/")
    cursor = _read_cursor(source)
    items: list[PullItem] = []
    while True:
        status, payload = client.round_trip(
            "GET",
            f"{base}/v1/changes?after={cursor}",
            None,
            _auth(token),
        )
        if status != 200:
            raise PullError("ingest rejected the pull", status=status)
        changes = payload.get("changes")
        if not isinstance(changes, list):
            raise PullError("ingest changes response has no list")
        if not changes:
            break
        for change in changes:
            seq = change.get("change_seq") if isinstance(change, dict) else None
            if isinstance(seq, bool) or not isinstance(seq, int) or seq <= cursor:
                raise PullError("change_seq went backwards")
            item = _apply_one(source, client, base, token, change)
            cursor = item.change_seq
            _write_cursor(source, cursor)
            items.append(item)
    return PullResult(tuple(items), cursor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.library_pull",
        description="Copy Dell-PLX snapshots this archive does not have.",
    )
    parser.add_argument("--ingest-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Archive data directory. Defaults to AMBER_DATA_DIR or the repo data directory.",
    )
    args = parser.parse_args(argv)
    try:
        token = read_token_file(args.token_file)
        source = args.data_dir if args.data_dir is not None else DATA_DIR
        result = pull(source, args.ingest_url, token)
    except TokenFileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except PullError as exc:
        print(exc.detail, file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"pull failed: {exc}", file=sys.stderr)
        return 1
    counts = {"stored": 0, "skipped": 0, "kept": 0}
    for item in result.items:
        counts[item.action] = counts.get(item.action, 0) + 1
        print(f"{item.action} {item.sid} change_seq={item.change_seq}")
    print(
        f"pull {counts['stored']} stored, {counts['skipped']} skipped, "
        f"{counts['kept']} kept, cursor {result.cursor}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
