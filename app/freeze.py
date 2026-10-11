"""Turn a live DOM into a script-free snapshot with local images and CSS."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urljoin, urldefrag, urlparse

from bs4 import BeautifulSoup, NavigableString

from .config import HOST, ID_ALPHABET, ID_LENGTH

# Possessive quantifiers: no backtracking, so a hostile stylesheet of `url(`
# repeats or long whitespace runs stays linear.
CSS_URL_RE = re.compile(r"""url\(\s*+(?:"([^"]*+)"|'([^']*+)'|([^'"()\s]*+))\s*+\)""", re.I)

# `@import "x.css"` (string form) is not a url(...) token.
CSS_IMPORT_RE = re.compile(r"""@import\s*+(?:"([^"]*+)"|'([^']*+)')""", re.I)

_MEDIA_URL_ATTRS = ("src", "poster", "background", "lowsrc", "dynsrc", "xlink:href")
_MEDIA_SRCSET_ATTRS = ("srcset", "imagesrcset")


def _is_local_host(host: str) -> bool:
    """Literal check, no DNS: names and addresses that mean this machine or LAN."""
    host = host.lower().rstrip(".")
    if host in {"localhost", HOST.lower()} or host.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host.strip("[]")).is_global
    except ValueError:
        pass
    # 2130706433, 0x7f.0.0.1, 0177.0.0.1, 127.1 ...
    return bool(re.fullmatch(r"(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+))*", host))


_SAVED_FILE_PATH_RE = re.compile(rf"^/[{re.escape(ID_ALPHABET)}]{{{ID_LENGTH}}}/r/")
# scheme (optional) followed by slashes that include a backslash: browsers read
# `\\host`, `/\host` and `https:\\host` as `//host`.
_BACKSLASH_AUTHORITY_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:)?[\\/]*\\")


def absolute_or_drop(value: str, base: str) -> str | None:
    """Resolve a subresource URL against the page it came from.

    A root-relative `/save?...` left as written would resolve against Amber
    when the snapshot is viewed, so it becomes https://<original host>/...
    Anything that is not http(s) or that points at this machine is dropped
    (None). data:, blob: and #fragment references are kept as they are.
    """
    v = (value or "").strip()
    if not v or v.startswith("#") or v.lower().startswith(("data:", "blob:")):
        return value
    if _SAVED_FILE_PATH_RE.match(v):
        return value  # Amber's own /<id>/r/<file>; a captured file, not a page URL
    if _BACKSLASH_AUTHORITY_RE.match(v):
        return None
    try:
        absolute = urljoin(base, v)
        parsed = urlparse(absolute)
        host = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or "\\" in parsed.netloc
        or _is_local_host(host)
    ):
        return None
    return absolute


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


def _css_value(raw: str, base: str, resource_map: dict[str, str], snapshot_id: str) -> str | None:
    name = lookup_resource(raw, base, resource_map)
    if name:
        return local_src(name, snapshot_id)
    return absolute_or_drop(raw, base)


MAX_CSS_REWRITES = 20_000


def rewrite_css(css_text: str, css_url: str, resource_map: dict[str, str], snapshot_id: str) -> str:
    """Rewrite url(...) and @import "..." targets; work is bounded.

    Repeated values are resolved once, and past MAX_CSS_REWRITES distinct
    resolutions the remaining targets are dropped without being resolved, so a
    hostile stylesheet costs a bounded number of urljoin calls.
    """
    memo: dict[str, str | None] = {}
    budget = [MAX_CSS_REWRITES]

    def resolve(raw: str) -> str | None:
        if raw in memo:
            return memo[raw]
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        memo[raw] = _css_value(raw, css_url, resource_map, snapshot_id)
        return memo[raw]

    def repl(match: re.Match) -> str:
        if match.group(1) is not None:
            quote, raw = '"', match.group(1)
        elif match.group(2) is not None:
            quote, raw = "'", match.group(2)
        else:
            quote, raw = "", match.group(3)
        if not raw.strip() or raw.startswith("data:") or raw.startswith("#"):
            return match.group(0)
        rewritten = resolve(raw)
        if rewritten is None:
            return "url()"
        if rewritten == raw:
            return match.group(0)
        if not quote and re.search(r"""[\s()'"]""", rewritten):
            quote = '"'
        if quote:
            rewritten = rewritten.replace(quote, "%22" if quote == '"' else "%27")
        return f"url({quote}{rewritten}{quote})"

    def import_repl(match: re.Match) -> str:
        quote, raw = ('"', match.group(1)) if match.group(1) is not None else ("'", match.group(2))
        if not raw.strip():
            return match.group(0)
        rewritten = resolve(raw)
        if rewritten is None:
            return "@import url()"
        return f"@import {quote}{rewritten.replace(quote, '%22')}{quote}"

    return CSS_IMPORT_RE.sub(import_repl, CSS_URL_RE.sub(repl, css_text))


