"""Turn a Mac ``POST /api/import`` body into one Dell-PLX snapshot.

The extractor runs only when this module is imported, which the ingest app
does inside the import request. Capture and Playwright are not imported.
The bytes are committed with ``commit_new_revision``, the same journal the
hourly push uses.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from .extract import extract_article
from .freeze import freeze_html
from .import_child import TooComplex, check_complexity, measure_parsed_tree
from .library_contract import MAX_IMPORT_BODY_BYTES, sha256_hex
from .library_ingest import (
    IngestError,
    StoredImport,
    allocate_snapshot_id,
    commit_new_revision,
    hold,
    stored_imports,
    snapshot_brief,
)
from .reader import build_reader_html
from .security import article_match_key, normalize_url, same_site, validate_public_http_url

log = logging.getLogger("amber.library_import")

MIN_IMPORT_HTML_CHARS = 200
MAX_IMPORT_TITLE_CHARS = 300
_MIN_USABLE_WORDS = 120


@dataclass(frozen=True)
class ImportResult:
    status: int
    body: dict


@dataclass(frozen=True)
class _Parsed:
    html: str
    url: str
    final: str
    title: str
    force: bool


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return value


def _title_is_missing(article: dict, url: str) -> bool:
    """True when extraction fell back to the URL or a placeholder."""
    title = (article.get("title") or "").strip()
    if not title or title == url or title == "[no-title]":
        return True
    host = (urlparse(url).hostname or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    return title.lower() in {host, bare}


def _parse_body(body: dict) -> _Parsed:
    if not isinstance(body.get("url"), str):
        raise IngestError(400, 'Body must be a JSON object with a string "url".')
    html = body.get("html")
    if not isinstance(html, str) or len(html.strip()) < MIN_IMPORT_HTML_CHARS:
        raise IngestError(
            400,
            f'"html" must be a string of at least {MIN_IMPORT_HTML_CHARS} characters.',
        )
    try:
        html_bytes = len(html.encode("utf-8"))
    except UnicodeEncodeError:
        raise IngestError(400, '"html" is not valid Unicode text.') from None
    if html_bytes > MAX_IMPORT_BODY_BYTES:
        raise IngestError(413, "Request body is too large.")
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        raise IngestError(400, '"title" must be a string.')
    title = " ".join((title or "").split())[:MAX_IMPORT_TITLE_CHARS]
    force = body.get("force", False)
    if force is None:
        force = False
    if not isinstance(force, bool):
        raise IngestError(400, '"force" must be true or false.')
    final_raw = body.get("final_url")
    if final_raw is not None and not isinstance(final_raw, str):
        raise IngestError(400, '"final_url" must be a string.')
    try:
        clean = validate_public_http_url(body["url"])
    except ValueError as exc:
        raise IngestError(400, str(exc)) from exc
    final = clean
    if final_raw is not None and final_raw.strip():
        try:
            final = validate_public_http_url(final_raw)
        except ValueError as exc:
            message = str(exc)
            if message.startswith(("Private or local", "Local URLs", "Numeric hosts")):
                raise IngestError(400, "final_url is a private or local address") from exc
            raise IngestError(400, f"final_url: {message}") from exc
        if not same_site(clean, final):
            raise IngestError(400, "final_url is a different site")
    return _Parsed(html=html, url=clean, final=final, title=title, force=force)


def _keys(final: str, url: str) -> list[str]:
    """Article keys for the final URL and the requested URL, final first."""
    found: list[str] = []
    for address in (final, url):
        key = article_match_key(address)
        if key and key not in found:
            found.append(key)
    if not found:
        raise IngestError(400, "URL is missing a host.")
    return found


def _match(data_dir, keys: list[str], content_sha: str) -> tuple[StoredImport | None, bool]:
    wanted = set(keys)
    found: list[StoredImport] = []
    seen: set[str] = set()
    for item in stored_imports(data_dir):
        if item.sid in seen:
            continue
        hit = item.match_key in wanted or any(
            article_match_key(url) in wanted for url in item.urls
        )
        if not hit:
            continue
        seen.add(item.sid)
        found.append(item)
    exact = [item for item in found if item.content_sha256 == content_sha]
    if exact:
        return max(exact, key=lambda item: item.change_seq), True
    if found:
        return max(found, key=lambda item: item.change_seq), False
    return None, False


def _build(parsed: _Parsed, sid: str, created_at: str) -> tuple[dict, dict[str, bytes]]:
    check_complexity(parsed.html)
    measure_parsed_tree(parsed.html)
    article = extract_article(parsed.html, parsed.url, scrub_secrets=True)
    if parsed.title and _title_is_missing(article, parsed.url):
        article["title"] = parsed.title
    frozen = freeze_html(parsed.html, parsed.url, {}, sid, scrub_secrets=True)
    reader = build_reader_html(article, parsed.url)
    word_count = article.get("word_count")
    if isinstance(word_count, bool) or not isinstance(word_count, int):
        word_count = 0
    paywalled = bool(article.get("paywalled"))
    meta = {
        "id": sid,
        "url": parsed.url,
        "final_url": parsed.final,
        "title": article.get("title"),
        "site_name": article.get("site_name"),
        "author": article.get("author"),
        "published_at": article.get("published_at"),
        "description": article.get("description"),
        "dek": article.get("dek"),
        "word_count": word_count,
        "paywalled": paywalled,
        "imported": True,
        "created_at": created_at,
    }
    files = {
        "page.html": frozen.encode("utf-8"),
        "reader.html": reader.encode("utf-8"),
        "article.html": (article.get("article_html") or "").encode("utf-8"),
        "article.txt": (article.get("article_text") or "").encode("utf-8"),
        "meta.json": json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"),
    }
    row = {
        "url": parsed.url,
        "url_normalized": normalize_url(parsed.url),
        "final_url": parsed.final,
        "title": _opt_str(article.get("title")),
        "site_name": _opt_str(article.get("site_name")),
        "author": _opt_str(article.get("author")),
        "published_at": _opt_str(article.get("published_at")),
        "description": _opt_str(article.get("description")),
        "created_at": created_at,
        "http_status": None,
        "word_count": word_count,
        "paywalled": paywalled,
        "status": "complete",
        "error": None,
    }
    return row, files


def _document(sid: str, row: dict, files: dict[str, bytes], match_key: str, content_sha: str) -> dict:
    listed = []
    for path in sorted(files):
        data = files[path]
        listed.append({"path": path, "sha256": sha256_hex(data), "size": len(data)})
    return {
        "id": sid,
        "revision": 1,
        "op": "upsert",
        "match_key": match_key,
        "content_sha256": content_sha,
        "row": row,
        "files": listed,
    }


def _blobs(files: dict[str, bytes]) -> list[tuple[str, bytes]]:
    blobs: dict[str, bytes] = {}
    for data in files.values():
        blobs.setdefault(sha256_hex(data), data)
    return list(blobs.items())


def _public_title(title: object, url: str) -> str | None:
    if not isinstance(title, str) or not title or title in {url, "[no-title]"}:
        return None
    return title


def _stored_body(data_dir, sid: str, revision: int, change_seq: int) -> dict:
    row = snapshot_brief(data_dir, sid)
    if row is None:
        raise IngestError(500, "import failed")
    url = row["url"] if isinstance(row["url"], str) else ""
    words = row["word_count"]
    if isinstance(words, bool) or not isinstance(words, int):
        words = 0
    title = _public_title(row["title"], url)
    return {
        "id": sid,
        "status": "complete",
        "usable": words >= _MIN_USABLE_WORDS and title is not None,
        "word_count": words,
        "title": title,
        "revision": revision,
        "change_seq": change_seq,
    }


def _import_locked(data_dir, parsed: _Parsed, content_sha: str) -> ImportResult:
    keys = _keys(parsed.final, parsed.url)
    existing, exact = _match(data_dir, keys, content_sha)
    if existing is not None and (exact or not parsed.force):
        if not exact:
            raise IngestError(409, "already archived", id=existing.sid)
        return ImportResult(
            200,
            _stored_body(data_dir, existing.sid, existing.revision, existing.change_seq),
        )
    sid = allocate_snapshot_id(data_dir)
    created_at = _now()
    try:
        row, files = _build(parsed, sid, created_at)
    except TooComplex as exc:
        raise IngestError(422, f"Page too complex to import: {exc}") from None
    except IngestError:
        raise
    except Exception as exc:
        log.error("import failed: %s", type(exc).__name__)
        raise IngestError(422, "Could not turn that HTML into a snapshot.") from None
    document = _document(sid, row, files, article_match_key(parsed.final), content_sha)
    result = commit_new_revision(data_dir, document, _blobs(files))
    return ImportResult(
        result.status,
        _stored_body(data_dir, result.sid, result.revision, result.change_seq),
    )


def accept_import(data_dir, body: dict) -> ImportResult:
    """Store one rendered page, or return the snapshot already stored for it.

    Same URL and the same HTML bytes return 200 and the existing id. The same
    URL with different HTML returns 409 unless ``force`` is true, which stores
    a new id. Nothing is fetched.
    """
    parsed = _parse_body(body)
    content_sha = sha256_hex(parsed.html.encode("utf-8"))
    with hold():
        return _import_locked(data_dir, parsed, content_sha)
