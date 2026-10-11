"""URL cleaning and SSRF guards for user-submitted pages."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "s_kwcid",
    "yclid",
    "_hsenc",
    "_hsmi",
}


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
        or (addr.version == 6 and addr.is_site_local)
    )


# Hosts written with leading zeros, hex parts, or fewer than four parts (0177.0.0.1,
# 0x7f.0.0.1, 2130706433, 127.1) mean different things to different parsers. Only
# plain dotted-quad addresses are accepted, so every parser reads the same address.
_NUMERIC_LIKE_HOST = re.compile(
    r"^(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+))*\.?$", re.IGNORECASE
)


def host_is_public(hostname: str) -> bool:
    host = hostname.split("%")[0]
    try:
        if _is_public_ip(host):
            return True
        if ipaddress.ip_address(host):
            return False
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve host: {host}") from exc
    if not infos:
        raise ValueError(f"Could not resolve host: {host}")
    for info in infos:
        ip = info[4][0]
        if not _is_public_ip(ip):
            return False
    return True


def _validate_public_http_url(url: str, *, enforce_length: bool) -> str:
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Paste a URL first.")
    if enforce_length and len(raw) > 2048:
        raise ValueError("URL is too long.")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs can be archived.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with credentials are not allowed.")
    host = parsed.hostname
    if not host:
        raise ValueError("URL is missing a host.")
    if host.lower().rstrip(".") in {"localhost", "localhost.localdomain"}:
        raise ValueError("Local URLs cannot be archived.")
    if _NUMERIC_LIKE_HOST.fullmatch(host):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError("Numeric hosts must be written as a plain IP address.") from None
    if not host_is_public(host):
        raise ValueError("Private or local network URLs cannot be archived.")
    return urlunparse(parsed._replace(fragment=""))


def validate_public_http_url(url: str) -> str:
    """Validate a user-submitted capture URL, including its intake length limit."""
    return _validate_public_http_url(url, enforce_length=True)


def validate_public_http_request_url(url: str) -> str:
    """Validate a browser request URL without applying the intake length limit."""
    return _validate_public_http_url(url, enforce_length=False)


# Dropped from the lookup key only; the stored URL is never altered.
_KEY_DROP_PARAMS = TRACKING_PARAMS | {
    "smid", "unlocked_article_code", "accesstoken", "sharetype", "referringsource",
    "ocid", "guccounter", "gaa_at", "gaa_n", "gaa_sig", "gaa_ts",
}
# Prefixes of share/tracking parameters that publishers generate per link
# (FT appends `syn-<hash>=1`).
_KEY_DROP_PREFIXES_BY_HOST = {
    "ft.com": ("syn-",),
}
# Names that are generic elsewhere, so they only count as share/tracking
# tokens on the publishers that use them that way.
_KEY_DROP_PARAMS_BY_HOST = {
    "wsj.com": {"st", "reflink", "mod"},
    "ft.com": {"token", "segmentid"},
    "washingtonpost.com": {"pwapi_token", "itid"},
    "nytimes.com": {"partner", "referringsource"},
    "theguardian.com": {"cmp"},
}


def _scoped_prefixes(host: str) -> tuple[str, ...]:
    found: tuple[str, ...] = ()
    for domain, prefixes in _KEY_DROP_PREFIXES_BY_HOST.items():
        if host == domain or host.endswith("." + domain):
            found += prefixes
    return found


def _scoped_drops(host: str) -> set[str]:
    drops: set[str] = set()
    for domain, names in _KEY_DROP_PARAMS_BY_HOST.items():
        if host == domain or host.endswith("." + domain):
            drops |= names
    return drops


def article_match_key(url: str | None) -> str:
    """Scheme-less identity of an article URL, for finding it in the archive.

    Case-folds the host, drops `www.`, default ports, the fragment, a trailing
    slash, share/tracking parameters, and sorts what query remains. Two URLs
    with the same key are treated as the same article.
    """
    if not url:
        return ""
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return ""
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    if ":" in host:
        host = f"[{host}]"
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    bare_host = (parsed.hostname or "").lower().rstrip(".")
    drops = _KEY_DROP_PARAMS | _scoped_drops(bare_host)
    prefixes = ("utm_",) + _scoped_prefixes(bare_host)
    pairs = sorted(
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in drops and not k.lower().startswith(prefixes)
    )
    query = urlencode(pairs)
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS
        ]
    )
    return urlunparse((scheme, netloc, path, "", query, ""))
