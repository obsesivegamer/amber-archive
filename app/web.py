"""Routes for the phone web app: its static files, a feed proxy and two small stores.

Everything here is same-origin only: Amber sends no CORS headers, the Host
allow-list and the cross-site middleware in main.py run first, and the feed
proxy refuses anything that is not the app's own fetch().
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import mimetypes
import os
import re
import tempfile
import threading
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import capture, db
from .api import _read_body_capped
from .config import USER_AGENT, WEBAPP_DIR
from .security import validate_public_http_url

log = logging.getLogger("amber.web")
router = APIRouter()

FEED_MAX_BYTES = 5 * 1024 * 1024
FEED_DEADLINE_S = 15.0
FEED_MAX_IN_FLIGHT = 8
FEED_MAX_WAITING = 64
FEED_SLOT_WAIT_S = 8.0
_feed_waiting = 0
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"
_FEED_TYPE_HINTS = ("xml", "rss", "atom", "text/plain", "text/html")
_FEED_ROOTS = {"rss", "feed", "rdf", "urlset", "sitemapindex"}
_feed_slots = threading.BoundedSemaphore(FEED_MAX_IN_FLIGHT)

MAX_HOMEPAGE_CODE_BYTES = 2 * 1024 * 1024
MAX_HOMEPAGE_BODY_BYTES = 4 * 1024 * 1024
MAX_SOURCES = 200
MAX_SOURCE_FEEDS = 10
MAX_SOURCES_BODY_BYTES = 5 * 1024 * 1024
_store_lock = threading.Lock()

WEBAPP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; font-src 'self' data:; connect-src 'self'; "
    "frame-src https://*.codesandbox.io; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp_iso() -> str:
    """Version stamp for a stored list: microseconds, so two writes in one second differ."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json_error(status: int, detail: str, **headers: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers or None)


# --- feed proxy -------------------------------------------------------------


class _FeedError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _feed_root(text: str) -> str | None:
    """Local name of the document element, after the XML prolog; None when
    the text is not a plain XML document (or declares an internal DTD subset,
    which a feed has no use for and which is how entity bombs are built)."""
    text = text[:65536]
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("<?", i):
            j = text.find("?>", i + 2)
            i = n if j < 0 else j + 2
        elif text.startswith("<!--", i):
            j = text.find("-->", i + 4)
            i = n if j < 0 else j + 3
        elif text[i : i + 9].upper() == "<!DOCTYPE":
            j, quote = i + 9, ""
            while j < n:
                ch = text[j]
                if quote:
                    if ch == quote:
                        quote = ""
                elif ch in "\"'":
                    quote = ch
                elif ch == "[":
                    return None
                elif ch == ">":
                    break
                j += 1
            if j >= n:
                return None
            i = j + 1
        else:
            break
    match = re.compile(r"<([A-Za-z_][\w.:-]*)").match(text, i)
    return match.group(1).rsplit(":", 1)[-1].lower() if match else None


def _feed_to_utf8(body: bytes, header_charset: str | None) -> str:
    if body.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif body.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        declared = re.match(rb"\s*<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._-]+)[\"']", body[:300])
        encoding = declared.group(1).decode("ascii") if declared else (header_charset or "utf-8")
    try:
        text = body.decode(encoding, errors="replace")
    except (LookupError, ValueError):
        text = body.decode("utf-8", errors="replace")
    # The bytes we send are UTF-8 whatever the feed declared.
    return re.sub(r"(\A\s*<\?xml[^>]*?encoding\s*=\s*)([\"'])[^\"']*\2", r"\1\2utf-8\2", text, count=1)


