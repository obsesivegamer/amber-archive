"""Article-mode toolbar exposes a top-level reader URL for Chrome Reader Mode."""

from __future__ import annotations

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.main import app


def _complete_sid() -> str:
    from app import db

    sid = db.allocate_id()
    url = "https://example.com/story"
    db.insert_snapshot(sid, url, url)
    db.update_snapshot(sid, status="complete", title="Story")
    return sid


def _open_reader(html: str):
    return BeautifulSoup(html, "lxml").select_one("a.open-reader")


def test_open_reader_control_on_article_mode_only(tmp_data):
    sid = _complete_sid()
    with TestClient(app) as client:
        article = client.get(f"/{sid}")
        webpage = client.get(f"/{sid}/webpage")
        screenshot = client.get(f"/{sid}/screenshot")

    assert article.status_code == 200
    link = _open_reader(article.text)
    assert link is not None
    assert link["href"] == f"/{sid}/reader"
    assert link.get("target") == "_blank"
    assert "noopener" in link.get("rel", [])
    assert link.get_text(strip=True) == "open reader"
    # Viewer still isolates the extract; the control is extra navigation.
    assert f'src="/{sid}/reader"' in article.text

    assert webpage.status_code == 200
    assert _open_reader(webpage.text) is None
    assert screenshot.status_code == 200
    assert _open_reader(screenshot.text) is None
