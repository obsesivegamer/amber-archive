"""Metered paywall: Playwright bounce sets document.referrer; capture keeps the full article."""

from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright

from app.capture import (
    BOUNCE_STUB_HTML,
    _capture_visit,
    _goto_article,
    extra_headers_for_visit,
)
from app.config import EXTRA_HEADERS, REFERRER_BOUNCE_ORIGINS, referrer_is_allowlisted
from app.extract import PAYWALL_WORD_LIMIT

METERED_FULL_TOKEN = "METERED_FULL_TOKEN_AMBER"

TEASER_ARTICLE_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Inside Kallas’ fightback against plans to tear up the EU’s diplomatic service</title>
  <meta property="og:title" content="Inside Kallas’ fightback against plans to tear up the EU’s diplomatic service">
  <meta property="og:site_name" content="POLITICO">
  <meta property="og:description" content="Commission diplomats said the plan would strip the service of its role.">
  <meta name="author" content="Nicholas Vinocur">
</head>
<body>
  <article>
    <h1>Inside Kallas’ fightback against plans to tear up the EU’s diplomatic service</h1>
    <div id="story">
      <p>Commission diplomats said the plan would strip the service of its role.</p>
      <p>Subscribe to continue reading.</p>
    </div>
  </article>
</body>
</html>
"""

FULL_ARTICLE_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Inside Kallas’ fightback against plans to tear up the EU’s diplomatic service</title>
  <meta property="og:title" content="Inside Kallas’ fightback against plans to tear up the EU’s diplomatic service">
  <meta property="og:site_name" content="POLITICO">
  <meta property="og:description" content="Commission diplomats said the plan would strip the service of its role.">
  <meta name="author" content="Nicholas Vinocur">
</head>
<body>
  <article>
    <h1>Inside Kallas’ fightback against plans to tear up the EU’s diplomatic service</h1>
    <div id="story">
      <p>Commission diplomats said the plan would strip the service of its role.</p>
      <p>METERED_FULL_TOKEN_AMBER Kallas told colleagues she would fight the
      proposal in the college and in the European Parliament if the draft
      reached the floor this autumn after the ambassadors' meeting.</p>
      <p>Several smaller member states backed her, arguing that a weaker
      diplomatic service would leave them dependent on the largest capitals
      for the first draft of every joint statement on foreign policy.</p>
      <p>Officials in the service said the rewrite would move desk officers
      into the Commission proper and leave only a thin coordination layer
      in the building across from the Schuman roundabout.</p>
      <p>Supporters of the plan answered that Europe already has too many
      parallel foreign-policy shops, and that a single chain of command
      would speed sanctions and make crisis statements less muddled.</p>
    </div>
  </article>
</body>
</html>
"""

FULL_STORY_INNER = """
      <p>Commission diplomats said the plan would strip the service of its role.</p>
      <p>METERED_FULL_TOKEN_AMBER Kallas told colleagues she would fight the
      proposal in the college and in the European Parliament if the draft
      reached the floor this autumn after the ambassadors' meeting.</p>
      <p>Several smaller member states backed her, arguing that a weaker
      diplomatic service would leave them dependent on the largest capitals
      for the first draft of every joint statement on foreign policy.</p>
      <p>Officials in the service said the rewrite would move desk officers
      into the Commission proper and leave only a thin coordination layer
      in the building across from the Schuman roundabout.</p>
      <p>Supporters of the plan answered that Europe already has too many
      parallel foreign-policy shops, and that a single chain of command
      would speed sanctions and make crisis statements less muddled.</p>
"""

# Piano-like: the full copy is only in the response when the request is a
# real cross-site navigation (Sec-Fetch-Site: cross-site) with a Google/X
# Referer and no meter cookie. extra_http_headers / page.goto(referer=)
# can fill document.referrer here, but those stay Sec-Fetch-Site: none
# (typed URL) and still get the teaser.
_PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Inside Kallas’ fightback against plans to tear up the EU’s diplomatic service</title>
  <meta property="og:title" content="Inside Kallas’ fightback against plans to tear up the EU’s diplomatic service">
  <meta property="og:site_name" content="POLITICO">
  <meta property="og:description" content="Commission diplomats said the plan would strip the service of its role.">
  <meta name="author" content="Nicholas Vinocur">
  <meta name="amber-http-referer" content="{referer}">
  <meta name="amber-fetch-site" content="{fetch_site}">
