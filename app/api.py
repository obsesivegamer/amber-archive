from __future__ import annotations

import re
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException

from . import db
from .config import ID_ALPHABET, ID_LENGTH

router = APIRouter(prefix="/api")

ID_RE = re.compile(rf"^[{re.escape(ID_ALPHABET)}]{{{ID_LENGTH}}}$")
MIN_USABLE_WORDS = 120
MAX_SINCE_DAYS = 365
MAX_LIMIT = 200
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
