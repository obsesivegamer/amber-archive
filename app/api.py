from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from . import capture, db
from .config import ID_ALPHABET, ID_LENGTH
from .ingest import ingest_html, screenshot_reader
from .security import article_match_key, normalize_url, validate_public_http_url

log = logging.getLogger("amber.api")
router = APIRouter(prefix="/api")

ID_RE = re.compile(rf"^[{re.escape(ID_ALPHABET)}]{{{ID_LENGTH}}}$")
MIN_USABLE_WORDS = 120
MAX_SINCE_DAYS = 365
MAX_LIMIT = 200
MAX_SAVE_BODY_BYTES = 16 * 1024
MAX_URL_LENGTH = 2048
MAX_IMPORT_BODY_BYTES = 12 * 1024 * 1024
MIN_IMPORT_HTML_CHARS = 200
MAX_IMPORT_TITLE_CHARS = 300
MAX_IMPORTS_IN_FLIGHT = 5
# Paywall stubs (FT "Subscribe to unlock this article" plus the price list) are
# 150-330 words of offers. Real articles that merely mention subscribing are
# longer, or carry the notice at the tail, so only a leading notice counts.
STUB_MAX_WORDS = 400
STUB_RE = re.compile(r"subscribe to (?:unlock|read)|already a subscriber", re.IGNORECASE)

# article.html still carries site chrome (NYT menus, related-story lists, video
# shortcuts). Block tags give paragraph breaks; when the page marks its article
# body, only that is read. Leaf <div>s are used only for pages that keep the
# body in divs (Reuters), where the block tags recover under half the text.
CHROME_TAGS = ["script", "style", "noscript", "svg", "nav", "dialog", "button", "footer", "aside", "figure", "video", "audio"]
BODY_SELECTOR = 'section[name="articleBody"], [itemprop="articleBody"]'
BLOCK_SELECTOR = "p, h2, h3, h4, blockquote, li, [class*='video-description']"
FILLER_RE = re.compile(
    r"what readers are saying|related content|trending in the times|editors[\u2019']? picks"
    r"|skip to content(?: skip to site index)?|\d+ min read"
    r"|roula khalaf, editor of the ft, selects.*|simply sign up to the .* myft digest.*",
    re.IGNORECASE,
)

HOSTNAME_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.IGNORECASE)
SITE_BY_HOST = {
    "nytimes.com": "The New York Times",
    "ft.com": "Financial Times",
    "wsj.com": "The Wall Street Journal",
    "washingtonpost.com": "The Washington Post",
    "newyorker.com": "The New Yorker",
    "politico.com": "POLITICO",
    "theatlantic.com": "The Atlantic",
    "theguardian.com": "The Guardian",
    "theintercept.com": "The Intercept",
    "arstechnica.com": "Ars Technica",
}


def _bare_host(host: str) -> str:
    host = host.strip().lower()
    return host[4:] if host.startswith("www.") else host


def _url_host(url: str | None) -> str:
    return _bare_host(urlparse(url or "").hostname or "")


def _site_name(snap: dict) -> str:
    name = (snap.get("site_name") or "").strip()
    if name and not HOSTNAME_RE.fullmatch(name):
        return name
    host = _bare_host(name) or _url_host(snap.get("final_url")) or _url_host(snap.get("url"))
    for known, display in SITE_BY_HOST.items():
        if host == known or host.endswith("." + known):
            return display
    return host


def _title_is_just_a_name(snap: dict, site: str) -> bool:
    title = _bare_host(snap.get("title") or "")
    if not title:
        return True
    names = {
        _bare_host(snap.get("site_name") or ""),
        site.lower(),
        _url_host(snap.get("final_url")),
        _url_host(snap.get("url")),
    }
    return title in names


def _read(sid: str, name: str) -> str:
    try:
        return (db.snap_dir(sid) / name).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def _blocks(scopes: list, selector: str) -> str:
    paragraphs = []
    seen: set[int] = set()
    for scope in scopes:
        for el in scope.select(selector):
            if id(el) in seen or el.select_one(selector):
                continue
            seen.add(id(el))
            text = " ".join(el.get_text(" ", strip=True).split())
            if text and not FILLER_RE.fullmatch(text):
                paragraphs.append(text)
    return "\n\n".join(paragraphs)


@lru_cache(maxsize=512)
def _derive_text(sid: str, stamp: tuple) -> str:
    flat = _read(sid, "article.txt")
    html = _read(sid, "article.html")
    if not html:
        return flat
    soup = BeautifulSoup(html, "lxml")
    for el in soup.find_all(CHROME_TAGS):
        el.decompose()
    body = soup.select(BODY_SELECTOR)
    text = _blocks(body or [soup], BLOCK_SELECTOR)
    if not body and len(text.split()) * 2 < len(flat.split()):
        with_divs = _blocks([soup], BLOCK_SELECTOR + ", div")
        if len(with_divs) > len(text):
            text = with_divs
    return text or flat


