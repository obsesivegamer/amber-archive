"""Paywalled / incomplete snapshots stay honest in lists and the viewer."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.extract import PAYWALL_WORD_LIMIT
from app.main import app
from tests.test_extract import PAYWALL

FULL_ARTICLE = """
<!doctype html>
<html>
<head>
  <meta property="og:title" content="City Council Approves Bridge After Years of Debate">
  <meta property="og:site_name" content="The Daily Test">
  <meta name="author" content="Jane Reporter">
</head>
<body>
  <article>
    <h1>City Council Approves Bridge After Years of Debate</h1>
    <p>The city council voted last night to fund a new bridge over the river after
    residents spent a decade arguing about traffic, cost, and the view from the
    south bank. Construction starts in May if the weather holds and the steel
    order arrives on time from the mill in the next county.</p>
    <p>Opponents said the money should have gone to buses and safer crossings
    near the school. Supporters answered that freight already clogs the old
    span every morning, and that another winter of patching the deck would
    cost more than starting over. The mayor called the vote a compromise.</p>
    <p>Work crews will close one lane of River Street while they set the
    piers. Local shops asked for weekend access and a printed map of detours.
    The council promised both, plus a public meeting before the first pour.</p>
  </article>
</body>
</html>
"""

INCOMPLETE_COPY = "incomplete · short extract"
VIEWER_NOTE = "Amber extracted only a short preview"


def _skip_screenshot(monkeypatch) -> None:
    async def _skip_shot(sid: str) -> None:
        return None

    monkeypatch.setattr("app.main.screenshot_reader", _skip_shot)


def _import(client: TestClient, html: str, name: str = "article.html") -> str:
    r = client.post(
        "/import",
        files={"file": (name, html, "text/html")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return r.headers["location"].strip("/")


def test_paywalled_snapshot_renders_incomplete_on_viewer_and_lists(tmp_data, monkeypatch):
    _skip_screenshot(monkeypatch)
    from app import db

    with TestClient(app) as client:
        sid = _import(client, PAYWALL, "teaser.html")
        snap = db.get_snapshot(sid)
        assert snap["paywalled"] is True
        assert snap["word_count"] < PAYWALL_WORD_LIMIT

        article = client.get(f"/{sid}")
        assert article.status_code == 200
        assert VIEWER_NOTE in article.text
        assert 'href="/#import"' in article.text
        assert "class=\"viewer-note\"" in article.text

        webpage = client.get(f"/{sid}/webpage")
        assert webpage.status_code == 200
        assert VIEWER_NOTE in webpage.text

        shot = client.get(f"/{sid}/screenshot")
        assert shot.status_code == 200
        assert VIEWER_NOTE in shot.text

        reader = client.get(f"/{sid}/reader")
        assert reader.status_code == 200
        assert "short preview" in reader.text
        assert "Import" in reader.text

        home = client.get("/")
        assert home.status_code == 200
        assert INCOMPLETE_COPY in home.text
        assert f'href="/{sid}"' in home.text

        saved = client.get("/saved")
        assert saved.status_code == 200
        assert INCOMPLETE_COPY in saved.text
        assert "Elon Musk" in saved.text


def test_complete_snapshot_does_not_show_incomplete_chrome(tmp_data, monkeypatch):
    _skip_screenshot(monkeypatch)
    from app import db
    from app.extract import extract_article

    extracted = extract_article(FULL_ARTICLE, "https://daily.test/bridge")
    assert extracted["paywalled"] is False
    assert extracted["word_count"] >= PAYWALL_WORD_LIMIT

    with TestClient(app) as client:
        sid = _import(client, FULL_ARTICLE, "full.html")
        snap = db.get_snapshot(sid)
        assert snap["paywalled"] is False

        article = client.get(f"/{sid}")
        assert article.status_code == 200
        assert VIEWER_NOTE not in article.text
        assert "viewer-note" not in article.text
        assert INCOMPLETE_COPY not in article.text

        home = client.get("/")
        assert INCOMPLETE_COPY not in home.text
        assert "City Council Approves Bridge" in home.text

        saved = client.get("/saved")
        assert INCOMPLETE_COPY not in saved.text
        assert "City Council Approves Bridge" in saved.text


def test_init_db_backfills_paywalled_from_word_count(tmp_path, monkeypatch):
    data = tmp_path / "legacy"
    snaps = data / "snaps"
    db_path = data / "amber.sqlite3"
    data.mkdir()
    monkeypatch.setattr("app.config.DATA_DIR", data)
    monkeypatch.setattr("app.config.SNAPS_DIR", snaps)
    monkeypatch.setattr("app.config.DB_PATH", db_path)
    monkeypatch.setattr("app.db.DATA_DIR", data)
    monkeypatch.setattr("app.db.SNAPS_DIR", snaps)
    monkeypatch.setattr("app.db.DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE snapshots (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            url_normalized TEXT NOT NULL,
            final_url TEXT,
            title TEXT,
            site_name TEXT,
            author TEXT,
            published_at TEXT,
            description TEXT,
            created_at TEXT NOT NULL,
            http_status INTEGER,
            word_count INTEGER,
            status TEXT NOT NULL,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO snapshots (
            id, url, url_normalized, title, created_at, word_count, status
        ) VALUES (?, ?, ?, ?, ?, ?, 'complete')
        """,
        (
            "UKrA8",
            "https://www.theinformation.com/articles/demo",
            "https://www.theinformation.com/articles/demo",
            "SpaceX Shakes Up Data Center Leadership",
            "2026-09-01T00:00:00Z",
            37,
        ),
    )
    conn.commit()
    conn.close()

    from app import db

    db.init_db()
    snap = db.get_snapshot("UKrA8")
    assert snap is not None
    assert snap["paywalled"] is True
    assert snap["word_count"] == 37
