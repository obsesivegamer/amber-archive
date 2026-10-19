"""Gift-link candidate queue.

The CLI reads and writes the local SQLite file. It does not bind or connect a
socket, and it does not fetch gift links. Finds are validated and deduped here;
a click later captures the stored unlock URL through the normal job queue.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import posixpath
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from . import db
from .config import ID_ALPHABET
from .security import article_match_key, normalize_url, validate_public_http_url

PUBLISHERS = frozenset({"nyt", "wsj", "ft", "wapo", "atlantic", "newyorker", "other"})

# Shorter than the usual public gift window. A reported expires_at still wins
# when it is earlier. Tune later.
PUBLISHER_TTL = {
    "nyt": timedelta(days=7),
    "wsj": timedelta(days=3),
    "ft": timedelta(days=7),
    "wapo": timedelta(days=7),
    "atlantic": timedelta(days=7),
    "newyorker": timedelta(days=7),
    "other": timedelta(days=2),
}
# Article hosts. Keys match PUBLISHERS. Only the apex, or that apex with one
# www label, is the publication. Other subdomains are not.
PUBLICATION_DOMAINS = {
    "nyt": "nytimes.com",
    "wsj": "wsj.com",
    "ft": "ft.com",
    "wapo": "washingtonpost.com",
    "atlantic": "theatlantic.com",
    "newyorker": "newyorker.com",
}
_APEX_HOSTS = frozenset(PUBLICATION_DOMAINS.values())
# FT gift links are redeemed on this host, not on ft.com.
_EXTRA_ALLOWED_HOSTS = frozenset({"giftarticle.ft.com"})
# The homepage and /newsletters are not articles. Compared after the path is
# percent-decoded and normalized.
_NON_ARTICLE_PATHS = frozenset({"/", "/newsletters"})
# Expired rows stay this long so a repeat find refreshes the same candidate
# instead of queueing a dead link again.
PURGE_AFTER = timedelta(days=30)

MAX_TITLE_CHARS = 300
MAX_AUTHOR_CHARS = 200
MAX_STDIN_BYTES = 2 * 1024 * 1024

CANDIDATE_ID_RE = re.compile(
    rf"^[{re.escape(ID_ALPHABET)}]{{{db.GIFT_CANDIDATE_ID_LENGTH}}}$"
)

_STORED_TIME = "%Y-%m-%dT%H:%M:%SZ"


def format_time(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc).replace(microsecond=0)
    return moment.strftime(_STORED_TIME)


def parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO timestamp")
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_stored(value: str) -> datetime:
    return datetime.strptime(value, _STORED_TIME).replace(tzinfo=timezone.utc)


def expiry_for(
    publisher: str, found_at: datetime, reported: datetime | None
) -> tuple[datetime, bool]:
    """Earlier of the reported expiry and found_at plus the publisher cap.

    The bool is ``expires_estimated``: true when the cap is what got stored.
    """
    cap = found_at + PUBLISHER_TTL[publisher]
    if reported is None or reported > cap:
        return cap, True
    return reported, False


def _present(find: dict, *names: str):
    for name in names:
        if name not in find:
            continue
        value = find[name]
        if value is None or value == "":
            continue
        return value
    return None


def _publisher(find: dict) -> str:
    if "publisher" not in find or find["publisher"] in (None, ""):
        return "other"
    value = find["publisher"]
    if not isinstance(value, str) or value.strip().lower() not in PUBLISHERS:
        raise ValueError("unknown publisher")
    return value.strip().lower()


def _short_text(value: object, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = " ".join(value.split())
    if not text:
        return None
    return text[:limit]


def _clean_url(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    try:
        return validate_public_http_url(value)
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc


def _optional_url(find: dict, field: str, *aliases: str) -> str | None:
    value = _present(find, field, *aliases)
    if value is None:
        return None
    return _clean_url(value, field)


def _keys(*urls: str | None) -> set[str]:
    found = set()
    for url in urls:
        if not url:
            continue
        key = article_match_key(url)
        if key:
            found.add(key)
    return found


def _row_keys(row: dict) -> set[str]:
    keys = _keys(row.get("canonical_url"), row.get("unlock_url"))
    stored = row.get("match_key")
    if stored:
        keys.add(stored)
    return keys


def _queue_match(rows: list[dict], keys: set[str]) -> dict | None:
    for row in rows:
        if _row_keys(row) & keys:
            return row
    return None


def _apply(conn: sqlite3.Connection, row: dict, updates: dict) -> None:
    if not updates:
        return
    db.update_gift_candidate_in(conn, row["id"], **updates)
    for key, value in updates.items():
        row[key] = bool(value) if key == "expires_estimated" else value


def _refresh_updates(row: dict, incoming: dict) -> dict:
    """A later reported expiry, or a newly filled-in canonical URL, wins.

    A find with no ``expires_at`` is capped from its own ``found_at``. Using
    that cap on a re-add would keep a dead link alive, so only a reported
    expiry that is later than the stored one moves the deadline.
    """
    updates: dict = {}
    reported = incoming.get("reported_expires_at")
    if reported and incoming["expires_at"] > row["expires_at"]:
        updates["expires_at"] = incoming["expires_at"]
        updates["expires_estimated"] = incoming["expires_estimated"]
        updates["unlock_url"] = incoming["unlock_url"]
    if incoming["canonical_url"] and not row.get("canonical_url"):
        updates["canonical_url"] = incoming["canonical_url"]
        canonical_key = article_match_key(incoming["canonical_url"])
        if canonical_key:
            updates["match_key"] = canonical_key
    if incoming["title"] and not row.get("title"):
        updates["title"] = incoming["title"]
    if incoming["source_url"] and not row.get("source_url"):
        updates["source_url"] = incoming["source_url"]
    if incoming["source_author"] and not row.get("source_author"):
        updates["source_author"] = incoming["source_author"]
    if row.get("publisher") == "other" and incoming["publisher"] != "other":
        updates["publisher"] = incoming["publisher"]
    return updates


def _usable_archive(urls: list[str], best_match, find_matches) -> bool:
    seen: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.append(url)
    for url in seen:
        _snap, _status, usable = best_match(find_matches(url))
        if usable:
            return True
    return False


def _normalized_path(path: str) -> str:
    """Percent-decode, collapse slashes, and resolve . and .. segments.

    posixpath.normpath keeps a leading double slash, so repeated slashes are
    collapsed first. The membership test then sees /newsletters for
    //newsletters, foo/../newsletters, and %6eewsletters.
    """
    decoded = unquote(path or "/")
    collapsed = re.sub(r"/{2,}", "/", decoded)
    if not collapsed.startswith("/"):
        collapsed = "/" + collapsed
    normalized = posixpath.normpath(collapsed)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized.lower()


def _host_and_path(url: str) -> tuple[str, str] | None:
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not host:
        return None
    return host, _normalized_path(parsed.path or "/")


def _address_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return True


def _allowed_publication_host(host: str) -> bool:
    """Apex, a single www label on that apex, or an explicit extra host."""
    host = host.lower().rstrip(".")
    if host in _EXTRA_ALLOWED_HOSTS or host in _APEX_HOSTS:
        return True
    if host.startswith("www."):
        return host[4:] in _APEX_HOSTS
    return False


def _allowed_article_url(url: str) -> bool:
    """True for an article on an allowed publication.

    Address literals are not publication domains. They stay on the public-URL
    check, which does not need a DNS lookup. On an allowed publication, the
    homepage and /newsletters are not articles. Any other hostname is rejected.
    """
    found = _host_and_path(url)
    if found is None:
        return False
    host, path = found
    if _address_literal(host):
        return True
    if not _allowed_publication_host(host):
        return False
    return path not in _NON_ARTICLE_PATHS


def _require_allowed_article(url: str, field: str) -> None:
    if not _allowed_article_url(url):
        raise ValueError(f"{field} is not an allowed article URL")


def _parse_find(find: dict) -> dict:
    publisher = _publisher(find)
    raw_unlock = _present(find, "unlock_url")
    if raw_unlock is None:
        raise ValueError("unlock_url is required")
    unlock = _clean_url(raw_unlock, "unlock_url")
    _require_allowed_article(unlock, "unlock_url")
    canonical = _optional_url(find, "canonical_url")
    if canonical:
        _require_allowed_article(canonical, "canonical_url")
    source_url = _optional_url(find, "source_url", "x_post_url")
    title = _short_text(_present(find, "title"), "title", MAX_TITLE_CHARS)
    author = _short_text(
        _present(find, "source_author", "x_author"), "source_author", MAX_AUTHOR_CHARS
    )
    raw_found = _present(find, "found_at")
    if raw_found is None:
        raise ValueError("found_at is required")
    found_at = parse_time(raw_found, "found_at")
    if "expires_at" in find and find["expires_at"] not in (None, ""):
        reported = parse_time(find["expires_at"], "expires_at")
    else:
        reported = None
    expires_at, estimated = expiry_for(publisher, found_at, reported)
    match_key = article_match_key(canonical or unlock)
    if not match_key:
        raise ValueError("unlock_url is not a usable article URL")
    return {
        "unlock_url": unlock,
        "canonical_url": canonical,
        "match_key": match_key,
        "publisher": publisher,
        "title": title,
        "source_url": source_url,
        "source_author": author,
        "found_at": format_time(found_at),
        "expires_at": format_time(expires_at),
        "expires_estimated": estimated,
        # Not stored. A missing report must not slide the cap forward on a re-add.
        "reported_expires_at": format_time(reported) if reported is not None else None,
    }


def _claim(incoming: dict) -> str:
    """Insert or refresh one find while holding the database write lock.

    The read and the write share one ``BEGIN IMMEDIATE`` transaction, and
    ``match_key`` is unique. A second add that loses the race refreshes the
    row the first add stored.
    """
    keys = _keys(incoming["canonical_url"], incoming["unlock_url"])
    conn = db.connect()
    try:
        for attempt in (1, 2):
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = db.load_gift_candidates(conn)
                existing = _queue_match(rows, keys)
                if existing is None:
                    existing = next(
                        (
                            row
                            for row in rows
                            if row.get("match_key") == incoming["match_key"]
                        ),
                        None,
                    )
                if existing is not None:
                    _apply(conn, existing, _refresh_updates(existing, incoming))
                    conn.commit()
                    return "refreshed"
                db.insert_gift_candidate_in(
                    conn,
                    unlock_url=incoming["unlock_url"],
                    canonical_url=incoming["canonical_url"],
                    match_key=incoming["match_key"],
                    publisher=incoming["publisher"],
                    title=incoming["title"],
                    source_url=incoming["source_url"],
                    source_author=incoming["source_author"],
                    found_at=incoming["found_at"],
                    expires_at=incoming["expires_at"],
                    expires_estimated=incoming["expires_estimated"],
                )
                conn.commit()
                return "added"
            except sqlite3.IntegrityError:
                conn.rollback()
                if attempt == 2:
                    raise
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()
    raise RuntimeError("Could not store the candidate.")


def add_finds(finds: list) -> list[str]:
    """Validate, dedupe and store finds. One result line per find."""
    db.init_db()
    from .api import _best_match, _find_matches

    lines: list[str] = []
    for find in finds:
        if not isinstance(find, dict):
            lines.append("rejected: find must be an object")
            continue
        try:
            incoming = _parse_find(find)
        except ValueError as exc:
            lines.append(f"rejected: {exc}")
            continue
        if _usable_archive(
            [incoming["canonical_url"], incoming["unlock_url"]], _best_match, _find_matches
        ):
            lines.append("archived")
            continue
        lines.append(_claim(incoming))
    return lines


def derived_status(row: dict, now: datetime, effective_status) -> str:
    """Status from expiry, dismissal and the linked snapshot. Never stored."""
    if row.get("dismissed_at"):
        return "dismissed"
    if _parse_stored(row["expires_at"]) <= now:
        return "expired"
    snapshot_id = row.get("snapshot_id")
    if not snapshot_id:
        return "new"
    snap = db.get_snapshot(snapshot_id)
    if snap is None:
        return "failed"
    status = effective_status(snap)
    if status in {"queued", "capturing"}:
        return "saving"
    if status == "complete":
        return "saved"
    return "failed"


def _saved_day(row: dict) -> str:
    stamp = row.get("clicked_at") or ""
    if not stamp and row.get("snapshot_id"):
        snap = db.get_snapshot(row["snapshot_id"])
        stamp = (snap or {}).get("created_at") or ""
    return stamp[:10]


def _archived(row: dict, best_match, find_matches) -> dict | None:
    for url in (row.get("canonical_url"), row.get("unlock_url")):
        if not url:
            continue
        snap, _status, usable = best_match(find_matches(url))
        if snap is not None and usable:
            return {"id": snap["id"], "usable": True}
    return None


def get_candidates_payload(include: str, *, now: datetime | None = None) -> dict:
    if include not in {"live", "all"}:
        raise HTTPException(400, "include must be live or all.")
    from .api import _best_match, _find_matches, _site_name, effective_status

    db.init_db()
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(timezone.utc).strftime("%Y-%m-%d")
    listed = []
    for row in db.list_gift_candidates():
        status = derived_status(row, moment, effective_status)
        if include == "live" and status not in {"new", "saving", "failed"}:
            if not (status == "saved" and _saved_day(row) == today):
                continue
        display = row.get("canonical_url") or row["unlock_url"]
        site = _site_name({"site_name": "", "url": display, "final_url": None, "title": ""})
        listed.append(
            {
                "id": row["id"],
                "unlock_url": row["unlock_url"],
                "canonical_url": row.get("canonical_url"),
                "site": site,
                "publisher": row["publisher"],
                "title": row.get("title"),
                "source_url": row.get("source_url"),
                "source_author": row.get("source_author"),
                "found_at": row["found_at"],
                "expires_at": row["expires_at"],
                "expires_estimated": bool(row["expires_estimated"]),
                "clicked_at": row.get("clicked_at"),
                "dismissed_at": row.get("dismissed_at"),
                "status": status,
                "archived": _archived(row, _best_match, _find_matches),
            }
        )
    return {"candidates": listed, "updated_at": format_time(moment)}


def events_since(since: datetime) -> dict:
    """Clicks and dismissals at or after ``since``, for Gift Links' watchlist."""
    db.init_db()
    events = []
    for row in db.list_gift_candidates():
        for kind, stamp in (("click", row.get("clicked_at")), ("dismiss", row.get("dismissed_at"))):
            if not stamp:
                continue
            if _parse_stored(stamp) < since:
                continue
            events.append(
                {
                    "at": stamp,
                    "canonical_url": row.get("canonical_url"),
                    "event": kind,
                    "id": row["id"],
                    "publisher": row["publisher"],
                    "source_author": row.get("source_author"),
                    "source_url": row.get("source_url"),
                    "title": row.get("title"),
                    "unlock_url": row["unlock_url"],
                }
            )
    events.sort(key=lambda item: (item["at"], item["id"], item["event"]))
    return {"events": events}


