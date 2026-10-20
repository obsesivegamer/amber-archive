"""Feed proxy client: browser TLS, SSRF and size checks, urllib fallback."""

import socket

import pytest
from fastapi.testclient import TestClient

from app import capture, security, web
from app.main import app

FEED_URL = "https://example.com/rss.xml"
PUBLIC_IP = "93.184.216.34"
FEED = (
    b'<?xml version="1.0" encoding="ISO-8859-1"?>'
    b'<rss version="2.0"><channel><title>T</title></channel></rss>'
)


class _Opt:
    RESOLVE = "resolve"
    NOPROXY = "noproxy"
    TIMEOUT_MS = "timeout"
    CONNECTTIMEOUT_MS = "connect"
    LOW_SPEED_LIMIT = "limit"
    LOW_SPEED_TIME = "stall"


class _Code:
    OPERATION_TIMEDOUT = 28


class _RequestException(OSError):
    def __init__(self, msg="", code=0, response=None):
        super().__init__(msg)
        self.code = code
        self.response = response


class _Response:
    def __init__(
        self, *, status=200, headers=None, body=b"", chunks=None, ip=PUBLIC_IP, charset="utf-8"
    ):
        self.status_code = status
        self.headers = headers if headers is not None else {"Content-Type": "application/rss+xml; charset=utf-8"}
        self.charset_encoding = charset
        self.primary_ip = ip
        self._chunks = list(chunks) if chunks is not None else [body]
        self.closed = False
        self.read = False

    def iter_content(self, chunk_size=None, decode_unicode=False):
        self.read = True
        yield from self._chunks

    def close(self):
        self.closed = True


class _Script:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sessions = []
        self.calls = []

    def client(self):
        script = self

        class Session:
            def __init__(self, **kwargs):
                script.sessions.append(kwargs)
                self.kwargs = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, **kwargs):
                script.calls.append((url, kwargs))
                action = script.responses.pop(0)
                if isinstance(action, BaseException):
                    raise action
                return action

        return _Opt, _Code, Session, _RequestException


def _names_only(host):
    return host == "example.com" or host.endswith(".example.com")


