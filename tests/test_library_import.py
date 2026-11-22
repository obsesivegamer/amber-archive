"""Dell-PLX import worker: freeze in scratch, commit only through ingest."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db, library_import
from app.library_contract import ID_RE, SNAPSHOT_COLUMNS
from app.library_import import ImportHooks, create_app
from app.library_push import PushError
from tests.test_library_push import SID, TOKEN, URL, boot, reader_rows, write_mac

IMPORT_TOKEN = "test-import-token"
ARTICLE_URL = "https://example.com/council-bridge-2026"
_CREATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def article_html(title: str = "Council funds the river bridge") -> str:
    sentence = "The council voted to fund a bridge that stays open in the rain. "
    return (
        "<!doctype html><html><head>"
        f"<title>{title}</title>"
        '<meta property="og:title" content="' + title + '">'
        '<meta property="og:site_name" content="The Daily Test">'
        "</head><body><article>"
        f"<h1>{title}</h1><p>{sentence * 40}</p>"
        "</article></body></html>"
    )


class HttpxTransport:
    """Sync client for the ingest app.

    httpx.ASGITransport in this httpx only implements the async request
    path. The worker pushes from a thread, so this uses the same portal
    TestClient the existing ingest tests use, on its own thread.
    """

    def __init__(self, app):
        self.client = TestClient(app)
        self.client.__enter__()
        self.calls: list[tuple[str, str]] = []

    def round_trip(self, method, url, body, headers):
        path = urlparse(url).path
        self.calls.append((method, path))
        response = self.client.request(method, path, content=body, headers=headers)
        if not response.content:
            return response.status_code, {}
        payload = response.json()
        if not isinstance(payload, dict):
            raise PushError("ingest returned a body that is not a JSON object")
        return response.status_code, payload

    def close(self):
        self.client.__exit__(None, None, None)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    saved = (
        config.DATA_DIR,
        config.SNAPS_DIR,
        config.DB_PATH,
        db.DATA_DIR,
        db.SNAPS_DIR,
        db.DB_PATH,
    )
    monkeypatch.setattr(
        "app.security.host_is_public",
        lambda host: host not in {"127.0.0.1", "localhost"},
    )
    library, _token_path, ingest = boot(tmp_path)
    transport = HttpxTransport(ingest)
    app = create_app(
        library_dir=library,
        scratch_dir=tmp_path / "scratch",
        state_dir=tmp_path / "state",
        import_token=IMPORT_TOKEN,
        ingest_token=TOKEN,
        ingest_url="http://ingest",
        transport=transport,
        thumbnails=False,
    )
    try:
        yield app, library, transport
    finally:
        transport.close()
        config.DATA_DIR, config.SNAPS_DIR, config.DB_PATH = saved[:3]
        db.DATA_DIR, db.SNAPS_DIR, db.DB_PATH = saved[3:]
        library_import._reserved.clear()
        from app import import_child

        import_child._failures.clear()
        import_child._blocked_until = 0.0


def _post(client, html=None, url=ARTICLE_URL, token=IMPORT_TOKEN, **fields):
    body = {"url": url, "html": article_html() if html is None else html, **fields}
    headers = {"Authorization": f"Bearer {token}"}
    return client.post("/api/import", json=body, headers=headers)


def _ids(library: Path) -> list[str]:
    return [row["id"] for row in reader_rows(library)]


def test_worker_does_not_reference_an_in_process_commit():
    source = Path(library_import.__file__).read_text(encoding="utf-8")
    assert "commit_document(" not in source
    assert "from .library_ingest" not in source
    assert "import app.library_ingest" not in source


def test_healthz_creates_nothing(worker):
    app, library, _transport = worker
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert _ids(library) == []


def test_import_commits_through_ingest_and_the_reader_can_list_it(worker):
    app, library, transport = worker
    scratch = app.state.scratch_dir
    opens: list[tuple[str, tuple[str, ...]]] = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        files = tuple(
            frame.filename
            for frame in traceback.extract_stack()
            if frame.filename.endswith(("library_import.py", "library_ingest.py", "db.py"))
        )
        opens.append((str(target), files))
        return real_connect(target, *args, **kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sqlite3, "connect", spy)
    try:
        with TestClient(app) as client:
            response = _post(client)
    finally:
        monkey.undo()
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["id"].startswith("Z")
    assert ID_RE.fullmatch(body["id"])
    assert ("POST", "/v1/revisions") in transport.calls
    assert any(method == "PUT" and path.startswith("/v1/blobs/") for method, path in transport.calls)
    assert _ids(library) == [body["id"]]
    row = reader_rows(library)[0]
    assert row["status"] == "complete"
    assert row["url"] == ARTICLE_URL
    assert _CREATED_AT.fullmatch(row["created_at"])
    assert set(row) >= set(SNAPSHOT_COLUMNS)
    snap = library / "snaps" / body["id"]
    assert (snap / "meta.json").is_file()
    assert (snap / "article.txt").is_file()
    assert not (scratch / "snaps" / body["id"]).exists()
    assert (app.state.state_dir / "budget.json").is_file()
    assert not (library / "budget.json").exists()
    library_db = (library / "amber.sqlite3").resolve()
    library_from_worker = []
    for target, files in opens:
        if any(name.endswith("library_ingest.py") for name in files):
            continue
        if not any(name.endswith("library_import.py") for name in files):
            continue
        is_library = "mode=ro" in target or (
            not target.startswith("file:") and Path(target).resolve() == library_db
        )
        if is_library:
            library_from_worker.append(target)
    assert library_from_worker, "dedup and allocation must open the library"
    assert all("mode=ro" in target for target in library_from_worker)
    for target, files in opens:
        if any(name.endswith("library_ingest.py") for name in files):
            continue
        if target.startswith("file:"):
            continue
        assert Path(target).resolve() != library_db


def test_second_post_is_idempotent(worker):
    app, library, _transport = worker
    with TestClient(app) as client:
        first = _post(client)
        second = _post(client)
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json() == {"detail": "already archived", "id": first.json()["id"]}
    assert _ids(library) == [first.json()["id"]]


def test_concurrent_posts_of_one_url_produce_one_id(worker):
    app, library, _transport = worker

    async def both():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://import",
            timeout=60,
        ) as client:
            import asyncio

            one, two = await asyncio.gather(
                client.post(
                    "/api/import",
                    json={"url": ARTICLE_URL, "html": article_html()},
                    headers={"Authorization": f"Bearer {IMPORT_TOKEN}"},
                ),
                client.post(
                    "/api/import",
                    json={"url": ARTICLE_URL, "html": article_html()},
                    headers={"Authorization": f"Bearer {IMPORT_TOKEN}"},
                ),
            )
            return one, two

    import asyncio

    one, two = asyncio.run(both())
    statuses = sorted([one.status_code, two.status_code])
    assert statuses == [201, 409]
    bodies = [one.json(), two.json()]
    created = next(item for item in bodies if "status" in item)
    rejected = next(item for item in bodies if item.get("detail") == "already archived")
    assert rejected["id"] == created["id"]
    assert _ids(library) == [created["id"]]


def test_mac_url_already_in_the_library_returns_that_id(worker, monkeypatch):
    """Dedup reads the shared library, not the scratch database."""
    app, library, _transport = worker
    mac = library.parent / "mac-seed"
    write_mac(mac)
    words = " ".join(["bridge"] * 180)
    snap = mac / "snaps" / SID
    (snap / "article.txt").write_text(words + "\n", encoding="utf-8")
    (snap / "article.html").write_text(f"<p>{words}</p>\n", encoding="utf-8")
    from app.library_push import push_snapshot

    # The worker's transport talks to ingest. Reuse it so the Mac id is really stored.
    result = push_snapshot(mac, SID, "http://ingest", TOKEN, transport=app.state.transport)
    assert result.status == 201
    library_opens: list[str] = []
    real = library_import.sqlite3.connect

    def spy(target, *args, **kwargs):
        library_opens.append(str(target))
        return real(target, *args, **kwargs)

    monkeypatch.setattr("app.library_import.sqlite3.connect", spy)
    with TestClient(app) as client:
        response = _post(client, url=URL)
    assert response.status_code == 409
    assert response.json() == {"detail": "already archived", "id": SID}
    assert _ids(library) == [SID]
    assert library_opens
    assert all("mode=ro" in item for item in library_opens)
    assert not any(item.startswith("Z") for item in _ids(library))


def test_short_mac_teaser_does_not_block_a_full_import(worker):
    """A Mac teaser of the same URL is not already archived.

    Gift Links treats 409 as success and then drops the HTML. The stock short
    fixture for https://www.ft.com/content/abc (K7mQp) must not eat a later
    full article.
    """
    app, library, _transport = worker
    mac = library.parent / "mac-teaser"
    write_mac(mac)
    from app.library_push import push_snapshot

    result = push_snapshot(mac, SID, "http://ingest", TOKEN, transport=app.state.transport)
    assert result.status == 201
    with TestClient(app) as client:
        response = _post(client, url=URL)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["usable"] is True
    assert body["id"].startswith("Z")
    assert body["id"] != SID
    assert set(_ids(library)) == {SID, body["id"]}


def test_crash_before_push_then_retry_commits_one_id(tmp_path, monkeypatch):
    _assert_one_commit_after_crash(tmp_path, monkeypatch, where="before_push")


def test_crash_after_blobs_before_revision_then_retry_commits_one_id(tmp_path, monkeypatch):
    _assert_one_commit_after_crash(tmp_path, monkeypatch, where="revision")


def test_crash_after_commit_before_response_retries_as_already_archived(tmp_path, monkeypatch):
    _assert_one_commit_after_crash(tmp_path, monkeypatch, where="after_commit")


def _assert_one_commit_after_crash(tmp_path, monkeypatch, *, where: str):
    monkeypatch.setattr(
        "app.security.host_is_public",
        lambda host: host not in {"127.0.0.1", "localhost"},
    )
    saved = (
        config.DATA_DIR,
        config.SNAPS_DIR,
        config.DB_PATH,
        db.DATA_DIR,
        db.SNAPS_DIR,
        db.DB_PATH,
    )
    library, _token_path, ingest = boot(tmp_path)
    inner = HttpxTransport(ingest)
    state = {"crash": where}

    class Crash:
        def __init__(self):
            self.crashed_id = None

        def round_trip(self, method, url, body, headers):
            path = urlparse(url).path
            if method == "POST" and path == "/v1/revisions" and state["crash"] in {
                "revision",
                "after_commit",
            }:
                self.crashed_id = json.loads(body)["id"]
                if state["crash"] == "revision":
                    state["crash"] = ""
                    raise ConnectionError("killed after blobs")
                inner.round_trip(method, url, body, headers)
                state["crash"] = ""
                raise ConnectionError("killed after commit")
            return inner.round_trip(method, url, body, headers)

    crash = Crash()

    def before_push():
        if state["crash"] == "before_push":
            state["crash"] = ""
            raise ConnectionError("killed before push")

    try:
        app = create_app(
            library_dir=library,
            scratch_dir=tmp_path / "scratch",
            state_dir=tmp_path / "state",
            import_token=IMPORT_TOKEN,
            ingest_token=TOKEN,
            ingest_url="http://ingest",
            transport=crash,
            hooks=ImportHooks(before_push=before_push),
            thumbnails=False,
        )
        with TestClient(app) as client:
            failed = _post(client)
            assert failed.status_code == 503
            if where == "after_commit":
                assert _ids(library) == [crash.crashed_id]
            else:
                assert _ids(library) == []
                if crash.crashed_id:
                    assert not (library / "snaps" / crash.crashed_id).exists()
            again = _post(client)
        if where == "after_commit":
            assert again.status_code == 409
            assert again.json()["id"] == crash.crashed_id
            assert _ids(library) == [crash.crashed_id]
        else:
            assert again.status_code == 201, again.text
            assert _ids(library) == [again.json()["id"]]
            assert again.json()["id"].startswith("Z")
            if crash.crashed_id and crash.crashed_id != again.json()["id"]:
                assert not (library / "snaps" / crash.crashed_id).exists()
    finally:
        inner.close()
        config.DATA_DIR, config.SNAPS_DIR, config.DB_PATH = saved[:3]
        db.DATA_DIR, db.SNAPS_DIR, db.DB_PATH = saved[3:]
        library_import._reserved.clear()


def test_ingest_409_on_a_new_id_reallocates(worker):
    app, library, transport = worker
    state = {"posts": 0, "first": None}
    original = transport.round_trip

    def round_trip(method, url, body, headers):
        path = urlparse(url).path
        if method == "POST" and path == "/v1/revisions":
            state["posts"] += 1
            if state["posts"] == 1:
                state["first"] = json.loads(body)["id"]
                return 409, {"detail": "revision is not stored + 1", "revision": 1}
        return original(method, url, body, headers)

    transport.round_trip = round_trip
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 201, response.text
    assert state["posts"] >= 2
    assert state["first"].startswith("Z")
    assert _ids(library) == [response.json()["id"]]
    if state["first"] != response.json()["id"]:
        assert not (library / "snaps" / state["first"]).exists()


def test_token_scope(worker):
    app, library, _transport = worker
    with TestClient(app) as client:
        missing = client.post(
            "/api/import",
            json={"url": ARTICLE_URL, "html": article_html()},
        )
        assert missing.status_code == 401
        assert missing.json() == {"detail": "unauthorized"}
        cookie = client.post(
            "/api/import",
            json={"url": ARTICLE_URL, "html": "x"},
            headers={"Cookie": "session=reader"},
        )
        assert cookie.status_code == 401
        ingest_token = client.post(
            "/api/import",
            json={"html": "short"},
            headers={"Authorization": f"Bearer {TOKEN}", "Cookie": "session=reader"},
        )
        assert ingest_token.status_code == 401
        authed_short = client.post(
            "/api/import",
            json={"url": ARTICLE_URL, "html": "short"},
            headers={
                "Authorization": f"Bearer {IMPORT_TOKEN}",
                "Cookie": "session=ignored",
            },
        )
        assert authed_short.status_code == 400
        assert "access-control-allow-origin" not in {
            key.lower() for key in authed_short.headers
        }
    status, payload = app.state.transport.round_trip(
        "PUT",
        "http://ingest/v1/blobs/" + "a" * 64,
        b"nope",
        {"Authorization": f"Bearer {IMPORT_TOKEN}", "Content-Length": "4"},
    )
    assert status == 401
    assert payload.get("detail") == "unauthorized"
    assert _ids(library) == []


def test_status_strings_match_the_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.security.host_is_public",
        lambda host: host not in {"127.0.0.1", "localhost"},
    )
    monkeypatch.setattr("app.api.reserve_thumbnail", lambda: False)
    monkeypatch.delenv("AMBER_INGEST_URL", raising=False)
    monkeypatch.delenv("AMBER_INGEST_TOKEN_FILE", raising=False)
    saved = (
        config.DATA_DIR,
        config.SNAPS_DIR,
        config.DB_PATH,
        db.DATA_DIR,
        db.SNAPS_DIR,
        db.DB_PATH,
    )
    mac_dir = tmp_path / "mac"
    mac_dir.mkdir()
    config.DATA_DIR = mac_dir
    config.SNAPS_DIR = mac_dir / "snaps"
    config.DB_PATH = mac_dir / "amber.sqlite3"
    db.DATA_DIR = mac_dir
    db.SNAPS_DIR = mac_dir / "snaps"
    db.DB_PATH = mac_dir / "amber.sqlite3"

    from app.main import app as mac_app

    cases = [
        ("text", None, 415),
        ("json", {"url": ARTICLE_URL}, 400),
        ("json", {"url": ARTICLE_URL, "html": "short"}, 400),
        ("json", {"url": ARTICLE_URL, "html": article_html(), "title": 1}, 400),
        ("json", {"url": ARTICLE_URL, "html": article_html(), "force": "yes"}, 400),
        (
            "json",
            {
                "url": ARTICLE_URL,
                "html": article_html(),
                "final_url": "http://127.0.0.1/secret",
            },
            400,
        ),
        (
            "json",
            {
                "url": ARTICLE_URL,
                "html": article_html(),
                "final_url": "https://other.example/elsewhere",
            },
            400,
        ),
    ]

    def run(client, token):
        found = []
        for kind, body, _expected in cases:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            if kind == "text":
                response = client.post(
                    "/api/import",
                    content=b"nope",
                    headers={**headers, "Content-Type": "text/plain"},
                )
            else:
                response = client.post("/api/import", json=body, headers=headers)
            found.append((response.status_code, response.json()))
        ok = client.post(
            "/api/import",
            json={"url": ARTICLE_URL, "html": article_html()},
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        again = client.post(
            "/api/import",
            json={"url": ARTICLE_URL, "html": article_html()},
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        return found, ok, again

    try:
        with TestClient(mac_app) as mac:
            mac_errors, mac_ok, mac_again = run(mac, "")
        library, _token_path, ingest = boot(tmp_path)
        transport = HttpxTransport(ingest)
        worker_app = create_app(
            library_dir=library,
            scratch_dir=tmp_path / "scratch",
            state_dir=tmp_path / "state",
            import_token=IMPORT_TOKEN,
            ingest_token=TOKEN,
            ingest_url="http://ingest",
            transport=transport,
            thumbnails=False,
        )
        with TestClient(worker_app) as client:
            worker_errors, worker_ok, worker_again = run(client, IMPORT_TOKEN)
    finally:
        config.DATA_DIR, config.SNAPS_DIR, config.DB_PATH = saved[:3]
        db.DATA_DIR, db.SNAPS_DIR, db.DB_PATH = saved[3:]
        library_import._reserved.clear()

    assert mac_errors == worker_errors
    details = [item[1]["detail"] for item in mac_errors]
    assert "final_url is a private or local address" in details
    assert "final_url is a different site" in details
    assert mac_ok.status_code == 201 and worker_ok.status_code == 201
    assert mac_again.status_code == worker_again.status_code == 409
    assert mac_again.json()["detail"] == worker_again.json()["detail"] == "already archived"
    assert set(mac_again.json()) == set(worker_again.json()) == {"detail", "id"}
    assert not mac_ok.json()["id"].startswith("Z")
    assert worker_ok.json()["id"].startswith("Z")


def test_created_at_matches_a_mac_import(worker):
    app, library, _transport = worker
    html_path = library.parent / "article.html"
    html_path.write_text(article_html(), encoding="utf-8")
    mac_dir = library.parent / "mac-format"
    mac_dir.mkdir()
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["AMBER_DATA_DIR"] = str(mac_dir)
    env["AMBER_FIXTURE"] = str(html_path)
    env["AMBER_URL"] = ARTICLE_URL
    env["PYTHONPATH"] = os.pathsep.join([str(repo), env.get("PYTHONPATH", "")])
    code = (
        "import json, os\n"
        "from app.ingest import ingest_html\n"
        "from app import db\n"
        "db.init_db()\n"
        "html = open(os.environ['AMBER_FIXTURE'], encoding='utf-8').read()\n"
        "url = os.environ['AMBER_URL']\n"
        "sid = ingest_html(html, url, title_hint='', final_url=url)\n"
        "snap = db.get_snapshot(sid)\n"
        "print(json.dumps({\n"
        "  'id': sid,\n"
        "  'created_at': snap['created_at'],\n"
        "  'title': snap['title'],\n"
        "  'word_count': snap['word_count'],\n"
        "  'status': snap['status'],\n"
        "  'url': snap['url'],\n"
        "}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    mac = json.loads(proc.stdout)
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 201, response.text
    row = reader_rows(library)[0]
    assert not mac["id"].startswith("Z")
    assert row["id"].startswith("Z")
    assert _CREATED_AT.fullmatch(mac["created_at"])
    assert _CREATED_AT.fullmatch(row["created_at"])
    assert row["title"] == mac["title"]
    assert row["word_count"] == mac["word_count"]
    assert row["status"] == mac["status"] == "complete"
    assert row["url"] == mac["url"]


def test_complex_page_matches_the_mac_422(worker, monkeypatch):
    monkeypatch.setattr("app.api.reserve_thumbnail", lambda: False)
    html = "<" * 60_001
    from app.main import app as mac_app

    # Point the Mac app at a throwaway dir for this one request's validation,
    # which fails before allocate. The worker fixture already bound scratch.
    with TestClient(mac_app) as mac:
        mac_response = mac.post(
            "/api/import",
            json={"url": ARTICLE_URL, "html": html},
        )
    with TestClient(worker[0]) as client:
        worker_response = _post(client, html=html)
    assert mac_response.status_code == worker_response.status_code == 422
    assert mac_response.json() == worker_response.json()
    assert mac_response.json()["detail"].startswith("Page too complex to import:")


def test_nested_scratch_is_refused(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    with pytest.raises(SystemExit, match="separate"):
        create_app(
            library_dir=library,
            scratch_dir=library / "scratch",
            state_dir=tmp_path / "state",
            import_token=IMPORT_TOKEN,
            ingest_token=TOKEN,
        )


def test_equal_tokens_are_refused(tmp_path):
    with pytest.raises(SystemExit, match="must differ"):
        create_app(
            library_dir=tmp_path / "library",
            scratch_dir=tmp_path / "scratch",
            state_dir=tmp_path / "state",
            import_token="same-token",
            ingest_token="same-token",
        )


def test_worker_refuses_a_non_loopback_host(monkeypatch):
    monkeypatch.setenv("AMBER_IMPORT_HOST", "0.0.0.0")
    with pytest.raises(SystemExit, match="127.0.0.1"):
        library_import.main()


def test_worker_refuses_the_reader_and_ingest_ports(monkeypatch):
    monkeypatch.setenv("AMBER_IMPORT_HOST", "127.0.0.1")
    monkeypatch.setenv("AMBER_IMPORT_PORT", "8081")
    with pytest.raises(SystemExit, match="8081"):
        library_import.main()


def test_worker_refuses_to_start_without_the_library_database(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    with pytest.raises(SystemExit, match="missing"):
        create_app(
            library_dir=library,
            scratch_dir=tmp_path / "scratch",
            state_dir=tmp_path / "state",
            import_token=IMPORT_TOKEN,
            ingest_token=TOKEN,
        )


def _short_html() -> str:
    words = " ".join(["note"] * 80)
    return (
        "<!doctype html><html><head><title>A short note</title></head>"
        f"<body><article><h1>A short note</h1><p>{words}</p></article></body></html>"
    )


def test_short_page_retry_keeps_one_id(worker):
    app, library, _transport = worker
    html = _short_html()
    with TestClient(app) as client:
        first = _post(client, html=html)
        second = _post(client, html=html)
    assert first.status_code == 201, first.text
    assert first.json()["usable"] is False
    assert second.status_code == 409
    assert second.json() == {"detail": "already archived", "id": first.json()["id"]}
    assert _ids(library) == [first.json()["id"]]


def test_a_larger_body_after_a_short_commit_is_a_new_id(worker):
    app, library, _transport = worker
    with TestClient(app) as client:
        short = _post(client, html=_short_html())
        full = _post(client)
    assert short.status_code == 201, short.text
    assert full.status_code == 201, full.text
    assert full.json()["id"] != short.json()["id"]
    assert full.json()["usable"] is True
    assert set(_ids(library)) == {short.json()["id"], full.json()["id"]}


def test_library_read_error_is_retryable_and_stores_nothing(worker, monkeypatch):
    def boom(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("app.library_import.open_library_ro", boom)
    app, library, _transport = worker
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 503
    assert response.json()["detail"] == "Could not read the library."
    assert _ids(library) == []
    assert not (app.state.state_dir / "budget.json").exists()


def test_allocate_database_error_is_503_and_refunds_the_budget(worker, monkeypatch):
    def boom(_library_dir, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("app.library_import.allocate_dell_id", boom)
    app, library, _transport = worker
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 503
    assert response.json()["detail"] == "Could not allocate a snapshot id."
    assert _ids(library) == []
    ledger = json.loads((app.state.state_dir / "budget.json").read_text(encoding="utf-8"))
    assert ledger == []


def test_ingest_outage_refunds_the_budget(worker):
    app, library, transport = worker

    def round_trip(_method, _url, _body, _headers):
        raise ConnectionError("ingest down")

    transport.round_trip = round_trip
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 503
    assert response.json()["detail"] == "Library ingest is unavailable."
    ledger = json.loads((app.state.state_dir / "budget.json").read_text(encoding="utf-8"))
    assert ledger == []
    assert _ids(library) == []


def test_ingest_400_is_a_permanent_422(worker):
    app, library, transport = worker
    original = transport.round_trip

    def round_trip(method, url, body, headers):
        if method == "POST" and urlparse(url).path == "/v1/revisions":
            return 400, {"detail": "unknown field"}
        return original(method, url, body, headers)

    transport.round_trip = round_trip
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 422
    assert response.json()["detail"] == "Library ingest rejected the snapshot."
    assert _ids(library) == []


def test_ingest_401_is_a_retryable_503(worker):
    app, library, transport = worker
    original = transport.round_trip

    def round_trip(method, url, body, headers):
        if method == "POST" and urlparse(url).path == "/v1/revisions":
            return 401, {"detail": "unauthorized"}
        return original(method, url, body, headers)

    transport.round_trip = round_trip
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 503
    assert response.json()["detail"] == "Library ingest is unavailable."
    ledger = json.loads((app.state.state_dir / "budget.json").read_text(encoding="utf-8"))
    assert ledger == []
    assert _ids(library) == []