def purge_expired(now: datetime | None = None) -> int:
    db.init_db()
    moment = now or datetime.now(timezone.utc)
    return db.purge_gift_candidates(format_time(moment - PURGE_AFTER))


def _lookup_capture(row: dict, clean: str) -> tuple[str, str | None, object]:
    from .api import _best_match, _find_matches, _is_usable, _site_name, effective_status

    linked = row.get("snapshot_id")
    if linked:
        snap = db.get_snapshot(linked)
        if snap is not None:
            status = effective_status(snap)
            if status == "complete":
                return ("exists", snap["id"], _is_usable(snap, _site_name(snap)))
            if status in {"queued", "capturing"}:
                return ("inflight", snap["id"], status)
    seen: list[str] = []
    for url in (row.get("canonical_url"), clean):
        if url and url not in seen:
            seen.append(url)
    for url in seen:
        snap, status, usable = _best_match(_find_matches(url))
        if snap is None:
            continue
        if status == "complete":
            return ("exists", snap["id"], usable)
        if status in {"queued", "capturing"}:
            return ("inflight", snap["id"], status)
    return ("new", None, None)


def _record_click(row: dict, snapshot_id: str) -> None:
    fields: dict = {}
    if row.get("snapshot_id") != snapshot_id:
        fields["snapshot_id"] = snapshot_id
    if not row.get("clicked_at"):
        fields["clicked_at"] = db.now_iso()
    if fields:
        db.update_gift_candidate(row["id"], **fields)