def _fetch_feed(raw_url: str) -> bytes:
    deadline = time.monotonic() + FEED_DEADLINE_S
    try:
        clean = validate_public_http_url(raw_url)
    except ValueError as exc:
        raise _FeedError(400, str(exc)) from exc

    seen: dict[str, str | None] = {}

    def on_response(resp) -> None:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not any(hint in ctype for hint in _FEED_TYPE_HINTS):
            raise _FeedError(502, "That address did not return a feed.")
        seen["charset"] = resp.headers.get_content_charset()

    def on_first_chunk(chunk: bytes) -> None:
        # Cheap early refusal of HTML/JSON/binary; whether the document element
        # is a feed's is decided on the whole body by _feed_root.
        head = chunk.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if not head or head.startswith(b"<") and not head.startswith((b"<html", b"<!doctype html")):
            return
        if not chunk.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise _FeedError(502, "That address did not return a feed.")

    try:
        body = capture._fetch(
            clean,
            {"User-Agent": USER_AGENT, "Accept": FEED_ACCEPT, "Accept-Language": "en-US,en;q=0.9"},
            deadline,
            FEED_MAX_BYTES,
            strict=True,
            on_response=on_response,
            on_first_chunk=on_first_chunk,
        )
    except _FeedError:
        raise
    except capture.FetchTooLarge:
        raise _FeedError(502, "The feed is larger than 5 MB.") from None
    except TimeoutError:
        raise _FeedError(504, "The feed took too long to answer (15 s limit).") from None
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise _FeedError(502, f"The feed server answered HTTP {status}.") from None
    except ValueError:
        raise _FeedError(502, "The feed resolves or redirects to an address that is not allowed.") from None
    except (OSError, http.client.HTTPException):
        raise _FeedError(502, "Could not fetch the feed.") from None

    text = _feed_to_utf8(body, seen.get("charset"))
    if _feed_root(text) not in _FEED_ROOTS:
        raise _FeedError(502, "That address did not return a feed.")
    return text.encode("utf-8", errors="replace")


def _refuse_non_app_fetch(request: Request) -> JSONResponse | None:
    """Only the web app's own fetch() may use the proxy.

    Cross-site pages are refused, and so is any same-origin <img>, <iframe>,
    stylesheet or navigation (such as a captured page the viewer serves on this
    origin). Requests without Fetch Metadata headers (curl) pass.
    """
    h = request.headers
    if h.get("sec-fetch-site") in {"cross-site", "same-site"}:
        return _json_error(403, "Cross-site requests are not allowed.")
    dest = h.get("sec-fetch-dest")
    if (dest is not None and dest != "empty") or h.get("sec-fetch-mode") in {"no-cors", "navigate"}:
        return _json_error(403, "The feed proxy is only for the web app's own requests.")
    return None


@router.get("/api/feedproxy")
async def feedproxy(request: Request, url: str = ""):
    refused = _refuse_non_app_fetch(request)
    if refused is not None:
        return refused
    if not url.strip():
        return _json_error(400, "Pass the feed address in ?url=.")
    # The app asks for every feed of every source at once, so wait a little for
    # a slot rather than failing the overflow. At most 8 fetches run; the number
    # of waiters is capped too. Not awaited between acquire and the executor
    # hand-off, so 8 is a hard cap. The slot is freed when the thread really
    # ends, even if this request is cancelled or times out first.
    global _feed_waiting
    if not _feed_slots.acquire(blocking=False):
        if _feed_waiting >= FEED_MAX_WAITING:
            return _json_error(429, "Too many feeds are being fetched. Try again shortly.", **{"Retry-After": "2"})
        _feed_waiting += 1
        try:
            give_up = time.monotonic() + FEED_SLOT_WAIT_S
            while not _feed_slots.acquire(blocking=False):
                if time.monotonic() >= give_up:
                    return _json_error(
                        429, "Too many feeds are being fetched. Try again shortly.", **{"Retry-After": "2"}
                    )
                await asyncio.sleep(0.05)
        finally:
            _feed_waiting -= 1
    loop = asyncio.get_running_loop()
    try:
        fut = loop.run_in_executor(None, _fetch_feed, url)
    except BaseException:
        _feed_slots.release()
        raise

    def _done(f: asyncio.Future) -> None:
        _feed_slots.release()
        if not f.cancelled():
            f.exception()

    fut.add_done_callback(_done)
    try:
        body = await asyncio.wait_for(asyncio.shield(fut), FEED_DEADLINE_S + 2)
    except TimeoutError:
        return _json_error(504, "The feed took too long to answer (15 s limit).")
    except _FeedError as exc:
        return _json_error(exc.status, exc.detail)
    return Response(
        body,
        media_type="application/xml; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            # Feed XML can carry XHTML-namespace <script>; keep it inert if
            # something ever navigates here.
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


# --- homepage and sources stores ---------------------------------------------


def _write_json_unlocked(name: str, obj: dict) -> None:
    data_dir = db.DATA_DIR
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, data_dir / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json_atomic(name: str, obj: dict) -> None:
    with _store_lock:
        _write_json_unlocked(name, obj)


def _put_sources(sources: list[dict], expected: str | None, updated_at: str) -> dict | None:
    """Write the list; when `expected` is given and the stored list has a
    different stamp, write nothing and return the stored document instead."""
    with _store_lock:
        if expected is not None:
            stored = _read_json("sources.json")
            if stored is not None and stored.get("updated_at") != expected:
                return stored
        _write_json_unlocked("sources.json", {"sources": sources, "updated_at": updated_at})
    return None


def _read_json(name: str) -> dict | None:
    try:
        data = json.loads((db.DATA_DIR / name).read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.exception("could not read %s", name)
        return None
    return data if isinstance(data, dict) else None


async def _json_body(request: Request, limit: int) -> dict:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "Send the request as application/json.")
    raw = await _read_body_capped(request, limit)
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, "Body must be valid JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object.")
    return body


