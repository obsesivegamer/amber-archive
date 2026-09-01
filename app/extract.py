"""Pull article metadata and a readable text copy from frozen HTML."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup
from readability import Document

_WS = re.compile(r"\s+")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("name", "headline", "value", "@value"):
            if value.get(key):
                return _text(value[key])
        return None
    if isinstance(value, list):
        for item in value:
            got = _text(item)
            if got:
                return got
        return None
    s = _WS.sub(" ", unescape(str(value))).strip()
    return s or None


def _meta(soup: BeautifulSoup, *keys: str) -> str | None:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find(
            "meta", attrs={"name": key}
        )
        if tag and tag.get("content"):
            return _text(tag["content"])
    return None


def _walk_ld(node: Any, found: list[dict]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_ld(item, found)
        return
    if not isinstance(node, dict):
        return
    types = node.get("@type")
    if isinstance(types, str):
        types = [types]
    if isinstance(types, list) and any(
        t in {"NewsArticle", "Article", "BlogPosting", "WebPage", "ReportageNewsArticle"}
        for t in types
    ):
        found.append(node)
    if "@graph" in node:
        _walk_ld(node["@graph"], found)


def _from_ld(html_soup: BeautifulSoup) -> dict:
    out: dict[str, str | None] = {}
    for script in html_soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found: list[dict] = []
        _walk_ld(data, found)
        for node in found:
            out.setdefault("title", _text(node.get("headline") or node.get("name")))
            out.setdefault("description", _text(node.get("description")))
            out.setdefault("author", _text(node.get("author")))
            out.setdefault(
                "published_at",
                _text(node.get("datePublished") or node.get("dateCreated")),
            )
            publisher = node.get("publisher")
            if isinstance(publisher, dict):
                out.setdefault("site_name", _text(publisher.get("name")))
    return out


def extract_article(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    ld = _from_ld(soup)

    title = (
        _meta(soup, "og:title", "twitter:title")
        or ld.get("title")
        or (soup.title.get_text(strip=True) if soup.title else None)
    )
    description = (
        ld.get("description")
        or _meta(soup, "og:description", "twitter:description", "description")
    )
    author = ld.get("author") or _meta(
        soup, "author", "article:author", "byl", "twitter:creator"
    )
    published_at = ld.get("published_at") or _meta(
        soup, "article:published_time", "pubdate", "date", "DC.date.issued"
    )
    site_name = ld.get("site_name") or _meta(soup, "og:site_name")
    if not site_name:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        site_name = host[4:] if host.startswith("www.") else host

    article_html = ""
    article_text = ""
    try:
        doc = Document(html)
        article_html = doc.summary(html_partial=True) or ""
        if not title:
            title = _text(doc.short_title() or doc.title())
    except Exception:
        article = soup.find("article") or soup.find(attrs={"itemprop": "articleBody"})
        if article:
            article_html = str(article)

    if article_html:
        art = BeautifulSoup(article_html, "lxml")
        for tag in art.find_all(["script", "iframe", "object", "embed", "form"]):
            tag.decompose()
        for tag in art.find_all(True):
            for attr in list(tag.attrs):
                if attr.lower().startswith("on"):
                    del tag.attrs[attr]
        article_html = str(art)

    if article_html:
        article_text = BeautifulSoup(article_html, "lxml").get_text("\n", strip=True)
    if not article_text:
        body = soup.body or soup
        article_text = body.get_text("\n", strip=True)

    article_text = _WS.sub(" ", article_text).strip()
    words = [w for w in re.split(r"\s+", article_text) if w]

    return {
        "title": title or url,
        "description": description,
        "author": author,
        "published_at": published_at,
        "site_name": site_name,
        "article_html": article_html,
        "article_text": article_text,
        "word_count": len(words),
    }
