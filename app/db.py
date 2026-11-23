from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from . import config
from .config import DATA_DIR, DB_PATH, DELL_ID_PREFIX, ID_ALPHABET, ID_LENGTH, SNAPS_DIR
from .extract import PAYWALL_WORD_LIMIT, article_is_paywalled

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    url_normalized TEXT NOT NULL,
    final_url TEXT,
    title TEXT,
    site_name TEXT,
    author TEXT,
    published_at TEXT,
    description TEXT,
    created_at TEXT NOT NULL,
    http_status INTEGER,
    word_count INTEGER,
    paywalled INTEGER,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_snaps_url ON snapshots(url_normalized);
CREATE INDEX IF NOT EXISTS idx_snaps_created ON snapshots(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_snaps_status ON snapshots(status);
"""


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back, then close.

    ``sqlite3.Connection.__exit__`` ends the transaction and leaves the
    connection open. ``with connect()`` would otherwise leak a file handle
    on every use, until the process hits the open-file limit.

    Callers must not use ``with conn:`` as a transaction scope on a
    connection they keep using afterwards: leaving the block closes it.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def readonly_library() -> bool:
    """The open database is the shared library and this process must not write it."""
    if not config.ingest_library():
        return False
    try:
        return Path(DB_PATH).resolve() == config.configured_library_db()
    except OSError:
        return False


def paywalled_bool(flag: object, word_count: object) -> bool:
    """The boolean ingest requires. A missing flag uses the word-count rule."""
    if flag is None:
        if isinstance(word_count, bool) or not isinstance(word_count, int):
            count = None
        else:
            count = word_count
        return article_is_paywalled(count)
    return bool(flag)


def connect() -> sqlite3.Connection:
    """Open the archive database.

    Unlike a plain ``sqlite3.Connection``, ``with connect() as db:`` commits
    or rolls back and then *closes* the connection when the block ends. Do
    not reuse ``db`` after the block, and copy rows out before leaving it.
    A read opens a new connection; this process does not keep one.
    """
    if readonly_library():
        return _connect_library_ro()
    return _connect_readwrite(DB_PATH)


def _connect_readwrite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, factory=_ClosingConnection)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


def _connect_library_ro() -> sqlite3.Connection:
    uri = "file:{}?mode=ro".format(quote(str(DB_PATH), safe="/:"))
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, factory=_ClosingConnection)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


# 8 characters, not the 5-character snapshot id, so a candidate route cannot
# collide with a snapshot route. Same alphabet as snapshot ids.
GIFT_CANDIDATE_ID_LENGTH = 8

_CANDIDATE_ADD_COLUMNS = (
    ("unlock_url", "TEXT"),
    ("canonical_url", "TEXT"),
    ("match_key", "TEXT"),
    ("publisher", "TEXT"),
    ("title", "TEXT"),
    ("source_url", "TEXT"),
    ("source_author", "TEXT"),
    ("found_at", "TEXT"),
    ("expires_at", "TEXT"),
    ("expires_estimated", "INTEGER"),
    ("dismissed_at", "TEXT"),
    ("clicked_at", "TEXT"),
    ("snapshot_id", "TEXT"),
    ("created_at", "TEXT"),
)


_RECAPTURE_ADD_COLUMNS = (
    ("unlock_url", "TEXT"),
    ("recapture_state", "TEXT"),
    ("recapture_reason", "TEXT"),
    ("recapture_attempts", "INTEGER"),
    ("recapture_next_at", "TEXT"),
    ("recapture_owner", "TEXT"),
)


def _ensure_recapture_columns(db: sqlite3.Connection) -> None:
    """Optional recapture fields. Existing rows stay NULL and keep their files."""
    cols = {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}
    for name, decl in _RECAPTURE_ADD_COLUMNS:
        if name not in cols:
            db.execute(f"ALTER TABLE snapshots ADD COLUMN {name} {decl}")
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_snaps_recapture
        ON snapshots(recapture_state, recapture_next_at)
        """
    )


def _ensure_paywalled_column(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}
    if "paywalled" not in cols:
        db.execute("ALTER TABLE snapshots ADD COLUMN paywalled INTEGER")
    db.execute(
        """
        UPDATE snapshots
        SET paywalled = CASE
            WHEN word_count IS NOT NULL AND word_count < ? THEN 1
            ELSE 0
        END
        WHERE status = 'complete' AND paywalled IS NULL
        """,
        (PAYWALL_WORD_LIMIT,),
    )


