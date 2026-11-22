"""Mac snapshot ids never start with Z. Dell ids do, and skip a live Z id."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.config import DELL_ID_PREFIX, ID_ALPHABET, ID_LENGTH
from app.db import allocate_id, new_snapshot_id
from app.library_contract import ID_RE, SNAPSHOTS_DDL
from app.library_import import allocate_dell_id, release_dell_id


def test_mac_draw_excludes_the_dell_prefix(tmp_data, monkeypatch):
    seen: list[str] = []
    real = __import__("secrets").choice

    def choice(alphabet: str) -> str:
        seen.append(alphabet)
        return real(alphabet)

    monkeypatch.setattr("app.db.secrets.choice", choice)
    sid = allocate_id()
    assert seen[0] == ID_ALPHABET.replace(DELL_ID_PREFIX, "")
    assert DELL_ID_PREFIX not in seen[0]
    assert sid[0] != DELL_ID_PREFIX
    assert ID_RE.fullmatch(sid)


def test_repeated_mac_ids_stay_out_of_the_dell_partition(tmp_data):
    ids = [allocate_id() for _ in range(64)]
    assert len(set(ids)) == 64
    assert all(not sid.startswith(DELL_ID_PREFIX) for sid in ids)
    assert all(len(sid) == ID_LENGTH for sid in ids)


def test_playwright_save_uses_the_mac_partition(tmp_data):
    from app import capture

    previous = capture.job_queue
    capture.job_queue = asyncio.Queue()
    sid = ""
    try:
        sid = capture.enqueue_capture(
            "https://example.com/playwright-save",
            "https://example.com/playwright-save",
        )
    finally:
        capture.job_queue = previous
        if sid:
            capture.jobs.pop(sid, None)
    assert sid
    assert not sid.startswith(DELL_ID_PREFIX)
    assert ID_RE.fullmatch(sid)


def test_dell_ids_start_with_z():
    for _ in range(32):
        sid = new_snapshot_id("dell")
        assert sid.startswith(DELL_ID_PREFIX)
        assert ID_RE.fullmatch(sid)
        assert set(sid[1:]) <= set(ID_ALPHABET)


def test_dell_allocator_skips_an_id_already_in_the_live_store(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    taken = "ZAAAA"
    free = "ZBBBB"
    conn = sqlite3.connect(library / "amber.sqlite3")
    try:
        conn.executescript(SNAPSHOTS_DDL)
        conn.execute(
            """
            INSERT INTO snapshots (id, url, url_normalized, created_at, status)
            VALUES (?, ?, ?, ?, 'complete')
            """,
            (taken, "https://example.com/legacy", "https://example.com/legacy", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    calls = iter((taken, free))
    sid = ""
    try:
        sid = allocate_dell_id(
            library,
            new_id=lambda: next(calls),
            scratch_has=lambda _sid: False,
        )
        assert sid == free
    finally:
        if sid:
            release_dell_id(sid)


def test_dell_allocator_refuses_a_missing_library_database(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    with pytest.raises(RuntimeError, match="missing"):
        allocate_dell_id(
            library,
            new_id=lambda: "ZAAAA",
            scratch_has=lambda _sid: False,
        )


def test_dell_allocator_refuses_when_the_store_cannot_be_queried(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    sqlite3.connect(library / "amber.sqlite3").close()
    with pytest.raises(sqlite3.OperationalError):
        allocate_dell_id(
            library,
            new_id=lambda: "ZAAAA",
            scratch_has=lambda _sid: False,
        )
