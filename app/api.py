from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
import unicodedata
from collections import deque
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import capture, db
from .config import ID_ALPHABET, ID_LENGTH
from .import_child import TooComplex, blocked_for, importing, ingest_in_child
from .ingest import screenshot_reader
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
MAX_IMPORTS_IN_FLIGHT = 2
MAX_THUMBNAILS_WAITING = 3
MAX_BODY_SECONDS = 60.0
MAX_IMPORT_BYTES_PER_DAY = 300 * 1024 * 1024
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
        if snap["id"] in importing:
            return "capturing"
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
    except (ValueError, RecursionError) as exc:
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
_thumbnails_waiting = 0
_thumbnail_tasks: set[asyncio.Task] = set()
# (time, bytes) of accepted imports in the last 24 hours. In memory: a restart
# forgets it, which is fine for a soft limit.
_import_ledger: deque[tuple[float, int]] = deque()
# One Chromium at a time for import thumbnails.
_screenshot_gate = asyncio.Semaphore(1)

# Public suffixes (registrable domain = suffix plus one label), including
# shared-hosting suffixes where every subdomain belongs to a different owner.
_PUBLIC_SUFFIXES = frozenset(
    "co.uk org.uk ac.uk gov.uk me.uk ltd.uk plc.uk com.au net.au org.au edu.au gov.au "
    "co.nz org.nz govt.nz co.jp or.jp ne.jp ac.jp go.jp com.br net.br org.br com.cn net.cn "
    "org.cn com.hk com.sg com.tw com.mx com.ar com.tr com.ua co.in net.in org.in co.za "
    "co.kr or.kr co.il co.id com.my com.ph com.pk com.eg com.sa com.ng "
    "github.io gitlab.io blogspot.com herokuapp.com vercel.app netlify.app pages.dev "
    "workers.dev web.app firebaseapp.com azurewebsites.net cloudfront.net "
    "s3.amazonaws.com appspot.com wordpress.com "
    "substack.com medium.com wixsite.com myshopify.com tumblr.com squarespace.com "
    "ghost.io notion.site".split()
)
_S3_LABEL_RE = re.compile(r"^s3(?:$|[-.].*)")


def _punycode_host(host: str) -> str:
    """UTS 46-ish fallback when the `idna` package is missing: NFKC and
    lower-case, then punycode per label. Deliberately not IDNA 2003, so ß stays
    ß (xn--zca) and does not collapse into ss."""
    labels = []
    for label in host.split("."):
        if label.isascii():
            labels.append(label)
        else:
            try:
                labels.append("xn--" + label.encode("punycode").decode("ascii"))
            except UnicodeError:
                labels.append(label)
    return ".".join(labels)


def _host_key(url: str) -> str:
    """Lower-cased IDNA 2008 (A-label) host, after NFKC so full-width forms compare equal."""
    host = unicodedata.normalize("NFKC", urlparse(url).hostname or "").lower().rstrip(".")
    if host.isascii():
        return host
    try:
        import idna

        return idna.encode(host, uts46=True, transitional=False).decode("ascii")
    except ImportError:
        return _punycode_host(host)
    except Exception:
        return _punycode_host(host)


def _registrable_domain(host: str) -> str:
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if host.endswith(".amazonaws.com"):
        # bucket.s3.amazonaws.com, bucket.s3.<region>.amazonaws.com,
        # bucket.s3-<region>.amazonaws.com: the whole host names one owner.
        if any(_S3_LABEL_RE.match(label) for label in labels[:-2]):
            return host
    for i in range(1, len(labels)):
        if ".".join(labels[i:]) in _PUBLIC_SUFFIXES:
            return ".".join(labels[i - 1:])
    return ".".join(labels[-2:])


def _port_class(url: str) -> int | str:
    try:
        port = urlparse(url).port
    except ValueError:
        return "invalid"
    return "default" if port in (None, 80, 443) else port


def _same_site(a: str, b: str) -> bool:
    try:
        host_a, host_b = _host_key(a), _host_key(b)
        if not host_a or not host_b or _port_class(a) != _port_class(b):
            return False
    except ValueError:
        return False
    return host_a == host_b or _registrable_domain(host_a) == _registrable_domain(host_b)


def claim_import_slot() -> None:
    """Take one of the MAX_IMPORTS_IN_FLIGHT slots, or refuse with 429.

    Synchronous on purpose: no await between the check and the increment.
    """
    global _imports_in_flight
    paused = blocked_for()
    if paused > 0:
        raise HTTPException(
            429,
            f"Imports are paused for {int(paused) + 1} s after repeated failures.",
            headers={"Retry-After": str(int(paused) + 1)},
        )
    if _imports_in_flight >= MAX_IMPORTS_IN_FLIGHT:
        raise HTTPException(429, "Too many imports are already running. Try again shortly.")
    _imports_in_flight += 1


def release_import_slot() -> None:
    global _imports_in_flight
    _imports_in_flight -= 1


