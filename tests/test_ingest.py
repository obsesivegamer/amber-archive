from app.ingest import ingest_html, original_url_from_html
from tests.test_extract import ARCHIVE_IS_SAVED


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