def _article_text(sid: str) -> str:
    folder = db.snap_dir(sid)
    stamp = []
    for name in ("article.txt", "article.html"):
        try:
            stamp.append(folder.joinpath(name).stat().st_mtime_ns)
        except OSError:
            stamp.append(0)
    return _derive_text(sid, tuple(stamp))


def _is_usable(snap: dict, site: str) -> bool:
    if snap["status"] != "complete":
        return False
    if int(snap.get("word_count") or 0) < MIN_USABLE_WORDS:
        return False
    if _title_is_just_a_name(snap, site):
        return False
    text = _article_text(snap["id"])
    words = len(text.split())
    if words < MIN_USABLE_WORDS:
        return False
    return not (words < STUB_MAX_WORDS and STUB_RE.search(text[:300]))


def _summary(snap: dict, site: str) -> dict:
    return {
        "id": snap["id"],
        "url": snap["url"],
        "final_url": snap.get("final_url"),
        "title": snap["title"],
        "site": site,
        "author": snap.get("author"),
        "published_at": snap.get("published_at"),
        "created_at": snap["created_at"],
        "word_count": snap["word_count"],
        "paywalled": snap["paywalled"],
    }


@router.get("/snapshots")
def list_snapshots(since_days: int = 14, limit: int = 60):
    since_days = max(1, min(since_days, MAX_SINCE_DAYS))
    limit = max(1, min(limit, MAX_LIMIT))
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    usable: list[tuple[dict, str]] = []
    seen_urls: set[str] = set()
    for snap in db.complete_since(cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")):
        if snap["url_normalized"] in seen_urls:
            continue
        site = _site_name(snap)
        if _is_usable(snap, site):
            seen_urls.add(snap["url_normalized"])
            usable.append((snap, site))

    sources: dict[str, dict] = {}
    for snap, site in usable:
        entry = sources.setdefault(site, {"site": site, "count": 0, "latest": snap["created_at"]})
        entry["count"] += 1
        entry["latest"] = max(entry["latest"], snap["created_at"])

    return {
        "total_usable": len(usable),
        "sources": sorted(sources.values(), key=lambda s: (-s["count"], s["site"])),
        "snapshots": [_summary(snap, site) for snap, site in usable[:limit]],
    }


@router.get("/snapshots/{sid}/text")
def snapshot_article_text(sid: str):
    snap = db.get_snapshot(sid) if ID_RE.fullmatch(sid) else None
    if snap:
        site = _site_name(snap)
        text = _article_text(snap["id"]) if _is_usable(snap, site) else ""
        if text:
            return {**_summary(snap, site), "text": text}
    raise HTTPException(404, "No usable article text for this snapshot")


def effective_status(snap: dict) -> str:
    """Snapshot status a client can rely on: queued, capturing, complete or failed.

    Jobs live in memory, so a row still pending/capturing with no live job was
    interrupted by a restart and will never finish.
    """
    status = snap["status"]
    if status in {"pending", "capturing"}:
        job = capture.jobs.get(snap["id"])
        if job and job["status"] in {"queued", "capturing"}:
            return job["status"]
        return "failed"
    return status


def _find_matches(url: str) -> list[dict]:
    """Snapshots of any status for this article, newest first.

    Compares article_match_key of the saved URL, its normalized form and the
    post-redirect final URL against the key of `url`.
    """
    key = article_match_key(url)
    if not key:
        return []
    parsed = urlparse(url if "://" in url else "https://" + url)
    candidates = db.find_candidates(parsed.hostname or "", parsed.path.rstrip("/") or "/")
    return [
        snap
        for snap in candidates
        if key
        in (
            article_match_key(snap["url_normalized"]),
            article_match_key(snap["url"]),
            article_match_key(snap.get("final_url")),
        )
    ]


def _best_match(matches: list[dict]) -> tuple[dict | None, str, bool]:
    """Pick the snapshot a client should use: (snapshot, effective status, usable).

    Preference: complete and usable, then in flight, then complete but
    unusable, then failed. Each group is newest first.
    """
    complete: list[dict] = []
    in_flight: dict | None = None
    failed: dict | None = None
    for snap in matches:
        status = effective_status(snap)
        if status == "complete":
            complete.append(snap)
        elif status in {"queued", "capturing"}:
            in_flight = in_flight or snap
        else:
            failed = failed or snap
    for snap in complete:
        if _is_usable(snap, _site_name(snap)):
            return snap, "complete", True
    if in_flight:
        return in_flight, effective_status(in_flight), False
    if complete:
        return complete[0], "complete", False
    if failed:
        return failed, "failed", False
    return None, "", False


def _lookup_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Pass the article URL in ?url=.")
    if len(raw) > MAX_URL_LENGTH:
        raise ValueError("URL is too long.")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only http and https URLs can be looked up.")
    return raw


@router.get("/lookup")
def lookup(response: Response, url: str = ""):
    response.headers["Cache-Control"] = "no-store"
    try:
        clean = _lookup_url(url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    snap, status, usable = _best_match(_find_matches(clean))
    if snap is None:
        return {"found": False}
    return {
        "found": True,
        "id": snap["id"],
        "status": status,
        "usable": usable,
        "title": snap.get("title"),
        "site": _site_name(snap),
        "url": snap["url"],
        "final_url": snap.get("final_url"),
        "created_at": snap["created_at"],
    }


@router.post("/save")
async def save(request: Request):
    # Only a JSON body is accepted. A cross-site <form> cannot send
    # application/json, and a cross-site fetch() that does needs a CORS
    # preflight, which this app never grants.
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "Send the request as application/json.")
    raw = await request.body()
    if len(raw) > MAX_SAVE_BODY_BYTES:
        raise HTTPException(413, "Request body is too large.")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, "Body must be valid JSON.") from exc
    if not isinstance(body, dict) or not isinstance(body.get("url"), str):
        raise HTTPException(400, 'Body must be a JSON object with a string "url".')
    force = body.get("force", False)
    if force is None:
        force = False
    if not isinstance(force, bool):
        raise HTTPException(400, '"force" must be true or false.')

    try:
        clean = await asyncio.to_thread(validate_public_http_url, body["url"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    normalized = normalize_url(clean)

    snap, status, usable = await asyncio.to_thread(
        lambda: _best_match(_find_matches(clean))
    )
    if snap is not None and not force and status == "complete":
        return JSONResponse({"id": snap["id"], "status": "exists", "usable": usable}, status_code=200)
    if snap is not None and status in {"queued", "capturing"}:
        return JSONResponse({"id": snap["id"], "status": status}, status_code=202)

    # No await between the in-flight check inside enqueue_capture and the
    # enqueue itself, so parallel saves of one URL collapse into one job.
    try:
        job_id = capture.enqueue_capture(clean, normalized)
    except capture.QueueFull:
        raise HTTPException(429, "Too many saves are already queued. Try again shortly.") from None
    return JSONResponse(
        {"id": job_id, "status": capture.jobs[job_id]["status"]}, status_code=202
    )


_imports_in_flight = 0
# One Chromium at a time for import thumbnails.
_screenshot_gate = asyncio.Semaphore(1)


async def _read_body_capped(request: Request, limit: int) -> bytes:
    """Read the request body, refusing with 413 as soon as it passes `limit`."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "Request body is too large.")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, "Request body is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


async def _screenshot_import(sid: str) -> None:
    try:
        async with _screenshot_gate:
            await screenshot_reader(sid, offline=True)
    except Exception:
        log.exception("reader screenshot failed for imported snapshot %s", sid)


@router.post("/import")
async def import_page(request: Request):
    """Store HTML a signed-in browser already rendered as a new, complete snapshot.

    Nothing is fetched: the HTML is parsed and frozen exactly as posted.
    """
    global _imports_in_flight
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "Send the request as application/json.")
    # No await between this check and the increment.
    if _imports_in_flight >= MAX_IMPORTS_IN_FLIGHT:
        raise HTTPException(429, "Too many imports are already running. Try again shortly.")
    _imports_in_flight += 1
    try:
        raw = await _read_body_capped(request, MAX_IMPORT_BODY_BYTES)
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "Body must be valid JSON.") from exc
        del raw
        if not isinstance(body, dict) or not isinstance(body.get("url"), str):
            raise HTTPException(400, 'Body must be a JSON object with a string "url".')
        html = body.get("html")
        if not isinstance(html, str) or len(html.strip()) < MIN_IMPORT_HTML_CHARS:
            raise HTTPException(
                400, f'"html" must be a string of at least {MIN_IMPORT_HTML_CHARS} characters.'
            )
        try:
            html.encode("utf-8")
        except UnicodeEncodeError:
            raise HTTPException(400, '"html" is not valid Unicode text.') from None
        title = body.get("title")
        if title is not None and not isinstance(title, str):
            raise HTTPException(400, '"title" must be a string.')
        title = " ".join((title or "").split())[:MAX_IMPORT_TITLE_CHARS]

        try:
            clean = await asyncio.to_thread(validate_public_http_url, body["url"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        try:
            sid = await asyncio.to_thread(ingest_html, html, clean, None, title)
        except Exception:
            log.exception("import failed for %s", clean)
            raise HTTPException(422, "Could not turn that HTML into a snapshot.") from None

        def decide() -> tuple[dict, bool]:
            snap = db.get_snapshot(sid)
            return snap, _is_usable(snap, _site_name(snap))

        snap, usable = await asyncio.to_thread(decide)
    finally:
        _imports_in_flight -= 1

    snap_title = snap.get("title")
    return JSONResponse(
        {
            "id": sid,
            "status": "complete",
            "usable": usable,
            "word_count": int(snap.get("word_count") or 0),
            "title": None if not snap_title or snap_title in {snap["url"], "[no-title]"} else snap_title,
        },
        status_code=201,
        background=BackgroundTask(_screenshot_import, sid),
    )
