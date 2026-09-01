"""Turn a live DOM into a script-free snapshot with local images and CSS."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urldefrag

from bs4 import BeautifulSoup, NavigableString

CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.I)

KEEP_LINK_RELS = {
    "stylesheet",
    "icon",
    "shortcut icon",
    "apple-touch-icon",
    "apple-touch-icon-precomposed",
}


def lookup_resource(url: str, base: str, resource_map: dict[str, str]) -> str | None:
    abs_url = urljoin(base, url.strip())
    abs_url, _ = urldefrag(abs_url)
    if abs_url in resource_map:
        return resource_map[abs_url]
    # Some CDNs vary only by cache-buster query order.
    for key, name in resource_map.items():
        if key.split("?")[0] == abs_url.split("?")[0]:
            return name
    return None


def local_src(filename: str, snapshot_id: str) -> str:
    return f"/{snapshot_id}/r/{filename}"


def rewrite_css(css_text: str, css_url: str, resource_map: dict[str, str], snapshot_id: str) -> str:
    def repl(match: re.Match) -> str:
        quote, raw = match.group(1), match.group(2)
        if raw.startswith("data:") or raw.startswith("#"):
            return match.group(0)
        name = lookup_resource(raw, css_url, resource_map)
        if not name:
            return match.group(0)
        rewritten = local_src(name, snapshot_id)
        return f"url({quote}{rewritten}{quote})"

    return CSS_URL_RE.sub(repl, css_text)


def _rewrite_attr(tag, attr: str, base: str, resource_map: dict[str, str], snapshot_id: str) -> None:
    value = tag.get(attr)
    if not value or not isinstance(value, str):
        return
    if value.startswith("data:") or value.startswith("blob:"):
        return
    name = lookup_resource(value, base, resource_map)
    if name:
        tag[attr] = local_src(name, snapshot_id)


def _rewrite_srcset(tag, attr: str, base: str, resource_map: dict[str, str], snapshot_id: str) -> None:
    value = tag.get(attr)
    if not value or not isinstance(value, str):
        return
    parts = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        bits = item.split()
        url, rest = bits[0], bits[1:]
        name = lookup_resource(url, base, resource_map)
        if name:
            url = local_src(name, snapshot_id)
        parts.append(" ".join([url, *rest]).strip())
    tag[attr] = ", ".join(parts)


def freeze_html(html: str, base_url: str, resource_map: dict[str, str], snapshot_id: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(["script", "noscript", "template"]):
        tag.decompose()

    for tag in soup.find_all(["iframe", "object", "embed", "applet"]):
        src = tag.get("src") or tag.get("data") or ""
        note = soup.new_tag("p")
        note["class"] = "amber-removed"
        note.string = f"[embedded content removed{': ' + src if src else ''}]"
        tag.replace_with(note)

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
        href = tag.get("href")
        if isinstance(href, str) and href.strip().lower().startswith("javascript:"):
            tag["href"] = "#"
        src = tag.get("src")
        if isinstance(src, str) and src.strip().lower().startswith("javascript:"):
            del tag.attrs["src"]

    for form in soup.find_all("form"):
        form["action"] = ""
        form["method"] = "get"
        form["onsubmit"] = "return false"
        if "target" in form.attrs:
            del form.attrs["target"]

    for tag in soup.find_all(["input", "button", "select", "textarea"]):
        tag["disabled"] = "disabled"

    for tag in soup.find_all(True):
        if tag.has_attr("contenteditable"):
            del tag.attrs["contenteditable"]
        if tag.has_attr("autofocus"):
            del tag.attrs["autofocus"]
        if tag.name in {"video", "audio"}:
            tag["preload"] = "none"
            if "autoplay" in tag.attrs:
                del tag.attrs["autoplay"]

    for meta in soup.find_all("meta"):
        http_equiv = (meta.get("http-equiv") or "").lower()
        if http_equiv == "refresh":
            meta.decompose()

    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        if rel not in KEEP_LINK_RELS:
            link.decompose()
            continue
        _rewrite_attr(link, "href", base_url, resource_map, snapshot_id)

    for tag in soup.find_all(["img", "source", "video", "audio", "track", "use"]):
        _rewrite_attr(tag, "src", base_url, resource_map, snapshot_id)
        _rewrite_attr(tag, "poster", base_url, resource_map, snapshot_id)
        _rewrite_attr(tag, "href", base_url, resource_map, snapshot_id)
        _rewrite_srcset(tag, "srcset", base_url, resource_map, snapshot_id)
        _rewrite_srcset(tag, "imagesrcset", base_url, resource_map, snapshot_id)

    for tag in soup.find_all(style=True):
        tag["style"] = rewrite_css(tag["style"], base_url, resource_map, snapshot_id)

    for style in soup.find_all("style"):
        if isinstance(style.string, NavigableString) or style.string:
            style.string = rewrite_css(style.get_text() or "", base_url, resource_map, snapshot_id)

    if not soup.head:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)

    csp = soup.new_tag("meta")
    csp["http-equiv"] = "Content-Security-Policy"
    csp["content"] = (
        "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; media-src 'self' data:; frame-src 'none'; script-src 'none';"
    )
    soup.head.insert(0, csp)

    robots = soup.new_tag("meta")
    robots["name"] = "robots"
    robots["content"] = "noindex, nofollow"
    soup.head.insert(1, robots)

    marker = soup.new_tag("meta")
    marker["name"] = "amber-snapshot"
    marker["content"] = snapshot_id
    soup.head.insert(2, marker)

    return str(soup)