</head>
<body>
  <article>
    <h1>Inside Kallas’ fightback against plans to tear up the EU’s diplomatic service</h1>
    <div id="story">{story}</div>
  </article>
  <script>
  (function () {{
    var used = /(?:^|;\\s*)meter=used(?:;|$)/.test(document.cookie);
    if ({js_unlock} && {granted} && !used) {{
      document.getElementById('story').innerHTML = {full_js};
    }} else if (!{granted}) {{
      document.cookie = 'meter=used; path=/';
    }}
    var marker = document.createElement('p');
    marker.id = 'amber-referrer';
    marker.textContent = document.referrer || 'EMPTY';
    document.body.appendChild(marker);
    var site = document.createElement('p');
    site.id = 'amber-fetch-site';
    site.textContent = {fetch_site_js};
    document.body.appendChild(site);
  }})();
  </script>
</body>
</html>
"""


_COMMENT_CHROME = (
    '<div class="comments"><h2>Comments</h2><p>'
    + " ".join(
        "Reader comment {i} about the diplomats and the reform plan that should "
        "not count as article body copy at all.".format(i=i)
        for i in range(8)
    )
    + "</p></div>"
)


def _page(
    referer: str, fetch_site: str, *, locked: bool, grant_server: bool, comments: bool = False
) -> bytes:
    if grant_server and not locked:
        story = FULL_STORY_INNER
    else:
        story = (
            "<p>Commission diplomats said the plan would strip the service of its role.</p>"
            "<p>Subscribe to continue reading.</p>"
        )
        if comments:
            story += _COMMENT_CHROME
    html = _PAGE.format(
        referer=referer.replace('"', ""),
        fetch_site=fetch_site.replace('"', ""),
        story=story,
        js_unlock="false" if locked else "true",
        granted="true" if grant_server and not locked else "false",
        full_js=repr(FULL_STORY_INNER),
        fetch_site_js=repr(fetch_site or "EMPTY"),
    )
    return html.encode()


class _State:
    last_referer = ""
    last_cookie = ""
    last_fetch_site = ""
    hits = 0
    article_navs: list[tuple[str, str]] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        _State.last_referer = self.headers.get("Referer", "")
        _State.last_cookie = self.headers.get("Cookie", "")
        _State.last_fetch_site = self.headers.get("Sec-Fetch-Site", "")
        _State.hits += 1
        if not self.path.startswith("/style"):
            _State.article_navs.append((_State.last_referer, _State.last_fetch_site))
        locked = self.path.startswith("/locked")
        cookie_used = "meter=used" in (_State.last_cookie or "")
        cross_site = _State.last_fetch_site == "cross-site"
        grant_server = (
            (not locked)
            and (not cookie_used)
            and referrer_is_allowlisted(_State.last_referer)
            and cross_site
        )
        if self.path.startswith("/referrer"):
            body = (
                "<!doctype html><html><body><p id='amber-referrer'></p>"
                "<script>document.getElementById('amber-referrer').textContent="
                "document.referrer||'EMPTY';</script></body></html>"
            ).encode()
        else:
            body = _page(
                _State.last_referer,
                _State.last_fetch_site,
                locked=locked,
                grant_server=grant_server,
                comments=self.path.startswith("/comments"),
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def metered_site():
    _State.last_referer = ""
    _State.last_cookie = ""
    _State.last_fetch_site = ""
    _State.hits = 0
    _State.article_navs = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()


def _wait_complete(client: TestClient, sid: str, seconds: int = 180) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{sid}").json()
        if job["status"] == "complete":
            return job
        if job["status"] == "failed":
            pytest.fail(job.get("error") or "capture failed")
        time.sleep(0.4)
    pytest.fail("capture timed out")


async def _fulfill_bounce(page, host: str) -> None:
    async def on_route(route):
        if host in route.request.url:
            if route.request.resource_type == "document":
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=BOUNCE_STUB_HTML,
                    headers={"Referrer-Policy": "origin"},
                )
                return
            await route.abort()
            return
        await route.continue_()

    await page.route("**/*", on_route)


async def _playwright_referrer(
    url: str,
    *,
    bounce: bool,
    reuse_after_direct: bool = False,
    bounce_origin: str = "https://www.google.com/",
) -> tuple[str, str]:
    headers = extra_headers_for_visit(bounce=bounce and not reuse_after_direct)
    if reuse_after_direct:
        headers = dict(EXTRA_HEADERS)
    host = {
        "https://www.google.com/": "www.google.com",
        "https://x.com/": "x.com",
    }[bounce_origin]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(extra_http_headers=headers)
        page = await context.new_page()
        try:
            if reuse_after_direct:
                await page.goto(url, wait_until="domcontentloaded")
                await _fulfill_bounce(page, host)
                await _goto_article(page, url, bounce_origin)
            elif bounce:
                await _fulfill_bounce(page, host)
                await _goto_article(page, url, bounce_origin)
            else:
                await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_selector("#amber-referrer")
            text = await page.inner_text("#amber-referrer")
            story = await page.inner_text("#story")
        finally:
            await browser.close()
        return text, story


async def _one_capture_visit(url: str, bounce_origin: str | None):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            visit = await _capture_visit(
                browser, {"resources": []}, url, bounce_origin=bounce_origin
            )
            try:
                return visit.html, visit.article, visit.final_url, visit.title
            finally:
                await visit.close()
        finally:
            await browser.close()


def test_extra_http_headers_referer_does_not_unlock_metered_article(metered_site):
    """Direct goto may carry EXTRA_HEADERS Referer; Piano still wants a bounce."""
    referrer, story = asyncio.run(_playwright_referrer(f"{metered_site}/article", bounce=False))
    assert METERED_FULL_TOKEN not in story
    # This Chromium copies extra_http_headers into document.referrer; that
    # alone must not be enough (the capture retry is the bounce click).
    assert referrer in {"EMPTY", "https://www.google.com/"}


def test_bounce_click_unlocks_metered_article(metered_site):
    referrer, story = asyncio.run(_playwright_referrer(f"{metered_site}/article", bounce=True))
    assert referrer.startswith("https://www.google.com")
    assert METERED_FULL_TOKEN in story


def test_x_bounce_sends_x_referer_not_google(metered_site):
    referrer, story = asyncio.run(
        _playwright_referrer(
            f"{metered_site}/article",
            bounce=True,
            bounce_origin="https://x.com/",
        )
    )
    assert referrer.startswith("https://x.com")
    assert "google.com" not in referrer
    assert METERED_FULL_TOKEN in story
    assert _State.last_referer.startswith("https://x.com")
    assert _State.last_fetch_site == "cross-site"
    assert not _State.last_referer.startswith("https://www.google.com")


def test_reused_context_keeps_meter_cookie_so_bounce_stays_teaser(metered_site):
    """Safety: a failed direct visit must not leave cookies that mark the meter used."""
    referrer, story = asyncio.run(
        _playwright_referrer(f"{metered_site}/article", bounce=False, reuse_after_direct=True)
    )
    assert referrer.startswith("https://www.google.com")
    assert METERED_FULL_TOKEN not in story


def test_capture_retries_metered_paywall_and_keeps_full_article(
    tmp_data, allow_private, metered_site
):
    from app.main import app
    from app import db

    with TestClient(app) as client:
        r = client.post(
            "/save",
            data={"url": f"{metered_site}/article"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        sid = r.headers["location"].rsplit("/", 1)[-1]
        _wait_complete(client, sid)

        snap = db.get_snapshot(sid)
        assert snap["paywalled"] is False
        assert snap["word_count"] >= PAYWALL_WORD_LIMIT
        text = (db.snap_dir(sid) / "article.txt").read_text(encoding="utf-8")
        assert METERED_FULL_TOKEN in text
        meta = db.read_json(db.snap_dir(sid) / "meta.json")
        assert meta["referrer_bounce"] == "https://www.google.com/"
        assert meta["referrer_retried"] is True

        page = client.get(f"/{sid}")
        assert page.status_code == 200
        assert "paywalled teaser" not in page.text
        reader = client.get(f"/{sid}/reader")
        assert reader.status_code == 200
        assert METERED_FULL_TOKEN in reader.text
        assert "paywalled teaser" not in reader.text


def test_capture_retries_when_teaser_has_reader_comments(
    tmp_data, allow_private, metered_site
):
    """Comments inside <article> must not look complete and skip the bounce."""
    from app.main import app
    from app import db

    with TestClient(app) as client:
        r = client.post(
            "/save",
            data={"url": f"{metered_site}/comments"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        sid = r.headers["location"].rsplit("/", 1)[-1]
        _wait_complete(client, sid)

        snap = db.get_snapshot(sid)
        assert snap["paywalled"] is False
        text = (db.snap_dir(sid) / "article.txt").read_text(encoding="utf-8")
        assert METERED_FULL_TOKEN in text
        assert "Reader comment 0" not in text
        meta = db.read_json(db.snap_dir(sid) / "meta.json")
        assert meta["referrer_bounce"] == "https://www.google.com/"
        assert meta["referrer_retried"] is True


def test_capture_hard_paywall_stays_incomplete(tmp_data, allow_private, metered_site, monkeypatch):
    monkeypatch.setattr(
        "app.capture.REFERRER_BOUNCE_RETRY_ORIGINS",
        ("https://www.google.com/",),
    )
    from app.main import app
    from app import db

    with TestClient(app) as client:
        r = client.post(
            "/save",
            data={"url": f"{metered_site}/locked"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        sid = r.headers["location"].rsplit("/", 1)[-1]
        _wait_complete(client, sid)

        snap = db.get_snapshot(sid)
        assert snap["paywalled"] is True
        assert snap["word_count"] < PAYWALL_WORD_LIMIT
        text = (db.snap_dir(sid) / "article.txt").read_text(encoding="utf-8")
        assert METERED_FULL_TOKEN not in text
        meta = db.read_json(db.snap_dir(sid) / "meta.json")
        assert meta["referrer_retried"] is True

        article = client.get(f"/{sid}")
        assert "this visit only kept a paywalled teaser" in article.text
        assert 'href="/#import"' in article.text
        reader = client.get(f"/{sid}/reader")
        assert "paywalled teaser" in reader.text
        assert "retried as a Google referrer visit" in reader.text
        assert "Import" in reader.text


def test_same_host_bounce_does_not_stub_article(allow_private, metered_site, monkeypatch):
    origin = metered_site.rstrip("/") + "/"
    monkeypatch.setattr(
        "app.capture.REFERRER_BOUNCE_ORIGINS",
        REFERRER_BOUNCE_ORIGINS + (origin,),
    )
    html, article, final_url, title = asyncio.run(
        _one_capture_visit(f"{metered_site}/article", origin)
    )
    assert "Amber referrer bounce" not in html
    assert title != "Amber"
    assert "Kallas" in (article.get("title") or title)
    assert final_url.startswith(metered_site)
    assert article.get("word_count", 0) > 3


def test_capture_x_retry_sends_x_referer(
    tmp_data, allow_private, metered_site, monkeypatch
):
    monkeypatch.setattr(
        "app.capture.REFERRER_BOUNCE_RETRY_ORIGINS",
        ("https://x.com/",),
    )
    from app.main import app
    from app import db

    with TestClient(app) as client:
        r = client.post(
            "/save",
            data={"url": f"{metered_site}/article"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        sid = r.headers["location"].rsplit("/", 1)[-1]
        _wait_complete(client, sid)

        snap = db.get_snapshot(sid)
        assert snap["paywalled"] is False
        meta = db.read_json(db.snap_dir(sid) / "meta.json")
        assert meta["referrer_bounce"] == "https://x.com/"
        text = (db.snap_dir(sid) / "article.txt").read_text(encoding="utf-8")
        assert METERED_FULL_TOKEN in text

        cross = [(ref, site) for ref, site in _State.article_navs if site == "cross-site"]
        assert cross, _State.article_navs
        assert all(ref.startswith("https://x.com") for ref, _ in cross)
        assert not any(ref.startswith("https://www.google.com") for ref, _ in cross)
