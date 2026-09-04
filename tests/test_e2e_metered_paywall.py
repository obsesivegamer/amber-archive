"""Metered paywall: Playwright bounce sets document.referrer; capture keeps the full article."""

from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright

from app.capture import BOUNCE_STUB_HTML, _goto_article
from app.config import EXTRA_HEADERS, referrer_is_allowlisted
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

# Piano-like: static HTML is always the teaser. JS unlocks the full copy
# only when document.referrer is an allowlisted Google/X origin and the
# meter cookie is unset. Server-side, a Google/X Referer unlocks the same
# full copy only on a cross-site navigation (a real bounce). extra_http_headers
# Referer plus a direct goto stays a teaser — that is the Chromium bug.
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
</head>
<body>
  <article>
    <h1>Inside Kallas’ fightback against plans to tear up the EU’s diplomatic service</h1>
    <div id="story">{story}</div>
  </article>
  <script>
  (function () {{
    var granted = false;
    try {{
      var host = (new URL(document.referrer)).hostname;
      granted = (
        host === 'www.google.com' ||
        host === 'news.google.com' ||
        host === 'x.com' ||
        host === 't.co'
      );
    }} catch (e) {{
      granted = false;
    }}
    var used = /(?:^|;\\s*)meter=used(?:;|$)/.test(document.cookie);
    if ({js_unlock} && granted && !used) {{
      document.getElementById('story').innerHTML = {full_js};
    }} else {{
      document.cookie = 'meter=used; path=/';
    }}
    var marker = document.createElement('p');
    marker.id = 'amber-referrer';
    marker.textContent = document.referrer || 'EMPTY';
    document.body.appendChild(marker);
  }})();
  </script>
</body>
</html>
"""


def _page(referer: str, *, locked: bool, grant_server: bool) -> bytes:
    if grant_server and not locked:
        story = FULL_STORY_INNER
    else:
        story = (
            "<p>Commission diplomats said the plan would strip the service of its role.</p>"
            "<p>Subscribe to continue reading.</p>"
        )
    html = _PAGE.format(
        referer=referer.replace('"', ""),
        story=story,
        js_unlock="false" if locked else "true",
        full_js=repr(FULL_STORY_INNER),
    )
    return html.encode()


class _State:
    last_referer = ""
    last_cookie = ""
    last_fetch_site = ""
    hits = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        _State.last_referer = self.headers.get("Referer", "")
        _State.last_cookie = self.headers.get("Cookie", "")
        _State.last_fetch_site = self.headers.get("Sec-Fetch-Site", "")
        _State.hits += 1
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
            body = _page(_State.last_referer, locked=locked, grant_server=grant_server)
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


async def _fulfill_google_bounce(page) -> None:
    async def on_route(route):
        if "www.google.com" in route.request.url and route.request.resource_type == "document":
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=BOUNCE_STUB_HTML,
                headers={"Referrer-Policy": "origin"},
            )
            return
        await route.continue_()

    await page.route("**/*", on_route)


async def _playwright_referrer(
    url: str, *, bounce: bool, reuse_after_direct: bool = False
) -> tuple[str, str]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(extra_http_headers=EXTRA_HEADERS)
        page = await context.new_page()
        try:
            if reuse_after_direct:
                await page.goto(url, wait_until="domcontentloaded")
                await _fulfill_google_bounce(page)
                await _goto_article(page, url, "https://www.google.com/")
            elif bounce:
                await _fulfill_google_bounce(page)
                await _goto_article(page, url, "https://www.google.com/")
            else:
                await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_selector("#amber-referrer")
            text = await page.inner_text("#amber-referrer")
            story = await page.inner_text("#story")
        finally:
            await browser.close()
        return text, story


def test_extra_http_headers_referer_leaves_document_referrer_empty(metered_site):
    referrer, story = asyncio.run(_playwright_referrer(f"{metered_site}/article", bounce=False))
    assert referrer == "EMPTY"
    assert METERED_FULL_TOKEN not in story


def test_bounce_plus_goto_referer_sets_document_referrer(metered_site):
    referrer, story = asyncio.run(_playwright_referrer(f"{metered_site}/article", bounce=True))
    assert referrer.startswith("https://www.google.com")
    assert METERED_FULL_TOKEN in story


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


def test_capture_hard_paywall_stays_incomplete(tmp_data, allow_private, metered_site, monkeypatch):
    monkeypatch.setattr(
        "app.capture.REFERRER_BOUNCE_ORIGINS",
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
