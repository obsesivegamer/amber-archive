"""Turn a saved HTML file (browser Save Page / archive.is snapshot) into an Amber snapshot."""

from __future__ import annotations

import io
import logging
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from PIL import Image

from . import db
from .extract import (
    PAYWALL_WORD_LIMIT,
    _media_url,
    _source_has_media,
    _video_has_src,
    article_is_paywalled,
    extract_article,
    sanitize_article_html,
)
from .freeze import freeze_html
from .reader import AMBER_INCOMPLETE_NOTICE_PREFIX, build_reader_html
from .security import normalize_url

log = logging.getLogger("amber.ingest")

_SAVED_FROM = re.compile(r"saved from url=\([^)]+\)(https?://\S+)", re.I)
_HTTP_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)


def _is_archive_host(host: str) -> bool:
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host.startswith("archive.") or host in {
        "archive.is",
        "archive.ph",
        "archive.today",
        "archive.md",
        "archive.fo",
        "archive.li",
        "archive.vn",
    }


def _clean_url(raw: str) -> str | None:
    url = (raw or "").strip().rstrip(".,);\"'")
    if not url.startswith("http"):
        return None
    host = urlparse(url).hostname or ""
    if not host or _is_archive_host(host):
        return None
    if host.endswith("google.com") or host.endswith("gstatic.com"):
        return None
    return url


def original_url_from_html(html: str, fallback: str = "") -> str:
    m = _SAVED_FROM.search(html[:4000])
    if m:
        got = _clean_url(m.group(1))
        if got:
            return got

    soup = BeautifulSoup(html, "lxml")
    for inp in soup.find_all("input"):
        got = _clean_url(inp.get("value") or "")
        if got:
            return got

    for link in soup.find_all("link"):
        href = link.get("href") or ""
        if "favicons" in href and "domain=" in href:
            qs = parse_qs(urlparse(href).query)
            domain = (qs.get("domain") or [""])[0]
            if domain and not _is_archive_host(domain):
                return "https://" + domain.lstrip("/")

    for match in _HTTP_URL.finditer(html[:120000]):
        got = _clean_url(match.group(0))
        if got and urlparse(got).path not in {"", "/"}:
            return got

    saved = _SAVED_FROM.search(html[:4000])
    if saved:
        return saved.group(1).rstrip('"').rstrip("/")
    return fallback or "https://example.com/"


def _save_jpeg(folder, screenshot_bytes: bytes) -> None:
    try:
        im = Image.open(io.BytesIO(screenshot_bytes))
        rgb = im.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=82)
        (folder / "screenshot.jpg").write_bytes(buf.getvalue())
        rgb.thumbnail((480, 360))
        rgb.save(folder / "thumb.jpg", "JPEG", quality=70)
    except Exception:
        (folder / "screenshot.jpg").write_bytes(screenshot_bytes)


async def screenshot_reader(sid: str) -> None:
    """Render reader.html to screenshot.jpg. Playwright sync API cannot run under uvicorn."""
    folder = db.snap_dir(sid)
    path = folder / "reader.html"
    if not path.exists():
        return
    html = path.read_text(encoding="utf-8")
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            await page.set_content(html, wait_until="domcontentloaded")
            shot = await page.screenshot(full_page=True, type="jpeg", quality=82)
        finally:
            await browser.close()
    _save_jpeg(folder, shot)


