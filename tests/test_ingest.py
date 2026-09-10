from app.ingest import (
    _extract_is_worse,
    _stored_reader_body_html,
    ingest_html,
    original_url_from_html,
    rebuild_reader,
)
from app.reader import build_reader_html
from tests.test_extract import (
    ARCHIVE_IS_SAVED,
    LOCKED_ARTICLE,
    NYT_BEETS_CHROME,
    POLITICO_RELATED_CARD,
)

# Amber wrap chrome with no usable article body (notice-only .body).
_READER_WRAP_NO_BODY = """<!doctype html>
<html><body>
<article class="wrap">
<h1>CHROME_OUTSIDE_TOKEN leftover title</h1>
<aside>By Chrome Author</aside>
<div class="body">
<p class="notice">Incomplete — Amber extracted only a short preview. CHROME_NOTICE_TOKEN</p>
</div>
</article>
</body></html>
"""


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


def test_rebuild_reader_from_stored_reader_html_body(tmp_data):
    """Empty article.html still rebuilds from the stored reader .body."""
    sid = ingest_html(
        LOCKED_ARTICLE, url="https://www.theinformation.com/articles/x"
    )
    from app import db
    from bs4 import BeautifulSoup

    folder = db.snap_dir(sid)
    page = (folder / "page.html").read_text(encoding="utf-8")
    before = db.get_snapshot(sid)
    stored_title = before["title"]
    stored_author = before["author"]

    soup = BeautifulSoup(
        (folder / "reader.html").read_text(encoding="utf-8"), "lxml"
    )
    h1 = soup.find("h1")
    if h1 is not None:
        h1.insert(0, "CHROME_OUTSIDE_TOKEN ")
    body = soup.select_one("div.body")
    notice = soup.new_tag("p", attrs={"class": "notice"})
    notice.string = (
        "Incomplete — Amber extracted only a short preview. CHROME_NOTICE_TOKEN"
    )
    share = soup.new_tag("p")
    share_a = soup.new_tag("a", href="https://example.com/share")
    share_a.string = "Share full article"
    share.append(share_a)
    body.insert(0, share)
    body.insert(0, notice)
    (folder / "reader.html").write_text(str(soup), encoding="utf-8")
    (folder / "article.html").write_text("  \n\t  ", encoding="utf-8")
    (folder / "article.txt").write_text("stale", encoding="utf-8")

    article = rebuild_reader(sid)
    text = (folder / "article.txt").read_text(encoding="utf-8")
    article_html = (folder / "article.html").read_text(encoding="utf-8")
    reader = (folder / "reader.html").read_text(encoding="utf-8")
    snap = db.get_snapshot(sid)
    assert article.get("rebuild_refused") is False
    assert article.get("rebuild_source") == "reader.html"
    assert "FREEBLURB_TOKEN_UKrA8" in text
    assert "FREEBLURB_TOKEN_UKrA8" in article_html
    assert "FREEBLURB_TOKEN_UKrA8" in reader
    assert "CHROME_OUTSIDE_TOKEN" not in article_html
    assert "CHROME_NOTICE_TOKEN" not in article_html
    assert "CHROME_NOTICE_TOKEN" not in reader
    assert "Share full article" not in article_html
    assert "Share full article" not in reader
    assert "Sign in" not in text
    assert article["paywalled"] is False
    assert article["word_count"] >= 90
    assert snap["word_count"] == article["word_count"]
    assert snap["title"] == stored_title
    assert snap["author"] == stored_author
    assert (folder / "page.html").read_text(encoding="utf-8") == page


