"""Amber web against a library it does not own.

Ingest mode reads ``amber.sqlite3`` with ``mode=ro`` and publishes saves
through the existing Dell allocator and ``push_snapshot``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db
from app.library_contract import SNAPSHOTS_DDL
from app.library_push import snapshot_push

TOKEN = "ingest-mode-token"
LIBRARY_SID = "K7mQp"
LIBRARY_TITLE = "Already On The Shelf"
ARTICLE_URL = "https://example.com/already"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        found[str(path.relative_to(root))] = _sha(path)
    return found


def _write_library(root: Path) -> Path:
    """An ingest-shaped library: snapshots only, no candidates or recapture columns."""
    root.mkdir(parents=True)
    snap = root / "snaps" / LIBRARY_SID
    snap.mkdir(parents=True)
    (snap / "reader.html").write_text(
        "<article><h1>Already On The Shelf</h1><p>kept</p></article>\n",
        encoding="utf-8",
    )
    (snap / "page.html").write_text("<p>kept</p>\n", encoding="utf-8")
    (snap / "res").mkdir()
    (snap / "res" / "site.css").write_text("article{color:#222}\n", encoding="utf-8")
    Image.new("RGB", (2, 2), "white").save(snap / "screenshot.jpg", "JPEG")
    db_path = root / "amber.sqlite3"
    conn = sqlite3.connect(db_path)
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
                LIBRARY_SID,
                ARTICLE_URL,
                "https://example.com/already",
                ARTICLE_URL,
                LIBRARY_TITLE,
                "Example",
                None,
                None,
                None,
                "2026-10-09T19:25:47Z",
                200,
                400,
                0,
                "complete",
                None,
            ),
        )
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    finally:
        conn.close()
    for extra in (db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        extra.unlink(missing_ok=True)
    return db_path


def _point_at_library(monkeypatch, library: Path, db_path: Path) -> None:
    monkeypatch.setenv("AMBER_DATA_DIR", str(library))
    monkeypatch.setenv("AMBER_LIBRARY_MODE", "ingest")
    for module in (db, config):
        monkeypatch.setattr(module, "DATA_DIR", library, raising=False)
        monkeypatch.setattr(module, "SNAPS_DIR", library / "snaps", raising=False)
        monkeypatch.setattr(module, "DB_PATH", db_path, raising=False)


@pytest.fixture
def ingest_paths(tmp_path, monkeypatch):
    library = tmp_path / "library"
    db_path = _write_library(library)
    state = tmp_path / "state" / "app.sqlite3"
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("AMBER_APP_STATE", str(state))
    monkeypatch.setenv("AMBER_SCRATCH_DIR", str(scratch))
    _point_at_library(monkeypatch, library, db_path)
    held = db._instance_lock
    db._instance_lock = None
    from app import library_import

    library_import._reserved.clear()
    yield {
        "library": library,
        "db_path": db_path,
        "state": state,
        "scratch": scratch,
    }
    current = db._instance_lock
    if current is not None and current is not held:
        current.close()
    db._instance_lock = held
    library_import._reserved.clear()


def _concrete(path: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1).split(":", 1)[0]
        if name == "sid":
            return LIBRARY_SID
        if name == "job_id":
            return "no-such-job"
        if name == "filename":
            return "missing.css"
        return "x"

    text = re.sub(r"\{([^}]+)\}", repl, path)
    return text or "/"


def _walk_routes(routes) -> list[tuple[set[str], str]]:
    found: list[tuple[set[str], str]] = []
    for route in routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            found.append((set(methods), path))
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(_walk_routes(inner.routes))
        nested = getattr(route, "routes", None)
        if nested:
            found.extend(_walk_routes(nested))
    return found


def _get_paths() -> list[str]:
    from app.main import app

    found: list[str] = []
    for methods, path in _walk_routes(app.routes):
        if "GET" in methods:
            found.append(_concrete(path))
    found.extend(
        [
            "/static/css/app.css",
            "/favicon.ico",
            "/search?q=Shelf",
            "/api/lookup?url=https://example.com/already",
            "/api/snapshots?since_days=14",
            f"/{LIBRARY_SID}/reader",
            f"/{LIBRARY_SID}/raw",
            f"/{LIBRARY_SID}/image.jpg",
            f"/{LIBRARY_SID}/thumb.jpg",
            f"/{LIBRARY_SID}/r/site.css",
            "/save?url=",
        ]
    )
    return list(dict.fromkeys(found))


def test_readonly_startup_and_get_routes_do_not_touch_the_library(ingest_paths):
    from app.main import app

    library = ingest_paths["library"]
    db_path = ingest_paths["db_path"]
    os.chmod(db_path, 0o444)
    before = _tree(library)

    scratch = ingest_paths["scratch"]
    (scratch / "snaps" / "Z2345").mkdir(parents=True)
    (scratch / "snaps" / "Z2345" / "page.html").write_text("<p>orphan</p>", encoding="utf-8")
    state = ingest_paths["state"]
    state.parent.mkdir(parents=True)
    conn = sqlite3.connect(state)
    try:
        conn.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                url_normalized TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                title TEXT,
                final_url TEXT,
                snapshot_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO jobs (
                id, url, url_normalized, status, error, title, final_url,
                snapshot_id, created_at
            ) VALUES ('Z2345', 'https://example.com/x', 'https://example.com/x',
                      'capturing', NULL, NULL, NULL, 'Z2345', '2026-01-01T00:00:00Z')
            """
        )
        conn.commit()
    finally:
        conn.close()

    paths = _get_paths()
    assert any(path.startswith("/api/candidates") for path in paths)
    assert "/recapture" in paths
    assert "/search" in paths

    with TestClient(app) as client:
        assert not (scratch / "snaps" / "Z2345").exists()
        for path in paths:
            response = client.get(path, follow_redirects=False)
            assert response.status_code < 500, path
        home = client.get("/")
        assert home.status_code == 200
        assert LIBRARY_TITLE in home.text
        saved = client.get("/saved")
        assert LIBRARY_TITLE in saved.text
        found = client.get("/search", params={"q": "Shelf"})
        assert found.status_code == 200
        assert LIBRARY_TITLE in found.text
        assert "No snapshots matched" not in found.text

    assert _tree(library) == before
    assert not (library / "amber.lock").exists()
    conn = sqlite3.connect(state)
    try:
        row = conn.execute("SELECT status, error FROM jobs WHERE id = 'Z2345'").fetchone()
    finally:
        conn.close()
    assert row == ("failed", "Interrupted before it finished.")


