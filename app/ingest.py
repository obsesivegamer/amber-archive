"""Turn a saved HTML file (browser Save Page / archive.is snapshot) into an Amber snapshot."""

from __future__ import annotations

import io
import logging
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from PIL import Image

from . import db
from .extract import extract_article
from .freeze import freeze_html
from .reader import build_reader_html
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
        status="complete",
        error=None,
    )
    return sid