def _ensure_candidates_table(db: sqlite3.Connection) -> None:
    """Add the gift-candidate queue. Safe to run on every startup and from the CLI.

    Same approach as ``_ensure_paywalled_column``: create the table when it is
    missing, then add any column an older file does not have yet. There is no
    ``status`` column. Live, saved, expired and dismissed are derived on read.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS candidates (
            id TEXT PRIMARY KEY,
            unlock_url TEXT NOT NULL,
            canonical_url TEXT,
            match_key TEXT NOT NULL,
            publisher TEXT NOT NULL,
            title TEXT,
            source_url TEXT,
            source_author TEXT,
            found_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            expires_estimated INTEGER NOT NULL DEFAULT 1,
            dismissed_at TEXT,
            clicked_at TEXT,
            snapshot_id TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    cols = {row[1] for row in db.execute("PRAGMA table_info(candidates)")}
    for name, decl in _CANDIDATE_ADD_COLUMNS:
        if name not in cols:
            db.execute(f"ALTER TABLE candidates ADD COLUMN {name} {decl}")
    _ensure_candidates_match_index(db)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidates_expires ON candidates(expires_at)"
    )


def _index_sql(db: sqlite3.Connection, name: str) -> str:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    ).fetchone()
    if row is None or not row["sql"]:
        return ""
    return row["sql"]


def _blank(value: object) -> bool:
    return value is None or value == ""


def _merge_candidate_group(rows: list[sqlite3.Row]) -> tuple[sqlite3.Row, dict]:
    """Fold duplicate rows into the newest one's id.

    Blank fields take a non-null value from an older copy. A click and a
    dismissal keep the earliest timestamp, a snapshot id keeps any non-null
    value, and the expiry keeps the later deadline (with that row's
    estimated flag).
    """
    survivor = max(rows, key=lambda row: row["rowid"])
    merged = {key: survivor[key] for key in survivor.keys() if key != "rowid"}

    def prefer_non_null(field: str) -> None:
        if not _blank(merged.get(field)):
            return
        for row in rows:
            if not _blank(row[field]):
                merged[field] = row[field]
                return

    for field in (
        "unlock_url",
        "canonical_url",
        "publisher",
        "title",
        "source_url",
        "source_author",
        "found_at",
        "created_at",
    ):
        prefer_non_null(field)

    for field in ("clicked_at", "dismissed_at"):
        stamps = [row[field] for row in rows if not _blank(row[field])]
        if stamps:
            merged[field] = min(stamps)

    if _blank(merged.get("snapshot_id")):
        for row in rows:
            if not _blank(row["snapshot_id"]):
                merged["snapshot_id"] = row["snapshot_id"]
                break

    best_expires = None
    best_estimated = None
    for row in rows:
        expires = row["expires_at"]
        if _blank(expires):
            continue
        estimated = row["expires_estimated"]
        if best_expires is None or expires > best_expires:
            best_expires = expires
            best_estimated = estimated
        elif expires == best_expires and not estimated and best_estimated:
            best_estimated = estimated
    if best_expires is not None:
        merged["expires_at"] = best_expires
        if best_estimated is not None:
            merged["expires_estimated"] = 1 if best_estimated else 0
    return survivor, merged


_MERGED_CANDIDATE_COLUMNS = (
    "unlock_url",
    "canonical_url",
    "publisher",
    "title",
    "source_url",
    "source_author",
    "found_at",
    "expires_at",
    "expires_estimated",
    "dismissed_at",
    "clicked_at",
    "snapshot_id",
    "created_at",
)


def _merge_duplicate_candidates(db: sqlite3.Connection) -> None:
    """One row per match key, keeping every non-null fact from the copies."""
    keys = db.execute(
        """
        SELECT match_key FROM candidates
        WHERE match_key IS NOT NULL
        GROUP BY match_key
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for (match_key,) in keys:
        rows = db.execute(
            "SELECT rowid, * FROM candidates WHERE match_key = ? ORDER BY rowid",
            (match_key,),
        ).fetchall()
        if len(rows) < 2:
            continue
        survivor, merged = _merge_candidate_group(rows)
        assignments = ", ".join(f"{name} = ?" for name in _MERGED_CANDIDATE_COLUMNS)
        values = [merged[name] for name in _MERGED_CANDIDATE_COLUMNS]
        values.append(survivor["id"])
        db.execute(
            f"UPDATE candidates SET {assignments} WHERE id = ?",
            values,
        )
        drop_ids = [row["id"] for row in rows if row["id"] != survivor["id"]]
        placeholders = ", ".join("?" for _ in drop_ids)
        db.execute(
            f"DELETE FROM candidates WHERE id IN ({placeholders})",
            drop_ids,
        )


