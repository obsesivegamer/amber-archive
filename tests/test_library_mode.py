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
    """WAL library the way ingest prepares a store, plus a candidate and a recapture flag."""
    from app.library_ingest import prepare_store

    prepare_store(root)
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
        conn.execute("ALTER TABLE snapshots ADD COLUMN recapture_state TEXT")
        conn.execute(
            """
            INSERT INTO snapshots (
                id, url, url_normalized, final_url, title, site_name, author,
                published_at, description, created_at, http_status, word_count,
                paywalled, status, error, recapture_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "pending",
            ),
        )
        conn.execute(
            """
            CREATE TABLE candidates (
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
        conn.execute(
            """
            INSERT INTO candidates (
                id, unlock_url, canonical_url, match_key, publisher, title,
                found_at, expires_at, expires_estimated, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                "cand0001",
                "https://example.com/gift",
                ARTICLE_URL,
                "example.com/already",
                "Example",
                "Secret Candidate",
                "2026-10-09T19:25:47Z",
                "2099-01-01T00:00:00Z",
                "2026-10-09T19:25:47Z",
            ),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()
    except Exception:
        conn.close()
        raise
    uri = "file:{}?mode=ro".format(quote(str(db_path), safe="/:"))
    reader = sqlite3.connect(uri, uri=True)
    try:
        reader.execute("select count(*) from snapshots").fetchone()
    finally:
        reader.close()
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
    state_dir = tmp_path / "state"
    state = state_dir / "app.sqlite3"
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("AMBER_APP_STATE", str(state_dir))
    monkeypatch.setenv("AMBER_READER_PASSWORD", "test-password")
    monkeypatch.setattr("app.auth.session_ok", lambda _cookie: True)
    monkeypatch.setattr("app.auth.basic_ok", lambda _header: True)
    monkeypatch.setenv("AMBER_SCRATCH_DIR", str(scratch))
    token = tmp_path / "ingest.token"
    token.write_text(TOKEN + "\n", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("AMBER_INGEST_TOKEN_FILE", str(token))
    monkeypatch.setenv("AMBER_INGEST_URL", "http://127.0.0.1:9")
    _point_at_library(monkeypatch, library, db_path)
    held = db._instance_lock
    db._instance_lock = None
    db._access = None
    db._writing_scratch = False
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
    db._access = None
    db._writing_scratch = False
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


def _schema_and_rows(db_path: Path):
    conn = sqlite3.connect(
        "file:{}?mode=ro".format(quote(str(db_path), safe="/:")),
        uri=True,
    )
    try:
        schema = conn.execute(
            """
            SELECT type, name, sql FROM sqlite_master
            WHERE sql IS NOT NULL
            ORDER BY type, name
            """
        ).fetchall()
        rows = conn.execute(
            "SELECT id, title, paywalled FROM snapshots ORDER BY id"
        ).fetchall()
        return schema, rows
    finally:
        conn.close()


def test_readonly_startup_and_get_routes_do_not_touch_the_library(ingest_paths):
    from app.library_import import allocate_dell_id
    from app.main import app

    library = ingest_paths["library"]
    db_path = ingest_paths["db_path"]
    content = _sha(db_path)
    schema, rows = _schema_and_rows(db_path)

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
        conn.execute(
            """
            INSERT INTO jobs (
                id, url, url_normalized, status, error, title, final_url,
                snapshot_id, created_at
            ) VALUES ('Zpub1', 'https://example.com/already', 'https://example.com/already',
                      'capturing', NULL, NULL, NULL, 'K7mQp', '2026-01-01T00:00:00Z')
            """
        )
        conn.commit()
    finally:
        conn.close()

    before_tree = {
        name: digest
        for name, digest in _tree(library).items()
        if not name.endswith(("-wal", "-shm"))
    }
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
        candidates = client.get("/api/candidates")
        assert candidates.status_code == 200
        assert candidates.json()["candidates"] == []
        assert "Secret Candidate" not in candidates.text
        recapture = client.get("/recapture")
        assert recapture.status_code == 200
        assert recapture.json()["pending"] == []
        allocated = allocate_dell_id(library, scratch_has=lambda _sid: False)
        assert allocated.startswith("Z")
        from app.library_import import release_dell_id

        release_dell_id(allocated)

    assert _sha(db_path) == content
    assert _schema_and_rows(db_path) == (schema, rows)
    after_tree = {
        name: digest
        for name, digest in _tree(library).items()
        if not name.endswith(("-wal", "-shm"))
    }
    assert after_tree == before_tree
    assert not (library / "amber.lock").exists()
    assert (ingest_paths["state"].with_name(ingest_paths["state"].name + ".lock")).is_file()
    assert not ingest_paths["state"].samefile(
        ingest_paths["state"].with_name(ingest_paths["state"].name + ".lock")
    )
    conn = sqlite3.connect(state)
    try:
        row = conn.execute("SELECT status, error FROM jobs WHERE id = 'Z2345'").fetchone()
        published = conn.execute(
            "SELECT status, error, snapshot_id FROM jobs WHERE id = 'Zpub1'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("failed", "Interrupted before it finished.")
    assert published == ("complete", None, "K7mQp")


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
        if self.server.mode == "blob-fail":
            self._send(500, {"detail": "blob failed"})
            return
        sha = self.path.rstrip("/").rsplit("/", 1)[-1]
        from app.library_ingest import install_blob

        install_blob(self.server.library, sha, body)
        self.server.blobs[sha] = body
        self._send(201, {"sha256": sha})

    def do_POST(self):
        if not self._authorized():
            return
        if self.server.delay:
            self.server.gate.set()
            time.sleep(self.server.delay)
        payload = json.loads(self._read().decode("utf-8"))
        self.server.posts.append(payload)
        if self.server.mode == "drop-once" and not self.server.dropped:
            from app.library_ingest import commit_document

            commit_document(self.server.library, payload)
            self.server.dropped = True
            self.close_connection = True
            return
        if self.server.mode == "fail" or (
            self.server.mode == "conflict-fail" and self.server.conflicted
        ):
            from app.library_ingest import _release_payload_blobs

            _release_payload_blobs(self.server.library, payload)
            self._send(500, {"detail": "commit failed"})
            return
        if self.server.mode in {"conflict", "conflict-fail"} and not self.server.conflicted:
            from app.library_ingest import _release_payload_blobs

            self.server.conflicted = True
            _release_payload_blobs(self.server.library, payload)
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
    server.delay = 0
    server.gate = threading.Event()
    server.dropped = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


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
def capture_app(ingest_paths):
    return ingest_paths


def _scratch_rows(scratch: Path) -> int:
    db_path = scratch / "amber.sqlite3"
    if not db_path.is_file():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        try:
            return int(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def _blob_files(root: Path) -> list[str]:
    blobs = root / "blobs"
    if not blobs.exists():
        return []
    return sorted(
        str(path.relative_to(root))
        for path in blobs.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def _blob(server, document: dict, name: str) -> bytes:
    for item in document["files"]:
        if item["path"] == name:
            return server.blobs[item["sha256"]]
    raise AssertionError(name)


def _save(client: TestClient, url: str) -> str:
    response = client.post("/save", data={"url": url}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"].rsplit("/", 1)[-1]


def test_failed_push_leaves_the_library_unchanged(capture_app, allow_private, monkeypatch):
    from app.main import app

    library = capture_app["library"]
    before_snaps = _tree(library / "snaps")
    server, base = _serve_ingest(library, "fail")
    monkeypatch.setenv("AMBER_INGEST_URL", base)
    page, url = _serve_html(SHORT_HTML)
    try:
        with TestClient(app) as client:
            sid = _save(client, url)
            job = _wait(client, sid)
            assert job["status"] == "failed"
            assert _tree(library / "snaps") == before_snaps
            assert _library_ids(capture_app["db_path"]) == [LIBRARY_SID]
            assert _blob_files(library) == []
            assert _scratch_rows(capture_app["scratch"]) == 0
            leftovers = list((capture_app["scratch"] / "snaps").glob("*")) if (capture_app["scratch"] / "snaps").exists() else []
            assert leftovers == []
    finally:
        server.shutdown()
        page.shutdown()


def test_save_pushes_paywalled_and_search_sees_it_without_restart(capture_app, allow_private, monkeypatch):
    from app.main import app

    library = capture_app["library"]
    server, base = _serve_ingest(library, "ok")
    monkeypatch.setenv("AMBER_INGEST_URL", base)
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


def test_conflict_reallocates_and_the_retry_keeps_paywalled(capture_app, allow_private, monkeypatch):
    from app import capture
    from app.main import app

    library = capture_app["library"]
    server, base = _serve_ingest(library, "conflict")
    monkeypatch.setenv("AMBER_INGEST_URL", base)
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
            for name in ("page.html", "reader.html", "meta.json"):
                assert first["id"].encode() not in _blob(server, second, name)
            capture.jobs.pop(sid, None)
            again = client.get(f"/api/jobs/{sid}")
            assert again.status_code == 200
            assert again.json()["snapshot_id"] == second["id"]
            assert again.json()["status"] == "complete"
            assert again.json()["title"] == "City Council Approves Bridge"
            redirect = client.get(f"/saving/{sid}", follow_redirects=False)
            assert redirect.status_code == 303
            assert redirect.headers["location"].endswith("/" + second["id"])
            viewed = client.get(f"/{first['id']}", follow_redirects=False)
            assert viewed.status_code == 303
            assert viewed.headers["location"].endswith("/" + second["id"])
            assert first["id"] not in viewed.text
    finally:
        server.shutdown()
        page.shutdown()


def test_blob_failure_and_failure_after_retarget_leave_no_scratch(
    capture_app, allow_private, monkeypatch
):
    from app.main import app

    library = capture_app["library"]
    before = _tree(library)
    blob_server, blob_base = _serve_ingest(library, "blob-fail")
    monkeypatch.setenv("AMBER_INGEST_URL", blob_base)
    page, url = _serve_html(SHORT_HTML)
    try:
        with TestClient(app) as client:
            job = _wait(client, _save(client, url))
            assert job["status"] == "failed"
            assert _tree(library) == before
            assert _scratch_rows(capture_app["scratch"]) == 0
    finally:
        blob_server.shutdown()
        page.shutdown()

    before_snaps = _tree(library / "snaps")
    retry, retry_base = _serve_ingest(library, "conflict-fail")
    monkeypatch.setenv("AMBER_INGEST_URL", retry_base)
    page, url = _serve_html(LONG_HTML)
    try:
        with TestClient(app) as client:
            job = _wait(client, _save(client, url))
            assert job["status"] == "failed"
            assert len(retry.posts) == 2
            assert retry.posts[0]["id"] != retry.posts[1]["id"]
            assert _tree(library / "snaps") == before_snaps
            assert _library_ids(capture_app["db_path"]) == [LIBRARY_SID]
            assert _blob_files(library) == []
            assert _scratch_rows(capture_app["scratch"]) == 0
            assert list((capture_app["scratch"] / "snaps").glob("*")) == []
    finally:
        retry.shutdown()
        page.shutdown()


def test_capture_failure_releases_the_id_and_scratch(capture_app, allow_private, monkeypatch):
    from app.library_import import _reserved
    from app.main import app

    server, base = _serve_ingest(capture_app["library"], "ok")
    monkeypatch.setenv("AMBER_INGEST_URL", base)
    try:
        with TestClient(app) as client:
            sid = _save(client, "http://127.0.0.1:1/no-such-page")
            job = _wait(client, sid)
            assert job["status"] == "failed"
            assert sid not in _reserved
            assert _scratch_rows(capture_app["scratch"]) == 0
            assert server.posts == []
    finally:
        server.shutdown()


def test_push_does_not_block_reads(capture_app, allow_private, monkeypatch):
    from app.main import app

    server, base = _serve_ingest(capture_app["library"], "ok")
    server.delay = 1.5
    monkeypatch.setenv("AMBER_INGEST_URL", base)
    page, url = _serve_html(SHORT_HTML)
    try:
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
            sid = _save(client, url)
            assert server.gate.wait(60)
            started = time.perf_counter()
            home = client.get("/")
            elapsed = time.perf_counter() - started
            assert home.status_code == 200
            assert elapsed < 0.5
            job = _wait(client, sid)
            assert job["status"] == "complete", job.get("error")
    finally:
        server.shutdown()
        page.shutdown()


def test_cleanup_error_after_publish_still_completes(capture_app, allow_private, monkeypatch):
    from app.main import app

    def boom(_sid: str) -> None:
        raise OSError("disk")

    monkeypatch.setattr(db, "discard_scratch", boom)
    server, base = _serve_ingest(capture_app["library"], "ok")
    monkeypatch.setenv("AMBER_INGEST_URL", base)
    page, url = _serve_html(SHORT_HTML)
    try:
        with TestClient(app) as client:
            job = _wait(client, _save(client, url))
            assert job["status"] == "complete", job.get("error")
            assert len(server.posts) == 1
            assert job["snapshot_id"] in _library_ids(capture_app["db_path"])
    finally:
        server.shutdown()
        page.shutdown()


def test_repeat_push_of_the_same_bytes_is_a_noop(capture_app, tmp_path):
    from app.library_push import push_snapshot

    server, base = _serve_ingest(capture_app["library"], "ok")
    scratch = tmp_path / "repeat"
    _scratch_row(scratch, "Z2345", paywalled=0, word_count=120, title="Same Bytes")
    try:
        first = push_snapshot(scratch, "Z2345", base, TOKEN)
        second = push_snapshot(scratch, "Z2345", base, TOKEN)
    finally:
        server.shutdown()
    assert first.status == 201
    assert second.status == 200
    assert second.change_seq == first.change_seq
    assert _library_ids(capture_app["db_path"]).count("Z2345") == 1


def test_default_mode_keeps_an_explicit_paywalled_flag():
    assert db.paywalled_bool(1, 400) is True
    assert db.paywalled_bool(0, 12) is False
    assert db.paywalled_bool(None, 12) is True
    assert db.paywalled_bool(None, 400) is False


def test_extract_always_sets_paywalled():
    from app.extract import extract_article

    got = extract_article("<html><body><p>Hello there.</p></body></html>", "https://example.com/a")
    assert "paywalled" in got
    assert isinstance(got["paywalled"], bool)


def test_ingest_mode_refuses_a_different_database(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("AMBER_LIBRARY_MODE", "ingest")
    monkeypatch.setenv("AMBER_DATA_DIR", str(library))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "elsewhere" / "amber.sqlite3")
    db._access = None
    db._writing_scratch = False
    try:
        with pytest.raises(RuntimeError, match="configured library"):
            db.configure_library_access()
    finally:
        db._access = None
        db._writing_scratch = False


def test_ingest_mode_refuses_an_unresolvable_library(tmp_path, monkeypatch):
    monkeypatch.setenv("AMBER_LIBRARY_MODE", "ingest")
    monkeypatch.setenv("AMBER_DATA_DIR", str(tmp_path))
    db._access = None
    db._writing_scratch = False

    def explode(self, *args, **kwargs):
        raise OSError("broken")

    monkeypatch.setattr(Path, "resolve", explode)
    try:
        with pytest.raises(RuntimeError, match="cannot resolve"):
            db.configure_library_access()
    finally:
        db._access = None
        db._writing_scratch = False


def test_failed_capture_releases_the_reserved_id(ingest_paths):
    from app.library_import import _reserved, allocate_dell_id

    db._init_app_state()
    db.prepare_scratch()
    sid = allocate_dell_id(ingest_paths["library"], scratch_has=lambda _sid: False)
    assert sid in _reserved
    db.fail_capture(sid, sid, "capture failed")
    assert sid not in _reserved
    assert _scratch_rows(ingest_paths["scratch"]) == 0


def _drop_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def _insert_closed(db_path: Path, sid: str, title: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO snapshots (
                id, url, url_normalized, final_url, title, status, created_at, paywalled
            ) VALUES (?, 'https://example.com/w', 'https://example.com/w',
                      'https://example.com/w', ?, 'complete', '2026-01-02T00:00:00Z', 0)
            """,
            (sid, title),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def test_reads_after_the_writer_commits_and_closes(ingest_paths):
    from app.main import app

    db_path = ingest_paths["db_path"]
    _insert_closed(db_path, "Z1111", "Closed Writer")
    _drop_sidecars(db_path)
    content = _sha(db_path)
    schema, rows = _schema_and_rows(db_path)
    _drop_sidecars(db_path)
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "Closed Writer" in home.text
    assert _sha(db_path) == content
    assert _schema_and_rows(db_path) == (schema, rows)

    _insert_closed(db_path, "Z2222", "Second Close")
    _drop_sidecars(db_path)
    after_writer = _sha(db_path)
    assert after_writer != content
    with TestClient(app) as client:
        found = client.get("/search", params={"q": "Second"})
        assert found.status_code == 200
        assert "Second Close" in found.text
    assert _sha(db_path) == after_writer
    schema2, rows2 = _schema_and_rows(db_path)
    assert schema2 == schema
    assert {row[0] for row in rows2} == {row[0] for row in rows} | {"Z2222"}


def test_web_does_not_write_rows_on_save_failure_or_conflict(ingest_paths, allow_private):
    from app.main import app

    library = ingest_paths["library"]
    db_path = ingest_paths["db_path"]
    _drop_sidecars(db_path)
    content = _sha(db_path)
    schema, rows = _schema_and_rows(db_path)
    os.environ["AMBER_INGEST_URL"] = "http://127.0.0.1:9"
    with TestClient(app) as client:
        failed_id = _save(client, "https://example.com/story")
        job = _wait(client, failed_id)
        assert job["status"] == "failed"
    assert _sha(db_path) == content
    assert _schema_and_rows(db_path) == (schema, rows)

    server, base = _serve_ingest(library, "ok")
    os.environ["AMBER_INGEST_URL"] = base
    _drop_sidecars(db_path)
    try:
        with TestClient(app) as client:
            sid = _save(client, "https://example.com/story")
            job = _wait(client, sid)
            assert job["status"] == "complete"
            found = client.get("/search", params={"q": "Example"})
            assert sid in found.text
        schema_ok, rows_ok = _schema_and_rows(db_path)
        assert schema_ok == schema
        assert {row[0] for row in rows_ok} == {row[0] for row in rows} | {sid}
    finally:
        server.shutdown()

    server, base = _serve_ingest(library, "conflict")
    os.environ["AMBER_INGEST_URL"] = base
    before_ids = {row[0] for row in _schema_and_rows(db_path)[1]}
    try:
        with TestClient(app) as client:
            first = _save(client, "https://example.com/long")
            job = _wait(client, first)
            final = job["snapshot_id"]
            assert final != first
            assert job["status"] == "complete"
        schema_conflict, rows_conflict = _schema_and_rows(db_path)
        assert schema_conflict == schema
        assert {row[0] for row in rows_conflict} == before_ids | {final}
        assert first not in {row[0] for row in rows_conflict}
    finally:
        server.shutdown()


def test_save_in_a_writable_directory_without_sidecars(ingest_paths, allow_private):
    from app.main import app

    library = ingest_paths["library"]
    db_path = ingest_paths["db_path"]
    _drop_sidecars(db_path)
    schema, rows = _schema_and_rows(db_path)
    _drop_sidecars(db_path)
    server, base = _serve_ingest(library, "ok")
    os.environ["AMBER_INGEST_URL"] = base
    try:
        with TestClient(app) as client:
            sid = _save(client, "https://example.com/story")
            assert _wait(client, sid)["status"] == "complete"
        schema2, rows2 = _schema_and_rows(db_path)
        assert schema2 == schema
        assert {row[0] for row in rows2} == {row[0] for row in rows} | {sid}
    finally:
        server.shutdown()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory mode 0555")
def test_missing_sidecars_in_a_readonly_directory_are_a_clear_503(ingest_paths):
    from app.main import app

    library = ingest_paths["library"]
    for name in ("amber.sqlite3-wal", "amber.sqlite3-shm"):
        (library / name).unlink(missing_ok=True)
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
                id, url, url_normalized, status, snapshot_id, created_at
            ) VALUES ('Zpub1', 'https://example.com/already', 'https://example.com/already',
                      'capturing', 'K7mQp', '2026-01-01T00:00:00Z')
            """
        )
        conn.commit()
    finally:
        conn.close()
    os.chmod(library, 0o555)
    names_before = {path.name for path in library.iterdir()}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/")
            assert response.status_code == 503
            assert response.status_code != 500
            assert "read-only" in response.text
            assert "Traceback" not in response.text
            search = client.get("/search", params={"q": "Shelf"})
            assert search.status_code == 503
            page = client.get("/", headers={"accept": "text/html"})
            assert page.status_code == 503
            assert "text/html" in page.headers["content-type"]
            assert "Amber is unavailable" in page.text
            api = client.get("/api/snapshots", headers={"accept": "text/html"})
            assert api.status_code == 503
            assert api.headers["content-type"].startswith("application/json")
        assert {path.name for path in library.iterdir()} == names_before
        conn = sqlite3.connect(state)
        try:
            published = conn.execute(
                "SELECT status, error FROM jobs WHERE id = 'Zpub1'"
            ).fetchone()
        finally:
            conn.close()
        assert published[0] == "unknown"
        assert "not marked failed" in (published[1] or "")
        assert not (library / "amber.sqlite3-wal").exists()
        assert not (library / "amber.sqlite3-shm").exists()
    finally:
        os.chmod(library, 0o755)


def test_lost_response_retries_the_same_id(capture_app, tmp_path):
    from app.library_import import _push_confirmed

    server, base = _serve_ingest(capture_app["library"], "drop-once")
    scratch = tmp_path / "lost"
    _scratch_row(scratch, "Z2345", paywalled=0, word_count=120, title="Same Bytes")
    try:
        _push_confirmed(
            scratch, "Z2345", base, TOKEN, capture_app["library"], transport=None
        )
    finally:
        server.shutdown()
    assert [item["id"] for item in server.posts] == ["Z2345", "Z2345"]
    assert _library_ids(capture_app["db_path"]).count("Z2345") == 1
    assert (scratch / "snaps" / "Z2345").is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory mode 0555")
def test_unconfirmed_push_keeps_scratch_when_the_library_cannot_be_read(ingest_paths, tmp_path):
    from urllib.error import URLError

    from app.library_import import UnconfirmedPush, _push_confirmed

    scratch = tmp_path / "keep"
    _scratch_row(scratch, "Z2345", paywalled=0, word_count=40, title="Kept")
    library = ingest_paths["library"]
    _drop_sidecars(ingest_paths["db_path"])
    os.chmod(library, 0o555)

    class Dropped:
        def round_trip(self, *_args, **_kwargs):
            raise URLError("timed out")

    dropped = Dropped()

    try:
        with pytest.raises(UnconfirmedPush):
            _push_confirmed(scratch, "Z2345", "http://127.0.0.1:9", TOKEN, library, transport=dropped)
    finally:
        os.chmod(library, 0o755)
    assert (scratch / "snaps" / "Z2345").is_dir()
    with pytest.raises(URLError):
        _push_confirmed(scratch, "Z2345", "http://127.0.0.1:9", TOKEN, library, transport=dropped)
    assert (scratch / "snaps" / "Z2345").is_dir()


def test_released_job_id_is_not_issued_again(ingest_paths):
    from app.library_import import allocate_dell_id

    db._init_app_state()
    db.prepare_scratch()
    sid = allocate_dell_id(ingest_paths["library"], scratch_has=lambda _sid: False, new_id=lambda: "Z2345")
    db.begin_capture(sid, "https://example.com/a", "https://example.com/a")
    db.fail_capture(sid, sid, "capture failed")
    with pytest.raises(RuntimeError, match="allocate"):
        allocate_dell_id(
            ingest_paths["library"],
            scratch_has=db.job_id_taken,
            new_id=lambda: "Z2345",
        )


def test_retarget_rewrites_srcset_and_scripts_only(tmp_path):
    from app.library_import import _retarget_scratch

    scratch = tmp_path / "scratch"
    _scratch_row(scratch, "Z2345", paywalled=0, word_count=40, title="Move")
    folder = scratch / "snaps" / "Z2345"
    (folder / "page.html").write_text(
        '<img srcset="/Z2345/r/a.jpg 1x, /Z2345/r/b.jpg 2x">',
        encoding="utf-8",
    )
    (folder / "res" / "app.js").write_text('const u = "/Z2345/r/app.js";', encoding="utf-8")
    nested = folder / "res" / "icons"
    nested.mkdir()
    (nested / "mark.svg").write_text('<svg href="/Z2345/r/mark.svg"/>', encoding="utf-8")
    (folder / "res" / "mod.mjs").write_text('import "/Z2345/r/mod.mjs";', encoding="utf-8")
    (folder / "article.html").write_text("see /Z2345/ in the prose", encoding="utf-8")
    _retarget_scratch(scratch, "Z2345", "Z2346")
    moved = scratch / "snaps" / "Z2346"
    assert "/Z2346/r/a.jpg" in (moved / "page.html").read_text(encoding="utf-8")
    assert "/Z2345/" not in (moved / "page.html").read_text(encoding="utf-8")
    assert "/Z2346/r/app.js" in (moved / "res" / "app.js").read_text(encoding="utf-8")
    assert "/Z2346/r/mark.svg" in (moved / "res" / "icons" / "mark.svg").read_text(encoding="utf-8")
    assert "/Z2346/r/mod.mjs" in (moved / "res" / "mod.mjs").read_text(encoding="utf-8")
    assert (moved / "article.html").read_text(encoding="utf-8") == "see /Z2345/ in the prose"


def test_unconfirmed_job_waits_for_a_real_library_row(ingest_paths):
    from app import capture
    from app.main import app

    sid = "Z6789"
    db._init_app_state()
    db.prepare_scratch()
    scratch = ingest_paths["scratch"] / "snaps" / sid
    scratch.mkdir(parents=True)
    (scratch / "page.html").write_text("kept", encoding="utf-8")
    with db._state_conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, url, url_normalized, status, error, snapshot_id, created_at
            ) VALUES (?, 'https://example.com/a', 'https://example.com/a',
                      'unconfirmed', 'lost', ?, '2026-01-01T00:00:00Z')
            """,
            (sid, sid),
        )
        conn.commit()
    capture.jobs[sid] = {
        "id": sid,
        "snapshot_id": sid,
        "status": "unconfirmed",
        "error": "lost",
        "url": "https://example.com/a",
        "final_url": None,
        "title": None,
        "resources": [],
        "capture_stats": {},
    }
    try:
        with TestClient(app) as client:
            first = client.get(f"/api/jobs/{sid}").json()
            assert first["status"] == "unconfirmed"
            assert scratch.is_dir()
            conn = sqlite3.connect(ingest_paths["db_path"])
            conn.execute(
                """
                INSERT INTO snapshots (
                    id, url, url_normalized, title, created_at, status, paywalled
                ) VALUES (?, 'https://example.com/a', 'https://example.com/a',
                          'Landed', '2026-01-02T00:00:00Z', 'complete', 0)
                """,
                (sid,),
            )
            conn.commit()
            conn.close()
            second = client.get(f"/api/jobs/{sid}").json()
            assert second["status"] == "complete"
        assert not scratch.exists()
    finally:
        capture.jobs.pop(sid, None)


def test_worker_records_an_unconfirmed_push(ingest_paths, monkeypatch):
    import asyncio

    from app import capture
    from app.library_import import UnconfirmedPush

    sid = "Z6792"
    db._init_app_state()
    db.prepare_scratch()
    db.begin_capture(sid, "https://example.com/a", "https://example.com/a")
    capture.jobs[sid] = {
        "id": sid,
        "snapshot_id": sid,
        "status": "capturing",
        "url": "https://example.com/a",
        "resources": [],
        "capture_stats": {},
    }
    capture.job_queue = asyncio.Queue()

    async def explode(_job_id: str) -> None:
        raise UnconfirmedPush(sid)

    monkeypatch.setattr(capture, "run_job", explode)

    async def go() -> None:
        await capture.job_queue.put(sid)
        task = asyncio.create_task(capture.worker())
        await capture.job_queue.join()
        task.cancel()

    try:
        asyncio.run(go())
        assert capture.jobs[sid]["status"] == "unconfirmed"
        row = db._job_row(sid)
        assert row["status"] == "unconfirmed"
    finally:
        capture.jobs.pop(sid, None)