def _normalize_timestamp(value: object) -> str:
    if value is None:
        return _now_iso()
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise HTTPException(400, '"timestamp" must be an ISO 8601 string.')
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, '"timestamp" must be an ISO 8601 string.') from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/api/homepage")
async def get_homepage():
    stored = await asyncio.to_thread(_read_json, "homepage.json")
    if (
        stored is None
        or not isinstance(stored.get("code"), str)
        or not isinstance(stored.get("timestamp"), str)
        or not isinstance(stored.get("bytes"), int)
    ):
        return _json_error(404, "No homepage yet", **{"Cache-Control": "no-store"})
    return JSONResponse(
        {"code": stored["code"], "timestamp": stored["timestamp"], "bytes": stored["bytes"]},
        headers={"Cache-Control": "no-store"},
    )


@router.put("/api/homepage")
async def put_homepage(request: Request):
    body = await _json_body(request, MAX_HOMEPAGE_BODY_BYTES)
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(400, '"code" must be a non-empty string.')
    try:
        size = len(code.encode("utf-8"))
    except UnicodeEncodeError:
        raise HTTPException(400, '"code" is not valid Unicode text.') from None
    if size > MAX_HOMEPAGE_CODE_BYTES:
        raise HTTPException(413, '"code" is larger than 2 MB.')
    timestamp = _normalize_timestamp(body.get("timestamp"))
    await asyncio.to_thread(
        _write_json_atomic, "homepage.json", {"code": code, "timestamp": timestamp, "bytes": size}
    )
    return JSONResponse({"bytes": size, "timestamp": timestamp}, headers={"Cache-Control": "no-store"})