def _ensure_candidates_match_index(db: sqlite3.Connection) -> None:
    """One row per match key, including expired and dismissed rows.

    A repeat find refreshes that row instead of inserting another live one.
    The first version of this table used a non-unique index. Upgrade it once,
    under a write lock, and do not drop it again once it is unique. Duplicate
    rows are merged before the unique index is built.
    """
    if "UNIQUE" in _index_sql(db, "idx_candidates_match_key").upper():
        return
    # The caller (init_db) may already be inside the implicit transaction
    # Python opens before DDL. Commit that, then take the write lock so two
    # startups cannot both rebuild this index.
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        if "UNIQUE" in _index_sql(db, "idx_candidates_match_key").upper():
            db.commit()
            return
        _merge_duplicate_candidates(db)
        if _index_sql(db, "idx_candidates_match_key"):
            db.execute("DROP INDEX idx_candidates_match_key")
        db.execute(
            "CREATE UNIQUE INDEX idx_candidates_match_key ON candidates(match_key)"
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


def init_db() -> None:
    if readonly_library():
        _init_app_state()
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(SCHEMA)
        _ensure_paywalled_column(db)
        _ensure_recapture_columns(db)
        _ensure_candidates_table(db)
        db.commit()


_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    url_normalized TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    title TEXT,
    final_url TEXT,
    snapshot_id TEXT,
    created_at TEXT NOT NULL
);
"""

_JOB_FIELDS = {"status", "error", "title", "final_url", "snapshot_id"}


def _outside_library(path: Path) -> None:
    library = Path(DATA_DIR).resolve()
    if path == library or path == library / "amber.sqlite3" or library in path.parents:
        raise RuntimeError(f"{path} must be outside the library directory")


def _state_file() -> Path:
    path = config.app_state_path()
    _outside_library(path)
    return path


def scratch_dir() -> Path:
    path = config.scratch_path()
    library = Path(DATA_DIR).resolve()
    if path == library or library in path.parents or path in library.parents:
        raise RuntimeError("scratch directory must be separate from the library")
    state = _state_file()
    if state == path or path in state.parents or state.is_relative_to(path):
        raise RuntimeError("AMBER_APP_STATE must not live inside the scratch directory")
    return path


def _init_app_state() -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_JOBS_DDL)
        conn.commit()
    finally:
        conn.close()


def _state_conn() -> sqlite3.Connection:
    return sqlite3.connect(_state_file(), check_same_thread=False, factory=_ClosingConnection)


def prepare_scratch() -> None:
    root = scratch_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "snaps").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "amber.sqlite3")
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def sweep_scratch() -> None:
    """Drop unpublished scratch snapshots. The library is not in this directory."""
    from .library_import import wipe_scratch_tree

    root = scratch_dir()
    if root.exists():
        wipe_scratch_tree(root)


def _scratch_conn() -> sqlite3.Connection:
    prepare_scratch()
    return _connect_readwrite(scratch_dir() / "amber.sqlite3")


def scratch_contains(sid: str) -> bool:
    root = scratch_dir()
    if (root / "snaps" / sid).exists():
        return True
    db_path = root / "amber.sqlite3"
    if not db_path.is_file():
        return False
    conn = sqlite3.connect(db_path)
    try:
        try:
            row = conn.execute("SELECT 1 FROM snapshots WHERE id = ?", (sid,)).fetchone()
        except sqlite3.Error:
            return False
        return row is not None
    finally:
        conn.close()


def begin_capture(sid: str, url: str, url_normalized: str) -> None:
    if not readonly_library():
        insert_snapshot(sid, url, url_normalized)
        return
    prepare_scratch()
    with _state_conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, url, url_normalized, status, error, title, final_url,
                snapshot_id, created_at
            ) VALUES (?, ?, ?, 'pending', NULL, NULL, NULL, ?, ?)
            """,
            (sid, url, url_normalized, sid, now_iso()),
        )
        conn.commit()
    with _scratch_conn() as conn:
        conn.execute(
            """
            INSERT INTO snapshots (id, url, url_normalized, created_at, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (sid, url, url_normalized, now_iso()),
        )
        conn.commit()


def mark_capture(sid: str, **fields: Any) -> None:
    if not readonly_library():
        update_snapshot(sid, **fields)
        return
    if fields:
        cols = ", ".join(f"{name} = ?" for name in fields)
        with _scratch_conn() as conn:
            conn.execute(
                f"UPDATE snapshots SET {cols} WHERE id = ?",
                [*fields.values(), sid],
            )
            conn.commit()
    job_fields = {name: value for name, value in fields.items() if name in _JOB_FIELDS}
    if job_fields.get("status") == "complete":
        job_fields.pop("status")
    if job_fields:
        update_job(sid, **job_fields)


def update_job(sid: str, **fields: Any) -> None:
    fields = {name: value for name, value in fields.items() if name in _JOB_FIELDS}
    if not fields:
        return
    cols = ", ".join(f"{name} = ?" for name in fields)
    with _state_conn() as conn:
        conn.execute(
            f"UPDATE jobs SET {cols} WHERE id = ?",
            [*fields.values(), sid],
        )
        conn.commit()


def finish_job(job_id: str, snapshot_id: str) -> None:
    update_job(job_id, status="complete", snapshot_id=snapshot_id)


def capture_folder(sid: str) -> Path:
    if readonly_library():
        return scratch_dir() / "snaps" / sid
    return snap_dir(sid)


def discard_scratch(sid: str) -> None:
    from .library_contract import ID_RE

    if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
        return
    root = scratch_dir().resolve()
    folder = (root / "snaps" / sid).resolve()
    if folder.parent != (root / "snaps").resolve():
        return
    if folder.is_symlink():
        folder.unlink()
    elif folder.exists():
        shutil.rmtree(folder)
    db_path = root / "amber.sqlite3"
    if not db_path.is_file():
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM snapshots WHERE id = ?", (sid,))
        conn.commit()
    finally:
        conn.close()


def fail_capture(job_id: str, sid: str, error: str) -> None:
    if not readonly_library():
        update_snapshot(sid, status="failed", error=error)
        return
    for gone in {job_id, sid}:
        discard_scratch(gone)
    update_job(job_id, status="failed", error=error)


def fill_scratch_paywalled(scratch: Path, sid: str) -> None:
    conn = sqlite3.connect(scratch / "amber.sqlite3")
    try:
        row = conn.execute(
            "SELECT paywalled, word_count FROM snapshots WHERE id = ?",
            (sid,),
        ).fetchone()
        if row is None or row[0] is not None:
            return
        flag = 1 if article_is_paywalled(row[1]) else 0
        conn.execute(
            "UPDATE snapshots SET paywalled = ? WHERE id = ? AND paywalled IS NULL",
            (flag, sid),
        )
        conn.commit()
    finally:
        conn.close()


def publish_capture(sid: str, on_id: Callable[[str], None] | None = None) -> str:
    from .library_import import commit_captured_snapshot
    from .library_push import _push_settings

    settings = _push_settings()
    if settings is None:
        raise RuntimeError("AMBER_INGEST_URL and AMBER_INGEST_TOKEN_FILE are required")
    url, token = settings
    return commit_captured_snapshot(
        DATA_DIR,
        scratch_dir(),
        sid,
        url,
        token,
        on_id=on_id,
    )


_instance_lock = None


def lock_path() -> Path:
    if readonly_library():
        return _state_file()
    return DATA_DIR / "amber.lock"


def hold_instance_lock() -> bool:
    """Take an exclusive lock for this process's life.

    False means another Amber process already owns this data directory, so
    its in-flight jobs are not ours to touch. In ingest mode the lock is the
    app-state database, not a file in the library directory.
    """
    global _instance_lock
    if _instance_lock is not None:
        return True
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False
    _instance_lock = handle
    return True


def fail_interrupted() -> int:
    """Mark rows left pending/capturing by a previous process as failed."""
    if readonly_library():
        with _state_conn() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'failed', error = 'Interrupted before it finished.'
                WHERE status IN ('pending', 'capturing', 'queued')
                """
            )
            conn.commit()
            return cur.rowcount
    with connect() as db:
        cur = db.execute(
            """
            UPDATE snapshots
            SET status = 'failed', error = 'Interrupted before it finished.'
            WHERE status IN ('pending', 'capturing')
            """
        )
        db.commit()
        return cur.rowcount


