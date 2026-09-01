from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SNAPS_DIR = DATA_DIR / "snaps"
DB_PATH = DATA_DIR / "amber.sqlite3"

HOST = "127.0.0.1"
PORT = 8080

VIEWPORT = {"width": 1024, "height": 768}
NAV_TIMEOUT_MS = 45_000
NETWORK_IDLE_MS = 8_000
RENDER_WAIT_MS = 1_800

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
