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

# Publisher UI that leaks into the chosen extract. Applied only after a
# candidate is picked — do not fold these into _RECIRC_SELECTORS (that list
# changes paywall scoring). Prefer tokens + roles over site-specific classes.
_ALWAYS_DROP_TAGS = frozenset({"button", "nav", "aside", "audio", "video", "footer"})
_CHROME_ROLES = frozenset(
    {"toolbar", "navigation", "menu", "menubar", "complementary"}
)
_TESTID_NEEDLES = (
    "share",
    "listen",
    "gift",
    "bookmark",
    "toolbar",
    "newsletter",
    "audio",
    "recirc",
)
_ARIA_NEEDLES = (
    "share",
    "listen",
    "gift this",
    "gift article",
    "bookmark",
    "save this",
    "save article",
    "copy link",
)
_CHROME_TOKEN_RE = re.compile(
    r"(?:^|[_\s\-])(?:"
    r"share|sharing|sharetools|share-tools|social-share|social-sharing|"
    r"listen|gift|bookmark|toolbar|"
    r"newsletter|subscribe|"
    r"recirc|related-stories|related-coverage|related-content|"
    r"article-tools|articletools|byline-tools|utility-bar|"
    r"hero__actions|content-listing"
    r")(?:[_\s\-]|$)",
    re.I,
)
_CHROME_TEXT_RE = re.compile(
    r"""
    ^\s*(
        share(\s+(full\s+article|this(\s+article)?|on\s+\w+|via\s+\w+))? |
        listen(\s*[·•\-\|:].*)? |
        gift(\s+this(\s+article)?)? |
        bookmark |
        save(\s+(this\s+)?article)? |
        copy(\s+link)? |
        copied |
        leer\s+en\s+espa[ñn]ol |
        read\s+in\s+(spanish|english|espa[ñn]ol) |
        advertisement |
        skip\s+advertisement |
        more\s+photos
    )\s*$
    """,
    re.I | re.VERBOSE,
)
_LANG_LINK_RE = re.compile(
    r"^(leer|read|lire|lesen|leggi|leia)\s+(en|in|auf)\s+\S+$",
    re.I,
)
_SHARE_HREF_RE = re.compile(
    r"(twitter\.com/intent|x\.com/intent|facebook\.com/shar|"
    r"linkedin\.com/share|api\.whatsapp\.com/send|whatsapp\.com/send|"
    r"mailto:\?|reddit\.com/submit|pinterest\.com/pin)",
    re.I,
)
_LAZY_SRC_ATTRS = ("src", "srcset", "data-src", "data-original", "data-lazy-src")
_LAYOUT_ATTRS = frozenset({"width", "height", "align", "hspace", "vspace", "border"})
_PROSE_KEEP_WORDS = 40


def article_is_paywalled(word_count: int | None) -> bool:
    return word_count is not None and int(word_count) < PAYWALL_WORD_LIMIT


def _prose_word_count(html: str) -> int:
    """Count non-link text without changing the candidate stored for the reader."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("a"):
        tag.decompose()
    return len(soup.get_text(" ", strip=True).split())


def _outermost(nodes: list) -> list:
    """Keep distinct outer nodes by identity, not BeautifulSoup markup equality."""
    by_id = {id(node): node for node in nodes}
    return [
        node for node in by_id.values()
        if not any(id(parent) in by_id for parent in node.parents)
    ]


def _without_recirc(soup: BeautifulSoup) -> BeautifulSoup:
    clone = BeautifulSoup(str(soup), "lxml")
    for node in _outermost(clone.select(", ".join(_RECIRC_SELECTORS))):
        node.decompose()
    return clone


def _is_unlikely_chrome(elem) -> bool:
    """Apply readability's class/id filter; retain its candidate exceptions."""
    if elem.name in {None, "html", "body", "[document]", "article"}:
        return False
    classes = elem.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    label = " ".join(classes) + " " + (elem.get("id") or "")
    return bool(
        _READABILITY_REGEXES["unlikelyCandidatesRe"].search(label)
        and not _READABILITY_REGEXES["okMaybeItsACandidateRe"].search(label)
    )


def _elem_label(elem) -> str:
    classes = elem.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    bits = list(classes)
    for key in ("id", "data-testid", "data-test-id", "aria-label"):
        val = elem.get(key)
        if val:
            bits.append(str(val))
    return " ".join(bits)


def _node_words(elem) -> int:
    return len(elem.get_text(" ", strip=True).split())


def _media_url(img) -> str:
    for key in _LAZY_SRC_ATTRS:
        val = (img.get(key) or "").strip()
        if val:
            return val
    return ""


def _has_content_media(elem) -> bool:
    for img in elem.find_all("img"):
        if _media_url(img):
            return True
    if elem.find("source") or elem.find("picture"):
        return True
    if elem.name == "figure" or elem.find("figure"):
        return True
    return False


def _safe_to_drop(elem) -> bool:
    """Keep wrappers that still hold a real body, not just a toolbar."""
    return _node_words(elem) < _PROSE_KEEP_WORDS


def _label_is_chrome(elem) -> bool:
    if elem.name in {None, "html", "body", "[document]", "article"}:
        return False
    return bool(_CHROME_TOKEN_RE.search(_elem_label(elem)))


def _testid_is_chrome(elem) -> bool:
    for key in ("data-testid", "data-test-id"):
        val = (elem.get(key) or "").lower()
        if any(needle in val for needle in _TESTID_NEEDLES):
            return True
    aria = (elem.get("aria-label") or "").lower()
    return any(needle in aria for needle in _ARIA_NEEDLES)


