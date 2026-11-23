"""Auth, app-state stores, and the routes the phone web build calls."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
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


def _ingest_env(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    db_path = library / "amber.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(db.SCHEMA)
    conn.commit()
    conn.close()
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
    monkeypatch.setattr(db, "_access", None)
    monkeypatch.setattr(db, "_writing_scratch", False)
    return library, db_path


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


def _fill(path: str) -> str:
    return (
        path.replace("{sid}", "Z2345")
        .replace("{job_id}", "Z2345")
        .replace("{cid}", "abcdefgh")
        .replace("{filename}", "site.css")
    )


def test_unauthenticated_routes_reject_loopback_and_forwarded_headers(authed):
    from starlette.routing import Mount

    from app.main import app

    forwarded = {
        "X-Forwarded-For": "10.0.0.8",
        "X-Forwarded-Proto": "https",
        "Tailscale-User-Login": "jerem@example.com",
        "Tailscale-User-Name": "jerem",
    }
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        targets: list[tuple[str, str]] = []
        for route in app.routes:
            path = getattr(route, "path", None)
            if not path:
                continue
            if isinstance(route, Mount):
                targets.append(("GET", path))
                continue
            for method in getattr(route, "methods", None) or set():
                if method in {"HEAD", "OPTIONS"}:
                    continue
                targets.append((method, _fill(path)))
        assert any(path == "/Z2345" for _method, path in targets)
        assert any(path == "/Z2345/article.txt" for _method, path in targets)
        for headers in ({}, forwarded):
            for method, path in targets:
                if path in {"/login", "/healthz", "/favicon.ico"} or path.startswith("/static"):
                    continue
                response = client.request(
                    method, path, headers=headers, follow_redirects=False
                )
                assert response.status_code in {401, 302, 303}, (
                    method,
                    path,
                    headers,
                    response.status_code,
                )
        page = client.get("/saved", headers={"accept": "text/html"}, follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"].startswith("/login")
        assert client.get("/healthz").status_code == 200
        assert client.get("/login").status_code == 200


def test_login_sets_the_amber_reader_cookie(authed):
    import python_multipart  # noqa: F401  required by Form()

    from app.main import app

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        wrong = client.post(
            "/login",
            data={"password": "nope", "next": "/saved"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert wrong.status_code == 401
        assert "amber_session" not in wrong.cookies
        assert "set-cookie" not in {name.lower() for name in wrong.headers}
        opened = client.post(
            "/login",
            data={"password": PASSWORD, "next": "//evil.example"},
            headers={"origin": "http://testserver", "x-forwarded-proto": "https"},
            follow_redirects=False,
        )
        assert opened.status_code == 303
        assert opened.headers["location"] == "/saved"
        issued = opened.cookies.get(auth.COOKIE_NAME)
        assert issued == _reader_cookie() or _signed(issued)
        cookie_header = opened.headers["set-cookie"].lower()
        assert "httponly" in cookie_header
        assert "samesite=lax" in cookie_header
        assert "secure" in cookie_header
        client.cookies.set(auth.COOKIE_NAME, issued)
        assert client.get("/saved").status_code == 200
        gone = client.post("/logout", follow_redirects=False)
        assert gone.status_code == 303
        assert "amber_session" not in gone.cookies or gone.cookies.get(auth.COOKIE_NAME) in {None, ""}


def _signed(cookie: str) -> bool:
    issued, _, sig = cookie.partition(".")
    key = hashlib.sha256(b"amber-reader-session-v1|" + PASSWORD.encode()).digest()
    expected = hmac.new(key, f"v1|{issued}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


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
        ("POST", "/api/save"),
        ("POST", "/api/import"),
        ("POST", "/api/candidates/abcdefgh/save"),
        ("POST", "/api/candidates/abcdefgh/dismiss"),
        ("GET", "/api/feedproxy?url=https://example.com/rss.xml"),
        ("PUT", "/api/homepage"),
        ("PUT", "/api/sources"),
    ]
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        client.cookies.set(auth.COOKIE_NAME, _reader_cookie())
        for method, path in calls:
            response = client.request(method, path, follow_redirects=False)
            assert response.status_code != 405, (method, path, response.status_code)
            if response.status_code == 404:
                try:
                    detail = response.json().get("detail", "")
                except ValueError:
                    detail = response.text
                assert detail != "Not Found", (method, path, detail)
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
    monkeypatch.setattr(db, "_access", None)
    monkeypatch.setattr(db, "_writing_scratch", False)
    raw = json.dumps({"candidates": [FIND]})
    assert import_exact(raw) == 0
    first = export_exact()
    exported = json.loads(first)
    columns = set(exported["candidates"][0])
    import sqlite3

    conn = sqlite3.connect(tmp_path / "app-state" / "app.sqlite3")
    try:
        schema = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    finally:
        conn.close()
    assert columns == schema
    monkeypatch.setenv("AMBER_APP_STATE", str(tmp_path / "fresh"))
    assert import_exact(first) == 0
    assert export_exact() == first
    assert import_exact(first) == 1


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


def test_candidates_closes_the_state_connection_when_the_library_will_not_open(tmp_path, monkeypatch):
    import sqlite3

    from app.candidates import get_candidates_payload
    from app.db import LibraryUnavailable

    library = tmp_path / "library"
    library.mkdir()
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
    monkeypatch.setattr(db, "_access", True)
    monkeypatch.setattr(db, "_writing_scratch", False)
    opened = {}
    real = db.candidates_connect

    def wrapped():
        conn = real()
        opened["conn"] = conn
        return conn

    monkeypatch.setattr(db, "candidates_connect", wrapped)

    def refuse():
        raise LibraryUnavailable("The library cannot be opened read-only.")

    monkeypatch.setattr(db, "connect", refuse)
    with pytest.raises(LibraryUnavailable):
        get_candidates_payload("live")
    with pytest.raises(sqlite3.ProgrammingError):
        opened["conn"].execute("SELECT 1")


def test_a_password_with_spaces_survives_the_file(tmp_path, monkeypatch):
    path = tmp_path / "reader.password"
    path.write_text("hello world\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("AMBER_READER_PASSWORD_FILE", str(path))
    monkeypatch.delenv("AMBER_READER_PASSWORD", raising=False)
    auth._password_cache = None
    assert auth.required_password() == "hello world"
    assert auth.required_password() == "hello world"
    path.write_text("hello world again\n", encoding="utf-8")
    # mtime may not move on a fast filesystem; force a new stamp.
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert auth.required_password() == "hello world again"


def test_ingest_get_from_loopback_stays_unauthenticated(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    db_path = library / "amber.sqlite3"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE snapshots (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, url_normalized TEXT NOT NULL,
            final_url TEXT, title TEXT, created_at TEXT NOT NULL, status TEXT NOT NULL,
            paywalled INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO snapshots (id, url, url_normalized, title, created_at, status, paywalled)
        VALUES ('K7mQp', 'https://example.com/a', 'https://example.com/a', 'Shelf',
                '2026-10-09T19:25:47Z', 'complete', 0)
        """
    )
    conn.commit()
    conn.close()
    before = db_path.read_bytes()
    monkeypatch.setenv("AMBER_LIBRARY_MODE", "ingest")
    monkeypatch.setenv("AMBER_DATA_DIR", str(library))
    monkeypatch.setenv("AMBER_APP_STATE", str(tmp_path / "app-state"))
    monkeypatch.setenv("AMBER_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("AMBER_INGEST_URL", "http://127.0.0.1:9")
    token = tmp_path / "token"
    token.write_text("token\n", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("AMBER_INGEST_TOKEN_FILE", str(token))
    monkeypatch.setenv("AMBER_READER_PASSWORD", PASSWORD)
    monkeypatch.setattr("app.db.DATA_DIR", library)
    monkeypatch.setattr("app.db.DB_PATH", db_path)
    monkeypatch.setattr("app.db.SNAPS_DIR", library / "snaps")
    monkeypatch.setattr("app.config.DATA_DIR", library)
    monkeypatch.setattr(db, "_access", None)
    monkeypatch.setattr(db, "_writing_scratch", False)
    from app.main import app

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        denied = client.get("/saved", follow_redirects=False)
        assert denied.status_code in {401, 303}
    assert db_path.read_bytes() == before


def test_candidates_add_writes_app_state(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    db_path = library / "amber.sqlite3"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript(db.SCHEMA)
    conn.commit()
    conn.close()
    before = db_path.read_bytes()
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
    monkeypatch.setattr("app.config.DATA_DIR", library)
    monkeypatch.setattr(db, "_access", None)
    monkeypatch.setattr(db, "_writing_scratch", False)
    from app.candidates import add_finds, dismiss_candidate

    lines = add_finds(
        [
            {
                "unlock_url": "http://93.184.216.34/story",
                "publisher": "nyt",
                "title": "A gift",
                "found_at": "2026-10-09T19:25:47Z",
            }
        ]
    )
    assert lines == ["added"]
    state = sqlite3.connect(tmp_path / "app-state" / "app.sqlite3")
    cid = state.execute("SELECT id FROM candidates").fetchone()[0]
    state.close()
    listed = db.list_gift_candidates()
    assert listed[0]["id"] == cid
    dismiss_candidate(cid)
    again = sqlite3.connect(tmp_path / "app-state" / "app.sqlite3")
    dismissed = again.execute("SELECT dismissed_at FROM candidates WHERE id = ?", (cid,)).fetchone()[0]
    again.close()
    assert dismissed
    assert db_path.read_bytes() == before


def test_recapture_attach_failure_is_a_503(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("AMBER_LIBRARY_MODE", "ingest")
    monkeypatch.setenv("AMBER_DATA_DIR", str(library))
    monkeypatch.setenv("AMBER_APP_STATE", str(tmp_path / "app-state"))
    monkeypatch.setenv("AMBER_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("AMBER_INGEST_URL", "http://127.0.0.1:9")
    token = tmp_path / "token"
    token.write_text("token\n", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("AMBER_INGEST_TOKEN_FILE", str(token))
    missing = library / "missing" / "amber.sqlite3"
    monkeypatch.setattr("app.db.DATA_DIR", library)
    monkeypatch.setattr("app.db.DB_PATH", missing)
    monkeypatch.setattr(db, "_access", True)
    monkeypatch.setattr(db, "_writing_scratch", False)
    with pytest.raises(db.LibraryUnavailable):
        db.recapture_snapshots()


def test_py310_open_failure_uses_the_message():
    class Old(sqlite3.OperationalError):
        @property
        def sqlite_errorcode(self):
            return None

    assert db._ro_open_failed(Old("unable to open database file")) is True
    assert db._ro_open_failed(Old("attempt to write a readonly database")) is True
    assert db._ro_open_failed(Old("disk I/O error")) is False


def test_events_and_save_read_app_state(tmp_path, monkeypatch, capsys):
    import asyncio
    from datetime import datetime, timezone

    from app.candidates import add_finds, events_since, main, save_candidate

    library, db_path = _ingest_env(tmp_path, monkeypatch)
    url = "http://93.184.216.34/story"
    assert add_finds(
        [
            {
                "unlock_url": url,
                "publisher": "nyt",
                "title": "Cities after the storm",
                "found_at": "2026-11-23T00:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        ]
    ) == ["added"]
    cid = db.list_gift_candidates()[0]["id"]
    sid = "Z2345"
    folder = library / "snaps" / sid
    folder.mkdir(parents=True)
    (folder / "article.txt").write_text("city " * 160, encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO snapshots (
            id, url, url_normalized, final_url, title, site_name, created_at,
            word_count, paywalled, status
        ) VALUES (?, ?, ?, ?, 'Cities after the storm', 'Example',
                  '2026-10-09T19:25:47Z', 220, 0, 'complete')
        """,
        (sid, url, url, url),
    )
    conn.commit()
    conn.close()
    library_after_seed = db_path.read_bytes()
    result = asyncio.run(save_candidate(cid))
    assert result.status_code == 200
    events = events_since(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert any(item["event"] == "click" and item["id"] == cid for item in events["events"])
    assert main(["list", "--events", "--since", "2020-01-01T00:00:00Z"]) == 0
    assert cid in capsys.readouterr().out
    state = sqlite3.connect(tmp_path / "app-state" / "app.sqlite3")
    clicked = state.execute(
        "SELECT clicked_at FROM candidates WHERE id = ?", (cid,)
    ).fetchone()[0]
    state.close()
    assert clicked
    assert db_path.read_bytes() == library_after_seed


def test_import_exact_rejects_unknown_fields(tmp_path, monkeypatch):
    from app.candidates import import_exact

    _ingest_env(tmp_path, monkeypatch)
    raw = json.dumps({"candidates": [{**FIND, "not_a_column": 1}]})
    assert import_exact(raw) == 1


def test_restart_fails_an_unconfirmed_job_ingest_does_not_have(tmp_path, monkeypatch):
    _ingest_env(tmp_path, monkeypatch)
    sid = "Z6789"
    db._init_app_state()
    db.prepare_scratch()
    scratch = tmp_path / "scratch" / "snaps" / sid
    scratch.mkdir(parents=True)
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
    monkeypatch.setattr(db, "_ingest_lists_snapshot", lambda _sid: False)
    assert db.fail_interrupted() == 1
    row = db._job_row(sid)
    assert row["status"] == "failed"
    db.sweep_scratch()
    assert not scratch.exists()


def test_restart_keeps_unconfirmed_when_ingest_cannot_be_asked(tmp_path, monkeypatch):
    _ingest_env(tmp_path, monkeypatch)
    sid = "Z6793"
    db._init_app_state()
    db.prepare_scratch()
    scratch = tmp_path / "scratch" / "snaps" / sid
    scratch.mkdir(parents=True)
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
    monkeypatch.setattr(db, "_ingest_lists_snapshot", lambda _sid: None)
    assert db.fail_interrupted() == 0
    assert db._job_row(sid)["status"] == "unconfirmed"
    db.sweep_scratch()
    assert scratch.is_dir()