def _scratch_row(root: Path, sid: str, *, paywalled, word_count: int, title: str) -> None:
    snap = root / "snaps" / sid
    (snap / "res").mkdir(parents=True)
    (snap / "meta.json").write_text(
        json.dumps({"id": sid, "title": title, "url": ARTICLE_URL}),
        encoding="utf-8",
    )
    (snap / "page.html").write_text("<p>page</p>\n", encoding="utf-8")
    conn = sqlite3.connect(root / "amber.sqlite3")
    try:
        conn.executescript(SNAPSHOTS_DDL)
        conn.execute(
            """
            INSERT INTO snapshots (
                id, url, url_normalized, final_url, title, created_at,
                word_count, paywalled, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'complete')
            """,
            (sid, ARTICLE_URL, "https://example.com/already", ARTICLE_URL, title, "2026-10-09T19:25:47Z", word_count, paywalled),
        )
        conn.commit()
    finally:
        conn.close()


def test_null_paywalled_follows_the_word_count_rule(tmp_path):
    short = tmp_path / "short"
    _scratch_row(short, "Z2345", paywalled=None, word_count=12, title="Teaser")
    document, _blobs = snapshot_push(short, "Z2345")
    assert document["row"]["paywalled"] is True

    long = tmp_path / "long"
    _scratch_row(long, "Z2346", paywalled=None, word_count=400, title="Full")
    document, _blobs = snapshot_push(long, "Z2346")
    assert document["row"]["paywalled"] is False

    explicit = tmp_path / "explicit"
    _scratch_row(explicit, "Z2347", paywalled=0, word_count=12, title="Kept")
    document, _blobs = snapshot_push(explicit, "Z2347")
    assert document["row"]["paywalled"] is False


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = self.server.html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve_html(html: str) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    server.html = html
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/story"


SHORT_HTML = """<!doctype html>
<html><head>
  <title>Ignore</title>
  <meta property="og:title" content="Teaser Only Headline">
  <meta property="og:site_name" content="The Daily Test">
</head>
<body><article><h1>Teaser Only Headline</h1>
<p>Subscribe to read the rest of this story.</p>
</article></body></html>
"""

LONG_HTML = """<!doctype html>
<html><head>
  <title>Ignore</title>
  <meta property="og:title" content="City Council Approves Bridge">
  <meta property="og:site_name" content="The Daily Test">
</head>
<body><article>
  <h1>City Council Approves Bridge</h1>
  <p>The city council voted last night to fund a new bridge over the river.
  Construction starts in May after a decade of debate among residents.
  Opponents said the money should have gone to buses and safer crossings
  near the school. Supporters answered that freight already clogs the old
  span every morning, and that another winter of patching the deck would
  cost more than starting over. The mayor called the vote a compromise
  and promised a public meeting before the first pour of concrete.</p>
</article></body></html>
"""


