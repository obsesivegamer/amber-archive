from app.ingest import (
    _extract_is_worse,
    ingest_html,
    original_url_from_html,
    rebuild_reader,
)
from tests.test_extract import (
    ARCHIVE_IS_SAVED,
    LOCKED_ARTICLE,
    NYT_BEETS_CHROME,
    POLITICO_RELATED_CARD,
)


def test_original_url_skips_archive_is_saved_from_comment():
    html = """<!doctype html>
    <!-- saved from url=(0024)https://archive.is/5QUGY -->
    <html><body>
    <input value="https://www.theinformation.com/briefings/exclusive-elon-musk-tells-tesla-staff-move-using-grok">
    </body></html>
    """
    assert original_url_from_html(html).startswith("https://www.theinformation.com/briefings/")


def test_original_url_from_chrome_saved_page():
    html = "<!-- saved from url=(0061)https://www.example.com/blog/hello-world -->\n<html></html>"
    assert original_url_from_html(html) == "https://www.example.com/blog/hello-world"


def test_ingest_saved_archive_is_article(tmp_data):
    sid = ingest_html(ARCHIVE_IS_SAVED)
    from app import db

    snap = db.get_snapshot(sid)
    assert snap["status"] == "complete"
    assert snap["author"] == "Grace Kay"
    assert snap["site_name"] == "The Information"
    assert "weekly limit" in (db.snap_dir(sid) / "article.txt").read_text(encoding="utf-8")
    reader = (db.snap_dir(sid) / "reader.html").read_text(encoding="utf-8")
    assert "Exclusive: Elon Musk Tells Tesla Staff" in reader
    assert "By Grace Kay" in reader
    assert "Andrew Milich" in reader
    meta = db.read_json(db.snap_dir(sid) / "meta.json")
    assert meta.get("paywalled") is True
    assert snap["paywalled"] is True


def test_ingest_politico_shaped_keeps_article_body(tmp_data):
    sid = ingest_html(POLITICO_RELATED_CARD, url="https://daily.test/kallas")
    from app import db

    snap = db.get_snapshot(sid)
    text = (db.snap_dir(sid) / "article.txt").read_text(encoding="utf-8")
    assert snap["paywalled"] is False
    assert "TOKEN_FULL_ARTICLE" in text
    assert snap["word_count"] >= 80
    meta = db.read_json(db.snap_dir(sid) / "meta.json")
    assert meta.get("imported") is True
    assert meta.get("paywalled") is False
    reader = (db.snap_dir(sid) / "reader.html").read_text(encoding="utf-8")
    assert "Listen" not in reader
    assert "Share via email" not in reader


def test_ingest_nyt_shaped_reader_is_readable(tmp_data):
    sid = ingest_html(
        NYT_BEETS_CHROME,
        url="https://www.nytimes.com/2026/08/10/well/eat/beets-health-benefits-recipes.html",
    )
    from app import db

    snap = db.get_snapshot(sid)
    folder = db.snap_dir(sid)
    text = (folder / "article.txt").read_text(encoding="utf-8")
    reader = (folder / "reader.html").read_text(encoding="utf-8")
    assert snap["paywalled"] is False
    assert snap["word_count"] >= 80
    assert "TOKEN_BEETS_BODY" in text
    assert "TOKEN_BEETS_RECIPE" in reader
    assert "Share full article" not in reader
    assert "Listen · 6:27" not in reader
    assert "Leer en español" not in reader
    assert "max-width: 100% !important" in reader
    assert "beets-bowl.jpg" in reader
    assert "width:1600" not in reader


def test_rebuild_reader_from_stored_article_html(tmp_data):
    sid = ingest_html(
        NYT_BEETS_CHROME,
        url="https://www.nytimes.com/2026/08/10/well/eat/beets-health-benefits-recipes.html",
    )
    from app import db

    folder = db.snap_dir(sid)
    page = (folder / "page.html").read_text(encoding="utf-8")
    assert "Share full article" in page
    (folder / "reader.html").write_text(
        "<html><body>DIRTY_SHARE_FULL_ARTICLE leftover Listen chrome</body></html>",
        encoding="utf-8",
    )
    (folder / "article.txt").write_text("stale", encoding="utf-8")
    article = rebuild_reader(sid)
    reader = (folder / "reader.html").read_text(encoding="utf-8")
    text = (folder / "article.txt").read_text(encoding="utf-8")
    assert article.get("rebuild_refused") is False
    assert article.get("rebuild_source") == "article.html"
    assert "DIRTY_SHARE_FULL_ARTICLE" not in reader
    assert "TOKEN_BEETS_BODY" in reader
    assert "TOKEN_BEETS_BODY" in text
    assert "Share full article" not in reader
    assert article["paywalled"] is False
    assert article["word_count"] >= 80
    snap = db.get_snapshot(sid)
    assert snap["word_count"] == article["word_count"]
    assert (folder / "page.html").read_text(encoding="utf-8") == page