def _as_snap(row: sqlite3.Row) -> dict:
    snap = dict(row)
    flag = snap.get("paywalled")
    if flag is None:
        snap["paywalled"] = article_is_paywalled(snap.get("word_count"))
    else:
        snap["paywalled"] = bool(flag)
    return snap


def new_snapshot_id(partition: str) -> str:
    """One random snapshot id in ``mac`` or ``dell``.

    Mac ids never start with ``DELL_ID_PREFIX``. Dell ids always do. The other
    four characters still use the full archive alphabet. This does not check
    the store; callers that might collide with a legacy id have to.
    """
    if partition == "mac":
        first_alphabet = ID_ALPHABET.replace(DELL_ID_PREFIX, "")
        if not first_alphabet or DELL_ID_PREFIX not in ID_ALPHABET:
            raise RuntimeError("snapshot alphabet has no Dell prefix to reserve")
        first = secrets.choice(first_alphabet)
    elif partition == "dell":
        first = DELL_ID_PREFIX
    else:
        raise ValueError(f"unknown id partition {partition}")
    rest = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH - 1))
    return first + rest


def _new_id() -> str:
    return new_snapshot_id("mac")


def allocate_id() -> str:
    """Allocate a Mac snapshot id. Never starts with the Dell prefix.

    Imports, Playwright saves, and ``ingest_html`` all come through here.
    The id is checked against this process's own database, which on the Mac
    is the Mac archive.
    """
    with connect() as db:
        for _ in range(32):
            sid = _new_id()
            exists = db.execute("SELECT 1 FROM snapshots WHERE id = ?", (sid,)).fetchone()
            if not exists and not (SNAPS_DIR / sid).exists():
                return sid
    raise RuntimeError("Could not allocate a snapshot id.")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def insert_snapshot(sid: str, url: str, url_normalized: str) -> None:
    with connect() as db:
        db.execute(
            """
            INSERT INTO snapshots (id, url, url_normalized, created_at, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (sid, url, url_normalized, now_iso()),
        )
        db.commit()


def update_snapshot(sid: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [sid]
    with connect() as db:
        db.execute(f"UPDATE snapshots SET {cols} WHERE id = ?", values)
        db.commit()


def get_snapshot(sid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM snapshots WHERE id = ?", (sid,)).fetchone()
        if row is not None:
            return _as_snap(row)
    if readonly_library():
        return _job_snapshot(sid)
    return None


def _job_snapshot(sid: str) -> dict | None:
    path = _state_file()
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{quote(str(path), safe='/:')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (sid,)).fetchone()
        except sqlite3.Error:
            return None
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "id": row["id"],
        "url": row["url"],
        "url_normalized": row["url_normalized"],
        "final_url": row["final_url"],
        "title": row["title"],
        "site_name": None,
        "author": None,
        "published_at": None,
        "description": None,
        "created_at": row["created_at"],
        "http_status": None,
        "word_count": None,
        "paywalled": False,
        "status": row["status"],
        "error": row["error"],
    }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _has_table(conn, table):
        return False
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


# Stay well under SQLite's bound-variable limit (999 on older builds).
_SNAPSHOT_ID_CHUNK = 500


def load_snapshots(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    """Snapshots for these ids, read on the caller's connection."""
    wanted = [sid for sid in dict.fromkeys(ids) if sid]
    if not wanted:
        return {}
    found: dict[str, dict] = {}
    for start in range(0, len(wanted), _SNAPSHOT_ID_CHUNK):
        chunk = wanted[start : start + _SNAPSHOT_ID_CHUNK]
        marks = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT * FROM snapshots WHERE id IN ({marks})",
            chunk,
        ).fetchall()
        for row in rows:
            found[row["id"]] = _as_snap(row)
    return found