async def save_candidate(cid: str) -> JSONResponse:
    """Capture the stored unlock URL. The request body is not a URL source."""
    if not CANDIDATE_ID_RE.fullmatch(cid):
        raise HTTPException(404, "No such candidate")
    row = db.get_gift_candidate(cid)
    if row is None:
        raise HTTPException(404, "No such candidate")
    # Dismissal wins over expiry, same order as derived_status. A dismissed
    # gift stays dismissed after it expires, so the client does not fall
    # back to saving its canonical URL.
    if row.get("dismissed_at"):
        raise HTTPException(409, "dismissed")
    if _parse_stored(row["expires_at"]) <= datetime.now(timezone.utc):
        return JSONResponse(
            {"detail": "gift link expired", "canonical_url": row.get("canonical_url")},
            status_code=410,
        )
    try:
        clean = await asyncio.to_thread(validate_public_http_url, row["unlock_url"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    kind, snapshot_id, extra = await asyncio.to_thread(_lookup_capture, row, clean)
    from . import capture

    if kind == "exists":
        _record_click(row, snapshot_id or "")
        return JSONResponse(
            {"id": snapshot_id, "status": "exists", "usable": bool(extra)},
            status_code=200,
        )
    if kind == "inflight":
        # Set before the next await. The worker reads the flag on this loop.
        capture.note_skip_referrer_bounce(snapshot_id or "")
        _record_click(row, snapshot_id or "")
        return JSONResponse(
            {"id": snapshot_id, "status": extra, "candidate": cid},
            status_code=202,
        )

    try:
        job_id = capture.enqueue_capture(
            clean, normalize_url(clean), skip_referrer_bounce=True
        )
    except capture.QueueFull:
        raise HTTPException(
            429, "Too many saves are already queued. Try again shortly."
        ) from None
    _record_click(row, job_id)
    return JSONResponse(
        {"id": job_id, "status": capture.jobs[job_id]["status"], "candidate": cid},
        status_code=202,
    )


def dismiss_candidate(cid: str) -> JSONResponse:
    if not CANDIDATE_ID_RE.fullmatch(cid):
        raise HTTPException(404, "No such candidate")
    row = db.get_gift_candidate(cid)
    if row is None:
        raise HTTPException(404, "No such candidate")
    dismissed_at = row.get("dismissed_at") or db.now_iso()
    if not row.get("dismissed_at"):
        db.update_gift_candidate(cid, dismissed_at=dismissed_at)
    return JSONResponse({"id": cid, "status": "dismissed", "dismissed_at": dismissed_at})


def _format_list_line(item: dict) -> str:
    title = item.get("title") or item.get("canonical_url") or item["unlock_url"]
    title = " ".join(str(title).split())
    return f"{item['id']}  {item['status']}  {item['publisher']}  {item['expires_at']}  {title}"


def _cmd_add() -> int:
    raw = sys.stdin.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        print("rejected: input is too large", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        print("rejected: could not parse JSON", file=sys.stderr)
        return 1
    if not isinstance(payload, dict) or not isinstance(payload.get("finds"), list):
        print('rejected: expected {"finds": [...]}', file=sys.stderr)
        return 1
    for line in add_finds(payload["finds"]):
        print(line)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    if args.events:
        if not args.since:
            print("--since is required with --events", file=sys.stderr)
            return 2
        try:
            since = parse_time(args.since, "since")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        json.dump(events_since(since), sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    payload = get_candidates_payload("all" if args.all else "live")
    for item in payload["candidates"]:
        print(_format_list_line(item))
    return 0


def _cmd_purge() -> int:
    print(f"purged {purge_expired()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.candidates")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("add", help="read {\"finds\": [...]} on stdin and queue them")
    listing = sub.add_parser("list", help="print the live queue")
    listing.add_argument("--all", action="store_true", help="include expired and dismissed")
    listing.add_argument("--events", action="store_true", help="print click and dismiss events as JSON")
    listing.add_argument("--since", help="ISO timestamp; required with --events")
    sub.add_parser("purge", help="delete candidates that expired more than 30 days ago")
    args = parser.parse_args(argv)
    db.init_db()
    if args.command == "add":
        return _cmd_add()
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "purge":
        return _cmd_purge()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