def ingest_html(
    html: str,
    url: str = "",
    screenshot_bytes: bytes | None = None,
) -> str:
    url = (url or original_url_from_html(html)).strip()
    if "://" not in url:
        url = "https://" + url
    sid = db.allocate_id()
    db.insert_snapshot(sid, url, normalize_url(url))
    folder = db.snap_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "res").mkdir(exist_ok=True)

    article = extract_article(html, url)
    frozen = freeze_html(html, url, {}, sid)
    reader = build_reader_html(article, url)

    (folder / "page.html").write_text(frozen, encoding="utf-8")
    (folder / "reader.html").write_text(reader, encoding="utf-8")
    (folder / "article.html").write_text(article.get("article_html") or "", encoding="utf-8")
    (folder / "article.txt").write_text(article.get("article_text") or "", encoding="utf-8")

    if screenshot_bytes:
        _save_jpeg(folder, screenshot_bytes)

    db.write_json(
        folder / "meta.json",
        {
            "id": sid,
            "url": url,
            "final_url": url,
            "title": article.get("title"),
            "site_name": article.get("site_name"),
            "author": article.get("author"),
            "published_at": article.get("published_at"),
            "description": article.get("description"),
            "dek": article.get("dek"),
            "word_count": article.get("word_count"),
            "paywalled": article.get("paywalled"),
            "imported": True,
            "created_at": db.now_iso(),
        },
    )
    db.update_snapshot(
        sid,
        final_url=url,
        title=article.get("title"),
        site_name=article.get("site_name"),
        author=article.get("author"),
        published_at=article.get("published_at"),
        description=article.get("description"),
        word_count=article.get("word_count"),
        paywalled=1 if article.get("paywalled") else 0,
        status="complete",
        error=None,
    )
    return sid


def _snapshot_extract_state(snap: dict, folder, meta: dict, url: str) -> dict:
    """Current stored extract, for worse-than checks. Does not read page.html."""
    txt_path = folder / "article.txt"
    text = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
    words = snap.get("word_count")
    if words is None:
        words = len([w for w in text.split() if w])
    return {
        "title": snap.get("title") or meta.get("title") or url,
        "description": snap.get("description") or meta.get("description"),
        "dek": meta.get("dek") or snap.get("description"),
        "author": snap.get("author") or meta.get("author"),
        "author_image": meta.get("author_image"),
        "published_at": snap.get("published_at") or meta.get("published_at"),
        "site_name": snap.get("site_name") or meta.get("site_name"),
        "article_html": (
            (folder / "article.html").read_text(encoding="utf-8")
            if (folder / "article.html").exists()
            else ""
        ),
        "article_text": text,
        "word_count": int(words or 0),
        "paywalled": bool(snap.get("paywalled")),
        "referrer_retried": bool(meta.get("referrer_retried")),
    }