def find_by_url(url_normalized: str, limit: int = 50) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT * FROM snapshots
            WHERE url_normalized = ? AND status = 'complete'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (url_normalized, limit),
        ).fetchall()
        return [_as_snap(r) for r in rows]


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_candidates(
    host: str,
    path: str = "/",
    limit: int = 2000,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Snapshots of any status whose url, url_normalized or final_url could be this article.

    A superset: LIKE narrows by host and path in SQL so a large archive is not
    cut off by a row cap; the caller makes the exact comparison. Newest first,
    ties broken by insertion order.

    Pass ``conn`` to query a connection the caller already has open. The gift
    queue does that so one request does not open a connection per article.
    """
    bare = host[4:] if host.startswith("www.") else host
    host_pat = f"%{_like_escape(bare)}%"
    path_pat = f"%{_like_escape(path)}%" if len(path) > 1 else "%"
    clauses = " OR ".join(
        f"({col} LIKE ? ESCAPE '\\' AND {col} LIKE ? ESCAPE '\\')"
        for col in ("url_normalized", "url", "final_url")
    )
    sql = f"""
            SELECT * FROM snapshots
            WHERE {clauses}
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """
    params = (host_pat, path_pat) * 3 + (limit,)

    def _read(db: sqlite3.Connection) -> list[dict]:
        rows = db.execute(sql, params).fetchall()
        return [_as_snap(r) for r in rows]

    if conn is not None:
        return _read(conn)
    with connect() as db:
        return _read(db)


def search_snapshots(query: str, limit: int = 50) -> list[dict]:
    q = f"%{query.strip()}%"
    with connect() as db:
        rows = db.execute(
            """
            SELECT * FROM snapshots
            WHERE status = 'complete' AND (
                url LIKE ? OR url_normalized LIKE ? OR title LIKE ?
                OR site_name LIKE ? OR description LIKE ?
            )
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (q, q, q, q, q, limit),
        ).fetchall()
        return [_as_snap(r) for r in rows]


