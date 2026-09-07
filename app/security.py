"""URL cleaning and SSRF guards for user-submitted pages."""

from __future__ import annotations

import ipaddress
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
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("Local URLs cannot be archived.")
    if not host_is_public(host):
        raise ValueError("Private or local network URLs cannot be archived.")
    return urlunparse(parsed._replace(fragment=""))


def validate_public_http_url(url: str) -> str:
    """Validate a user-submitted capture URL, including its intake length limit."""
    return _validate_public_http_url(url, enforce_length=True)


def validate_public_http_request_url(url: str) -> str:
    """Validate a browser request URL without applying the intake length limit."""
    return _validate_public_http_url(url, enforce_length=False)


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