def _article_from_stored_html(html: str, current: dict, url: str) -> dict:
    cleaned = sanitize_article_html(html)
    text = ""
    if cleaned:
        text = BeautifulSoup(cleaned, "lxml").get_text("\n", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    words = [w for w in text.split() if w]
    article = dict(current)
    article.update(
        {
            "article_html": cleaned,
            "article_text": text,
            "word_count": len(words),
            "paywalled": article_is_paywalled(len(words)),
        }
    )
    return article


def _stored_reader_body_html(reader_html: str) -> str:
    """Inner HTML of Amber's `.body`, excluding wrap chrome and Amber's banner."""
    if not (reader_html or "").strip():
        return ""
    soup = BeautifulSoup(reader_html, "lxml")
    node = soup.select_one("div.body")
    if node is None:
        return ""
    for child in node.find_all("p", class_="notice", recursive=False):
        text = child.get_text(" ", strip=True)
        if text.startswith(AMBER_INCOMPLETE_NOTICE_PREFIX):
            child.decompose()
    return node.decode_contents().strip()


def _has_retained_media(root) -> bool:
    """True when sanitize kept a real img/src/srcset or video URL, not an empty figure."""
    for img in root.find_all("img"):
        if _media_url(img):
            return True
    for video in root.find_all("video"):
        if _video_has_src(video):
            return True
    for source in root.find_all("source"):
        if _source_has_media(source):
            return True
    return False


def _sanitized_body_is_usable(article: dict) -> bool:
    """True when a sanitized extract has prose or retained content media."""
    if int(article.get("word_count") or 0) > 0:
        return True
    html = (article.get("article_html") or "").strip()
    if not html:
        return False
    soup = BeautifulSoup(html, "lxml")
    root = soup.body or soup
    return _has_retained_media(root)


def _article_from_stored_reader(reader_html: str, current: dict, url: str) -> dict | None:
    body = _stored_reader_body_html(reader_html)
    if not body:
        return None
    article = _article_from_stored_html(body, current, url)
    if not _sanitized_body_is_usable(article):
        return None
    return article


def _extract_is_worse(candidate: dict, current: dict) -> bool:
    """Refuse a rebuild that would collapse a stored extract.

    Half-or-worse applies to every nonempty stored count, including short
    paywalled teasers (39→1, 40→20). Small chrome-only drops stay allowed.
    """
    old_n = int(current.get("word_count") or 0)
    new_n = int(candidate.get("word_count") or 0)
    old_pw = bool(current.get("paywalled"))
    new_pw = bool(candidate.get("paywalled"))
    if old_n <= 0 and not (current.get("article_text") or "").strip():
        return False
    if not old_pw and new_pw:
        return True
    if old_n > 0 and new_n <= 0:
        return True
    if old_n >= PAYWALL_WORD_LIMIT and new_n < PAYWALL_WORD_LIMIT:
        return True
    if old_n > 0 and new_n * 2 <= old_n:
        return True
    return False


def _write_rebuilt(folder, article: dict, url: str, sid: str, meta: dict, meta_path, *, update_identity: bool) -> None:
    reader = build_reader_html(article, url)
    (folder / "reader.html").write_text(reader, encoding="utf-8")
    (folder / "article.html").write_text(article.get("article_html") or "", encoding="utf-8")
    (folder / "article.txt").write_text(article.get("article_text") or "", encoding="utf-8")
    fields = {
        "word_count": article.get("word_count"),
        "paywalled": 1 if article.get("paywalled") else 0,
    }
    if update_identity:
        fields.update(
            {
                "title": article.get("title"),
                "site_name": article.get("site_name"),
                "author": article.get("author"),
                "published_at": article.get("published_at"),
                "description": article.get("description"),
            }
        )
    db.update_snapshot(sid, **fields)
    if meta_path.exists():
        meta["word_count"] = article.get("word_count")
        meta["paywalled"] = article.get("paywalled")
        meta["dek"] = article.get("dek") or meta.get("dek")
        if update_identity:
            meta.update(
                {
                    "title": article.get("title"),
                    "site_name": article.get("site_name"),
                    "author": article.get("author"),
                    "published_at": article.get("published_at"),
                    "description": article.get("description"),
                }
            )
        db.write_json(meta_path, meta)


def rebuild_reader(sid: str) -> dict:
    """Rebuild reader.html from the stored extract. No live fetch.

    Source order: nonempty article.html, else the `.body` inner HTML of
    stored reader.html, else re-extract frozen page.html. Refuses to write
    if the candidate is worse than the stored extract.
    """
    snap = db.get_snapshot(sid)
    if not snap:
        raise ValueError(f"No snapshot {sid}")
    folder = db.snap_dir(sid)
    url = snap.get("final_url") or snap.get("url") or ""
    meta_path = folder / "meta.json"
    meta = db.read_json(meta_path) if meta_path.exists() else {}
    current = _snapshot_extract_state(snap, folder, meta, url)

    html_path = folder / "article.html"
    stored_html = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    page_path = folder / "page.html"

    if stored_html.strip():
        article = _article_from_stored_html(stored_html, current, url)
        source = "article.html"
        update_identity = False
    else:
        reader_path = folder / "reader.html"
        stored_reader = (
            reader_path.read_text(encoding="utf-8") if reader_path.exists() else ""
        )
        from_reader = _article_from_stored_reader(stored_reader, current, url)
        if from_reader is not None:
            article = from_reader
            source = "reader.html"
            update_identity = False
        else:
            if not page_path.exists():
                raise FileNotFoundError(
                    f"No article.html, reader.html body, or page.html for {sid}"
                )
            article = extract_article(page_path.read_text(encoding="utf-8"), url)
            source = "page.html"
            update_identity = True
            if meta.get("referrer_retried"):
                article["referrer_retried"] = True

    if _extract_is_worse(article, current):
        current["rebuild_refused"] = True
        current["rebuild_source"] = source
        current["rebuild_reason"] = (
            f"{source} extract is worse than stored "
            f"({article.get('word_count')} words, paywalled={article.get('paywalled')} "
            f"vs stored {current.get('word_count')} words, "
            f"paywalled={current.get('paywalled')})"
        )
        return current

    article["rebuild_source"] = source
    article["rebuild_refused"] = False
    if meta.get("referrer_retried"):
        article["referrer_retried"] = True
    _write_rebuilt(
        folder, article, url, sid, meta, meta_path, update_identity=update_identity
    )
    return article