def check_import_budget(nbytes: int) -> None:
    if _imported_bytes_today() + nbytes > MAX_IMPORT_BYTES_PER_DAY:
        raise HTTPException(
            429,
            f"Daily import budget used up ({MAX_IMPORT_BYTES_PER_DAY // (1024 * 1024)} MB "
            "of HTML per 24 hours). Try again later.",
        )


def record_import_bytes(nbytes: int) -> None:
    _import_ledger.append((time.time(), nbytes))


def _imported_bytes_today() -> int:
    cutoff = time.time() - 24 * 3600
    while _import_ledger and _import_ledger[0][0] < cutoff:
        _import_ledger.popleft()
    return sum(size for _, size in _import_ledger)


async def _read_body_capped(request: Request, limit: int, deadline: float = MAX_BODY_SECONDS) -> bytes:
    """Read the request body: 413 as soon as it passes `limit`, 408 past `deadline` seconds."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "Request body is too large.")
    chunks: list[bytes] = []
    total = 0
    try:
        async with asyncio.timeout(deadline):
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, "Request body is too large.")
                chunks.append(chunk)
    except TimeoutError:
        raise HTTPException(408, "The request body took too long to arrive.") from None
    return b"".join(chunks)


def reserve_thumbnail() -> bool:
    """Count one thumbnail as waiting, or say there are already too many."""
    global _thumbnails_waiting
    if _thumbnails_waiting >= MAX_THUMBNAILS_WAITING:
        return False
    _thumbnails_waiting += 1
    return True


async def run_reserved_thumbnail(sid: str, render) -> None:
    """Render under the one-Chromium-at-a-time gate; never raises."""
    global _thumbnails_waiting
    try:
        async with _screenshot_gate:
            await render(sid)
    except Exception:
        log.exception("reader screenshot failed for imported snapshot %s", sid)
    finally:
        _thumbnails_waiting -= 1


@router.post("/import")
async def import_page(request: Request):
    """Store HTML a signed-in browser already rendered as a new, complete snapshot.

    Nothing is fetched: the HTML is parsed and frozen exactly as posted, in a
    child process with a deadline and a memory cap.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "Send the request as application/json.")
    claim_import_slot()
    try:
        raw = await _read_body_capped(request, MAX_IMPORT_BODY_BYTES)
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError) as exc:
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
            html_bytes = len(html.encode("utf-8"))
        except UnicodeEncodeError:
            raise HTTPException(400, '"html" is not valid Unicode text.') from None
        title = body.get("title")
        if title is not None and not isinstance(title, str):
            raise HTTPException(400, '"title" must be a string.')
        title = " ".join((title or "").split())[:MAX_IMPORT_TITLE_CHARS]
        force = body.get("force", False)
        if force is None:
            force = False
        if not isinstance(force, bool):
            raise HTTPException(400, '"force" must be true or false.')
        final_raw = body.get("final_url")
        if final_raw is not None and not isinstance(final_raw, str):
            raise HTTPException(400, '"final_url" must be a string.')

        try:
            clean = await asyncio.to_thread(validate_public_http_url, body["url"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        final = clean
        if final_raw is not None and final_raw.strip():
            try:
                final = await asyncio.to_thread(validate_public_http_url, final_raw)
            except ValueError as exc:
                message = str(exc)
                if message.startswith(("Private or local", "Local URLs", "Numeric hosts")):
                    # Stable strings: the extension maps these distinctly.
                    raise HTTPException(400, "final_url is a private or local address") from exc
                raise HTTPException(400, f"final_url: {message}") from exc
            if not _same_site(clean, final):
                raise HTTPException(400, "final_url is a different site")

        if not force:
            def usable_copy() -> dict | None:
                for address in dict.fromkeys((clean, final)):
                    found, status, usable = _best_match(_find_matches(address))
                    if found is not None and status == "complete" and usable:
                        return found
                return None

            existing = await asyncio.to_thread(usable_copy)
            if existing is not None:
                return JSONResponse(
                    {"detail": "already archived", "id": existing["id"]}, status_code=409
                )

        # Charged before parsing and kept charged on failure: a page that
        # makes the parser burn CPU or time out still used the budget.
        check_import_budget(html_bytes)
        record_import_bytes(html_bytes)

        try:
            sid = await ingest_in_child(html, clean, title, final)
        except TooComplex as exc:
            raise HTTPException(422, f"Page too complex to import: {exc}") from None
        except Exception:
            log.exception("import failed for %s", clean)
            raise HTTPException(422, "Could not turn that HTML into a snapshot.") from None

        def decide() -> tuple[dict, bool]:
            snap = db.get_snapshot(sid)
            return snap, _is_usable(snap, _site_name(snap))

        snap, usable = await asyncio.to_thread(decide)
    finally:
        release_import_slot()

    if reserve_thumbnail():
        task = asyncio.create_task(run_reserved_thumbnail(sid, screenshot_reader))
        _thumbnail_tasks.add(task)
        task.add_done_callback(_thumbnail_tasks.discard)
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
    )
