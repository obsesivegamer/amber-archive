"""Turn a saved HTML file (browser Save Page / archive.is snapshot) into an Amber snapshot."""

from __future__ import annotations

import io
import re
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image

from . import db
from .extract import extract_article
from .freeze import freeze_html
from .reader import build_reader_html
from .security import normalize_url

_SAVED_FROM = re.compile(r"saved from url=\([^)]+\)(https?://\S+)", re.I)
_ORIG_IN_ARCHIVE = re.compile(
    r"https?://(?:www\.)?theinformation\.com/\S+|https?://(?!archive\.)[a-z0-9.-]+/\S+",
    re.I,
)


def original_url_from_html(html: str, fallback: str = "") -> str:
    m = _SAVED_FROM.search(html[:4000])
    if m:
        url = m.group(1).rstrip('"').rstrip("/")
        if "archive." not in url:
            return url
    soup = BeautifulSoup(html, "lxml")
    for inp in soup.find_all("input"):
        val = inp.get("value") or ""
        idx = val.find("https://www.theinformation.com")
        if idx >= 0:
            return val[idx:]
        if val.startswith("http") and "archive." not in val and "theinformation.com" in val:
            return val
    text = html[:80000]
    m = re.search(
        r"https://www\.theinformation\.com/briefings/[a-z0-9-]+",
        text,
    )
    if m:
        return m.group(0)
    return fallback or "https://example.com/"


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
