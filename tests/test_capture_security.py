"""Exercise capture URL checks at the browser and urllib transport boundaries."""

import asyncio
import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from app import capture


@pytest.fixture
def guarded_site(monkeypatch):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            hits.append((self.path, self.headers.get("User-Agent")))
            if self.path in {"/redirect", "/redirect-private"}:
                destination = (
                    "/redirect-private" if self.path == "/redirect"
                    else f"http://127.0.0.1:{self.server.server_port}/blocked-redirect"
                )
                self.send_response(302)
                self.send_header("Location", destination)
                self.end_headers()
                return
            if self.path == "/article":
                body = f"""<!doctype html><title>Guard fixture</title>
                <article><h1>Guard fixture</h1><p>Public article content.</p></article>
                <script>
                  fetch('/allowed-resource');
                  fetch('http://127.0.0.1:{self.server.server_port}/blocked-resource')
                    .catch(() => {{}});
                  window.open('http://127.0.0.1:{self.server.server_port}/blocked-popup');
                </script>""".encode()
            else:
                body = b"PUBLIC_RESPONSE"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_getaddrinfo = socket.getaddrinfo

    def public_dns(host, port, *args, **kwargs):
        if host == "public.amber.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    # Keep validate_public_http_url intact. Only the test publisher's DNS and
    # connection destination are substituted; literal private URLs stay blocked.
    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    try:
        yield f"http://public.amber.test:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_crawler_checks_initial_url_and_every_redirect(guarded_site, monkeypatch):
    url, hits = guarded_site
    connections = []

    def connect(connection):
        connections.append(connection.host)
        connection.sock = socket.create_connection(("127.0.0.1", connection.port), connection.timeout)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", connect)
    monkeypatch.setattr(capture.urllib.request, "getproxies", lambda: {})

    with pytest.raises(ValueError, match="Private or local"):
        capture._http_get("http://127.0.0.1/blocked-initial", "Amber test")
    assert connections == []

    assert capture._http_get(url + "/allowed", "Amber test") == "PUBLIC_RESPONSE"
    with pytest.raises(ValueError, match="Private or local"):
        capture._http_get(url + "/redirect", "Amber test")
    assert hits == [
        ("/allowed", "Amber test"),
        ("/redirect", "Amber test"),
        ("/redirect-private", "Amber test"),
    ]
    assert connections == ["public.amber.test"] * 3


def test_browser_rejects_private_initial_url_before_creating_context():
    browser = AsyncMock()
    with pytest.raises(ValueError, match="Private or local"):
        asyncio.run(capture._capture_visit(browser, {}, "http://127.0.0.1/blocked"))
    browser.new_context.assert_not_called()


def test_browser_closes_context_when_setup_fails():
    browser = AsyncMock()
    context = browser.new_context.return_value
    context.new_page.side_effect = RuntimeError("fixture setup failure")
    with pytest.raises(RuntimeError, match="fixture setup failure"):
        asyncio.run(capture._capture_visit(browser, {}, "http://93.184.216.34/article"))
    context.close.assert_awaited_once()


@pytest.mark.browser
def test_browser_blocks_private_subresources_and_popups(guarded_site):
    url, hits = guarded_site

    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=[
                "--host-resolver-rules=MAP public.amber.test 127.0.0.1",
                "--no-proxy-server",
            ])
            try:
                visit = await capture._capture_visit(browser, {}, url + "/article")
                try:
                    assert visit.http_status == 200
                    assert visit.final_url == url + "/article"
                    assert "Public article content" in visit.article["article_text"]
                    assert len(visit.context.pages) == 2  # The popup was attempted.
                finally:
                    await visit.close()
            finally:
                await browser.close()

    asyncio.run(run())
    paths = [path for path, _ in hits]
    assert "/article" in paths
    assert "/allowed-resource" in paths
    assert not any(path.startswith("/blocked") for path in paths)