def _clean_sources(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise HTTPException(400, '"sources" must be a list.')
    if len(raw) > MAX_SOURCES:
        raise HTTPException(400, f'"sources" has more than {MAX_SOURCES} items.')
    cleaned: list[dict] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        where = f"sources[{index}]"
        if not isinstance(item, dict):
            raise HTTPException(400, f"{where} must be an object.")
        sid, name, feeds, enabled = item.get("id"), item.get("name"), item.get("feedUrls"), item.get("enabled")
        if not isinstance(sid, str) or not 1 <= len(sid) <= 64:
            raise HTTPException(400, f'{where}.id must be a string of 1 to 64 characters.')
        if sid in seen_ids:
            raise HTTPException(400, f"{where}.id is a duplicate.")
        seen_ids.add(sid)
        if not isinstance(name, str) or len(name) > 120:
            raise HTTPException(400, f"{where}.name must be a string of at most 120 characters.")
        if not isinstance(feeds, list) or len(feeds) > MAX_SOURCE_FEEDS:
            raise HTTPException(400, f"{where}.feedUrls must be a list of at most {MAX_SOURCE_FEEDS} addresses.")
        for feed in feeds:
            if (
                not isinstance(feed, str)
                or len(feed) > 2000
                or not re.match(r"https?://[^\s/?#]", feed, re.IGNORECASE)
                or re.search(r"\s", feed)
            ):
                raise HTTPException(400, f"{where}.feedUrls must hold http(s) addresses of at most 2000 characters.")
        if not isinstance(enabled, bool):
            raise HTTPException(400, f"{where}.enabled must be true or false.")
        cleaned.append({"id": sid, "name": name, "feedUrls": list(feeds), "enabled": enabled})
    try:
        json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(400, '"sources" holds text that is not valid Unicode.') from None
    return cleaned


@router.get("/api/sources")
async def get_sources():
    stored = await asyncio.to_thread(_read_json, "sources.json")
    if (
        stored is None
        or not isinstance(stored.get("sources"), list)
        or not isinstance(stored.get("updated_at"), str)
    ):
        return _json_error(404, "No sources yet", **{"Cache-Control": "no-store"})
    return JSONResponse(
        {"sources": stored["sources"], "updated_at": stored["updated_at"]},
        headers={"Cache-Control": "no-store"},
    )


@router.put("/api/sources")
async def put_sources(request: Request):
    body = await _json_body(request, MAX_SOURCES_BODY_BYTES)
    sources = _clean_sources(body.get("sources"))
    expected = body.get("if_updated_at")
    if expected is not None and not isinstance(expected, str):
        raise HTTPException(400, '"if_updated_at" must be a string.')
    updated_at = _stamp_iso()
    conflict = await asyncio.to_thread(_put_sources, sources, expected, updated_at)
    if conflict is not None:
        return JSONResponse(
            {
                "detail": "Sources changed on another device.",
                "updated_at": conflict.get("updated_at"),
                "sources": conflict.get("sources", []),
            },
            status_code=409,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse({"count": len(sources), "updated_at": updated_at}, headers={"Cache-Control": "no-store"})


# --- static web app ------------------------------------------------------------

_WEBAPP_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".txt": "text/plain; charset=utf-8",
}
# Vite names bundles name-<8+ hash chars>.ext under assets/.
_HASHED_ASSET_RE = re.compile(r"^assets/.+[-.][A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")


def _webapp_file(root: Path, rel: str) -> Path | None:
    """The regular file `rel` names inside `root`, or None. Symlinks are
    followed and must still land inside `root`."""
    try:
        candidate = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def _is_unsafe_path(rel: str) -> bool:
    """Absolute, backslash, NUL or any dot segment (`..`, `.env`, `.vite/`): never served."""
    dotted = any(part.startswith(".") for part in rel.split("/") if part)
    return rel.startswith("/") or "\\" in rel or "\x00" in rel or dotted


@router.api_route("/app", methods=["GET", "HEAD"])
async def webapp_redirect():
    return RedirectResponse("/app/", status_code=308)


@router.api_route("/app/{rel:path}", methods=["GET", "HEAD"])
async def webapp(rel: str):
    root = WEBAPP_DIR
    if root is None:
        return PlainTextResponse(
            "The web app is not installed: set AMBER_WEBAPP_DIR to a built web app folder and restart Amber.",
            status_code=404,
        )
    index = root / "index.html"
    if not index.is_file():
        return PlainTextResponse(
            "AMBER_WEBAPP_DIR has no index.html: build the web app and restart Amber.", status_code=404
        )
    if _is_unsafe_path(rel):
        return PlainTextResponse("Not found", status_code=404)
    target = _webapp_file(root, rel) if rel else None
    if target is None:
        last = rel.rsplit("/", 1)[-1]
        # A missing script, stylesheet or image is a 404, not the HTML shell.
        if rel and (rel.startswith("assets/") or "." in last):
            return PlainTextResponse("Not found", status_code=404)
        target, rel = index.resolve(), "index.html"
    kind = _WEBAPP_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0]
    hashed = bool(_HASHED_ASSET_RE.match(rel))
    return FileResponse(
        target,
        media_type=kind or "application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable" if hashed else "no-cache",
            "Content-Security-Policy": WEBAPP_CSP,
            "X-Content-Type-Options": "nosniff",
        },
    )
