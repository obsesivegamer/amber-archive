"""Ingest process for the Dell-PLX library.

One complete snapshot arrives as ``PUT /v1/blobs/{sha256}`` and
``POST /v1/revisions``. This process writes ``snaps/<id>/`` and the
``snapshots`` row amber-reader lists, then checkpoints the database.

The phone reader has to open that file with ``mode=ro`` and no ``immutable``
flag before ingest is started. ``immutable=1`` maps the file and does not
take a WAL lock, so a checkpoint into the live file can tear a read.
``docs/ops/dell-plx-ingest.md`` has the patch.

The commit order is the one in the library plan: stage the directory, fsync,
rename it into ``snaps/<id>``, then insert the snapshot row and ``change_seq``
in one transaction. A crash after the rename and before that transaction is
rolled back on the next startup. Startup also deletes leftover staging
directories.

This module is the writer. It does not import capture, Playwright, or the
HTML importer, and it does not serve archived HTML. Run it with
``python -m app.library_ingest``. The Mac command is ``python -m app.library_push``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .library_contract import (
    CHANGES_DDL,
    ID_RE,
    MAX_BLOB_BYTES,
    MAX_FILES,
    MAX_REVISION_BYTES,
    REVISIONS_DDL,
    SHA256_RE,
    SNAPSHOTS_DDL,
    SNAPSHOT_COLUMNS,
    TokenFileError,
    read_token_file,
    sha256_hex,
    snapshot_relpath,
    tokens_equal,
)

log = logging.getLogger("amber.library_ingest")

_LOCK = threading.Lock()
_CHECKPOINT_TRIES = 5
_CHECKPOINT_PAUSE_S = 0.05
_OPTIONAL_STRINGS = (
    "final_url",
    "title",
    "site_name",
    "author",
    "published_at",
    "description",
    "error",
)
_OPTIONAL_INTS = ("http_status", "word_count")
_TOP_LEVEL = {"id", "revision", "op", "match_key", "row", "files"}


class IngestError(Exception):
    def __init__(self, status: int, detail: str, **extra: object):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra


@dataclass(frozen=True)
class RevisionDoc:
    sid: str
    revision: int
    op: str
    match_key: str
    row: dict
    files: list[dict]
    row_json: str
    files_json: str


@dataclass(frozen=True)
class CommitResult:
    status: int
    change_seq: int
    sid: str
    revision: int


def _dirs(data_dir: Path) -> tuple[Path, Path, Path, Path]:
    return (
        data_dir / "snaps",
        data_dir / "ingest-staging",
        data_dir / "ingest-previous",
        data_dir / "ingest-journal",
    )


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir(path: Path) -> None:
    """Create ``path`` and fsync it and its parent so the directory entry is durable."""
    path.mkdir(parents=True, exist_ok=True)
    _fsync_dir(path)
    parent = path.parent
    if parent.exists():
        _fsync_dir(parent)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canon(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def blob_path(data_dir: Path, sha: str) -> Path:
    return data_dir / "blobs" / sha[:2] / sha


def _connect(data_dir: Path) -> sqlite3.Connection:
    """Open the library database. Callers close it on every path, including errors.

    Setup failures close the handle here. A PRAGMA error otherwise leaves the
    descriptor open, and ingest is the process that keeps accepting requests.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "amber.sqlite3")
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SNAPSHOTS_DDL)
    conn.executescript(REVISIONS_DDL)
    conn.executescript(CHANGES_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
    if "paywalled" not in columns:
        conn.execute("ALTER TABLE snapshots ADD COLUMN paywalled INTEGER")


def _checkpoint(conn: sqlite3.Connection) -> None:
    """Copy the WAL into the main file and truncate it.

    ``PRAGMA wal_checkpoint`` returns ``(busy, log, checkpointed)``. A reader
    holding a WAL frame sets ``busy`` above zero and leaves ``log`` ahead of
    ``checkpointed``. Retry, then fail the request so the caller does not
    treat a partial checkpoint as done.
    """
    last = (1, 0, 0)
    for attempt in range(_CHECKPOINT_TRIES):
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None:
            last = (1, 0, 0)
        else:
            last = (int(row[0]), int(row[1]), int(row[2]))
        busy, log_frames, checkpointed = last
        if busy == 0 and log_frames == checkpointed:
            return
        if attempt + 1 < _CHECKPOINT_TRIES:
            time.sleep(_CHECKPOINT_PAUSE_S)
    log.warning(
        "wal checkpoint incomplete busy=%s log=%s checkpointed=%s",
        last[0],
        last[1],
        last[2],
    )
    raise IngestError(503, "database checkpoint is busy")


def _checkpoint_db(data_dir: Path) -> None:
    if not (data_dir / "amber.sqlite3").is_file():
        return
    conn = _connect(data_dir)
    try:
        _ensure_schema(conn)
        conn.commit()
        _checkpoint(conn)
    finally:
        conn.close()


def _stored_revision(conn: sqlite3.Connection, sid: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT revision, op, match_key, change_seq, files_json, row_json
        FROM library_revisions
        WHERE id = ?
        """,
        (sid,),
    ).fetchone()


def _revision_number(data_dir: Path, sid: str) -> int:
    if not (data_dir / "amber.sqlite3").is_file():
        return 0
    conn = _connect(data_dir)
    try:
        _ensure_schema(conn)
        stored = _stored_revision(conn, sid)
        return int(stored["revision"]) if stored else 0
    finally:
        conn.close()


def _rm(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _recover_unlocked(data_dir: Path) -> None:
    """Put back the previous snapshot when a rename never got its SQL commit."""
    snaps, staging_root, previous_root, journal_root = _dirs(data_dir)
    _recover_journals(data_dir, snaps, staging_root, previous_root, journal_root)
    if staging_root.is_dir():
        for child in list(staging_root.iterdir()):
            _rm(child)
    blobs = data_dir / "blobs"
    if blobs.is_dir():
        for tmp in blobs.rglob("*.tmp"):
            tmp.unlink(missing_ok=True)
    _checkpoint_db(data_dir)


def _recover_journals(
    data_dir: Path,
    snaps: Path,
    staging_root: Path,
    previous_root: Path,
    journal_root: Path,
) -> None:
    if not journal_root.is_dir():
        return
    for path in sorted(journal_root.glob("*.json")):
        _recover_journal(data_dir, path, snaps, staging_root, previous_root)
    for leftover in list(journal_root.iterdir()):
        if leftover.name.endswith(".tmp"):
            leftover.unlink(missing_ok=True)


def _recover_journal(
    data_dir: Path,
    path: Path,
    snaps: Path,
    staging_root: Path,
    previous_root: Path,
) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return
    if not isinstance(payload, dict):
        path.unlink(missing_ok=True)
        return
    sid = payload.get("id")
    revision = payload.get("revision")
    if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
        path.unlink(missing_ok=True)
        return
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        path.unlink(missing_ok=True)
        return
    staging_name = payload.get("staging")
    staging = None
    if (
        isinstance(staging_name, str)
        and staging_name not in ("", ".", "..")
        and "/" not in staging_name
        and "\\" not in staging_name
    ):
        staging = staging_root / staging_name
    final = snaps / sid
    backup = previous_root / sid
    if _revision_number(data_dir, sid) >= revision:
        if backup.exists() or backup.is_symlink():
            _rm(backup)
        if staging is not None and (staging.exists() or staging.is_symlink()):
            _rm(staging)
        path.unlink(missing_ok=True)
        return
    log.warning("rolling back unpublished snapshot %s revision %s", sid, revision)
    had_previous = bool(payload.get("had_previous"))
    if backup.exists() and not backup.is_symlink():
        if final.is_symlink() or final.exists():
            _rm(final)
        os.rename(backup, final)
    elif staging is not None and (staging.exists() or staging.is_symlink()):
        _rm(staging)
    elif (final.exists() or final.is_symlink()) and not had_previous:
        _rm(final)
    if staging is not None and (staging.exists() or staging.is_symlink()):
        _rm(staging)
    path.unlink(missing_ok=True)
    if snaps.is_dir():
        _fsync_dir(snaps)


def prepare_store(data_dir: Path) -> None:
    with _LOCK:
        _prepare_unlocked(data_dir)


def _prepare_unlocked(data_dir: Path) -> None:
    _mkdir(data_dir)
    _mkdir(data_dir / "snaps")
    conn = _connect(data_dir)
    try:
        _ensure_schema(conn)
        conn.commit()
        _checkpoint(conn)
    finally:
        conn.close()
    _recover_unlocked(data_dir)


def _parse(payload: dict) -> RevisionDoc:
    unknown = set(payload) - _TOP_LEVEL
    if unknown:
        raise IngestError(400, f"unknown field {sorted(unknown)[0]}")
    sid = payload.get("id")
    if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
        raise IngestError(400, "id must be a 5-character snapshot id")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise IngestError(400, "revision must be a positive integer")
    if payload.get("op") != "upsert":
        raise IngestError(400, "op must be upsert")
    match_key = payload.get("match_key", "")
    if match_key is None:
        match_key = ""
    if not isinstance(match_key, str):
        raise IngestError(400, "match_key must be a string")
    row = _normalize_row(payload.get("row"))
    files = _normalize_files(payload.get("files"))
    return RevisionDoc(
        sid=sid,
        revision=revision,
        op="upsert",
        match_key=match_key,
        row=row,
        files=files,
        row_json=_canon(row),
        files_json=_canon(files),
    )


def _normalize_row(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise IngestError(400, "row must be an object")
    allowed = set(SNAPSHOT_COLUMNS) - {"id"}
    extra = set(raw) - allowed
    if extra:
        raise IngestError(400, f"unknown row field {sorted(extra)[0]}")
    if raw.get("status") != "complete":
        raise IngestError(400, "status must be complete")
    for key in ("url", "url_normalized", "created_at"):
        value = raw.get(key)
        if not isinstance(value, str) or value == "":
            raise IngestError(400, f"{key} is required")
    if not isinstance(raw.get("paywalled"), bool):
        raise IngestError(400, "paywalled must be a boolean")
    out: dict = {
        "url": raw["url"],
        "url_normalized": raw["url_normalized"],
        "created_at": raw["created_at"],
        "status": "complete",
        "paywalled": 1 if raw["paywalled"] else 0,
    }
    for key in _OPTIONAL_STRINGS:
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise IngestError(400, f"{key} must be a string or null")
        out[key] = value
    for key in _OPTIONAL_INTS:
        value = raw.get(key)
        if value is None:
            out[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise IngestError(400, f"{key} must be an integer or null")
        out[key] = value
    return out


def _normalize_files(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise IngestError(400, "files must be a list")
    if len(raw) > MAX_FILES:
        raise IngestError(400, "too many files")
    seen: set[str] = set()
    files: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise IngestError(400, "file entry must be an object")
        path = snapshot_relpath(item.get("path"))
        if path is None:
            raise IngestError(400, "path is not inside the snapshot")
        if path in seen:
            raise IngestError(400, f"duplicate path {path}")
        seen.add(path)
        sha = item.get("sha256")
        if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha.lower()):
            raise IngestError(400, "sha256 must be 64 hex characters")
        sha = sha.lower()
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_BLOB_BYTES:
            raise IngestError(400, "size is invalid")
        files.append({"path": path, "sha256": sha, "size": size})
    if "meta.json" not in seen:
        raise IngestError(400, "files must include meta.json")
    files.sort(key=lambda item: item["path"])
    return files


def _same(stored: sqlite3.Row, doc: RevisionDoc) -> bool:
    return (
        stored["op"] == doc.op
        and (stored["match_key"] or "") == doc.match_key
        and stored["files_json"] == doc.files_json
        and stored["row_json"] == doc.row_json
    )


def _copy_blobs(data_dir: Path, staging: Path, doc: RevisionDoc) -> None:
    for item in doc.files:
        src = blob_path(data_dir, item["sha256"])
        if not src.is_file():
            raise IngestError(400, f"blob not stored: {item['sha256']}")
        digest = _hash_file(src)
        if digest != item["sha256"]:
            raise IngestError(400, f"sha256 does not match stored blob {item['sha256']}")
        if src.stat().st_size != item["size"]:
            raise IngestError(400, f"size does not match blob {item['sha256']}")
        dest = staging / item["path"]
        _mkdir(dest.parent)
        try:
            os.link(src, dest)
        except OSError:
            with src.open("rb") as inp, dest.open("wb") as out:
                shutil.copyfileobj(inp, out, length=1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
        _fsync_dir(dest.parent)


def _fsync_tree(root: Path) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            fd = os.open(Path(dirpath) / name, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_dir(Path(dirpath))


def _stage_and_swap(data_dir: Path, doc: RevisionDoc) -> None:
    """Stage, fsync, then rename into ``snaps/<id>``.

    When the id already exists the previous directory is renamed aside and
    the staging directory is renamed into its place. Those are two renames,
    so a crash between them leaves the id missing. The journal is written
    first, and startup moves ``ingest-previous/<id>`` back. ``renameat2``
    ``RENAME_EXCHANGE`` would make that swap one syscall. It is not used:
    the two-step order is the one recovery already understands, and a
    missing id for that window is the failure mode the journal covers.
    """
    snaps, staging_root, previous_root, journal_root = _dirs(data_dir)
    _mkdir(snaps)
    _mkdir(staging_root)
    final = snaps / doc.sid
    if final.is_symlink():
        raise IngestError(400, "snapshot path is a symlink")
    if final.exists() and not final.is_dir():
        raise IngestError(400, "snapshot path is not a directory")
    had_previous = final.exists()
    staging_name = f"{doc.sid}-{doc.revision}-{secrets.token_hex(4)}"
    staging = staging_root / staging_name
    _mkdir(staging)
    _copy_blobs(data_dir, staging, doc)
    _fsync_tree(staging)
    _write_journal(
        journal_root,
        doc.sid,
        {
            "id": doc.sid,
            "revision": doc.revision,
            "had_previous": had_previous,
            "staging": staging_name,
        },
    )
    backup = previous_root / doc.sid
    if had_previous:
        _mkdir(previous_root)
        if backup.exists() or backup.is_symlink():
            _rm(backup)
        os.rename(final, backup)
        _fsync_dir(previous_root)
        _fsync_dir(snaps)
    os.rename(staging, final)
    _fsync_dir(snaps)


def _write_journal(journal_root: Path, sid: str, payload: dict) -> None:
    _mkdir(journal_root)
    target = journal_root / f"{sid}.json"
    tmp = journal_root / f".{sid}.{secrets.token_hex(4)}.tmp"
    data = _canon(payload).encode("utf-8")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    _fsync_dir(journal_root)


def _apply_sql(data_dir: Path, doc: RevisionDoc) -> int:
    conn = _connect(data_dir)
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        stored = _stored_revision(conn, doc.sid)
        stored_rev = int(stored["revision"]) if stored else 0
        if doc.revision != stored_rev + 1:
            conn.rollback()
            raise IngestError(409, "revision is not stored + 1", revision=stored_rev)
        seq = int(
            conn.execute(
                "SELECT COALESCE(MAX(change_seq), 0) + 1 AS n FROM library_changes"
            ).fetchone()["n"]
        )
        columns = SNAPSHOT_COLUMNS
        values = [doc.sid if column == "id" else doc.row[column] for column in columns]
        assignments = ", ".join(
            f"{column} = excluded.{column}" for column in columns if column != "id"
        )
        conn.execute(
            f"""
            INSERT INTO snapshots ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(id) DO UPDATE SET {assignments}
            """,
            values,
        )
        conn.execute(
            """
            INSERT INTO library_revisions (
                id, revision, op, match_key, change_seq, files_json, row_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                revision = excluded.revision,
                op = excluded.op,
                match_key = excluded.match_key,
                change_seq = excluded.change_seq,
                files_json = excluded.files_json,
                row_json = excluded.row_json
            """,
            (
                doc.sid,
                doc.revision,
                doc.op,
                doc.match_key,
                seq,
                doc.files_json,
                doc.row_json,
            ),
        )
        wire_row = dict(doc.row)
        wire_row["paywalled"] = bool(doc.row["paywalled"])
        body = {
            "id": doc.sid,
            "revision": doc.revision,
            "op": doc.op,
            "match_key": doc.match_key,
            "row": wire_row,
            "files": doc.files,
            "change_seq": seq,
        }
        conn.execute(
            """
            INSERT INTO library_changes (change_seq, id, revision, op, body_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (seq, doc.sid, doc.revision, doc.op, _canon(body)),
        )
        conn.commit()
        _checkpoint(conn)
        return seq
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _discard_backup(data_dir: Path, sid: str) -> None:
    _rm(data_dir / "ingest-previous" / sid)
    journal = data_dir / "ingest-journal" / f"{sid}.json"
    journal.unlink(missing_ok=True)
    previous = data_dir / "ingest-previous"
    journal_root = data_dir / "ingest-journal"
    if previous.is_dir():
        _fsync_dir(previous)
    if journal_root.is_dir():
        _fsync_dir(journal_root)


def _release_blobs(data_dir: Path, doc: RevisionDoc) -> None:
    """Drop blob names that the snapshot directory now holds."""
    seen: set[str] = set()
    for item in doc.files:
        sha = item["sha256"]
        if sha in seen:
            continue
        seen.add(sha)
        blob_path(data_dir, sha).unlink(missing_ok=True)


def _rollback_publish(data_dir: Path) -> None:
    """Undo journals from a commit that raised. Startup runs the wider scan."""
    snaps, staging_root, previous_root, journal_root = _dirs(data_dir)
    _recover_journals(data_dir, snaps, staging_root, previous_root, journal_root)
    if staging_root.is_dir():
        for child in list(staging_root.iterdir()):
            _rm(child)


def commit_document(data_dir: Path, payload: dict) -> CommitResult:
    with _LOCK:
        doc = _parse(payload)
        conn = _connect(data_dir)
        try:
            _ensure_schema(conn)
            stored = _stored_revision(conn, doc.sid)
        finally:
            conn.close()
        stored_rev = int(stored["revision"]) if stored else 0
        if stored and doc.revision == stored_rev:
            if _same(stored, doc):
                _release_blobs(data_dir, doc)
                return CommitResult(200, int(stored["change_seq"]), doc.sid, doc.revision)
            raise IngestError(
                409,
                "stored revision does not match this body",
                revision=stored_rev,
            )
        if doc.revision != stored_rev + 1:
            raise IngestError(409, "revision is not stored + 1", revision=stored_rev)
        try:
            _stage_and_swap(data_dir, doc)
            seq = _apply_sql(data_dir, doc)
        except Exception:
            _rollback_publish(data_dir)
            raise
        _discard_backup(data_dir, doc.sid)
        _release_blobs(data_dir, doc)
        return CommitResult(201, seq, doc.sid, doc.revision)


def install_blob(data_dir: Path, sha: str, body: bytes) -> bool:
    """Store ``body`` under ``sha``. Return True when this call wrote it."""
    with _LOCK:
        dest = blob_path(data_dir, sha)
        if dest.is_file() and dest.stat().st_size == len(body) and _hash_file(dest) == sha:
            return False
        _mkdir(dest.parent)
        tmp = dest.parent / f".{sha}.{secrets.token_hex(4)}.tmp"
        try:
            with tmp.open("wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, dest)
            _fsync_dir(dest.parent)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return True


def _error(exc: IngestError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
    return JSONResponse(
        status_code=exc.status,
        content={"detail": exc.detail, **exc.extra},
        headers=headers,
    )


def _authorize(request: Request) -> None:
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or presented == "" or presented != presented.strip():
        raise IngestError(401, "unauthorized")
    try:
        ok = tokens_equal(presented, request.app.state.token)
    except UnicodeError:
        ok = False
    if not ok:
        raise IngestError(401, "unauthorized")


def _body_bytes(request: Request) -> bytes:
    """Read the body from a sync endpoint running in the AnyIO worker thread."""
    import anyio

    return anyio.from_thread.run(request.body)


def _content_length(request: Request, cap: int, cap_detail: str) -> int:
    raw = request.headers.get("content-length")
    if raw is None:
        raise IngestError(400, "Content-Length is required")
    try:
        value = int(raw)
    except ValueError:
        raise IngestError(400, "Content-Length is required") from None
    if value < 0:
        raise IngestError(400, "Content-Length is required")
    if value > cap:
        raise IngestError(413, cap_detail)
    return value


def _resolve(data_dir: Path | None, token_file: Path | None) -> tuple[Path, str]:
    if data_dir is None:
        raw = os.environ.get("AMBER_DATA_DIR", "").strip()
        if not raw:
            raise SystemExit("amber ingest: set AMBER_DATA_DIR")
        data_dir = Path(raw)
    if token_file is None:
        raw_token = os.environ.get("AMBER_INGEST_TOKEN_FILE", "").strip()
        if not raw_token:
            raise SystemExit("amber ingest: set AMBER_INGEST_TOKEN_FILE")
        token_file = Path(raw_token)
    try:
        token = read_token_file(token_file)
    except TokenFileError as exc:
        raise SystemExit(f"amber ingest: {exc}") from exc
    return data_dir, token


def create_app(
    data_dir: Path | None = None,
    token_file: Path | None = None,
) -> FastAPI:
    data_dir, token = _resolve(data_dir, token_file)
    prepare_store(data_dir)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.data_dir = data_dir
    app.state.token = token

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.put("/v1/blobs/{sha256}")
    def put_blob(sha256: str, request: Request) -> JSONResponse:
        try:
            _authorize(request)
            sha = sha256.lower()
            if not SHA256_RE.fullmatch(sha):
                raise IngestError(400, "sha256 must be 64 hex characters")
            length = _content_length(request, MAX_BLOB_BYTES, "blob exceeds 32 MiB")
            body = _body_bytes(request)
            if len(body) != length:
                raise IngestError(400, "Content-Length does not match body")
            if sha256_hex(body) != sha:
                raise IngestError(400, "sha256 does not match body")
            created = install_blob(request.app.state.data_dir, sha, body)
        except IngestError as exc:
            return _error(exc)
        return JSONResponse(
            status_code=201 if created else 200,
            content={"sha256": sha},
        )

    @app.post("/v1/revisions")
    def post_revision(request: Request) -> JSONResponse:
        try:
            _authorize(request)
            media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media != "application/json":
                raise IngestError(415, "Content-Type must be application/json")
            length = _content_length(
                request, MAX_REVISION_BYTES, "revision body exceeds 1 MiB"
            )
            raw = _body_bytes(request)
            if len(raw) != length:
                raise IngestError(400, "Content-Length does not match body")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                raise IngestError(400, "body is not JSON") from None
            if not isinstance(payload, dict):
                raise IngestError(400, "body must be a JSON object")
            result = commit_document(request.app.state.data_dir, payload)
        except IngestError as exc:
            return _error(exc)
        except Exception:
            log.exception("revision commit failed")
            return JSONResponse(status_code=500, content={"detail": "commit failed"})
        return JSONResponse(
            status_code=result.status,
            content={
                "id": result.sid,
                "revision": result.revision,
                "change_seq": result.change_seq,
            },
        )

    return app


def main() -> None:
    host = os.environ.get("AMBER_INGEST_HOST", "127.0.0.1")
    if host != "127.0.0.1":
        raise SystemExit("amber ingest: AMBER_INGEST_HOST must be 127.0.0.1")
    raw_port = os.environ.get("AMBER_INGEST_PORT", "8082")
    try:
        port = int(raw_port)
    except ValueError:
        raise SystemExit("amber ingest: AMBER_INGEST_PORT must be an integer") from None
    if port == 8081:
        raise SystemExit("amber ingest: port 8081 is the reader")
    if not 1 <= port <= 65535:
        raise SystemExit("amber ingest: AMBER_INGEST_PORT must be 1-65535")
    import uvicorn

    uvicorn.run(
        "app.library_ingest:create_app",
        factory=True,
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
