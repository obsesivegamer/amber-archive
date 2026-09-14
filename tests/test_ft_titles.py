from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.extract import extract_article
from app.ingest import ingest_html

HTML = (Path(__file__).parent / "fixtures" / "ft_banner.html").read_text()
TITLE = "Russia hits evacuated Kyiv-Warsaw train on line used by foreign leaders"
URL = "https://www.ft.com/content/cdce9cd6-4857-4332-af34-ffc9ce45eadf"


@pytest.mark.parametrize("headline", [
    TITLE,
    "Joseph Stiglitz’s ‘progressive AI agenda’ for the economy",
    "Iraq’s militias grow bolder in Iran war",
])
def test_ft_article_headline_wins_over_banner(headline):
    got = extract_article(HTML.replace(TITLE, headline), URL)
    assert got["title"] == headline
    assert "TOKEN_FT_END" in got["article_text"]
    assert "train.jpg" in got["article_html"]
    assert got["word_count"] >= 80
    assert got["paywalled"] is False


@pytest.mark.parametrize("metadata", ["", "A different social media headline", "A truncated headline…"])
def test_ft_headline_does_not_depend_on_metadata(metadata):
    soup = BeautifulSoup(HTML, "lxml")
    soup.title.decompose()
    soup.find("meta", property="og:title")["content"] = metadata
    got = extract_article(str(soup), URL)
    assert got["title"] == TITLE


@pytest.mark.parametrize("headline", [None, "", "Short", "x" * 241])
def test_ft_banner_is_not_a_fallback_headline(headline):
    soup = BeautifulSoup(HTML, "lxml")
    heading = soup.select_one("h1.o-topper__headline")
    if headline is None:
        heading.decompose()
    else:
        heading.string = headline
    got = extract_article(str(soup), URL)
    assert got["title"] == TITLE


def test_ft_headline_wins_over_unrelated_heading_outside_banner():
    html = HTML.replace('<div class="article-content">', '<h1>Explore our latest reporting</h1><div class="article-content">')
    assert extract_article(html, URL)["title"] == TITLE


def test_ft_dek_comes_from_article_heading():
    html = HTML.replace(
        '</h1>\n      </div>',
        '</h1><p>Railway staff arranged onward travel after inspecting the route.</p>\n      </div>',
    )
    assert extract_article(html, URL)["dek"] == "Railway staff arranged onward travel after inspecting the route."


def test_ordinary_article_keeps_visible_heading_over_social_title():
    html = '<meta property="og:title" content="A different social media headline"><h1>A complete visible article headline</h1><p>Article text.</p>'
    assert extract_article(html, "https://daily.test/story")["title"] == "A complete visible article headline"


def test_import_persists_ft_headline_everywhere(tmp_data):
    from app import db
    from app.main import app

    sid = ingest_html(HTML, url=URL)
    folder = db.snap_dir(sid)
    assert db.get_snapshot(sid)["title"] == TITLE
    assert db.read_json(folder / "meta.json")["title"] == TITLE
    reader = BeautifulSoup((folder / "reader.html").read_text(), "lxml")
    assert reader.h1.get_text(" ", strip=True) == TITLE
    assert "TOKEN_FT_END" in (folder / "article.txt").read_text()
    with TestClient(app) as client:
        saved = BeautifulSoup(client.get("/saved").text, "lxml")
        assert saved.find("a", href=f"/{sid}").get_text(" ", strip=True) == TITLE
