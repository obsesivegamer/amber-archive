"""Session cookie and HTTP Basic, compatible with amber-reader.

The signing key is ``sha256("amber-reader-session-v1|" + password)``. A cookie
minted by amber-reader for the same password is accepted here. The password
comes from ``AMBER_READER_PASSWORD_FILE`` when that is set, otherwise from
``AMBER_READER_PASSWORD``. Nothing in this module trusts the client address
or ``X-Forwarded-*`` / ``Tailscale-User-*`` headers.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse, Response

REALM = "amber-reader"
USERNAME = os.environ.get("AMBER_READER_USERNAME", "amber")
COOKIE_NAME = "amber_session"
SESSION_DAYS = int(os.environ.get("AMBER_READER_SESSION_DAYS", "30"))
SESSION_MAX_AGE = SESSION_DAYS * 24 * 3600

OPEN_PATHS = frozenset({"/healthz", "/login", "/favicon.ico"})


class MissingPassword(RuntimeError):
    pass


# path, mtime_ns, password. Invalidated when the file's mtime changes.
_password_cache: tuple[str, int, str] | None = None


def required_password() -> str:
    global _password_cache
    raw = os.environ.get("AMBER_READER_PASSWORD_FILE", "").strip()
    if raw:
        from .library_contract import read_token_file

        path = Path(raw)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = -1
        resolved = str(path)
        if (
            _password_cache is not None
            and _password_cache[0] == resolved
            and _password_cache[1] == mtime
            and mtime >= 0
        ):
            return _password_cache[2]
        password = read_token_file(path)
        _password_cache = (resolved, path.stat().st_mtime_ns, password)
        return password
    _password_cache = None
    password = os.environ.get("AMBER_READER_PASSWORD", "")
    if not password.strip():
        raise MissingPassword(
            "AMBER_READER_PASSWORD_FILE or AMBER_READER_PASSWORD is not set. "
            "Amber refuses to serve this process unauthenticated."
        )
    return password


def password_configured() -> bool:
    if os.environ.get("AMBER_READER_PASSWORD_FILE", "").strip():
        return True
    return bool(os.environ.get("AMBER_READER_PASSWORD", "").strip())


def password_ok(given: str) -> bool:
    return secrets.compare_digest(given or "", required_password())


def _key() -> bytes:
    return hashlib.sha256(b"amber-reader-session-v1|" + required_password().encode()).digest()


def _sign(issued: str) -> str:
    return hmac.new(_key(), f"v1|{issued}".encode(), hashlib.sha256).hexdigest()


def issue_session() -> str:
    issued = str(int(time.time()))
    return f"{issued}.{_sign(issued)}"


def session_ok(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    issued, _, sig = cookie.partition(".")
    if not issued.isdigit():
        return False
    if not hmac.compare_digest(sig, _sign(issued)):
        return False
    return 0 <= time.time() - int(issued) <= SESSION_MAX_AGE


def request_is_https(request) -> bool:
    """True for a real https URL, or for tailscale serve's own proxy.

    ``X-Forwarded-Proto`` is honored only from the proxy's loopback address,
    and only to set the cookie's Secure flag. It never authenticates a request.
    """
    if request.url.scheme == "https":
        return True
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1"}:
        return False
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def set_session_cookie(response: Response, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issue_session(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def basic_ok(header: str | None) -> bool:
    if not header:
        return False
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    user, sep, given = decoded.partition(":")
    if not sep:
        return False
    user_ok = secrets.compare_digest(user, USERNAME)
    return password_ok(given) and user_ok


def _wants_html(request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


def _is_api(request) -> bool:
    path = request.url.path
    return path == "/api" or path.startswith("/api/")


def _open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith("/static/")


CHALLENGE = f'AmberBasic realm="{REALM}"'


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not password_configured():
            return await call_next(request)
        if _open(request.url.path):
            return await call_next(request)
        required_password()
        if session_ok(request.cookies.get(COOKIE_NAME)) or basic_ok(
            request.headers.get("authorization")
        ):
            return await call_next(request)
        if _wants_html(request) and not _is_api(request):
            nxt = request.url.path
            if request.url.query:
                nxt += f"?{request.url.query}"
            return RedirectResponse(f"/login?next={nxt}", status_code=303)
        return PlainTextResponse(
            "Authentication required.",
            status_code=401,
            headers={"WWW-Authenticate": CHALLENGE},
        )
