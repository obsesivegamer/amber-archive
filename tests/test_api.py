from fastapi.testclient import TestClient

from app.main import app, fmt_bytes, fmt_duration, mime_for_filename


def test_homepage_and_about_render():
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "time capsule" in home.text.lower()
        about = client.get("/about")
        assert about.status_code == 200


def test_openapi_schema_is_not_served():
    with TestClient(app) as client:
        schema = client.get("/openapi.json")
        assert schema.status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404


def test_save_rejects_localhost():
    with TestClient(app) as client:
        r = client.post("/save", data={"url": "http://127.0.0.1/secret"}, follow_redirects=False)
        assert r.status_code == 303
        assert "error=" in r.headers["location"]


def test_save_rejects_empty():
    with TestClient(app) as client:
        r = client.post("/save", data={"url": ""}, follow_redirects=False)
        assert r.status_code == 303
        assert "Paste" in r.headers["location"] or "error=" in r.headers["location"]


def test_css_mime_is_not_octet_stream_on_windows():
    assert mime_for_filename("app.css") == "text/css"
    assert mime_for_filename("font.woff2") == "font/woff2"
    assert mime_for_filename("hero.jpg") == "image/jpeg"


def test_capture_metric_formatters():
    assert fmt_bytes(0) == "0 B"
    assert fmt_bytes(1024) == "1.0 KB"
    assert fmt_bytes(1024 * 1024) == "1.0 MB"
    assert fmt_duration(0) == "0 ms"
    assert fmt_duration(999) == "999 ms"
    assert fmt_duration(1_000) == "1 s"
    assert fmt_duration(61_250) == "1m 1.2s"


def test_import_html_creates_article_snapshot(tmp_data, monkeypatch):
    from tests.test_extract import ARCHIVE_IS_SAVED

    async def _skip_shot(sid: str) -> None:
        return None

    monkeypatch.setattr("app.main.screenshot_reader", _skip_shot)
    with TestClient(app) as client:
        r = client.post(
            "/import",
            files={"file": ("article.html", ARCHIVE_IS_SAVED, "text/html")},
            follow_redirects=False,
        )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/")
    sid = location.strip("/")
    from app import db

    snap = db.get_snapshot(sid)
    assert snap["status"] == "complete"
    assert "Elon Musk" in (snap.get("title") or "")
    assert snap["author"] == "Grace Kay"


def test_snapshot_labels_original_and_final_urls(tmp_data):
    from app import db

    original = "https://original.example/story?from=bookmark"
    final = "https://final.example/story"
    sid = db.allocate_id()
    db.insert_snapshot(sid, original, original)
    db.update_snapshot(sid, final_url=final, status="complete")
    with TestClient(app) as client:
        page = client.get(f"/{sid}")

    assert page.status_code == 200
    assert "original.example/story?from=bookmark" in page.text
    assert "final.example/story" in page.text
    assert 'class="url-label">original' in page.text
    assert 'class="url-label">final' in page.text


def test_saved_page_lists_all_snapshots(tmp_data, monkeypatch):
    from tests.test_extract import ARCHIVE_IS_SAVED

    async def _skip_shot(sid: str) -> None:
        return None

    monkeypatch.setattr("app.main.screenshot_reader", _skip_shot)
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert 'href="/saved"' in home.text
        client.post(
            "/import",
            files={"file": ("article.html", ARCHIVE_IS_SAVED, "text/html")},
            follow_redirects=False,
        )
        listing = client.get("/saved")
    assert listing.status_code == 200
    assert "Exclusive: Elon Musk" in listing.text
    assert "everything you've saved" in listing.text.lower()


def test_delete_snapshot_removes_row_and_files(tmp_data, monkeypatch):
    from tests.test_extract import ARCHIVE_IS_SAVED

    async def _skip_shot(sid: str) -> None:
        return None

    monkeypatch.setattr("app.main.screenshot_reader", _skip_shot)
    with TestClient(app) as client:
        r = client.post(
            "/import",
            files={"file": ("article.html", ARCHIVE_IS_SAVED, "text/html")},
            follow_redirects=False,
        )
        sid = r.headers["location"].strip("/")
        from app import db

        folder = db.snap_dir(sid)
        assert folder.exists()
        gone = client.post(f"/saved/{sid}/delete", follow_redirects=False)
        assert gone.status_code == 303
        assert gone.headers["location"] == "/saved"
        assert db.get_snapshot(sid) is None
        assert not folder.exists()
        listing = client.get("/saved")
        assert sid not in listing.text
