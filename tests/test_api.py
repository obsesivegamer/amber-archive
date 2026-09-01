from fastapi.testclient import TestClient

from app.main import app, mime_for_filename


def test_homepage_and_about_render():
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "time capsule" in home.text.lower()
        about = client.get("/about")
        assert about.status_code == 200


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