def test_rebuild_refuses_worse_page_html_extract(tmp_data):
    """Without article.html or a reader body, frozen page.html must not clobber."""
    sid = ingest_html(
        LOCKED_ARTICLE, url="https://www.theinformation.com/articles/x"
    )
    from app import db

    folder = db.snap_dir(sid)
    stored_txt = (folder / "article.txt").read_text(encoding="utf-8")
    stored_html = (folder / "article.html").read_text(encoding="utf-8")
    page = (folder / "page.html").read_text(encoding="utf-8")
    before = db.get_snapshot(sid)
    (folder / "article.html").write_text("", encoding="utf-8")
    (folder / "reader.html").write_text(_READER_WRAP_NO_BODY, encoding="utf-8")
    article = rebuild_reader(sid)
    assert article.get("rebuild_refused") is True
    assert article.get("rebuild_source") == "page.html"
    assert "FREEBLURB_TOKEN_UKrA8" in stored_txt
    assert (folder / "article.txt").read_text(encoding="utf-8") == stored_txt
    assert (folder / "article.html").read_text(encoding="utf-8") == ""
    assert (folder / "reader.html").read_text(encoding="utf-8") == _READER_WRAP_NO_BODY
    assert (folder / "page.html").read_text(encoding="utf-8") == page
    after = db.get_snapshot(sid)
    assert after["word_count"] == before["word_count"]
    assert after["paywalled"] is False
    assert after["title"] == before["title"]
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
    (folder / "article.html").write_text("", encoding="utf-8")
    (folder / "reader.html").write_text(_READER_WRAP_NO_BODY, encoding="utf-8")
    (folder / "page.html").write_text(
        "<!doctype html><html><body><p>Sign</p></body></html>",
        encoding="utf-8",
    )
    article = rebuild_reader(sid)
    assert article.get("rebuild_refused") is True
    assert article.get("rebuild_source") == "page.html"
    assert (folder / "article.txt").read_text(encoding="utf-8") == stored_txt
    assert (folder / "reader.html").read_text(encoding="utf-8") == _READER_WRAP_NO_BODY
    after = db.get_snapshot(sid)
    assert after["word_count"] == stored_n
    assert after["paywalled"] is True


def test_stored_reader_body_html_uses_body_not_wrap_chrome():
    html = """<!doctype html><html><body>
    <article class="wrap">
      <h1>CHROME_OUTSIDE_TOKEN Title</h1>
      <aside>By Chrome Author</aside>
      <div class="body">
        <p class="notice">Incomplete — Amber extracted only a short preview. CHROME_NOTICE_TOKEN</p>
        <p class="notice">Correction: TOKEN_PUBLISHER_NOTICE editors later confirmed the count.</p>
        <p>TOKEN_READER_BODY the real extract lives here.</p>
      </div>
    </article>
    </body></html>"""
    got = _stored_reader_body_html(html)
    assert "TOKEN_READER_BODY" in got
    assert "TOKEN_PUBLISHER_NOTICE" in got
    assert "CHROME_OUTSIDE_TOKEN" not in got
    assert "CHROME_NOTICE_TOKEN" not in got
    assert "Chrome Author" not in got
    assert _stored_reader_body_html("") == ""
    assert _stored_reader_body_html("<html><body><h1>Title</h1><p>words</p></body></html>") == ""
    assert _stored_reader_body_html(_READER_WRAP_NO_BODY) == ""


def test_rebuild_prefers_article_html_when_reader_is_invalid_utf8(tmp_data):
    """Valid article.html must win even if unused reader.html is not UTF-8."""
    body = " ".join(f"keep{i}" for i in range(100))
    html = f"""<!doctype html>
<html>
<head><meta property="og:title" content="Primary extract"></head>
<body><article><h1>Primary extract</h1><p>TOKEN_ARTICLE_PRIMARY {body}</p></article></body>
</html>"""
    sid = ingest_html(html, url="https://daily.test/primary")
    from app import db

    folder = db.snap_dir(sid)
    page = (folder / "page.html").read_text(encoding="utf-8")
    assert (folder / "article.html").read_text(encoding="utf-8").strip()
    (folder / "reader.html").write_bytes(b"\xff\xfe not utf-8 \x80\x81")
    article = rebuild_reader(sid)
    assert article.get("rebuild_refused") is False
    assert article.get("rebuild_source") == "article.html"
    assert "TOKEN_ARTICLE_PRIMARY" in (folder / "article.txt").read_text(encoding="utf-8")
    assert article["word_count"] >= 100
    assert (folder / "page.html").read_text(encoding="utf-8") == page


