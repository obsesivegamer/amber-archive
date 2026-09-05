"""Pull article metadata and a readable text copy from frozen HTML."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup
from readability import Document
from readability.readability import REGEXES as _READABILITY_REGEXES

_WS = re.compile(r"\s+")

# Soft paywalls often leave only a dek / og:description. Same bit is stored on
# the snapshot row so lists and the viewer can stay honest.
PAYWALL_WORD_LIMIT = 80

# Recirc / related-story chrome. readability-lxml will pick a single
# article-card excerpt (live Politico: 30 words, then Incomplete) or the
# whole "readers read next" listing. Bounce cannot fix that — the full
# copy is already in the article body. Strip these before scoring.
_RECIRC_SELECTORS = (
    ".article-card",
    "[class*='article-card']",
    ".content-listing",
    "aside",
)

# Non-body modules inside <article>. Used only on the article-dump fallback
# so comments / promo / footer cannot inflate the score past PAYWALL_WORD_LIMIT
# and suppress the bounce retry. Not added to _RECIRC_SELECTORS (readability
# already drops most of these; widening that list is a separate, tested change).
_ARTICLE_CHROME_SELECTORS = (
    ".comments",
    ".reader-comments",
    ".promo",
    "footer",
    ".footer",
    ".related-stories",
)


def article_is_paywalled(word_count: int | None) -> bool:
    return word_count is not None and int(word_count) < PAYWALL_WORD_LIMIT


def _html_word_count(html: str, *, exclude_anchors: bool = False) -> int:
    if not html:
        return 0
    soup = BeautifulSoup(html, "lxml")
    if exclude_anchors:
        for tag in soup.find_all("a"):
            tag.decompose()
    text = _WS.sub(" ", soup.get_text(" ", strip=True)).strip()
    return len([w for w in re.split(r"\s+", text) if w])


def _without_recirc(soup: BeautifulSoup) -> BeautifulSoup:
    clone = BeautifulSoup(str(soup), "lxml")
    for sel in _RECIRC_SELECTORS:
        for el in clone.select(sel):
            el.decompose()
    return clone


def _strip_selectors(soup: BeautifulSoup, selectors: tuple[str, ...]) -> None:
    for sel in selectors:
        for el in soup.select(sel):
            el.decompose()


def _is_unlikely_chrome(elem) -> bool:
    """Same class+id unlikely filter readability uses before scoring.

    Exact-token `_ARTICLE_CHROME_SELECTORS` miss `id="comments"`,
    `comment-list`, `sponsored`, `sidebar`. Those stay in the dump, beat
    the teaser, and skip bounce. Keep nodes readability would keep
    (`okMaybeItsACandidateRe`). Do not spare wrappers just because they
    contain ``article`` / ``main`` — WordPress comments use that shape.
    """
    name = getattr(elem, "name", None)
    if not name or name in {"html", "body", "[document]", "article"}:
        return False
    classes = elem.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    s = "{} {}".format(" ".join(classes), elem.get("id") or "")
    if len(s) < 2:
        return False
    if not _READABILITY_REGEXES["unlikelyCandidatesRe"].search(s):
        return False
    if _READABILITY_REGEXES["okMaybeItsACandidateRe"].search(s):
        return False
    return True


def _strip_unlikely_chrome(soup: BeautifulSoup) -> None:
    for el in list(soup.find_all(True)):
        if _is_unlikely_chrome(el):
            el.decompose()


def _outer_article_content(host: BeautifulSoup) -> list:
    """`.article__content` roots that are not nested in another match.

    ``select`` returns parent and child; joining both double-counts the same
    prose (a 52-word teaser becomes 104 and looks complete).
    """
    nodes = host.select(".article__content")
    chosen = set(nodes)
    return [node for node in nodes if not any(parent in chosen for parent in node.parents)]


def _outer_hosts(nodes: list) -> list:
    """Keep outermost article hosts so a nested chrome `<article>` cannot re-enter."""
    chosen = set(nodes)
    return [node for node in nodes if not any(parent in chosen for parent in node.parents)]


def _chrome_boxes(soup: BeautifulSoup) -> set:
    boxes: set = set()
    for sel in _ARTICLE_CHROME_SELECTORS + _RECIRC_SELECTORS:
        boxes.update(soup.select(sel))
    for el in soup.find_all(True):
        if _is_unlikely_chrome(el):
            boxes.add(el)
    return boxes


def _host_inside_chrome(host, boxes: set) -> bool:
    return any(parent in boxes for parent in host.parents)


def _from_article_dom(soup: BeautifulSoup) -> str:
    """The page's own article body, after recirc and non-body modules are gone.

    Score by non-anchor words so a link-dense ``<article>`` shell (Most read,
    related headlines) cannot out-count readability's filtered pick. Comments,
    promo, footer, and related-stories are stripped before that score so they
    cannot make a teaser look complete. Nested ``<article>`` hosts inside that
    chrome are skipped — P3 already drops them from the outer clone, but
    ``find_all("article")`` would otherwise clone them in isolation.
    """
    hosts: list = []
    hosts.extend(soup.find_all(attrs={"itemprop": "articleBody"}))
    hosts.extend(soup.find_all("article"))
    hosts = _outer_hosts(hosts)
    chrome = _chrome_boxes(soup)
    best = ""
    best_n = 0
    seen: set[int] = set()
    for host in hosts:
        if _host_inside_chrome(host, chrome):
            continue
        marker = id(host)
        if marker in seen:
            continue
        seen.add(marker)
        clone = BeautifulSoup(str(host), "lxml")
        had_content_roots = bool(_outer_article_content(clone))
        _strip_selectors(clone, _ARTICLE_CHROME_SELECTORS)
        _strip_unlikely_chrome(clone)
        roots = _outer_article_content(clone)
        if had_content_roots and not roots:
            # Chrome strip ate every content root (nested in <footer>, or
            # classed "article__content comments"). Do not widen to the
            # whole <article> — that reintroduces off-list junk the roots
            # had excluded and can flip a teaser to Complete.
            continue
        html = "".join(str(c) for c in roots) if roots else str(clone.body or clone)
        n = _html_word_count(html, exclude_anchors=True)
        if n > best_n:
            best, best_n = html, n
    return best


def _prefer_richer_html(*candidates: str) -> str:
    best = ""
    best_n = 0
    for html in candidates:
        n = _html_word_count(html, exclude_anchors=True)
        if n > best_n:
            best, best_n = html, n
    return best


def _article_dom_if_substantial(soup: BeautifulSoup) -> str:
    """Offer the article dump only when it looks like a real body.

    A Most-read list plus a newsletter blurb is ~40 non-anchor words. If that
    dump still competes on raw (or even non-anchor) count, it beats
    readability's ~35-word teaser, stores chrome, and skips the bounce retry.
    Require at least PAYWALL_WORD_LIMIT non-anchor words before it can win.
    """
    html = _from_article_dom(soup)
    if _html_word_count(html, exclude_anchors=True) < PAYWALL_WORD_LIMIT:
        return ""
    return html


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
        payload = None
        if isinstance(data, dict):
            payload = data.get("article") or data.get("briefing")
        if not isinstance(payload, dict):
            continue
        out.setdefault("title", _text(payload.get("title") or payload.get("headline")))
        body = (
            payload.get("fullText")
            or payload.get("body")
            or payload.get("content")
            or payload.get("html")
            or payload.get("freeBlurb")
        )
        dek = _text(
            payload.get("subTitle")
            or payload.get("dek")
            or payload.get("teaser")
            or payload.get("standfirst")
        )
        if isinstance(body, str) and body.strip():
            out.setdefault("article_html", body)
        if dek:
            out.setdefault("dek", dek)
            out.setdefault("description", dek)
        authors = payload.get("authors")
        if isinstance(authors, list) and authors:
            first = authors[0]
            out.setdefault("author", _text(first))
            if isinstance(first, dict) and first.get("picture"):
                out.setdefault("author_image", first.get("picture"))
        out.setdefault(
            "published_at",
            _text(payload.get("publishedAt") or payload.get("published_at")),
        )
        source = payload.get("source")
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


def _first_h1(soup: BeautifulSoup) -> tuple[str | None, Any]:
    for h in soup.find_all("h1"):
        t = _text(h.get_text(" ", strip=True))
        if t and 12 <= len(t) <= 240:
            return t, h
    return None, None


def _dek_near_h1(h1) -> str | None:
    if h1 is None:
        return None
    nxt = h1.find_next(["div", "p", "h2"])
    if not nxt:
        return None
    t = _text(nxt.get_text(" ", strip=True))
    if not t or not (24 <= len(t) <= 240):
        return None
    if t.lower().startswith("by "):
        return None
    if "subscribe" in t.lower():
        return None
    return t


def extract_article(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    ld = _from_ld(soup)
    rails = _from_react_on_rails(soup)
    nxt = _from_next_data(soup)
    h1_text, h1_tag = _first_h1(soup)
    og_title = _meta(soup, "og:title", "twitter:title")
    if og_title and (og_title.endswith("…") or og_title.endswith("...")):
        og_title = None

    title = (
        h1_text
        or og_title
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
    if site_name and "archive." in site_name.lower():
        site_name = None
    for div in soup.find_all("div"):
        bits = [b.strip() for b in div.stripped_strings if b.strip()]
        if not bits:
            continue
        if bits[0] == "By" and len(bits) >= 2:
            if not author:
                author = bits[1]
            if "Source:" in bits:
                i = bits.index("Source:")
                if i + 1 < len(bits) and not site_name:
                    site_name = bits[i + 1]
            break
        if bits[0].startswith("By "):
            if not author:
                author = _text(bits[0][3:])
            break
    if not author:
        by = soup.find(string=re.compile(r"^\s*By\s+\S"))
        if by:
            author = _text(re.sub(r"^\s*By\s+", "", str(by), count=1))
            if author and "@" in author:
                author = _text(re.sub(r"\s+\S+@\S+.*$", "", author))
    if not site_name:
        for raw in soup.stripped_strings:
            if raw.lower().startswith("source:"):
                site_name = _text(re.sub(r"^source:\s*", "", raw, flags=re.I))
                break
    for time_tag in soup.find_all("time"):
        visible = _text(time_tag.get_text(" ", strip=True))
        if not visible:
            continue
        if re.search(r"\b(PDT|PST|EST|EDT|CT|PT|am|pm)\b", visible, re.I):
            published_at = visible
            break
        if not published_at:
            published_at = visible
    if not site_name:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        site_name = host[4:] if host.startswith("www.") else host

    article_html = rails.get("article_html") or nxt.get("article_html") or ""
    article_text = ""
    if not article_html:
        body_soup = _without_recirc(soup)
        readability_html = ""
        try:
            doc = Document(str(body_soup))
            readability_html = doc.summary(html_partial=True) or ""
            if not title:
                title = _text(doc.short_title() or doc.title())
        except Exception:
            readability_html = ""
        article_html = _prefer_richer_html(
            readability_html, _article_dom_if_substantial(body_soup)
        )

    if article_html:
        art = BeautifulSoup(article_html, "lxml")
        for tag in art.find_all(["script", "iframe", "object", "embed", "form"]):
            tag.decompose()
        for tag in art.find_all(True):
            for attr in list(tag.attrs):
                if attr.lower().startswith("on"):
                    del tag.attrs[attr]
        inner = art.body
        article_html = inner.decode_contents() if inner else str(art)

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
        "dek": rails.get("dek") or _dek_near_h1(h1_tag) or nxt.get("description"),
        "author": author,
        "author_image": rails.get("author_image"),
        "published_at": published_at,
        "site_name": site_name,
        "article_html": article_html,
        "article_text": article_text,
        "word_count": len(words),
        "paywalled": article_is_paywalled(len(words)),
    }