def test_rebuild_keeps_information_json_extract(tmp_data):
    """Frozen page.html has no rails JSON; rebuild must use stored article.html."""
    sid = ingest_html(
        LOCKED_ARTICLE, url="https://www.theinformation.com/articles/x"
    )
    from app import db

    folder = db.snap_dir(sid)
    page = (folder / "page.html").read_text(encoding="utf-8")
    assert "FREEBLURB_TOKEN_UKrA8" not in page
    assert "js-react-on-rails-component" not in page
    before = db.get_snapshot(sid)
    assert before["paywalled"] is False
    assert before["word_count"] >= 90
    assert "FREEBLURB_TOKEN_UKrA8" in (folder / "article.txt").read_text(encoding="utf-8")
    (folder / "reader.html").write_text(
        "<html><body>DIRTY leftover chrome</body></html>", encoding="utf-8"
    )
    article = rebuild_reader(sid)
    text = (folder / "article.txt").read_text(encoding="utf-8")
    reader = (folder / "reader.html").read_text(encoding="utf-8")
    snap = db.get_snapshot(sid)
    assert article.get("rebuild_refused") is False
    assert article.get("rebuild_source") == "article.html"
    assert "FREEBLURB_TOKEN_UKrA8" in text
    assert "FREEBLURB_TOKEN_UKrA8" in reader
    assert "Sign in" not in text
    assert article["paywalled"] is False
    assert snap["paywalled"] is False
    assert article["word_count"] >= 90
    assert snap["word_count"] == article["word_count"]
    assert (folder / "page.html").read_text(encoding="utf-8") == page


def test_rebuild_refuses_worse_page_html_extract(tmp_data):
    """Without article.html, frozen page.html must not clobber a complete extract."""
    sid = ingest_html(
        LOCKED_ARTICLE, url="https://www.theinformation.com/articles/x"
    )
    from app import db

    folder = db.snap_dir(sid)
    stored_txt = (folder / "article.txt").read_text(encoding="utf-8")
    stored_html = (folder / "article.html").read_text(encoding="utf-8")
    stored_reader = (folder / "reader.html").read_text(encoding="utf-8")
    before = db.get_snapshot(sid)
    (folder / "article.html").write_text("", encoding="utf-8")
    article = rebuild_reader(sid)
    assert article.get("rebuild_refused") is True
    assert article.get("rebuild_source") == "page.html"
    assert "FREEBLURB_TOKEN_UKrA8" in stored_txt
    assert (folder / "article.txt").read_text(encoding="utf-8") == stored_txt
    assert (folder / "article.html").read_text(encoding="utf-8") == ""
    assert (folder / "reader.html").read_text(encoding="utf-8") == stored_reader
    after = db.get_snapshot(sid)
    assert after["word_count"] == before["word_count"]
    assert after["paywalled"] is False
    assert stored_html  # ingest did store the JSON body


def test_extract_is_worse_refuses_short_collapses():
    """Half-or-worse must fire below the old 40-word / strict-half gates."""
    assert _extract_is_worse({"word_count": 1, "paywalled": True}, {"word_count": 39, "paywalled": True})
    assert _extract_is_worse({"word_count": 20, "paywalled": True}, {"word_count": 40, "paywalled": True})
    assert _extract_is_worse({"word_count": 0, "paywalled": True}, {"word_count": 12, "paywalled": True})
    assert not _extract_is_worse(
        {"word_count": 200, "paywalled": False}, {"word_count": 216, "paywalled": False}
    )
    assert not _extract_is_worse(
        {"word_count": 10, "paywalled": True}, {"word_count": 0, "paywalled": True, "article_text": ""}
    )


def test_rebuild_refuses_short_legacy_page_html_collapse(tmp_data):
    """A 39-word stored teaser must not be overwritten by a 1-word page.html."""
    body = " ".join(f"word{i}" for i in range(39))
    html = f"""<!doctype html>
<html>
<head><meta property="og:title" content="Short teaser"></head>
<body><article><h1>Short teaser</h1><p>{body}</p></article></body>
</html>
"""
    sid = ingest_html(html, url="https://daily.test/short")
    from app import db

    folder = db.snap_dir(sid)
    before = db.get_snapshot(sid)
    assert before["paywalled"] is True
    assert 20 <= before["word_count"] < 80
    stored_n = before["word_count"]
    stored_txt = (folder / "article.txt").read_text(encoding="utf-8")
    stored_reader = (folder / "reader.html").read_text(encoding="utf-8")
    (folder / "article.html").write_text("", encoding="utf-8")
    (folder / "page.html").write_text(
        "<!doctype html><html><body><p>Sign</p></body></html>",
        encoding="utf-8",
    )
    article = rebuild_reader(sid)
    assert article.get("rebuild_refused") is True
    assert article.get("rebuild_source") == "page.html"
    assert (folder / "article.txt").read_text(encoding="utf-8") == stored_txt
    assert (folder / "reader.html").read_text(encoding="utf-8") == stored_reader
    after = db.get_snapshot(sid)
    assert after["word_count"] == stored_n
    assert after["paywalled"] is True
