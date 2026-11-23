"""Auth, app-state stores, and the routes the phone web build calls."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.config import parse_allowed_hosts

PASSWORD = "test-password"
FIND = {
    "id": "AbCdEf12",
    "unlock_url": "https://example.com/gift",
    "canonical_url": "https://example.com/story",
    "match_key": "example.com/story",
    "publisher": "Example",
    "title": "A story",
    "source_url": "https://example.com/story",
    "source_author": None,
    "found_at": "2026-10-09T19:25:47Z",
    "expires_at": "2099-01-01T00:00:00Z",
    "expires_estimated": True,
    "dismissed_at": None,
    "clicked_at": None,
    "snapshot_id": None,
    "created_at": "2026-10-09T19:25:47Z",
}


def _basic() -> dict[str, str]:
    import base64

    token = base64.b64encode(f"amber:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _reader_cookie() -> str:
    issued = str(int(time.time()))
    key = hashlib.sha256(b"amber-reader-session-v1|" + PASSWORD.encode()).digest()
    sig = hmac.new(key, f"v1|{issued}".encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{sig}"


@pytest.fixture
def authed(tmp_data, monkeypatch):
    monkeypatch.setenv("AMBER_READER_PASSWORD", PASSWORD)
    return tmp_data


def test_allowed_hosts_accept_the_tailnet_name_and_a_port():
    hosts, rejected = parse_allowed_hosts(
        "dell-plx-reader.tailf9612a.ts.net, 127.0.0.1:8084"
    )
    assert hosts == frozenset({"dell-plx-reader.tailf9612a.ts.net", "127.0.0.1"})
    assert rejected == []


def test_unauthenticated_routes_reject_loopback_and_forwarded_headers(authed):
    from app.main import app

    headers = {
        "X-Forwarded-For": "10.0.0.8",
        "X-Forwarded-Proto": "https",
        "Tailscale-User-Login": "jerem@example.com",
        "Tailscale-User-Name": "jerem",
    }
    with TestClient(app) as client:
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if not path or "{" in path:
                continue
            if path in {"/login", "/healthz", "/favicon.ico"} or path.startswith("/static"):
                continue
            for method in methods:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                response = client.request(method, path, headers=headers, follow_redirects=False)
                assert response.status_code in {401, 302, 303}, (method, path, response.status_code)
        page = client.get(
            "/saved", headers={**headers, "accept": "text/html"}, follow_redirects=False
        )
        assert page.status_code == 303
        assert page.headers["location"].startswith("/login")
        assert client.get("/healthz").status_code == 200
        assert client.get("/login").status_code == 200


def test_an_amber_reader_cookie_is_accepted(authed):
    from app.main import app

    with TestClient(app) as client:
        client.cookies.set(auth.COOKIE_NAME, _reader_cookie())
        assert client.get("/saved").status_code == 200
        assert client.get("/healthz").status_code == 200


def test_phone_routes_are_not_missing(authed, tmp_path):
    from app.main import app

    folder = db.snap_dir("Z2345")
    folder.mkdir(parents=True)
    (folder / "article.txt").write_text("hello archive\n", encoding="utf-8")
    (folder / "reader.html").write_text("<p>hello archive</p>", encoding="utf-8")
    (folder / "page.html").write_text("<p>hello archive</p>", encoding="utf-8")
    db.insert_snapshot("Z2345", "https://example.com/a", "https://example.com/a")
    db.update_snapshot("Z2345", status="complete", title="Hello", word_count=40, paywalled=1)
    calls = [
        ("GET", "/api/snapshots?since_days=14&limit=5"),
        ("GET", "/api/snapshots/Z2345/text"),
        ("GET", "/api/lookup?url=https://example.com/a"),
        ("GET", "/api/jobs/Z2345"),
        ("GET", "/api/candidates"),
        ("GET", "/api/homepage"),
        ("GET", "/api/homepage/rebuild-request"),
        ("GET", "/api/sources"),
        ("GET", "/Z2345/article.txt"),
    ]
    with TestClient(app) as client:
        client.cookies.set(auth.COOKIE_NAME, _reader_cookie())
        for method, path in calls:
            response = client.request(method, path)
            if response.status_code == 404:
                detail = ""
                try:
                    detail = response.json().get("detail", "")
                except ValueError:
                    detail = response.text
                assert detail != "Not Found", (path, detail)
        rebuilt = client.post(
            "/api/homepage/rebuild-request",
            json={},
            headers={"content-type": "application/json"},
        )
        assert rebuilt.status_code == 200
        assert rebuilt.json()["requested_at"]
        text = client.get("/Z2345/article.txt")
        assert text.status_code == 200
        assert text.text == "hello archive\n"


def test_candidate_round_trip_is_byte_equal(tmp_path, monkeypatch):
    from app.candidates import export_exact, import_exact

    library = tmp_path / "library"
    library.mkdir()
    (library / "amber.sqlite3").write_bytes(b"")
    monkeypatch.setenv("AMBER_LIBRARY_MODE", "ingest")
    monkeypatch.setenv("AMBER_DATA_DIR", str(library))
    monkeypatch.setenv("AMBER_APP_STATE", str(tmp_path / "app-state"))
    monkeypatch.setenv("AMBER_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("AMBER_INGEST_URL", "http://127.0.0.1:9")
    token = tmp_path / "token"
    token.write_text("token\n", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("AMBER_INGEST_TOKEN_FILE", str(token))
    monkeypatch.setattr("app.db.DATA_DIR", library)
    monkeypatch.setattr("app.db.DB_PATH", library / "amber.sqlite3")
    monkeypatch.setattr("app.config.DATA_DIR", library)
    db._access = None
    db._writing_scratch = False
    raw = json.dumps({"candidates": [FIND]})
    assert import_exact(raw) == 0
    first = export_exact()
    assert import_exact(first) == 1
    assert export_exact() == first
    assert (tmp_path / "app-state" / "app.sqlite3").is_file()


def test_sources_import_refuses_a_non_empty_store_and_put_returns_409(authed, monkeypatch):
    from app.main import app
    from app.sources import import_exact

    monkeypatch.setattr("app.web.store_dir", lambda: authed)
    raw = json.dumps(
        {"sources": [{"id": "one", "name": "One", "feedUrls": ["https://example.com/rss"], "enabled": True}], "updated_at": "2026-10-09T19:25:47.000000Z"}
    )
    assert import_exact(raw, replace=False) == 0
    assert import_exact(raw, replace=False) == 1
    assert import_exact(raw, replace=True) == 0
    with TestClient(app) as client:
        client.cookies.set(auth.COOKIE_NAME, _reader_cookie())
        stale = client.put(
            "/api/sources",
            json={
                "sources": [{"id": "one", "name": "Two", "feedUrls": ["https://example.com/rss"], "enabled": True}],
                "if_updated_at": "2000-01-01T00:00:00.000000Z",
            },
        )
        assert stale.status_code == 409


def test_homepage_rejects_an_older_timestamp(authed):
    from app.main import app

    with TestClient(app) as client:
        client.cookies.set(auth.COOKIE_NAME, _reader_cookie())
        first = client.put(
            "/api/homepage",
            json={"code": "body", "timestamp": "2026-10-09T19:25:47Z"},
        )
        assert first.status_code == 200
        older = client.put(
            "/api/homepage",
            json={"code": "older", "timestamp": "2026-10-01T00:00:00Z"},
        )
        assert older.status_code == 409
        assert client.get("/api/homepage").json()["code"] == "body"


def test_recapture_queue_attaches_the_library_read_only(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    db_path = library / "amber.sqlite3"
    monkeypatch.setenv("AMBER_LIBRARY_MODE", "ingest")
    monkeypatch.setenv("AMBER_DATA_DIR", str(library))
    monkeypatch.setenv("AMBER_APP_STATE", str(tmp_path / "app-state"))
    monkeypatch.setenv("AMBER_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("AMBER_INGEST_URL", "http://127.0.0.1:9")
    token = tmp_path / "token"
    token.write_text("token\n", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("AMBER_INGEST_TOKEN_FILE", str(token))
    monkeypatch.setattr("app.db.DATA_DIR", library)
    monkeypatch.setattr("app.db.DB_PATH", db_path)
    monkeypatch.setattr("app.db.SNAPS_DIR", library / "snaps")
    monkeypatch.setattr("app.config.DATA_DIR", library)
    db._access = None
    db._writing_scratch = False
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE snapshots (
            id TEXT PRIMARY KEY, url TEXT, url_normalized TEXT, final_url TEXT,
            title TEXT, site_name TEXT, author TEXT, published_at TEXT,
            description TEXT, created_at TEXT, http_status INTEGER, word_count INTEGER,
            paywalled INTEGER, status TEXT, error TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO snapshots (
            id, url, url_normalized, title, created_at, status, paywalled
        ) VALUES ('AbC12', 'https://example.com/a', 'https://example.com/a',
                  'Joined', '2026-10-09T19:25:47Z', 'complete', 0)
        """
    )
    conn.commit()
    conn.close()
    db._init_app_state()
    state = sqlite3.connect(tmp_path / "app-state" / "app.sqlite3")
    state.execute(
        """
        INSERT INTO recapture_queue (id, state, updated_at)
        VALUES ('AbC12', 'pending', '2026-10-09T19:25:47Z')
        """
    )
    state.commit()
    state.close()
    rows = db.recapture_snapshots()
    assert [row["id"] for row in rows] == ["AbC12"]
    assert rows[0]["title"] == "Joined"
    assert rows[0]["recapture_state"] == "pending"