def _rewrite_attr(tag, attr: str, base: str, resource_map: dict[str, str], snapshot_id: str) -> None:
    value = tag.get(attr)
    if not value or not isinstance(value, str):
        return
    if value.startswith("data:") or value.startswith("blob:"):
        return
    rewritten = _css_value(value, base, resource_map, snapshot_id)
    if rewritten is None:
        del tag.attrs[attr]
    elif rewritten != value:
        tag[attr] = rewritten


_SRCSET_SPACE = " \t\n\r\f"


def parse_srcset(value: str) -> list[tuple[str, str]]:
    """Split a srcset into (url, descriptor) candidates the way browsers do.

    The URL is everything up to the next whitespace, so commas inside it
    (`w_120,c_limit/a.jpg`, `width=1024,quality=80/...`, `data:image/png;base64,...`)
    stay put. A URL that ends in commas has no descriptor; otherwise the
    descriptor runs to the next comma outside parentheses.
    """
    candidates: list[tuple[str, str]] = []
    pos, n = 0, len(value)
    while True:
        while pos < n and (value[pos] in _SRCSET_SPACE or value[pos] == ","):
            pos += 1
        if pos >= n:
            return candidates
        start = pos
        while pos < n and value[pos] not in _SRCSET_SPACE:
            pos += 1
        url = value[start:pos]
        if url.endswith(","):
            candidates.append((url.rstrip(","), ""))
            continue
        dstart, depth = pos, 0
        while pos < n:
            ch = value[pos]
            if ch == "(":
                depth += 1
            elif ch == ")" and depth:
                depth -= 1
            elif ch == "," and not depth:
                break
            pos += 1
        candidates.append((url, " ".join(value[dstart:pos].split())))


def _rewrite_srcset(tag, attr: str, base: str, resource_map: dict[str, str], snapshot_id: str) -> None:
    value = tag.get(attr)
    if not value or not isinstance(value, str):
        return
    parts = []
    changed = False
    for url, descriptor in parse_srcset(value):
        if not url.startswith(("data:", "blob:")):
            rewritten = _css_value(url, base, resource_map, snapshot_id)
            if rewritten is None:
                changed = True
                continue
            if rewritten != url:
                changed = True
                url = rewritten
        parts.append(f"{url} {descriptor}".strip())
    if not changed:
        return
    if parts:
        tag[attr] = ", ".join(parts)
    else:
        del tag.attrs[attr]


def rewrite_media_urls(root, base: str, resource_map: dict[str, str], snapshot_id: str) -> None:
    """Point every subresource URL at the original site, a captured file, or nothing."""
    for tag in root.find_all(True):
        for attr in _MEDIA_URL_ATTRS:
            _rewrite_attr(tag, attr, base, resource_map, snapshot_id)
        if tag.name in {"use", "image"}:
            _rewrite_attr(tag, "href", base, resource_map, snapshot_id)
        for attr in _MEDIA_SRCSET_ATTRS:
            _rewrite_srcset(tag, attr, base, resource_map, snapshot_id)
        if "formaction" in tag.attrs:
            del tag.attrs["formaction"]


_SECRET_META_RE = re.compile(r"csrf|xsrf|token|nonce|authenticity|secret|session", re.I)
# No '/', '.' or ':' in the charset, so media addresses and URLs never match.
_OPAQUE_TOKEN_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_+=-]{32,}$")
_JWT_RE = re.compile(r"^eyJ[\w-]+\.[\w-]+\.[\w-]*$")
_INPUT_TYPES_KEEPING_VALUE = {"submit", "button", "reset", "image", "checkbox", "radio"}


def strip_secrets(soup) -> None:
    """Drop what a signed-in page carries that is not article content: form
    values, CSRF/token metas and opaque token-looking data-* values."""
    for meta in soup.find_all("meta"):
        name = meta.get("name") or meta.get("property") or meta.get("http-equiv") or ""
        if isinstance(name, str) and _SECRET_META_RE.search(name):
            meta.decompose()
    for tag in soup.find_all(True):
        if tag.name == "input" and tag.has_attr("value"):
            kind = str(tag.get("type") or "text").lower()
            if kind not in _INPUT_TYPES_KEEPING_VALUE:
                del tag.attrs["value"]
        for attr in list(tag.attrs):
            value = tag.attrs[attr]
            if not attr.startswith("data-") or not isinstance(value, str):
                continue
            if _OPAQUE_TOKEN_RE.match(value) or _JWT_RE.match(value):
                del tag.attrs[attr]


def freeze_html(
    html: str,
    base_url: str,
    resource_map: dict[str, str],
    snapshot_id: str,
    *,
    scrub_secrets: bool = False,
) -> str:
    soup = BeautifulSoup(html, "lxml")
    if scrub_secrets:
        strip_secrets(soup)

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
            if attr.lower().startswith("on") or attr.lower() in {"ping", "attributionsrc"}:
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

    rewrite_media_urls(soup, base_url, resource_map, snapshot_id)

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