def _install_dns(monkeypatch, answers):
    monkeypatch.setattr(security, "host_is_public", _names_only)

    def getaddrinfo(host, port, *args, **kwargs):
        ip = answers.get(host, PUBLIC_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(web.socket, "getaddrinfo", getaddrinfo)


def _forbid_urllib(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("urllib fallback ran")

    monkeypatch.setattr(capture, "_fetch", explode)


def test_browser_client_fetches_a_feed(monkeypatch):
    _install_dns(monkeypatch, {"example.com": PUBLIC_IP})
    _forbid_urllib(monkeypatch)
    script = _Script([_Response(body=FEED, charset="iso-8859-1")])
    monkeypatch.setattr(web, "_curl_client", script.client)
    body = web._fetch_feed(FEED_URL)
    assert b"<rss" in body
    assert b'encoding="utf-8"' in body
    assert len(script.sessions) == 1
    kwargs = script.sessions[0]
    assert kwargs["impersonate"] == web.FEED_IMPERSONATE == "chrome131"
    assert kwargs["trust_env"] is False
    assert kwargs["allow_redirects"] is False
    assert kwargs["max_redirects"] == 0
    options = kwargs["curl_options"]
    assert options[_Opt.RESOLVE] == [f"example.com:443:{PUBLIC_IP}"]
    assert options[_Opt.NOPROXY] == "*"
    assert options[_Opt.LOW_SPEED_LIMIT] == 1
    assert options[_Opt.LOW_SPEED_TIME] == int(web.FEED_FIRST_BYTE_S)
    assert 0 < options[_Opt.TIMEOUT_MS] <= int(web.FEED_DEADLINE_S * 1000)
    assert options[_Opt.CONNECTTIMEOUT_MS] <= int(web.FEED_FIRST_BYTE_S * 1000)
    url, call = script.calls[0]
    assert url == FEED_URL
    assert call["stream"] is True
    assert call["allow_redirects"] is False
    assert call["headers"]["User-Agent"]
    assert script.responses == []


def test_browser_client_rejects_a_private_peer_and_a_private_redirect(monkeypatch):
    _install_dns(monkeypatch, {"example.com": PUBLIC_IP})
    _forbid_urllib(monkeypatch)
    private_body = _Response(body=FEED, ip="10.0.0.1")
    script = _Script([private_body])
    monkeypatch.setattr(web, "_curl_client", script.client)
    with pytest.raises(web._FeedError) as peer:
        web._fetch_feed(FEED_URL)
    assert peer.value.status == 502
    assert "not allowed" in peer.value.detail
    assert private_body.read is False
    assert private_body.closed is True

    redirect = _Response(status=302, headers={"Location": "http://127.0.0.1/secret"}, body=b"")
    script = _Script([redirect, _Response(body=FEED)])
    monkeypatch.setattr(web, "_curl_client", script.client)
    with pytest.raises(web._FeedError) as bounced:
        web._fetch_feed(FEED_URL)
    assert bounced.value.status == 502
    assert "not allowed" in bounced.value.detail
    assert len(script.calls) == 1
    assert redirect.read is False
    assert redirect.closed is True


def test_browser_client_follows_a_public_redirect(monkeypatch):
    _install_dns(monkeypatch, {"example.com": PUBLIC_IP, "feeds.example.com": PUBLIC_IP})
    _forbid_urllib(monkeypatch)
    redirect = _Response(
        status=302, headers={"Location": "https://feeds.example.com/rss.xml"}, body=b""
    )
    script = _Script([redirect, _Response(body=FEED)])
    monkeypatch.setattr(web, "_curl_client", script.client)
    body = web._fetch_feed(FEED_URL)
    assert b"<rss" in body
    assert [url for url, _ in script.calls] == [FEED_URL, "https://feeds.example.com/rss.xml"]
    assert redirect.read is False
    assert redirect.closed is True
    assert all(session["impersonate"] == "chrome131" for session in script.sessions)
    assert script.sessions[1]["curl_options"][_Opt.RESOLVE] == [f"feeds.example.com:443:{PUBLIC_IP}"]


def test_browser_client_rejects_a_rebound_private_address(monkeypatch):
    _install_dns(monkeypatch, {"example.com": "127.0.0.1"})
    _forbid_urllib(monkeypatch)
    script = _Script([_Response(body=FEED)])
    monkeypatch.setattr(web, "_curl_client", script.client)
    with pytest.raises(web._FeedError) as exc:
        web._fetch_feed(FEED_URL)
    assert exc.value.status == 502
    assert "not allowed" in exc.value.detail
    assert script.sessions == []


def test_browser_client_rejects_an_oversized_feed(monkeypatch):
    _install_dns(monkeypatch, {"example.com": PUBLIC_IP})
    _forbid_urllib(monkeypatch)
    huge = _Response(chunks=[b"<?xml version='1.0'?><rss>", b"x" * (web.FEED_MAX_BYTES + 1)])
    script = _Script([huge])
    monkeypatch.setattr(web, "_curl_client", script.client)
    with pytest.raises(web._FeedError) as exc:
        web._fetch_feed(FEED_URL)
    assert exc.value.status == 502
    assert exc.value.detail == "The feed is larger than 5 MB."
    assert huge.closed is True


def test_browser_client_maps_a_stall_and_an_http_error(monkeypatch):
    _install_dns(monkeypatch, {"example.com": PUBLIC_IP})
    _forbid_urllib(monkeypatch)
    stalled = _RequestException("Operation timed out", code=_Code.OPERATION_TIMEDOUT)
    script = _Script([stalled])
    monkeypatch.setattr(web, "_curl_client", script.client)
    with pytest.raises(web._FeedError) as timeout:
        web._fetch_feed(FEED_URL)
    assert timeout.value.status == 504
    assert "15 s" in timeout.value.detail

    missing = _Response(status=404, headers={"Content-Type": "text/plain"}, body=b"nope")
    script = _Script([missing])
    monkeypatch.setattr(web, "_curl_client", script.client)
    with pytest.raises(web._FeedError) as http:
        web._fetch_feed(FEED_URL)
    assert http.value.status == 502
    assert http.value.detail == "The feed server answered HTTP 404."
    assert missing.read is False


def test_private_url_is_rejected_before_either_client(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("client ran")

    monkeypatch.setattr(web, "_curl_client", explode)
    monkeypatch.setattr(capture, "_fetch", explode)
    with pytest.raises(web._FeedError) as exc:
        web._fetch_feed("http://127.0.0.1/secret")
    assert exc.value.status == 400


def test_urllib_fallback_when_curl_is_not_installed(monkeypatch):
    seen = {}

    class _Headers:
        def get(self, name, default=None):
            return "application/rss+xml; charset=utf-8"

        def get_content_charset(self, failobj=None):
            return "utf-8"

    class _Resp:
        headers = _Headers()

    def fake_fetch(url, headers, deadline, max_bytes, **kwargs):
        seen["args"] = (url, headers, max_bytes, kwargs)
        kwargs["on_response"](_Resp())
        kwargs["on_first_chunk"](FEED)
        return FEED

    monkeypatch.setattr(web, "_curl_client", lambda: None)
    monkeypatch.setattr(capture, "_fetch", fake_fetch)
    monkeypatch.setattr(security, "host_is_public", _names_only)
    body = web._fetch_feed(FEED_URL)
    assert b"<rss" in body
    url, headers, max_bytes, kwargs = seen["args"]
    assert url == FEED_URL
    assert max_bytes == web.FEED_MAX_BYTES
    assert kwargs["strict"] is True
    assert kwargs["socket_timeout"] == web.FEED_DEADLINE_S
    assert headers["Accept"] == web.FEED_ACCEPT


def test_curl_client_is_none_when_the_import_fails(monkeypatch):
    import sys

    real_import = __import__
    cached = {
        name: module
        for name, module in list(sys.modules.items())
        if name == "curl_cffi" or name.startswith("curl_cffi.")
    }
    for name in cached:
        sys.modules.pop(name, None)

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "curl_cffi" or name.startswith("curl_cffi."):
            raise ImportError("curl_cffi is not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", guarded)
    try:
        assert web._curl_client() is None
    finally:
        sys.modules.update(cached)


def test_installed_curl_client_impersonates_chrome():
    curl_cffi = pytest.importorskip("curl_cffi")
    from curl_cffi.requests import BrowserType

    client = web._curl_client()
    assert client is not None
    assert curl_cffi.__version__ == "0.9.0"
    assert "chrome131" in BrowserType.__members__
    assert web.FEED_IMPERSONATE in BrowserType.__members__


def test_feedproxy_route_uses_the_browser_client(tmp_data, monkeypatch):
    _install_dns(monkeypatch, {"example.com": PUBLIC_IP})
    _forbid_urllib(monkeypatch)
    script = _Script([_Response(body=FEED)])
    monkeypatch.setattr(web, "_curl_client", script.client)
    with TestClient(app) as client:
        response = client.get("/api/feedproxy", params={"url": FEED_URL})
    assert response.status_code == 200
    assert "xml" in response.headers["content-type"]
    assert b"<rss" in response.content
    assert script.sessions[0]["impersonate"] == "chrome131"