def test_rebuild_keeps_publisher_notice_in_reader_body(tmp_data):
    """Publisher class=notice corrections must survive; only Amber's banner is stripped."""
    body = " ".join(f"keep{i}" for i in range(100))
    correction = (
        "Correction: TOKEN_PUBLISHER_NOTICE editors later confirmed "
        "the headcount was wrong on Tuesday night downtown after review."
    )
    assert len(correction.split()) == 15
    html = f"""<!doctype html>
<html>
<head><meta property="og:title" content="Notice probe"></head>
<body><article><h1>Notice probe</h1>
<p class="notice">{correction}</p>
<p>{body}</p>
</article></body>
</html>"""
    sid = ingest_html(html, url="https://daily.test/notice")
    from app import db

    folder = db.snap_dir(sid)
    page = (folder / "page.html").read_text(encoding="utf-8")
    before = db.get_snapshot(sid)
    amber_banner = (
        '<p class="notice">Incomplete — Amber extracted only a short preview. '
        "The page may have limited access, or extraction may have missed the full article. "
        "If the full piece is visible in your browser, save the page as HTML, then "
        "Import that file on Amber's homepage.</p>"
    )
    reader = f"""<!doctype html>
<html><body>
<article class="wrap">
<h1>CHROME_OUTSIDE_TOKEN {before["title"]}</h1>
<div class="body">
{amber_banner}
<p class="notice">{correction}</p>
<p>{body}</p>
</div>
</article>
</body></html>"""
    (folder / "article.html").write_text("", encoding="utf-8")
    (folder / "article.txt").write_text("stale", encoding="utf-8")
    (folder / "reader.html").write_text(reader, encoding="utf-8")
    article = rebuild_reader(sid)
    article_html = (folder / "article.html").read_text(encoding="utf-8")
    text = (folder / "article.txt").read_text(encoding="utf-8")
    rebuilt = (folder / "reader.html").read_text(encoding="utf-8")
    assert article.get("rebuild_refused") is False
    assert article.get("rebuild_source") == "reader.html"
    assert "TOKEN_PUBLISHER_NOTICE" in article_html
    assert "TOKEN_PUBLISHER_NOTICE" in text
    assert "TOKEN_PUBLISHER_NOTICE" in rebuilt
    assert "CHROME_OUTSIDE_TOKEN" not in article_html
    assert article["word_count"] >= 115
    assert db.get_snapshot(sid)["title"] == before["title"]
    assert (folder / "page.html").read_text(encoding="utf-8") == page


def test_rebuild_empty_reader_placeholder_falls_back_to_page_html(tmp_data):
    """Amber notice plus empty <p></p> is not a stored extract; use page.html."""
    token = "TOKEN_PAGE_RECOVER"
    body = " ".join(f"story{i}" for i in range(90))
    html = f"""<!doctype html>
<html>
<head><meta property="og:title" content="Recoverable"></head>
<body><article><h1>Recoverable</h1><p>{token} {body}</p></article></body>
</html>"""
    sid = ingest_html(html, url="https://daily.test/recover")
    from app import db

    folder = db.snap_dir(sid)
    page = (folder / "page.html").read_text(encoding="utf-8")
    empty_reader = build_reader_html(
        {
            "title": "Recoverable",
            "article_html": "",
            "article_text": "",
            "paywalled": True,
            "word_count": 0,
        },
        "https://daily.test/recover",
    )
    assert "<p></p>" in empty_reader
    assert "Incomplete — Amber extracted only a short preview." in empty_reader
    (folder / "article.html").write_text("", encoding="utf-8")
    (folder / "article.txt").write_text("", encoding="utf-8")
    (folder / "reader.html").write_text(empty_reader, encoding="utf-8")
    db.update_snapshot(sid, word_count=0, paywalled=1)

    article = rebuild_reader(sid)
    text = (folder / "article.txt").read_text(encoding="utf-8")
    article_html = (folder / "article.html").read_text(encoding="utf-8")
    assert article.get("rebuild_refused") is False
    assert article.get("rebuild_source") == "page.html"
    assert token in text
    assert token in article_html
    assert article["word_count"] >= 80
    assert (folder / "page.html").read_text(encoding="utf-8") == page


