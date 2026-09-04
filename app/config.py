import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

_data = os.environ.get("AMBER_DATA_DIR")
DATA_DIR = Path(_data).resolve() if _data else ROOT / "data"
SNAPS_DIR = DATA_DIR / "snaps"
DB_PATH = DATA_DIR / "amber.sqlite3"

HOST = os.environ.get("AMBER_HOST", "127.0.0.1")
PORT = int(os.environ.get("AMBER_PORT", "8080"))

VIEWPORT = {"width": 1280, "height": 900}
NAV_TIMEOUT_MS = 45_000
NETWORK_IDLE_MS = 8_000
RENDER_WAIT_MS = 2_200
EXTRA_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

# EXTRA_HEADERS Referer is not enough: Chromium page.goto is an address-bar
# navigation (Sec-Fetch-Site: none). Soft/metered paywalls that grant a
# free read to Google or X traffic are retried from these origins only —
# never a user-supplied Referer — by landing on a stub and clicking through.
REFERRER_BOUNCE_ORIGINS = (
    "https://www.google.com/",
    "https://news.google.com/",
    "https://x.com/",
    "https://t.co/",
)
# One attempt per family. news.google.com and t.co stay on the allowlist
# (require_bounce_origin) but are not extra Playwright visits.
REFERRER_BOUNCE_RETRY_ORIGINS = (
    "https://www.google.com/",
    "https://x.com/",
)
REFERRER_BOUNCE_HOSTS = frozenset(
    (urlparse(origin).hostname or "").lower() for origin in REFERRER_BOUNCE_ORIGINS
)


def referrer_is_allowlisted(referrer: str | None) -> bool:
    host = (urlparse(referrer or "").hostname or "").lower()
    return bool(host) and host in REFERRER_BOUNCE_HOSTS


GOOGLEBOT_UA = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)

MAX_RESOURCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RESOURCE_BYTES = 80 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

BLOCKED_HOST_SNIPPETS = (
    "google-analytics.com",
    "googletagmanager.com",
    "googleadservices.com",
    "doubleclick.net",
    "scorecardresearch.com",
    "facebook.net",
    "connect.facebook.net",
    "adsystem.com",
    "adservice.google",
    "hotjar.com",
    "fullstory.com",
    "segment.io",
    "segment.com",
    "newrelic.com",
    "nr-data.net",
    "sentry.io",
    "clarity.ms",
    "ads-twitter.com",
    "criteo.com",
    "taboola.com",
    "outbrain.com",
    "chartbeat.com",
    "parsely.com",
    "cdn.cxense.com",
)

ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ID_LENGTH = 5
