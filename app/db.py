from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DB_PATH, ID_ALPHABET, ID_LENGTH, SNAPS_DIR, DATA_DIR
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


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(SCHEMA)
        _ensure_paywalled_column(db)
        db.commit()


def _as_snap(row: sqlite3.Row) -> dict:
    snap = dict(row)
    flag = snap.get("paywalled")
    if flag is None:
        snap["paywalled"] = article_is_paywalled(snap.get("word_count"))
    else:
        snap["paywalled"] = bool(flag)
    return snap


def _new_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


def allocate_id() -> str:
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
    with connect() as db:
        row = db.execute("SELECT * FROM snapshots WHERE id = ?", (sid,)).fetchone()
    return _as_snap(row) if row else None


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


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
