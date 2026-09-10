"""End-to-end: local article page -> Playwright snapshot -> frozen HTML + screenshot + text."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x00\x03\x00\x01\x00\x05\xfe\xd4\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)

CSS = (
    "body{font-family:serif;background:#fff}h1{font-size:28px}"
    "img{width:40px;height:40px;background-image:url('/hero.png')}"
)
FONT = b"fixture font response"

HTML = """<!doctype html>
<html>
<head>
  <title>Ignore me</title>
  <meta property="og:title" content="City Council Approves Bridge">
  <meta property="og:site_name" content="The Daily Test">
  <meta property="og:description" content="The city council voted last night to fund a new bridge over the river. Construction starts in May.">
  <link rel="stylesheet" href="/style.css">
  <link rel="preload" href="/fixture.woff2" as="font" type="font/woff2" crossorigin>
  <script>document.title = "pwned"</script>
</head>
<body>
  <div id="paywall" class="paywall">Subscribe to continue</div>
    <article>
    <h1>City Council Approves Bridge</h1>
    <p>The city council voted last night to fund a new bridge over the river.</p>
    <p>Construction starts in May after a decade of debate among residents.</p>
    <p>Opponents said the money should have gone to buses and safer crossings
    near the school. Supporters answered that freight already clogs the old
    span every morning, and that another winter of patching the deck would
    cost more than starting over. The mayor called the vote a compromise.</p>
    <p>Work crews will close one lane of River Street while they set the
    piers. Local shops asked for weekend access and a printed map of detours.
    The council promised both, plus a public meeting before the first pour.</p>
    <img src="/hero.png" alt="bridge">
    <a href="javascript:alert(1)">bad</a>
    <form action="/login"><input name="password"><button>send</button></form>
    <iframe src="https://google-analytics.com.test/ad"></iframe>
  </article>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/style.css"):
            body, ctype = CSS.encode(), "text/css"
        elif self.path.startswith("/hero.png"):
            body, ctype = PNG, "image/png"
        elif self.path.startswith("/fixture.woff2"):
            body, ctype = FONT, "font/woff2"
        else:
            body, ctype = HTML.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}/article"
    server.shutdown()


def _wait_complete(client: TestClient, sid: str, seconds: int = 90) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{sid}").json()
        if job["status"] == "complete":
            return job
        if job["status"] == "failed":
            pytest.fail(job.get("error") or "capture failed")
        time.sleep(0.4)
    pytest.fail("capture timed out")


def test_end_to_end_archives_article(tmp_data, allow_private, local_site):
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/save", data={"url": local_site}, follow_redirects=False)
        assert r.status_code == 303
        sid = r.headers["location"].rsplit("/", 1)[-1]
        job = _wait_complete(client, sid)
        stats = job["capture_stats"]
        assert job["final_url"] == local_site
        assert stats["blocked_resources"] >= 1
        assert stats["saved_resources"] == 3
        assert stats["duration_ms"] > 0

        page = client.get(f"/{sid}")
        assert page.status_code == 200
        assert "article" in page.text
        assert "final" in page.text
        assert f"{stats['saved_resources']} assets" in page.text
        assert "saved" in page.text
        reader = client.get(f"/{sid}/reader")
        assert reader.status_code == 200
        assert "City Council Approves Bridge" in reader.text
        assert "voted last night" in reader.text

        raw = client.get(f"/{sid}/raw")
        assert raw.status_code == 200
        html = raw.text
        assert "<script" not in html.lower() or "script-src" in html
        assert "alert('xss')" not in html
        assert "<h1" in html.lower()
        assert "style=" in html
        assert "javascript:alert" not in html
        assert "amber-removed" in html
        assert f"/{sid}/r/" in html
        assert 'disabled="disabled"' in html or "disabled" in html

        resource = None
        needle = f"/{sid}/r/"
        for token in html.replace("'", '"').split('"'):
            if token.startswith(needle):
                resource = token[len(needle) :].split()[0]
                break
        assert resource, "expected rewritten local asset"
        asset = client.get(f"/{sid}/r/{resource}")
        assert asset.status_code == 200

        shot = client.get(f"/{sid}/image.jpg")
        assert shot.status_code == 200
        assert shot.content[:2] == b"\xff\xd8"
        assert len(shot.content) > 1000

        text = client.get(f"/{sid}/text", follow_redirects=True)
        assert text.status_code == 200

        from app import db

        meta = db.read_json(db.snap_dir(sid) / "meta.json")
        res_dir = db.snap_dir(sid) / "res"
        assert stats["bytes_saved"] == sum(
            path.stat().st_size for path in res_dir.iterdir() if path.is_file()
        )
        assert meta["capture_stats"] == stats
        assert meta["final_url"] == local_site
        from app import capture

        capture.jobs.pop(sid, None)
        archived_job = client.get(f"/api/jobs/{sid}").json()
        assert archived_job["capture_stats"] == stats
        assert archived_job["final_url"] == local_site
        origin = local_site.rsplit("/", 1)[0]
        for path, expected in (("/hero.png", PNG), ("/fixture.woff2", FONT)):
            filename = meta["resources"][origin + path]
            saved = client.get(f"/{sid}/r/{filename}")
            assert saved.status_code == 200
            assert saved.content == expected
        assert meta.get("referrer_retried") is False
        assert meta.get("referrer_bounce") is None


POLITICO_SHAPED = (
    Path(__file__).resolve().parent / "fixtures" / "politico_related_card.html"
).read_text(encoding="utf-8")


class PoliticoHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body, ctype = POLITICO_SHAPED.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def politico_shaped_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), PoliticoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}/article"
    server.shutdown()


def test_capture_keeps_article_body_not_related_card(
    tmp_data, allow_private, politico_shaped_site
):
    """PR #3 bounce is not the unlock: this HTML is already the full article.

    readability prefers the recirc card. Capture must store the article body
    anyway, without a bounce retry, and must not mark that card as complete.
    """
    from app.main import app
    from app import db

    with TestClient(app) as client:
        r = client.post(
            "/save", data={"url": politico_shaped_site}, follow_redirects=False
        )
        assert r.status_code == 303
        sid = r.headers["location"].rsplit("/", 1)[-1]
        _wait_complete(client, sid)

        text = (db.snap_dir(sid) / "article.txt").read_text(encoding="utf-8")
        assert "TOKEN_FULL_ARTICLE" in text
        snap = db.get_snapshot(sid)
        assert snap["paywalled"] is False
        assert snap["word_count"] >= 80
        meta = db.read_json(db.snap_dir(sid) / "meta.json")
        assert meta.get("referrer_retried") is False
        assert meta.get("referrer_bounce") is None
        reader = client.get(f"/{sid}/reader")
        assert reader.status_code == 200
        assert "TOKEN_FULL_ARTICLE" in reader.text
        assert "short preview" not in reader.text
