"""SQLite connections close when their ``with`` block ends."""

from __future__ import annotations

import sqlite3

import pytest

from app import db


def test_with_connect_commits_and_then_closes(tmp_data):
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO snapshots (id, url, url_normalized, created_at, status)
            VALUES (?, ?, ?, ?, 'complete')
            """,
            (
                "abcde",
                "http://93.184.216.34/a",
                "http://93.184.216.34/a",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    stored = db.get_snapshot("abcde")
    assert stored is not None
    assert stored["url"] == "http://93.184.216.34/a"


def test_with_connect_rolls_back_and_closes_on_error(tmp_data):
    with pytest.raises(RuntimeError, match="nope"):
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO snapshots (id, url, url_normalized, created_at, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    "zzzzz",
                    "http://93.184.216.34/z",
                    "http://93.184.216.34/z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            raise RuntimeError("nope")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert db.get_snapshot("zzzzz") is None
