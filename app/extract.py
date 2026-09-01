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


def _from_react_on_rails(soup: BeautifulSoup) -> dict:
    out: dict[str, str | None] = {}
    for script in soup.select("script.js-react-on-rails-component"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        briefing = data.get("briefing") if isinstance(data, dict) else None
        if not isinstance(briefing, dict):
            continue
        out.setdefault("title", _text(briefing.get("headline") or briefing.get("title")))
        body = briefing.get("body") or briefing.get("content") or briefing.get("html")
        dek = _text(briefing.get("dek") or briefing.get("teaser") or briefing.get("standfirst"))
        if isinstance(body, str) and body.strip():
            out.setdefault("article_html", body)
        if dek:
            out.setdefault("dek", dek)
            out.setdefault("description", dek)
        authors = briefing.get("authors")
        if isinstance(authors, list) and authors:
            first = authors[0]
            out.setdefault("author", _text(first))
            if isinstance(first, dict) and first.get("picture"):
                out.setdefault("author_image", first.get("picture"))
        out.setdefault("published_at", _text(briefing.get("publishedAt") or briefing.get("published_at")))
        source = briefing.get("source")
        if isinstance(source, dict):
            out.setdefault("site_name", _text(source.get("name")))
    return out


def _from_next_data(soup: BeautifulSoup) -> dict:
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return {}
    raw = tag.string or tag.get_text() or ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    props = data.get("props", {}).get("pageProps", {}) if isinstance(data, dict) else {}
    article = props.get("article") or props.get("post") or props.get("data") or props
    if not isinstance(article, dict):
        return {}
    return {
        "title": _text(article.get("title") or article.get("headline")),
        "description": _text(article.get("dek") or article.get("excerpt") or article.get("description")),
        "author": _text(article.get("author")),
        "published_at": _text(article.get("publishedAt") or article.get("date")),
        "article_html": article.get("body") if isinstance(article.get("body"), str) else None,
    }


def _paragraphs_to_html(text: str) -> str:
    parts = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not parts:
        parts = [text.strip()]
    return "".join(f"<p>{p}</p>" for p in parts)


def extract_article(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    ld = _from_ld(soup)
    rails = _from_react_on_rails(soup)
    nxt = _from_next_data(soup)

    title = (
        _meta(soup, "og:title", "twitter:title")
        or rails.get("title")
        or nxt.get("title")
        or ld.get("title")
        or (soup.title.get_text(strip=True) if soup.title else None)
    )
    description = max(
        (
            c
            for c in (
                rails.get("description"),
                nxt.get("description"),
                ld.get("description"),
                _meta(soup, "og:description", "twitter:description", "description"),
            )
            if c
        ),
        key=len,
        default=None,
    )
    author = (
        rails.get("author")
        or nxt.get("author")
        or ld.get("author")
        or _meta(soup, "author", "article:author", "byl", "twitter:creator")
    )
    published_at = (
        rails.get("published_at")
        or nxt.get("published_at")
        or ld.get("published_at")
        or _meta(soup, "article:published_time", "pubdate", "date", "DC.date.issued")
    )
    site_name = (
        rails.get("site_name")
        or ld.get("site_name")
        or _meta(soup, "og:site_name")
    )
    if not site_name:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        site_name = host[4:] if host.startswith("www.") else host

    article_html = rails.get("article_html") or nxt.get("article_html") or ""
    article_text = ""
    if not article_html:
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
    desc_words = [w for w in re.split(r"\s+", description or "") if w]
    # Soft paywalls often leave only a lock/CTA in the DOM while the lede
    # is still in og:description / JSON. Prefer the longer copy.
    if description and len(desc_words) > max(len(words), 12):
        article_text = _WS.sub(" ", description).strip()
        if not article_html or len(words) < 40:
            article_html = _paragraphs_to_html(description)
        words = desc_words

    return {
        "title": title or url,
        "description": description,
        "dek": rails.get("dek") or nxt.get("description"),
        "author": author,
        "author_image": rails.get("author_image"),
        "published_at": published_at,
        "site_name": site_name,
        "article_html": article_html,
        "article_text": article_text,
        "word_count": len(words),
        "paywalled": len(words) < 80,
    }
