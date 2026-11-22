"""After a successful Mac save, push that id when ingest is configured."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.library_push import (
    PushError,
    PushRejected,
    PushResult,
    _push_now,
    await_pending_pushes,
    schedule_library_push,
    schedule_library_push_after,
    thumbnail_hold_active,
)


def _token(tmp_path):
    path = tmp_path / "ingest.token"
    path.write_text("mac-push-token\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_push_is_a_noop_until_ingest_is_configured(monkeypatch):
    monkeypatch.delenv("AMBER_INGEST_URL", raising=False)
    monkeypatch.delenv("AMBER_INGEST_TOKEN_FILE", raising=False)
    called: list[str] = []
    monkeypatch.setattr("app.library_push._push_now", lambda sid: called.append(sid))
    schedule_library_push("K7mQp")
    assert called == []


def test_configured_push_runs_and_a_failure_does_not_raise(tmp_path, monkeypatch):
    token = _token(tmp_path)
    monkeypatch.setenv("AMBER_INGEST_URL", "https://dell-plx-reader.tailf9612a.ts.net:8443")
    monkeypatch.setenv("AMBER_INGEST_TOKEN_FILE", str(token))
    seen: list[tuple[str, str, str]] = []

    def fake(source, sid, url, tok, transport=None):
        seen.append((sid, url, tok))
        if sid == "fail1":
            raise PushError("ingest down")
        return PushResult(status=201, revision=1, change_seq=1)

    monkeypatch.setattr("app.library_push.push_snapshot", fake)

    async def go():
        schedule_library_push("K7mQp")
        schedule_library_push("fail1")
        await await_pending_pushes()

    asyncio.run(go())
    assert {item[0] for item in seen} == {"K7mQp", "fail1"}
    assert seen[0][1] == "https://dell-plx-reader.tailf9612a.ts.net:8443"
    assert seen[0][2] == "mac-push-token"


def test_push_waits_for_the_thumbnail(tmp_path, monkeypatch):
    token = _token(tmp_path)
    monkeypatch.setenv("AMBER_INGEST_URL", "http://127.0.0.1:8082")
    monkeypatch.setenv("AMBER_INGEST_TOKEN_FILE", str(token))
    order: list[str] = []

    async def thumb():
        order.append("thumb")

    def push_now(sid: str) -> None:
        order.append("push:" + sid)

    monkeypatch.setattr("app.library_push._push_now", push_now)

    async def go():
        task = asyncio.create_task(thumb())
        schedule_library_push_after("K7mQp", task)
        await await_pending_pushes()

    asyncio.run(go())
    assert order == ["thumb", "push:K7mQp"]


def test_api_import_schedules_a_mac_partition_push(tmp_data, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.test_library_import import ARTICLE_URL, article_html

    scheduled: list[str] = []

    def record(sid: str, thumbnail=None) -> None:
        scheduled.append(sid)

    monkeypatch.setattr("app.security.host_is_public", lambda host: True)
    monkeypatch.setattr("app.api.reserve_thumbnail", lambda: False)
    monkeypatch.setattr("app.api.schedule_library_push_after", record)
    monkeypatch.delenv("AMBER_INGEST_URL", raising=False)
    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/api/import",
            json={"url": ARTICLE_URL, "html": article_html()},
        )
    assert response.status_code == 201
    sid = response.json()["id"]
    assert scheduled == [sid]
    assert not sid.startswith("Z")


def test_homepage_import_schedules_push(tmp_data, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.test_extract import ARCHIVE_IS_SAVED

    scheduled: list[str] = []
    monkeypatch.setattr(
        "app.library_push.schedule_library_push",
        lambda sid: scheduled.append(sid),
    )

    async def _skip(sid: str) -> None:
        return None

    monkeypatch.setattr("app.main.screenshot_reader", _skip)
    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/import",
            files={"file": ("article.html", ARCHIVE_IS_SAVED, "text/html")},
            follow_redirects=False,
        )
    assert response.status_code == 303
    sid = response.headers["location"].strip("/")
    assert scheduled == [sid]
    assert not sid.startswith("Z")


def test_capture_completion_is_what_schedules_the_save_push():
    """Playwright saves call schedule_library_push once the files are on disk.

    Launching Chromium here would test the browser, not the hook. The call
    site is run_job, after status is set to complete.
    """
    source = (Path(__file__).resolve().parents[1] / "app" / "capture.py").read_text(
        encoding="utf-8"
    )
    marker = 'job["status"] = "complete"'
    assert marker in source
    tail = source.split(marker, 1)[1]
    assert "schedule_library_push(sid)" in tail.split("async def ", 1)[0]


def test_already_stored_push_is_quiet(tmp_path, monkeypatch, caplog):
    token = _token(tmp_path)
    monkeypatch.setenv("AMBER_INGEST_URL", "http://127.0.0.1:8082")
    monkeypatch.setenv("AMBER_INGEST_TOKEN_FILE", str(token))

    def fake(*_args, **_kwargs):
        raise PushRejected(409, "stored revision does not match this body")

    monkeypatch.setattr("app.library_push.push_snapshot", fake)
    with caplog.at_level(logging.DEBUG, logger="amber.library_push"):
        _push_now("K7mQp")
    assert "already stored with a different body" in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_thumbnail_hold_covers_the_render_and_then_clears(tmp_data):
    from app import api, db

    api._thumbnails_waiting = 1
    seen: dict[str, bool] = {}

    async def render(sid: str) -> None:
        seen["held"] = thumbnail_hold_active(db.DATA_DIR, sid)

    asyncio.run(api.run_reserved_thumbnail("K7mQp", render))
    assert seen["held"] is True
    assert thumbnail_hold_active(db.DATA_DIR, "K7mQp") is False
    assert api._thumbnails_waiting == 0
