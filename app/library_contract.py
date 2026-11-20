"""Wire rules for pushing one snapshot to the Dell-PLX library.

Stdlib only. The push command and the ingest process both use this so a path
or a hash is accepted in one place. It does not import the capture stack.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import stat
from pathlib import Path

from .config import ID_ALPHABET, ID_LENGTH

ID_RE = re.compile(rf"^[{ID_ALPHABET}]{{{ID_LENGTH}}}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Stated caps. The revision document carries hashes, not file bytes.
MAX_BLOB_BYTES = 32 * 1024 * 1024
MAX_REVISION_BYTES = 1 * 1024 * 1024
MAX_FILES = 512

SNAPSHOT_COLUMNS = (
    "id",
    "url",
    "url_normalized",
    "final_url",
    "title",
    "site_name",
    "author",
    "published_at",
    "description",
    "created_at",
    "http_status",
    "word_count",
    "paywalled",
    "status",
    "error",
)

# Same snapshots table amber-archive writes and amber-reader selects.
# Ingest creates it when the library database is new. Gift candidates stay
# on the Mac, so this list has no candidates table.
SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    url_normalized TEXT NOT NULL,
    final_url TEXT,
    title TEXT,
    site_name TEXT,
    author TEXT,
    published_at TEXT,
    description TEXT,
    created_at TEXT NOT NULL,
    http_status INTEGER,
    word_count INTEGER,
    paywalled INTEGER,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_snaps_url ON snapshots(url_normalized);
CREATE INDEX IF NOT EXISTS idx_snaps_created ON snapshots(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_snaps_status ON snapshots(status);
"""

REVISIONS_DDL = """
CREATE TABLE IF NOT EXISTS library_revisions (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    op TEXT NOT NULL,
    match_key TEXT,
    change_seq INTEGER NOT NULL,
    files_json TEXT NOT NULL,
    row_json TEXT NOT NULL
);
"""

CHANGES_DDL = """
CREATE TABLE IF NOT EXISTS library_changes (
    change_seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    op TEXT NOT NULL,
    body_json TEXT NOT NULL
);
"""


class TokenFileError(Exception):
    """The bearer-token file cannot be used."""


def snapshot_relpath(path: str) -> str | None:
    """A file inside one snapshot, or None.

    Top-level names and one level under ``res/`` are the tree the reader
    opens. ``..``, absolute paths, and any other directory are rejected.
    """
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        return None
    if path.startswith("/"):
        return None
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2 and parts[0] == "res":
        return "res/" + parts[1]
    return None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tokens_equal(presented: str, expected: str) -> bool:
    """Compare bearer tokens in constant time, including when lengths differ."""
    return hmac.compare_digest(
        hashlib.sha256(presented.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def read_token_file(path: Path) -> str:
    """Read one token from a regular file that group and other cannot access.

    A single trailing newline is ignored so a file written by a text editor
    still matches. Mode ``0600`` is the expected mode; any mode with no group
    or other bits is accepted.
    """
    try:
        if path.is_symlink():
            raise TokenFileError("token file must not be a symlink")
        info = path.stat()
    except OSError as exc:
        raise TokenFileError("token file is missing") from exc
    if not stat.S_ISREG(info.st_mode):
        raise TokenFileError("token file must be a regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise TokenFileError("token file must be mode 0600")
    if not info.st_mode & stat.S_IRUSR:
        raise TokenFileError("token file must be readable by its owner")
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise TokenFileError("token file must be utf-8") from exc
    if raw.endswith("\n"):
        raw = raw[:-1]
    if raw.endswith("\r"):
        raw = raw[:-1]
    if raw == "" or "\n" in raw or "\r" in raw:
        raise TokenFileError("token file must be one non-empty line")
    return raw