def _promote_lazy_src(img) -> None:
    src = (img.get("src") or "").strip()
    if src and not src.lower().startswith("data:image"):
        return
    for key in ("data-src", "data-original", "data-lazy-src"):
        val = (img.get(key) or "").strip()
        if val:
            img["src"] = val
            return


def _is_tiny_placeholder(img) -> bool:
    if _media_url(img) and (
        img.get("data-src") or img.get("srcset") or img.get("data-original")
    ):
        return False
    src = (img.get("src") or "").strip()
    if src.lower().startswith("data:image") and len(src) < 120:
        return True
    return str(img.get("width") or "") == "1" and str(img.get("height") or "") == "1"


def _drop_empty_shells(root) -> None:
    keep = {"html", "body", "[document]", "br", "hr", "img", "picture", "source"}
    changed = True
    while changed:
        changed = False
        for elem in list(root.find_all(True)):
            if getattr(elem, "decomposed", False) or elem.name in keep:
                continue
            if _has_content_media(elem):
                continue
            if elem.get_text(" ", strip=True):
                continue
            if elem.find(["img", "picture", "figure", "source"]):
                continue
            elem.decompose()
            changed = True


def _strip_publisher_layout(root) -> None:
    """Publisher (and capture-inlined) styles must not blow out the reader."""
    for elem in root.find_all(True):
        if "style" in elem.attrs:
            del elem.attrs["style"]
        if elem.name in {"img", "figure", "picture", "video", "table"}:
            for attr in _LAYOUT_ATTRS:
                elem.attrs.pop(attr, None)


def sanitize_article_html(html: str) -> str:
    """Remove site chrome from an extract; keep prose, figures, and captions."""
    if not html or not str(html).strip():
        return html
    soup = BeautifulSoup(html, "lxml")
    root = soup.body or soup

    always = []
    maybe = []
    for elem in root.find_all(True):
        role = (elem.get("role") or "").strip().lower()
        if elem.name in _ALWAYS_DROP_TAGS or role in _CHROME_ROLES:
            always.append(elem)
            continue
        if _testid_is_chrome(elem) or _label_is_chrome(elem):
            maybe.append(elem)
            continue
        elem_id = (elem.get("id") or "").lower()
        if "recirc" in elem_id:
            maybe.append(elem)

    for node in _outermost(always):
        if not getattr(node, "decomposed", False):
            node.decompose()
    for node in _outermost(maybe):
        if getattr(node, "decomposed", False):
            continue
        if _safe_to_drop(node):
            node.decompose()

    for a in list(root.find_all("a")):
        if getattr(a, "decomposed", False):
            continue
        href = a.get("href") or ""
        text = a.get_text(" ", strip=True)
        if (
            _SHARE_HREF_RE.search(href)
            or _CHROME_TEXT_RE.match(text)
            or _LANG_LINK_RE.match(text)
        ):
            a.decompose()

    for elem in list(root.find_all(["p", "div", "span", "li", "section", "h2", "h3"])):
        if getattr(elem, "decomposed", False):
            continue
        text = elem.get_text(" ", strip=True)
        if text and len(text) <= 80 and _CHROME_TEXT_RE.match(text):
            elem.decompose()

    for elem in list(root.find_all(True)):
        if getattr(elem, "decomposed", False):
            continue
        hidden = elem.get("aria-hidden")
        if hidden not in {"true", True, "True"} and not elem.has_attr("hidden"):
            continue
        if elem.find_parent("figcaption") is not None:
            continue
        if _safe_to_drop(elem) and not _has_content_media(elem):
            elem.decompose()

    for img in list(root.find_all("img")):
        if getattr(img, "decomposed", False):
            continue
        _promote_lazy_src(img)
        if not _media_url(img) or _is_tiny_placeholder(img):
            img.decompose()

    for svg in list(root.find_all("svg")):
        if getattr(svg, "decomposed", False):
            continue
        if svg.find_parent("figure") is None:
            svg.decompose()

    _drop_empty_shells(root)
    _strip_publisher_layout(root)

    inner = soup.body
    return inner.decode_contents() if inner else str(soup)


def _article_body_candidate(soup: BeautifulSoup) -> str:
    """Select body roots before stripping chrome, so scope can only shrink."""
    clone = BeautifulSoup(str(soup), "lxml")
    hosts = _outermost(
        clone.find_all(attrs={"itemprop": "articleBody"}) + clone.find_all("article")
    )
    groups = []
    for host in hosts:
        roots = (
            [host] if "article__content" in (host.get("class") or [])
            else _outermost(host.select(".article__content"))
        )
        groups.append(roots or [host])

    chrome = clone.select(", ".join(_ARTICLE_CHROME_SELECTORS))
    chrome.extend(node for node in clone.find_all(True) if _is_unlikely_chrome(node))
    for node in _outermost(chrome):
        node.decompose()

    best, best_count = "", PAYWALL_WORD_LIMIT - 1
    for roots in groups:
        # decompose() clears removed roots and their descendants. Never replace
        # a removed body with its wider article shell or a nested comment host.
        html = "".join(str(node) for node in roots if not node.decomposed)
        count = _prose_word_count(html)
        if count > best_count:
            best, best_count = html, count
    return best


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
        article_html = max(
            ("", readability_html, _article_body_candidate(body_soup)),
            key=_prose_word_count,
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
        article_html = sanitize_article_html(article_html)

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
