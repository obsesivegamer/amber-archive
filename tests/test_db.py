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


def test_connect_closes_half_built_connection_on_setup_error(tmp_data, monkeypatch):
    opened = []

    class Recording(db._ClosingConnection):
        def execute(self, sql, *args):
            if "foreign_keys" in sql:
                raise sqlite3.OperationalError("boom")
            return super().execute(sql, *args)

    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        conn = real_connect(*args, **{**kwargs, "factory": Recording})
        opened.append(conn)
        return conn

    monkeypatch.setattr(db.sqlite3, "connect", spy)
    with pytest.raises(sqlite3.OperationalError, match="boom"):
        db.connect()
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_load_snapshots_handles_more_ids_than_the_variable_limit(tmp_data):
    total = 2500
    with db.connect() as conn:
        conn.executemany(
            """
            INSERT INTO snapshots (id, url, url_normalized, created_at, status)
            VALUES (?, ?, ?, '2026-01-01T00:00:00Z', 'complete')
            """,
            [(f"id{n}", f"http://93.184.216.34/{n}", f"http://93.184.216.34/{n}") for n in range(total)],
        )
    ids = [f"id{n}" for n in range(total)] + ["missing", "", "id0"]
    with db.connect() as conn:
        # Older SQLite builds cap bound variables at 999; force that here.
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        found = db.load_snapshots(conn, ids)
    assert len(found) == total
    assert found["id2499"]["url"] == "http://93.184.216.34/2499"