def recent_snapshots(limit: int = 12) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT * FROM snapshots
            WHERE status = 'complete'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_as_snap(r) for r in rows]


def complete_snapshots() -> list[dict]:
    """Every complete snapshot, oldest first."""
    with connect() as db:
        rows = db.execute(
            """
            SELECT * FROM snapshots
            WHERE status = 'complete'
            ORDER BY created_at ASC, rowid ASC
            """
        ).fetchall()
        return [_as_snap(row) for row in rows]


def recapture_snapshots() -> list[dict]:
    """Snaps waiting on a recapture, paused, or stopped."""
    with connect() as db:
        if readonly_library() and not _has_column(db, "snapshots", "recapture_state"):
            return []
        rows = db.execute(
            """
            SELECT * FROM snapshots
            WHERE recapture_state IN (
                'pending', 'running', 'gave_up',
                'needs_browser_capture', 'needs_fresh_gift'
            )
            ORDER BY created_at ASC, rowid ASC
            """
        ).fetchall()
        return [_as_snap(row) for row in rows]


def complete_since(created_after: str) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT * FROM snapshots
            WHERE status = 'complete' AND created_at >= ?
            ORDER BY created_at DESC
            """,
            (created_after,),
        ).fetchall()
        return [_as_snap(r) for r in rows]


def count_snapshots() -> int:
    with connect() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE status = 'complete'"
        ).fetchone()
        return int(row["n"] if row else 0)


def all_ids() -> list[str]:
    with connect() as db:
        rows = db.execute("SELECT id FROM snapshots").fetchall()
        return [r["id"] for r in rows]


def delete_snapshot(sid: str) -> bool:
    existed = get_snapshot(sid) is not None
    folder = snap_dir(sid)
    folder_existed = folder.exists()
    with connect() as db:
        db.execute("DELETE FROM snapshots WHERE id = ?", (sid,))
        db.commit()
    if folder_existed:
        shutil.rmtree(folder, ignore_errors=True)
    return existed or folder_existed


def snap_dir(sid: str) -> Path:
    return SNAPS_DIR / sid


def _as_candidate(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["expires_estimated"] = bool(item.get("expires_estimated"))
    return item


_CANDIDATE_UPDATABLE = {
    "unlock_url",
    "canonical_url",
    "match_key",
    "publisher",
    "title",
    "source_url",
    "source_author",
    "found_at",
    "expires_at",
    "expires_estimated",
    "dismissed_at",
    "clicked_at",
    "snapshot_id",
}


def insert_gift_candidate_in(
    conn: sqlite3.Connection,
    *,
    unlock_url: str,
    canonical_url: str | None,
    match_key: str,
    publisher: str,
    title: str | None,
    source_url: str | None,
    source_author: str | None,
    found_at: str,
    expires_at: str,
    expires_estimated: bool,
) -> dict:
    """Insert one candidate on ``conn`` without committing.

    The caller holds the write transaction so a concurrent add cannot insert
    the same match key between the existence check and this insert.
    """
    created_at = now_iso()
    estimated = 1 if expires_estimated else 0
    for _ in range(32):
        cid = "".join(secrets.choice(ID_ALPHABET) for _ in range(GIFT_CANDIDATE_ID_LENGTH))
        if conn.execute("SELECT 1 FROM candidates WHERE id = ?", (cid,)).fetchone():
            continue
        conn.execute(
            """
            INSERT INTO candidates (
                id, unlock_url, canonical_url, match_key, publisher, title,
                source_url, source_author, found_at, expires_at,
                expires_estimated, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cid,
                unlock_url,
                canonical_url,
                match_key,
                publisher,
                title,
                source_url,
                source_author,
                found_at,
                expires_at,
                estimated,
                created_at,
            ),
        )
        row = conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        return _as_candidate(row)
    raise RuntimeError("Could not allocate a candidate id.")