def test_rebuild_empty_figure_after_placeholder_falls_back_to_page_html(tmp_data):
    """An empty <figure> left after a placeholder img is not retained media."""
    token = "TOKEN_PAGE_RECOVER_FIGURE"
    body = " ".join(f"story{i}" for i in range(90))
    html = f"""<!doctype html>
<html>
<head><meta property="og:title" content="Recoverable figure"></head>
<body><article><h1>Recoverable figure</h1><p>{token} {body}</p></article></body>
</html>"""
    sid = ingest_html(html, url="https://daily.test/recover-figure")
    from app import db

    folder = db.snap_dir(sid)
    page = (folder / "page.html").read_text(encoding="utf-8")
    reader = build_reader_html(
        {
            "title": "Recoverable figure",
            "article_html": '<figure><img src=""></figure>',
            "article_text": "",
            "paywalled": False,
            "word_count": 0,
        },
        "https://daily.test/recover-figure",
    )
    (folder / "article.html").write_text("", encoding="utf-8")
    (folder / "article.txt").write_text("", encoding="utf-8")
    (folder / "reader.html").write_text(reader, encoding="utf-8")
    db.update_snapshot(sid, word_count=0, paywalled=0)

    article = rebuild_reader(sid)
    text = (folder / "article.txt").read_text(encoding="utf-8")
    article_html = (folder / "article.html").read_text(encoding="utf-8")
    assert article.get("rebuild_refused") is False
    assert article.get("rebuild_source") == "page.html"
    assert token in text
    assert token in article_html
    assert article["word_count"] >= 80
    assert (folder / "page.html").read_text(encoding="utf-8") == page


def test_rebuild_reader_keeps_media_only_body(tmp_data):
    """A zero-word reader body with retained media is usable; do not take page.html."""
    token = "TOKEN_PAGE_STORY"
    body = " ".join(f"story{i}" for i in range(90))
    html = f"""<!doctype html>
<html>
<head><meta property="og:title" content="Photo essay"></head>
<body><article><h1>Photo essay</h1><p>{token} {body}</p></article></body>
</html>"""
    sid = ingest_html(html, url="https://daily.test/photo")
    from app import db

    folder = db.snap_dir(sid)
    page = (folder / "page.html").read_text(encoding="utf-8")
    reader = build_reader_html(
        {
            "title": "Photo essay",
            "article_html": (
                '<figure><img src="https://cdn.example/shot.jpg" alt=""></figure>'
            ),
            "article_text": "",
            "paywalled": False,
            "word_count": 0,
        },
        "https://daily.test/photo",
    )
    (folder / "article.html").write_text("", encoding="utf-8")
    (folder / "article.txt").write_text("", encoding="utf-8")
    (folder / "reader.html").write_text(reader, encoding="utf-8")
    db.update_snapshot(sid, word_count=0, paywalled=0)

    article = rebuild_reader(sid)
    article_html = (folder / "article.html").read_text(encoding="utf-8")
    assert article.get("rebuild_refused") is False
    assert article.get("rebuild_source") == "reader.html"
    assert "shot.jpg" in article_html
    assert token not in article_html
    assert (folder / "page.html").read_text(encoding="utf-8") == page
