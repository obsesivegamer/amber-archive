"""Push one complete snapshot into an empty library and read it back.

The list query is amber-reader's ``list_snapshots`` (``app/index.py``): open
``amber.sqlite3`` with ``mode=ro&immutable=1`` and select rows whose status is
``complete``. Ids that are not the 5-character archive alphabet are dropped.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

import pytest
from fastapi.testclient import TestClient

from app.library_contract import ID_RE, SNAPSHOTS_DDL, sha256_hex
from app import library_ingest
from app.library_ingest import commit_document, create_app, prepare_store
from app.library_push import PushError, PushResult, main, push_snapshot, snapshot_push

SID = "K7mQp"
SIBLING = "mN3pQ"
TOKEN = "test-ingest-token"
URL = "https://www.ft.com/content/abc"
TITLE = "City Council Approves Bridge"
ARTICLE = "The city council voted last night to fund a new bridge.\n"
CREATED = "2026-10-09T19:25:47Z"


class _Killed(BaseException):
    """Stands in for a process kill after the directory rename."""


class _AppTransport:
    def __init__(self, app):
        self.http = TestClient(app)
        self.http.__enter__()

    def round_trip(self, method, url, body, headers):
        response = self.http.request(method, urlparse(url).path, content=body, headers=headers)
        if not response.content:
            return response.status_code, {}
        return response.status_code, response.json()


def write_token(path: Path, token: str = TOKEN) -> Path:
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def write_mac(data: Path, sid: str = SID, *, status: str = "complete", title: str = TITLE) -> None:
    snap = data / "snaps" / sid
    (snap / "res").mkdir(parents=True)
    meta = {"id": sid, "title": title, "url": URL, "final_url": URL, "created_at": CREATED}
    files = {
        "page.html": "<p>page</p>\n",
        "reader.html": "<article>reader</article>\n",
        "article.html": "<p>article</p>\n",
        "article.txt": ARTICLE,
        "meta.json": json.dumps(meta),
        "res/site.css": "article{color:#222}\n",
    }
    for rel, text in files.items():
        (snap / rel).write_text(text, encoding="utf-8")
    conn = sqlite3.connect(data / "amber.sqlite3")
    try:
        conn.executescript(SNAPSHOTS_DDL)
        conn.execute(
            """
            INSERT INTO snapshots (
                id, url, url_normalized, final_url, title, site_name, author,
                published_at, description, created_at, http_status, word_count,
                paywalled, status, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                URL,
                "https://ft.com/content/abc",
                URL,
                title,
                "Financial Times",
                None,
                None,
                None,
                CREATED,
                200,
                1200,
                0,
                status,
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def boot(tmp_path: Path):
    token = write_token(tmp_path / "ingest.token")
    library = tmp_path / "library"
    app = create_app(data_dir=library, token_file=token)
    return library, token, app


def reader_rows(data_dir: Path) -> list[dict]:
    """amber-reader's sqlite list: immutable open, status complete, valid ids."""
    db_path = data_dir / "amber.sqlite3"
    uri = "file:{}?mode=ro&immutable=1".format(quote(str(db_path), safe="/:"))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("select 1 from snapshots limit 1")
        found = []
        for row in conn.execute(
            "select * from snapshots where status = 'complete' order by created_at desc"
        ):
            sid = row["id"] or ""
            if not ID_RE.fullmatch(sid):
                continue
            item = dict(row)
            item["paywalled"] = bool(item.get("paywalled"))
            item["status"] = item.get("status") or "complete"
            found.append(item)
        return found
    finally:
        conn.close()


def insert_snapshot(db_path: Path, sid: str, title: str, created_at: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO snapshots (
                id, url, url_normalized, final_url, title, site_name, author,
                published_at, description, created_at, http_status, word_count,
                paywalled, status, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                URL,
                "https://ft.com/content/abc",
                URL,
                title,
                "Financial Times",
                None,
                None,
                None,
                created_at,
                200,
                40,
                0,
                "complete",
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_push_lists_on_the_reader_and_repeat_is_idempotent(tmp_path):
    mac = tmp_path / "mac"
    write_mac(mac)
    library, _token, app = boot(tmp_path)
    transport = _AppTransport(app)
    first = push_snapshot(mac, SID, "http://ingest", TOKEN, transport=transport)
    assert first.status == 201
    assert first.revision == 1
    assert first.change_seq == 1

    rows = reader_rows(library)
    assert [row["id"] for row in rows] == [SID]
    assert rows[0]["title"] == TITLE
    assert rows[0]["status"] == "complete"
    assert rows[0]["url"] == URL
    snap = library / "snaps" / SID
    mac_snap = mac / "snaps" / SID
    assert (snap / "article.txt").read_text(encoding="utf-8") == ARTICLE
    assert (snap / "page.html").read_bytes() == (mac_snap / "page.html").read_bytes()
    assert (snap / "reader.html").read_bytes() == (mac_snap / "reader.html").read_bytes()
    assert (snap / "res" / "site.css").read_bytes() == (mac_snap / "res" / "site.css").read_bytes()
    assert json.loads((snap / "meta.json").read_text(encoding="utf-8"))["title"] == TITLE

    sentinel = snap / "from-rsync.txt"
    sentinel.write_text("leave me\n", encoding="utf-8")
    second = push_snapshot(mac, SID, "http://ingest", TOKEN, transport=transport)
    assert second.status == 200
    assert second.revision == 1
    assert second.change_seq == 1
    assert sentinel.read_text(encoding="utf-8") == "leave me\n"
    assert [path.name for path in (library / "snaps").iterdir()].count(SID) == 1

    conn = sqlite3.connect(library / "amber.sqlite3")
    try:
        assert conn.execute("select count(*) from snapshots where id = ?", (SID,)).fetchone()[0] == 1
        assert conn.execute("select count(*) from library_revisions").fetchone()[0] == 1
        assert conn.execute("select count(*) from library_changes").fetchone()[0] == 1
        stored = conn.execute(
            "select revision, match_key, change_seq from library_revisions"
        ).fetchone()
        assert stored == (1, "ft.com/content/abc", 1)
    finally:
        conn.close()

    health = transport.http.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"ok": True}
    hidden = transport.http.get(f"/{SID}/reader")
    assert hidden.status_code == 404
    assert b"<article>" not in hidden.content
    assert ARTICLE.encode() not in hidden.content


def test_wrong_token_is_401(tmp_path):
    mac = tmp_path / "mac"
    write_mac(mac)
    library, _token, app = boot(tmp_path)
    http = _AppTransport(app).http
    document, blobs = snapshot_push(mac, SID)
    digest, data = blobs[0]
    denied = http.put(f"/v1/blobs/{digest}", content=data, headers={"Authorization": "Bearer nope"})
    assert denied.status_code == 401
    assert isinstance(denied.json()["detail"], str)
    posted = http.post(
        "/v1/revisions",
        content=json.dumps(document).encode(),
        headers={"Authorization": "Bearer nope", "Content-Type": "application/json"},
    )
    assert posted.status_code == 401
    assert isinstance(posted.json()["detail"], str)
    assert not (library / "snaps" / SID).exists()


def test_bad_revision_and_bad_hash(tmp_path):
    mac = tmp_path / "mac"
    write_mac(mac)
    library, _token, app = boot(tmp_path)
    transport = _AppTransport(app)
    assert push_snapshot(mac, SID, "http://ingest", TOKEN, transport=transport).status == 201
    http = transport.http
    headers = {**auth_headers(), "Content-Type": "application/json"}

    skipped = snapshot_push(mac, SID, revision=3)
    skipped_doc, _blobs = skipped
    response = http.post("/v1/revisions", content=json.dumps(skipped_doc).encode(), headers=headers)
    assert response.status_code == 409
    body = response.json()
    assert isinstance(body["detail"], str)
    assert body["revision"] == 1

    tampered, _blobs = snapshot_push(mac, SID, revision=1)
    tampered["row"]["title"] = "Other title"
    response = http.post("/v1/revisions", content=json.dumps(tampered).encode(), headers=headers)
    assert response.status_code == 409
    assert response.json()["revision"] == 1
    assert reader_rows(library)[0]["title"] == TITLE

    digest = sha256_hex(b"the real bytes")
    bad = http.put(
        f"/v1/blobs/{digest}",
        content=b"not those bytes",
        headers=auth_headers(),
    )
    assert 400 <= bad.status_code < 500
    assert isinstance(bad.json()["detail"], str)
    assert not (library / "blobs").exists() or digest not in {
        path.name for path in (library / "blobs").rglob("*") if path.is_file()
    }

    missing, _blobs = snapshot_push(mac, SID, revision=2)
    missing["row"]["title"] = "Should Not Publish"
    for item in missing["files"]:
        if item["path"] == "article.txt":
            item["sha256"] = digest
            item["size"] = len(b"not those bytes")
    response = http.post("/v1/revisions", content=json.dumps(missing).encode(), headers=headers)
    assert 400 <= response.status_code < 500
    assert isinstance(response.json()["detail"], str)
    assert (library / "snaps" / SID / "article.txt").read_text(encoding="utf-8") == ARTICLE
    assert reader_rows(library)[0]["title"] == TITLE


def _next_revision(mac: Path) -> tuple[dict, bytes]:
    document, _blobs = snapshot_push(mac, SID, revision=2)
    article = b"should not stick\n"
    document["row"]["title"] = "Should Not Publish"
    digest = sha256_hex(article)
    for item in document["files"]:
        if item["path"] == "article.txt":
            item["sha256"] = digest
            item["size"] = len(article)
    return document, article


def _put_article(app, article: bytes) -> None:
    digest = sha256_hex(article)
    response = _AppTransport(app).http.put(
        f"/v1/blobs/{digest}",
        content=article,
        headers=auth_headers(),
    )
    assert response.status_code == 201


def test_sql_error_keeps_the_previous_snapshot(tmp_path, monkeypatch):
    mac = tmp_path / "mac"
    write_mac(mac)
    library, _token, app = boot(tmp_path)
    transport = _AppTransport(app)
    assert push_snapshot(mac, SID, "http://ingest", TOKEN, transport=transport).status == 201
    original = (library / "snaps" / SID / "article.txt").read_bytes()
    document, article = _next_revision(mac)
    _put_article(app, article)

    def boom(_data_dir, _doc):
        raise RuntimeError("disk died")

    monkeypatch.setattr("app.library_ingest._apply_sql", boom)
    response = transport.http.post(
        "/v1/revisions",
        content=json.dumps(document).encode(),
        headers={**auth_headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 500
    assert isinstance(response.json()["detail"], str)
    assert (library / "snaps" / SID / "article.txt").read_bytes() == original
    assert reader_rows(library)[0]["title"] == TITLE
    assert not (library / "ingest-journal" / f"{SID}.json").exists()
    conn = sqlite3.connect(library / "amber.sqlite3")
    try:
        assert conn.execute("select revision from library_revisions").fetchone()[0] == 1
        assert conn.execute("select count(*) from library_changes").fetchone()[0] == 1
    finally:
        conn.close()


def test_restart_after_kill_keeps_the_previous_snapshot(tmp_path, monkeypatch):
    mac = tmp_path / "mac"
    write_mac(mac)
    library, _token, app = boot(tmp_path)
    assert push_snapshot(
        mac, SID, "http://ingest", TOKEN, transport=_AppTransport(app)
    ).status == 201
    original = (library / "snaps" / SID / "article.txt").read_bytes()
    document, article = _next_revision(mac)
    _put_article(app, article)

    def die(_data_dir, _doc):
        raise _Killed()

    monkeypatch.setattr("app.library_ingest._apply_sql", die)
    with pytest.raises(_Killed):
        commit_document(library, document)
    assert (library / "ingest-journal" / f"{SID}.json").is_file()
    assert (library / "snaps" / SID / "article.txt").read_bytes() == article

    prepare_store(library)
    assert (library / "snaps" / SID / "article.txt").read_bytes() == original
    assert reader_rows(library)[0]["title"] == TITLE
    assert not (library / "ingest-journal" / f"{SID}.json").exists()
    staging = library / "ingest-staging"
    assert not staging.exists() or list(staging.iterdir()) == []
    conn = sqlite3.connect(library / "amber.sqlite3")
    try:
        assert conn.execute("select revision from library_revisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_restart_after_kill_on_the_first_push_lists_nothing(tmp_path, monkeypatch):
    mac = tmp_path / "mac"
    write_mac(mac)
    library, _token, app = boot(tmp_path)
    document, blobs = snapshot_push(mac, SID)
    http = _AppTransport(app).http
    for digest, data in blobs:
        assert http.put(f"/v1/blobs/{digest}", content=data, headers=auth_headers()).status_code == 201

    def die(_data_dir, _doc):
        raise _Killed()

    monkeypatch.setattr("app.library_ingest._apply_sql", die)
    with pytest.raises(_Killed):
        commit_document(library, document)
    assert (library / "snaps" / SID / "article.txt").is_file()

    prepare_store(library)
    assert not (library / "snaps" / SID).exists()
    assert reader_rows(library) == []
    conn = sqlite3.connect(library / "amber.sqlite3")
    try:
        assert conn.execute("select count(*) from snapshots").fetchone()[0] == 0
        assert conn.execute("select count(*) from library_revisions").fetchone()[0] == 0
    finally:
        conn.close()


def test_rsync_sibling_survives_the_push(tmp_path):
    mac = tmp_path / "mac"
    write_mac(mac)
    library, _token, app = boot(tmp_path)
    sibling = library / "snaps" / SIBLING
    sibling.mkdir(parents=True)
    (sibling / "marker.txt").write_text("keep\n", encoding="utf-8")
    (sibling / "meta.json").write_text("{}\n", encoding="utf-8")
    old = library / "snaps" / SID
    old.mkdir()
    (old / "article.txt").write_text("rsync copy\n", encoding="utf-8")
    insert_snapshot(library / "amber.sqlite3", SIBLING, "Sibling Story", "2026-01-01T00:00:00Z")
    insert_snapshot(library / "amber.sqlite3", SID, "Rsync Title", "2026-02-01T00:00:00Z")

    result = push_snapshot(mac, SID, "http://ingest", TOKEN, transport=_AppTransport(app))
    assert result.status == 201
    assert (sibling / "marker.txt").read_text(encoding="utf-8") == "keep\n"
    assert (library / "snaps" / SID / "article.txt").read_text(encoding="utf-8") == ARTICLE
    assert sorted(path.name for path in (library / "snaps").iterdir()) == [SID, SIBLING]
    by_id = {row["id"]: row for row in reader_rows(library)}
    assert set(by_id) == {SID, SIBLING}
    assert by_id[SID]["title"] == TITLE
    assert by_id[SIBLING]["title"] == "Sibling Story"
    conn = sqlite3.connect(library / "amber.sqlite3")
    try:
        assert conn.execute("select count(*) from snapshots where id = ?", (SID,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_startup_removes_stale_staging(tmp_path):
    mac = tmp_path / "mac"
    write_mac(mac)
    library, token, app = boot(tmp_path)
    assert push_snapshot(
        mac, SID, "http://ingest", TOKEN, transport=_AppTransport(app)
    ).status == 201
    junk = library / "ingest-staging" / "junk"
    junk.mkdir(parents=True)
    (junk / "meta.json").write_text("{}\n", encoding="utf-8")
    create_app(data_dir=library, token_file=token)
    assert not junk.exists()
    assert (library / "snaps" / SID / "article.txt").read_text(encoding="utf-8") == ARTICLE
    assert [row["id"] for row in reader_rows(library)] == [SID]


def test_connect_closes_when_pragma_setup_fails(tmp_path, monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    class Boom(sqlite3.Connection):
        def execute(self, sql, *args):
            if "foreign_keys" in sql:
                raise sqlite3.OperationalError("boom")
            return super().execute(sql, *args)

    def spy(*args, **kwargs):
        conn = real_connect(*args, factory=Boom, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr("app.library_ingest.sqlite3.connect", spy)
    with pytest.raises(sqlite3.OperationalError, match="boom"):
        library_ingest._connect(tmp_path / "library")
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_loose_token_file_is_refused(tmp_path):
    path = tmp_path / "token"
    path.write_text("secret\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(SystemExit, match="0600"):
        create_app(data_dir=tmp_path / "library", token_file=path)


def test_push_refuses_a_snapshot_that_is_not_complete(tmp_path):
    mac = tmp_path / "mac"
    write_mac(mac, status="pending")
    library, _token, app = boot(tmp_path)
    with pytest.raises(PushError, match="not complete"):
        push_snapshot(mac, SID, "http://ingest", TOKEN, transport=_AppTransport(app))
    assert reader_rows(library) == []


def test_cli_reports_the_push(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake(source, sid, ingest_url, token, transport=None):
        seen.update(source=source, sid=sid, ingest_url=ingest_url, token=token)
        return PushResult(status=201, revision=1, change_seq=4)

    monkeypatch.setattr("app.library_push.push_snapshot", fake)
    token = write_token(tmp_path / "token")
    data = tmp_path / "mac"
    data.mkdir()
    code = main(
        [
            SID,
            "--ingest-url",
            "https://dell-plx-reader.tailf9612a.ts.net:8443",
            "--token-file",
            str(token),
            "--data-dir",
            str(data),
        ]
    )
    assert code == 0
    assert seen["sid"] == SID
    assert seen["token"] == TOKEN
    assert seen["source"] == data
    assert "201 K7mQp revision=1 change_seq=4" in capsys.readouterr().out


def test_ingest_import_does_not_load_capture():
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(repo), *sys.path])
    code = (
        "import sys\n"
        "import app.library_ingest\n"
        "banned = [name for name in sys.modules if name == 'playwright' "
        "or name.startswith('playwright.') or name in "
        "{'app.capture', 'app.main', 'app.ingest', 'app.db', 'app.extract'}]\n"
        "raise SystemExit('loaded ' + ','.join(banned) if banned else 0)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