def insert_gift_candidate(
    *,
    unlock_url: str,
    canonical_url: str | None,
    match_key: str,
    publisher: str,
    title: str | None,
    source_url: str | None,
    source_author: str | None,
    found_at: str,
    expires_at: str,
    expires_estimated: bool,
) -> dict:
    with connect() as conn:
        stored = insert_gift_candidate_in(
            conn,
            unlock_url=unlock_url,
            canonical_url=canonical_url,
            match_key=match_key,
            publisher=publisher,
            title=title,
            source_url=source_url,
            source_author=source_author,
            found_at=found_at,
            expires_at=expires_at,
            expires_estimated=expires_estimated,
        )
        conn.commit()
        return stored


def update_gift_candidate_in(conn: sqlite3.Connection, cid: str, **fields: Any) -> None:
    """Update allowlisted columns on ``conn`` without committing."""
    unknown = set(fields) - _CANDIDATE_UPDATABLE
    if unknown:
        raise ValueError(f"Unknown candidate fields: {sorted(unknown)}")
    if not fields:
        return
    assignments: list[str] = []
    values: list[Any] = []
    for name, value in fields.items():
        if name == "expires_estimated":
            value = 1 if value else 0
        assignments.append(f"{name} = ?")
        values.append(value)
    values.append(cid)
    conn.execute(
        f"UPDATE candidates SET {', '.join(assignments)} WHERE id = ?",
        values,
    )


def update_gift_candidate(cid: str, **fields: Any) -> None:
    with connect() as conn:
        update_gift_candidate_in(conn, cid, **fields)
        conn.commit()


def get_gift_candidate(cid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        return _as_candidate(row) if row else None


def load_gift_candidates(conn: sqlite3.Connection) -> list[dict]:
    if readonly_library() and not _has_table(conn, "candidates"):
        return []
    rows = conn.execute(
        "SELECT * FROM candidates ORDER BY found_at DESC, rowid DESC"
    ).fetchall()
    return [_as_candidate(r) for r in rows]


def list_gift_candidates() -> list[dict]:
    with connect() as conn:
        return load_gift_candidates(conn)


def purge_gift_candidates(expired_before: str) -> int:
    """Delete candidates whose expires_at is strictly older than ``expired_before``."""
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM candidates WHERE expires_at < ?",
            (expired_before,),
        )
        conn.commit()
        return cur.rowcount


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
