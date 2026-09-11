"""Article-mode toolbar exposes a top-level reader URL for Chrome Reader Mode."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import app

_OPEN_READER = re.compile(r'<a\b([^>]*\bclass="open-reader"[^>]*)>', re.IGNORECASE)


def _complete_sid() -> str:
    from app import db

    sid = db.allocate_id()
    url = "https://example.com/story"
    db.insert_snapshot(sid, url, url)
    db.update_snapshot(sid, status="complete", title="Story")
    return sid


def _open_reader_attrs(html: str) -> dict[str, str] | None:
    match = _OPEN_READER.search(html)
    if not match:
        return None
    return dict(re.findall(r'([a-zA-Z_:][\w:.-]*)="([^"]*)"', match.group(1)))


def test_open_reader_control_on_article_mode_only(tmp_data):
    sid = _complete_sid()
    with TestClient(app) as client:
        article = client.get(f"/{sid}")
        webpage = client.get(f"/{sid}/webpage")
        screenshot = client.get(f"/{sid}/screenshot")

    assert article.status_code == 200
    attrs = _open_reader_attrs(article.text)
    assert attrs is not None
    assert attrs["href"] == f"/{sid}/reader"
    assert attrs.get("target") == "_blank"
    assert "noopener" in attrs.get("rel", "")
    assert ">open reader<" in article.text
    # Viewer still isolates the extract; the control is extra navigation.
    assert f'src="/{sid}/reader"' in article.text

    assert webpage.status_code == 200
    assert _open_reader_attrs(webpage.text) is None
    assert screenshot.status_code == 200
    assert _open_reader_attrs(screenshot.text) is None