class _IngestHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _read(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self._send(401, {"detail": "unauthorized"})
        return False

    def do_PUT(self):
        if not self._authorized():
            return
        body = self._read()
        sha = self.path.rstrip("/").rsplit("/", 1)[-1]
        self.server.blobs[sha] = body
        self._send(201, {"sha256": sha})

    def do_POST(self):
        if not self._authorized():
            return
        payload = json.loads(self._read().decode("utf-8"))
        self.server.posts.append(payload)
        if self.server.mode == "fail":
            self._send(500, {"detail": "commit failed"})
            return
        if self.server.mode == "conflict" and not self.server.conflicted:
            self.server.conflicted = True
            self._send(409, {"detail": "revision is not stored + 1", "revision": 1})
            return
        from app.library_ingest import commit_document, install_blob, prepare_store

        prepare_store(self.server.library)
        for item in payload["files"]:
            install_blob(self.server.library, item["sha256"], self.server.blobs[item["sha256"]])
        result = commit_document(self.server.library, payload)
        self._send(
            result.status,
            {"id": result.sid, "revision": result.revision, "change_seq": result.change_seq},
        )


def _serve_ingest(library: Path, mode: str) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _IngestHandler)
    server.library = library
    server.mode = mode
    server.posts = []
    server.blobs = {}
    server.conflicted = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def _token(path: Path) -> None:
    path.write_text(TOKEN + "\n", encoding="utf-8")
    path.chmod(0o600)


def _wait(client: TestClient, sid: str) -> dict:
    deadline = time.time() + 90
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{sid}")
        if response.status_code == 404:
            time.sleep(0.2)
            continue
        job = response.json()
        if job["status"] in {"complete", "failed"}:
            return job
        time.sleep(0.3)
    pytest.fail("capture timed out")


def _library_ids(db_path: Path) -> list[str]:
    uri = "file:{}?mode=ro".format(quote(str(db_path), safe="/:"))
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT id FROM snapshots WHERE status = 'complete' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


@pytest.fixture
def capture_app(ingest_paths, tmp_path, monkeypatch, allow_private):
    token = tmp_path / "ingest.token"
    _token(token)
    monkeypatch.setenv("AMBER_INGEST_TOKEN_FILE", str(token))
    yield ingest_paths


def _save(client: TestClient, url: str) -> str:
    response = client.post("/save", data={"url": url}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"].rsplit("/", 1)[-1]


def test_failed_push_leaves_the_library_unchanged(capture_app, allow_private):
    from app.main import app

    library = capture_app["library"]
    before = _tree(library)
    server, base = _serve_ingest(library, "fail")
    monkey_url = os.environ
    monkey_url["AMBER_INGEST_URL"] = base
    page, url = _serve_html(SHORT_HTML)
    try:
        with TestClient(app) as client:
            sid = _save(client, url)
            job = _wait(client, sid)
            assert job["status"] == "failed"
            assert _tree(library) == before
            assert _library_ids(capture_app["db_path"]) == [LIBRARY_SID]
            leftovers = list((capture_app["scratch"] / "snaps").glob("*")) if (capture_app["scratch"] / "snaps").exists() else []
            assert leftovers == []
    finally:
        server.shutdown()
        page.shutdown()
        os.environ.pop("AMBER_INGEST_URL", None)


def test_save_pushes_paywalled_and_search_sees_it_without_restart(capture_app, allow_private):
    from app.main import app

    library = capture_app["library"]
    server, base = _serve_ingest(library, "ok")
    os.environ["AMBER_INGEST_URL"] = base
    page, url = _serve_html(SHORT_HTML)
    try:
        with TestClient(app) as client:
            sid = _save(client, url)
            job = _wait(client, sid)
            assert job["status"] == "complete", job.get("error")
            assert job["snapshot_id"].startswith("Z")
            assert server.posts, "ingest received no revision"
            assert server.posts[-1]["row"]["paywalled"] is True
            assert server.posts[-1]["id"] == job["snapshot_id"]
            found = client.get("/search", params={"q": "Teaser Only Headline"})
            assert found.status_code == 200
            assert "Teaser Only Headline" in found.text
            assert f'href="/{job["snapshot_id"]}"' in found.text
            home = client.get("/")
            assert "Teaser Only Headline" in home.text
            saved = client.get("/saved")
            assert "Teaser Only Headline" in saved.text
            assert job["snapshot_id"] in _library_ids(capture_app["db_path"])
    finally:
        server.shutdown()
        page.shutdown()
        os.environ.pop("AMBER_INGEST_URL", None)


def test_conflict_reallocates_and_the_retry_keeps_paywalled(capture_app, allow_private):
    from app.main import app

    library = capture_app["library"]
    server, base = _serve_ingest(library, "conflict")
    os.environ["AMBER_INGEST_URL"] = base
    page, url = _serve_html(LONG_HTML)
    try:
        with TestClient(app) as client:
            sid = _save(client, url)
            job = _wait(client, sid)
            assert job["status"] == "complete", job.get("error")
            assert len(server.posts) == 2
            first, second = server.posts
            assert first["id"] != second["id"]
            assert first["id"].startswith("Z") and second["id"].startswith("Z")
            assert second["row"]["paywalled"] is False
            assert job["snapshot_id"] == second["id"]
            assert first["id"] not in _library_ids(capture_app["db_path"])
            assert second["id"] in _library_ids(capture_app["db_path"])
            found = client.get("/search", params={"q": "City Council Approves Bridge"})
            assert "City Council Approves Bridge" in found.text
            assert f'href="/{second["id"]}"' in found.text
            assert f'href="/{first["id"]}"' not in found.text
    finally:
        server.shutdown()
        page.shutdown()
        os.environ.pop("AMBER_INGEST_URL", None)
