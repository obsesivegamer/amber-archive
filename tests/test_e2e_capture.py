"""End-to-end: local article page -> Playwright snapshot -> frozen HTML + screenshot + text."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x00\x03\x00\x01\x00\x05\xfe\xd4\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)

CSS = "body{font-family:serif;background:#fff}h1{font-size:28px}img{width:40px;height:40px}"

HTML = """<!doctype html>
<html>
<head>
  <title>Ignore me</title>
  <meta property="og:title" content="City Council Approves Bridge">
  <meta property="og:site_name" content="The Daily Test">
  <meta property="og:description" content="The city council voted last night to fund a new bridge over the river. Construction starts in May.">
  <link rel="stylesheet" href="/style.css">
  <script>document.title = "pwned"</script>
</head>
<body>
  <div id="paywall" class="paywall">Subscribe to continue</div>
  <article>
    <h1>City Council Approves Bridge</h1>
    <p>The city council voted last night to fund a new bridge over the river.</p>
    <p>Construction starts in May after a decade of debate among residents.</p>
    <img src="/hero.png" alt="bridge">
    <a href="javascript:alert(1)">bad</a>
    <form action="/login"><input name="password"><button>send</button></form>
    <iframe src="https://evil.example/ad"></iframe>
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
        _wait_complete(client, sid)

        page = client.get(f"/{sid}")
        assert page.status_code == 200
        assert "article" in page.text
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



