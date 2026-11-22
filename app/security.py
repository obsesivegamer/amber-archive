"""URL cleaning and SSRF guards for user-submitted pages."""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
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
    "theatlantic.com": {"gift"},
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


# Public suffixes (registrable domain = suffix plus one label), including
# shared-hosting suffixes where every subdomain belongs to a different owner.
_PUBLIC_SUFFIXES = frozenset(
    "co.uk org.uk ac.uk gov.uk me.uk ltd.uk plc.uk com.au net.au org.au edu.au gov.au "
    "co.nz org.nz govt.nz co.jp or.jp ne.jp ac.jp go.jp com.br net.br org.br com.cn net.cn "
    "org.cn com.hk com.sg com.tw com.mx com.ar com.tr com.ua co.in net.in org.in co.za "
    "co.kr or.kr co.il co.id com.my com.ph com.pk com.eg com.sa com.ng "
    "github.io gitlab.io blogspot.com herokuapp.com vercel.app netlify.app pages.dev "
    "workers.dev web.app firebaseapp.com azurewebsites.net cloudfront.net "
    "s3.amazonaws.com appspot.com wordpress.com "
    "substack.com medium.com wixsite.com myshopify.com tumblr.com squarespace.com "
    "ghost.io notion.site".split()
)
_S3_LABEL_RE = re.compile(r"^s3(?:$|[-.].*)")


def _punycode_host(host: str) -> str:
    """UTS 46-ish fallback when the `idna` package is missing: NFKC and
    lower-case, then punycode per label. Deliberately not IDNA 2003, so ß stays
    ß (xn--zca) and does not collapse into ss."""
    labels = []
    for label in host.split("."):
        if label.isascii():
            labels.append(label)
        else:
            try:
                labels.append("xn--" + label.encode("punycode").decode("ascii"))
            except UnicodeError:
                labels.append(label)
    return ".".join(labels)


def _host_key(url: str) -> str:
    """Lower-cased IDNA 2008 (A-label) host, after NFKC so full-width forms compare equal."""
    host = unicodedata.normalize("NFKC", urlparse(url).hostname or "").lower().rstrip(".")
    if host.isascii():
        return host
    try:
        import idna

        return idna.encode(host, uts46=True, transitional=False).decode("ascii")
    except ImportError:
        return _punycode_host(host)
    except Exception:
        return _punycode_host(host)


def _registrable_domain(host: str) -> str:
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if host.endswith(".amazonaws.com"):
        # bucket.s3.amazonaws.com, bucket.s3.<region>.amazonaws.com,
        # bucket.s3-<region>.amazonaws.com: the whole host names one owner.
        if any(_S3_LABEL_RE.match(label) for label in labels[:-2]):
            return host
    for i in range(1, len(labels)):
        if ".".join(labels[i:]) in _PUBLIC_SUFFIXES:
            return ".".join(labels[i - 1 :])
    return ".".join(labels[-2:])


def _port_class(url: str) -> int | str:
    try:
        port = urlparse(url).port
    except ValueError:
        return "invalid"
    return "default" if port in (None, 80, 443) else port


def same_site(a: str, b: str) -> bool:
    """True when two URLs belong to one site, including a shared registrable domain."""
    try:
        host_a, host_b = _host_key(a), _host_key(b)
        if not host_a or not host_b or _port_class(a) != _port_class(b):
            return False
    except ValueError:
        return False
    return host_a == host_b or _registrable_domain(host_a) == _registrable_domain(host_b)
