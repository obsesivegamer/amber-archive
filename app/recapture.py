"""Retry a bad snapshot later, and keep the same id.

The detector in ``quality`` labels a snap. This module decides what to do with
a bad one and is the only place that schedules that work.

Data shape, on the snapshot row and mirrored into ``meta.json`` when a snap is
actually queued or retried (a good snap only gets a SQLite reason, so its
files stay untouched):

- ``unlock_url`` — optional gift or unlock URL. NULL on snaps that never had one.
- ``recapture_state`` — ``pending``, ``running``, ``gave_up``,
  ``needs_browser_capture``, or ``needs_fresh_gift``. NULL means not queued.
- ``recapture_reason`` — detector reason (``ok`` when the snap graded good).
- ``recapture_attempts`` — how many /save fetches have run.
- ``recapture_next_at`` — when a pending snap is due, or the claim lease while
  ``running``.
- ``recapture_owner`` — token of the process that holds a running row. File
  swaps and status writes compare-and-set on this token. NULL otherwise.

``meta.json`` gains ``unlock_url`` when one is known, and ``recapture`` with
``state``, ``reason``, ``attempts``, ``next_at`` and ``history``. History
entries are ``{at, outcome, reason, detail, score, new_score, url_kind}``.
They do not copy the unlock URL.

Imported snaps are not sent back through /save. NYT, WSJ and WaPo need a live
``unlock_url`` (the snap field, ``meta.json``, or a gift candidate). A missing,
expired or still-walled gift stops as ``needs_fresh_gift``. SSRF, a private
redirect, or a paywall on any other retry stops as ``needs_browser_capture``.

Nothing here starts a timer. ``POST /recapture/run`` and
``python -m app.recapture`` call :func:`run`.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from PIL import Image

from . import db
from .extract import extract_article
from .freeze import freeze_html, rewrite_css
from .quality import PAYWALL_REASONS, Grade, grade_html, grade_parts, is_better
from .reader import build_reader_html
from .security import article_match_key

log = logging.getLogger("amber.recapture")

# After a failed fetch that is still worth retrying: 1h, then 6h, then 24h.
# The fourth failure gives up.
BACKOFF_SECONDS = (60 * 60, 6 * 60 * 60, 24 * 60 * 60)
MAX_ATTEMPTS = 4
CLAIM_LEASE_SECONDS = 15 * 60
# One pass leases a few due rows, refreshes each lease before that fetch,
# then comes back for the next few. A long queue is not stamped at once.
CLAIM_BATCH = 4

# Plain article URLs on these hosts come back paywalled. Retry the unlock URL
# or stop. FT is not in this list: a plain FT URL can still be tried once.
_GIFT_HOSTS = ("nytimes.com", "wsj.com", "washingtonpost.com")
_UNLOCK_QUERY = ("unlocked_article_code", "accesstoken", "pwapi_token", "gift")
# History stores a fixed detail, and this strips a gift token if one lands in
# a string anyway. The key name goes too, so the log cannot be replayed.
_GIFT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:unlocked_article_code|accesstoken|pwapi_token|gift|st)"
    r"=[^&\s\"']+",
    re.I,
)
_TERMINAL_VALUE_ERRORS = (
    "private or local",
    "local urls",
    "numeric hosts",
    "credentials",
    "only http and https",
)

_RUN_LOCK = threading.Lock()

Fetch = Callable[[str], Any]


class _Unlock:
    def __init__(self, url: str, expires_at: str | None, expired: bool, source: str):
        self.url = url
        self.expires_at = expires_at
        self.expired = expired
        self.source = source


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clock(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _host(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def requires_gift(*urls: str | None) -> bool:
    for url in urls:
        host = _host(url or "")
        if any(host == name or host.endswith("." + name) for name in _GIFT_HOSTS):
            return True
    return False


def looks_like_unlock(url: str | None) -> bool:
    raw = url or ""
    lowered = raw.lower()
    if "giftarticle" in lowered:
        return True
    host = _host(raw)
    try:
        query = parse_qs(urlparse(raw).query)
    except ValueError:
        return False
    names = {key.lower() for key in query}
    if names & set(_UNLOCK_QUERY):
        return True
    return "st" in names and (host == "wsj.com" or host.endswith(".wsj.com"))


def failure_kind(exc: BaseException) -> str:
    """Classify a /save fetch error.

    Private-host, credential, and non-http rejections stop. A DNS miss and a
    timeout stay on the backoff schedule. Other errors do too.
    """
    message = str(exc).lower()
    if "redirected to a blocked address" in message:
        return "private_redirect"
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if isinstance(exc, ValueError):
        if "could not resolve host" in message:
            return "dns"
        if any(snippet in message for snippet in _TERMINAL_VALUE_ERRORS):
            return "ssrf"
        return "transient"
    return "transient"


def _failure_detail(kind: str) -> str:
    return {
        "private_redirect": "private redirect",
        "ssrf": "ssrf",
        "dns": "dns",
        "timeout": "timeout",
    }.get(kind, "error")


def _redact(value: str) -> str:
    return _GIFT_TOKEN_RE.sub("[redacted]", value)


def _delay_for(attempts: int) -> int | None:
    index = attempts - 1
    if attempts >= MAX_ATTEMPTS or index < 0 or index >= len(BACKOFF_SECONDS):
        return None
    return BACKOFF_SECONDS[index]


def _load_meta(sid: str) -> dict:
    path = db.snap_dir(sid) / "meta.json"
    if not path.exists():
        return {}
    try:
        data = db.read_json(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(sid: str, meta: dict) -> None:
    folder = db.snap_dir(sid)
    if not folder.is_dir():
        return
    path = folder / "meta.json"
    tmp = path.with_name(path.name + f".{secrets.token_hex(8)}.recapture-new")
    try:
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_text(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def grade_snap(sid: str) -> Grade:
    folder = db.snap_dir(sid)
    meta = _load_meta(sid)
    url = meta.get("final_url") or meta.get("url") or ""
    page = _read_text(folder / "page.html")
    if page.strip():
        return grade_html(page, url)
    text = _read_text(folder / "article.txt")
    return grade_parts(article_text=text, title=str(meta.get("title") or ""), page_text=text)


def _candidate_for(snap: dict, meta: dict) -> dict | None:
    keys = set()
    for url in (
        snap.get("url"),
        snap.get("final_url"),
        meta.get("url"),
        meta.get("final_url"),
        meta.get("unlock_url"),
        snap.get("unlock_url"),
    ):
        key = article_match_key(url if isinstance(url, str) else "")
        if key:
            keys.add(key)
    linked: list[dict] = []
    keyed: list[dict] = []
    for row in db.list_gift_candidates():
        if row.get("dismissed_at"):
            continue
        if row.get("snapshot_id") == snap["id"]:
            linked.append(row)
            continue
        row_keys = {
            article_match_key(row.get("canonical_url")),
            article_match_key(row.get("unlock_url")),
        }
        row_keys.discard("")
        if keys & row_keys:
            keyed.append(row)
    pool = linked or keyed
    if not pool:
        return None
    return max(pool, key=lambda row: row.get("expires_at") or "")


def resolve_unlock(snap: dict, meta: dict, now: datetime) -> _Unlock | None:
    """Live or expired gift URL for this snap, from the row, meta, or a candidate."""
    column = (snap.get("unlock_url") or "").strip()
    meta_url = meta.get("unlock_url") if isinstance(meta.get("unlock_url"), str) else ""
    url = column or meta_url.strip()
    expires = meta.get("unlock_expires_at") if isinstance(meta.get("unlock_expires_at"), str) else None
    source = "snap" if url else ""
    candidate = _candidate_for(snap, meta)
    if candidate and candidate.get("unlock_url"):
        cand_url = str(candidate["unlock_url"])
        cand_exp = candidate.get("expires_at")
        if not url:
            url, expires, source = cand_url, cand_exp, "candidate"
        elif not expires and (
            url == cand_url or article_match_key(url) == article_match_key(cand_url)
        ):
            expires = cand_exp
    if not url:
        for maybe in (snap.get("url"), snap.get("final_url")):
            if isinstance(maybe, str) and looks_like_unlock(maybe):
                url, source = maybe, "snap_url"
                if candidate and not expires:
                    expires = candidate.get("expires_at")
                break
    if not url:
        return None
    expired = bool(expires) and expires <= _iso(now)
    return _Unlock(url, expires, expired, source or "snap")


def _complete_snaps() -> list[dict]:
    return db.complete_snapshots()


def _queue_rows() -> list[dict]:
    return db.recapture_snapshots()


def _entry(snap: dict) -> dict:
    state = snap.get("recapture_state")
    if state == "running":
        state = "pending"
    return {
        "id": snap["id"],
        "url": snap.get("url"),
        "title": snap.get("title"),
        "state": state,
        "reason": snap.get("recapture_reason"),
        "attempts": int(snap.get("recapture_attempts") or 0),
        "next_at": snap.get("recapture_next_at"),
        "has_unlock_url": bool(snap.get("unlock_url")),
    }


def status() -> dict:
    """Read the queue. Does not fetch and does not grade."""
    grouped: dict[str, list[dict]] = {
        "pending": [],
        "gave_up": [],
        "needs_browser_capture": [],
        "needs_fresh_gift": [],
    }
    for snap in _queue_rows():
        item = _entry(snap)
        bucket = item["state"]
        if bucket in grouped:
            grouped[bucket].append(item)
    grouped["busy"] = False
    grouped["attempted"] = 0
    grouped["replaced"] = []
    return grouped


def _scrub_history(entry: dict) -> dict:
    cleaned = {}
    for key, value in entry.items():
        cleaned[key] = _redact(value) if isinstance(value, str) else value
    return cleaned


def _still_owner(sid: str, token: str) -> bool:
    """Refresh this row's lease from the current time if `token` still holds it.

    The deadline is ``now()`` at this refresh, not the clock the run started with.
    A long queue must not leave a row expiring from the moment it was first claimed.
    """
    lease = _iso(_clock(None) + timedelta(seconds=CLAIM_LEASE_SECONDS))
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id FROM snapshots
            WHERE id = ? AND recapture_state = 'running' AND recapture_owner = ?
            """,
            (sid, token),
        ).fetchone()
        if row is None:
            conn.commit()
            return False
        conn.execute(
            """
            UPDATE snapshots
            SET recapture_next_at = ?
            WHERE id = ? AND recapture_state = 'running' AND recapture_owner = ?
            """,
            (lease, sid, token),
        )
        conn.commit()
    return True


def _release(sid: str, token: str, fields: dict[str, Any]) -> bool:
    """Write status only while `token` still owns the row, then drop the lease."""
    fields = dict(fields)
    fields["recapture_owner"] = None
    cols = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values())
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            f"""
            UPDATE snapshots
            SET {cols}
            WHERE id = ? AND recapture_state = 'running' AND recapture_owner = ?
            """,
            (*values, sid, token),
        )
        owned = cur.rowcount == 1
        conn.commit()
    return owned


def _remember(
    sid: str,
    snap: dict,
    meta: dict,
    *,
    state: str | None,
    reason: str | None,
    attempts: int | None,
    next_at: str | None,
    grade: Grade | None,
    history: dict | None,
    unlock: _Unlock | None,
    write_meta: bool,
    owner: str | None = None,
    now: datetime | None = None,
    content: dict | None = None,
) -> bool:
    """Persist queue status. With `owner`, skip the write if the lease was lost."""
    if owner is not None and not _still_owner(sid, owner):
        return False
    if write_meta:
        stored = dict(meta)
        stored["id"] = sid
        if grade is not None:
            stored["quality"] = grade.as_dict()
        rec = stored.get("recapture")
        if not isinstance(rec, dict):
            rec = {}
        rec["state"] = state or "ok"
        rec["reason"] = reason
        rec["attempts"] = int(attempts or 0)
        rec["next_at"] = next_at
        if history:
            past = rec.get("history")
            if not isinstance(past, list):
                past = []
            past.append(_scrub_history(history))
            rec["history"] = [
                _scrub_history(item) if isinstance(item, dict) else item for item in past[-12:]
            ]
        stored["recapture"] = rec
        if unlock is not None:
            stored["unlock_url"] = unlock.url
            if unlock.expires_at:
                stored["unlock_expires_at"] = unlock.expires_at
    else:
        stored = meta
    fields: dict[str, Any] = {
        "recapture_state": state,
        "recapture_reason": reason,
        "recapture_next_at": next_at,
    }
    if attempts is not None:
        fields["recapture_attempts"] = attempts
    if unlock is not None and unlock.url and not snap.get("unlock_url"):
        fields["unlock_url"] = unlock.url
    if content:
        fields.update(content)
    if owner is not None:
        if not _release(sid, owner, fields):
            return False
        if write_meta:
            _write_meta(sid, stored)
        return True
    if write_meta:
        _write_meta(sid, stored)
    db.update_snapshot(sid, **fields)
    return True


def _history(
    now: datetime,
    *,
    outcome: str,
    reason: str | None,
    detail: str | None = None,
    score: int | None = None,
    new_score: int | None = None,
    url_kind: str | None = None,
) -> dict:
    return _scrub_history(
        {
            "at": _iso(now),
            "outcome": outcome,
            "reason": reason,
            "detail": detail,
            "score": score,
            "new_score": new_score,
            "url_kind": url_kind,
        }
    )


def _stop_for_gift(snap: dict, unlock: _Unlock | None) -> str | None:
    """Why /save must not be tried, or None when a fetch is allowed."""
    if unlock is not None and unlock.expired:
        return "gift link expired"
    if requires_gift(snap.get("url"), snap.get("final_url")) and (
        unlock is None or unlock.expired
    ):
        return "no unlock url"
    return None


def _classify_one(snap: dict, now: datetime) -> None:
    if snap.get("recapture_state"):
        return
    if snap.get("recapture_reason") == "ok":
        return
    meta = _load_meta(snap["id"])
    grade = grade_snap(snap["id"])
    unlock = resolve_unlock(snap, meta, now)
    if grade.label == "good":
        _remember(
            snap["id"],
            snap,
            meta,
            state=None,
            reason="ok",
            attempts=int(snap.get("recapture_attempts") or 0),
            next_at=None,
            grade=None,
            history=None,
            unlock=unlock,
            write_meta=False,
        )
        return
    detail = None
    outcome = "scheduled"
    state: str | None = "pending"
    next_at: str | None = _iso(now)
    if meta.get("imported"):
        state, next_at, outcome, detail = (
            "needs_browser_capture",
            None,
            "needs_browser_capture",
            "imported",
        )
    else:
        gift_block = _stop_for_gift(snap, unlock)
        if gift_block:
            state, next_at, outcome, detail = (
                "needs_fresh_gift",
                None,
                "needs_fresh_gift",
                gift_block,
            )
    _remember(
        snap["id"],
        snap,
        meta,
        state=state,
        reason=grade.reason,
        attempts=int(snap.get("recapture_attempts") or 0),
        next_at=next_at,
        grade=grade,
        history=_history(
            now,
            outcome=outcome,
            reason=grade.reason,
            detail=detail,
            score=grade.score,
        ),
        unlock=unlock,
        write_meta=True,
    )


def _classify(now: datetime) -> None:
    for snap in _complete_snaps():
        _classify_one(snap, now)


def _claim_due(now: datetime) -> list[tuple[str, str]]:
    """Lease up to CLAIM_BATCH due rows, each with its own owner token and lease."""
    now_s = _iso(now)
    claimed: list[tuple[str, str]] = []
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        stale = conn.execute(
            """
            SELECT id FROM snapshots
            WHERE status = 'complete'
              AND recapture_state = 'running'
              AND recapture_next_at IS NOT NULL
              AND recapture_next_at <= ?
            """,
            (now_s,),
        ).fetchall()
        for row in stale:
            conn.execute(
                """
                UPDATE snapshots
                SET recapture_state = 'pending', recapture_owner = NULL
                WHERE id = ? AND recapture_state = 'running' AND recapture_next_at <= ?
                """,
                (row["id"], now_s),
            )
        due = conn.execute(
            """
            SELECT id FROM snapshots
            WHERE status = 'complete'
              AND recapture_state = 'pending'
              AND (recapture_next_at IS NULL OR recapture_next_at <= ?)
            ORDER BY created_at ASC, rowid ASC
            LIMIT ?
            """,
            (now_s, CLAIM_BATCH),
        ).fetchall()
        for row in due:
            token = secrets.token_hex(16)
            lease = _iso(now + timedelta(seconds=CLAIM_LEASE_SECONDS))
            cur = conn.execute(
                """
                UPDATE snapshots
                SET recapture_state = 'running',
                    recapture_next_at = ?,
                    recapture_owner = ?
                WHERE id = ? AND recapture_state = 'pending'
                """,
                (lease, token, row["id"]),
            )
            if cur.rowcount:
                claimed.append((row["id"], token))
        conn.commit()
    return claimed


def _coerce(url: str, fetched: Any) -> dict:
    if isinstance(fetched, str):
        return {"html": fetched, "final_url": url, "title": "", "http_status": None}
    if isinstance(fetched, dict) and isinstance(fetched.get("html"), str):
        fetched.setdefault("final_url", url)
        return fetched
    raise RuntimeError("recapture fetch returned no html")


def _prefer(new: Any, old: Any) -> Any:
    """Keep the stored value when the new extract has nothing for this field."""
    if isinstance(new, str):
        new = new.strip() or None
    if new:
        return new
    return old


def _jpeg_pair(raw: bytes) -> tuple[bytes, bytes | None]:
    """Encode a screenshot in memory. The snap file is replaced afterwards."""
    try:
        image = Image.open(io.BytesIO(raw))
        rgb = image.convert("RGB")
        shot = io.BytesIO()
        rgb.save(shot, "JPEG", quality=82)
        rgb.thumbnail((480, 360))
        thumb = io.BytesIO()
        rgb.save(thumb, "JPEG", quality=70)
        return shot.getvalue(), thumb.getvalue()
    except Exception:
        return raw, None


def _safe_resource_name(filename: str) -> bool:
    return (
        bool(filename)
        and filename not in {".", ".."}
        and "/" not in filename
        and "\\" not in filename
    )


def _resource_body(
    filename: str,
    packed: Any,
    resource_map: dict,
    final_url: str,
    sid: str,
) -> bytes | None:
    if isinstance(packed, tuple):
        body = packed[0]
    else:
        body = packed
    if not isinstance(body, (bytes, bytearray)):
        return None
    data = bytes(body)
    if filename.endswith(".css"):
        css_url = next((url for url, name in resource_map.items() if name == filename), final_url)
        try:
            text = data.decode("utf-8", errors="replace")
            text = rewrite_css(text, css_url, resource_map, sid)
            return text.encode("utf-8")
        except Exception:
            return data
    return data


def _stage_capture(sid: str, snap: dict, meta: dict, fetched: dict, token: str) -> tuple[Any, Grade, dict, dict]:
    """Write the replacement set under DATA_DIR and grade that set.

    Nothing in the snap directory is touched here. The stage lives on the
    same filesystem as the snaps so a later replace does not copy across devices.
    """
    final_url = fetched.get("final_url") or snap.get("final_url") or snap.get("url") or ""
    resource_map = fetched.get("resource_map") if isinstance(fetched.get("resource_map"), dict) else {}
    bodies = fetched.get("resource_bodies") if isinstance(fetched.get("resource_bodies"), dict) else {}
    frozen = freeze_html(fetched["html"], final_url, resource_map, sid)
    article = extract_article(frozen, final_url)
    reader = build_reader_html(article, snap.get("url") or final_url)
    stage = db.DATA_DIR / "recapture-staging" / token
    try:
        if stage.exists():
            shutil.rmtree(stage)
        (stage / "res").mkdir(parents=True)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise
    (stage / "page.html").write_text(frozen, encoding="utf-8")
    (stage / "article.html").write_text(article.get("article_html") or "", encoding="utf-8")
    (stage / "article.txt").write_text(article.get("article_text") or "", encoding="utf-8")
    (stage / "reader.html").write_text(reader, encoding="utf-8")
    for filename, packed in bodies.items():
        if not isinstance(filename, str) or not _safe_resource_name(filename):
            continue
        data = _resource_body(filename, packed, resource_map, final_url, sid)
        if data is None:
            continue
        (stage / "res" / filename).write_bytes(data)
    shot = fetched.get("screenshot")
    if isinstance(shot, (bytes, bytearray)) and shot:
        image, thumb = _jpeg_pair(bytes(shot))
        (stage / "screenshot.jpg").write_bytes(image)
        if thumb is not None:
            (stage / "thumb.jpg").write_bytes(thumb)
    grade = grade_html(frozen, final_url)
    text_grade = grade_parts(
        article_text=article.get("article_text") or "",
        title=str(article.get("title") or ""),
        page_text=article.get("article_text") or "",
    )
    if grade.label == "good" and text_grade.label != "good":
        grade = text_grade
    stored = dict(meta)
    stored.update(
        {
            "id": sid,
            "url": snap.get("url") or stored.get("url"),
            "final_url": final_url,
            "title": _prefer(article.get("title"), stored.get("title")),
            "site_name": _prefer(article.get("site_name"), stored.get("site_name")),
            "author": _prefer(article.get("author"), stored.get("author")),
            "published_at": _prefer(article.get("published_at"), stored.get("published_at")),
            "description": _prefer(article.get("description"), stored.get("description")),
            "dek": _prefer(article.get("dek"), stored.get("dek")),
            "word_count": article.get("word_count"),
            "paywalled": article.get("paywalled"),
            "quality": grade.as_dict(),
        }
    )
    if fetched.get("http_status") is not None:
        stored["http_status"] = fetched.get("http_status")
    content: dict[str, Any] = {
        "final_url": final_url,
        "title": stored.get("title"),
        "site_name": stored.get("site_name"),
        "author": stored.get("author"),
        "published_at": stored.get("published_at"),
        "description": stored.get("description"),
        "word_count": stored.get("word_count"),
        "paywalled": 1 if stored.get("paywalled") else 0,
        "status": "complete",
        "error": None,
    }
    if stored.get("http_status") is not None:
        content["http_status"] = stored["http_status"]
    return stage, grade, stored, content


def _publish_staged(stage, folder) -> None:
    """Move a finished stage into the snap. page.html lands last.

    A crash before that replace leaves the previous page.html, which is what
    a later grade trusts. article.txt from this attempt is already beside it
    only after both replaces have run; callers record ok only then.
    """
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "res").mkdir(exist_ok=True)
    files = [path for path in stage.rglob("*") if path.is_file()]

    def order(path) -> tuple[int, str]:
        rel = str(path.relative_to(stage))
        if rel == "page.html":
            return (2, rel)
        if rel == "article.txt":
            return (1, rel)
        return (0, rel)

    for path in sorted(files, key=order):
        dest = folder / path.relative_to(stage)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, dest)


def _schedule(
    sid: str,
    snap: dict,
    meta: dict,
    *,
    now: datetime,
    attempts: int,
    grade: Grade,
    history: dict,
    unlock: _Unlock | None,
    owner: str | None = None,
    content: dict | None = None,
) -> str:
    delay = _delay_for(attempts)
    if delay is None:
        if not _remember(
            sid,
            snap,
            meta,
            state="gave_up",
            reason=grade.reason,
            attempts=attempts,
            next_at=None,
            grade=grade,
            history=history,
            unlock=unlock,
            write_meta=True,
            owner=owner,
            now=now,
            content=content,
        ):
            return "lost"
        return "gave_up"
    if not _remember(
        sid,
        snap,
        meta,
        state="pending",
        reason=grade.reason,
        attempts=attempts,
        next_at=_iso(now + timedelta(seconds=delay)),
        grade=grade,
        history=history,
        unlock=unlock,
        write_meta=True,
        owner=owner,
        now=now,
        content=content,
    ):
        return "lost"
    return "pending"


def _attempt(sid: str, now: datetime, fetch: Fetch, owner: str) -> str:
    """Retry one claimed snap. Returns ``replaced`` or another outcome name."""
    snap = db.get_snapshot(sid)
    if snap is None:
        return "missing"
    meta = _load_meta(sid)
    grade = grade_snap(sid)
    unlock = resolve_unlock(snap, meta, now)
    if grade.label == "good":
        _remember(
            sid,
            snap,
            meta,
            state=None,
            reason="ok",
            attempts=int(snap.get("recapture_attempts") or 0),
            next_at=None,
            grade=None,
            history=None,
            unlock=None,
            write_meta=False,
            owner=owner,
            now=now,
        )
        return "kept"
    if meta.get("imported"):
        _remember(
            sid,
            snap,
            meta,
            state="needs_browser_capture",
            reason=grade.reason,
            attempts=int(snap.get("recapture_attempts") or 0),
            next_at=None,
            grade=grade,
            history=_history(
                now,
                outcome="needs_browser_capture",
                reason=grade.reason,
                detail="imported",
                score=grade.score,
            ),
            unlock=unlock,
            write_meta=True,
            owner=owner,
            now=now,
        )
        return "needs_browser_capture"
    gift_block = _stop_for_gift(snap, unlock)
    if gift_block:
        _remember(
            sid,
            snap,
            meta,
            state="needs_fresh_gift",
            reason=grade.reason,
            attempts=int(snap.get("recapture_attempts") or 0),
            next_at=None,
            grade=grade,
            history=_history(
                now,
                outcome="needs_fresh_gift",
                reason=grade.reason,
                detail=gift_block,
                score=grade.score,
            ),
            unlock=unlock,
            write_meta=True,
            owner=owner,
            now=now,
        )
        return "needs_fresh_gift"

    url, url_kind = (snap.get("url") or ""), "article"
    if unlock is not None and not unlock.expired:
        url, url_kind = unlock.url, "unlock"
    if not _still_owner(sid, owner):
        return "lost"
    attempts = int(snap.get("recapture_attempts") or 0) + 1
    try:
        fetched = _coerce(url, fetch(url))
    except Exception as exc:
        kind = failure_kind(exc)
        detail = _failure_detail(kind)
        if kind in {"ssrf", "private_redirect"}:
            _remember(
                sid,
                snap,
                meta,
                state="needs_browser_capture",
                reason=grade.reason,
                attempts=attempts,
                next_at=None,
                grade=grade,
                history=_history(
                    now,
                    outcome="needs_browser_capture",
                    reason=grade.reason,
                    detail=detail,
                    score=grade.score,
                    url_kind=url_kind,
                ),
                unlock=unlock,
                write_meta=True,
                owner=owner,
                now=now,
            )
            return "needs_browser_capture"
        return _schedule(
            sid,
            snap,
            meta,
            now=now,
            attempts=attempts,
            grade=grade,
            history=_history(
                now,
                outcome="error",
                reason=grade.reason,
                detail=detail,
                score=grade.score,
                url_kind=url_kind,
            ),
            unlock=unlock,
            owner=owner,
        )

    stage = None
    try:
        stage, new_grade, staged_meta, content = _stage_capture(sid, snap, meta, fetched, owner)
        if new_grade.reason in PAYWALL_REASONS:
            state = "needs_fresh_gift" if url_kind == "unlock" or requires_gift(
                snap.get("url"), snap.get("final_url")
            ) else "needs_browser_capture"
            detail = "gift link burned" if state == "needs_fresh_gift" else "paywall"
            _remember(
                sid,
                snap,
                meta,
                state=state,
                reason=new_grade.reason,
                attempts=attempts,
                next_at=None,
                grade=grade,
                history=_history(
                    now,
                    outcome=state,
                    reason=new_grade.reason,
                    detail=detail,
                    score=grade.score,
                    new_score=new_grade.score,
                    url_kind=url_kind,
                ),
                unlock=unlock,
                write_meta=True,
                owner=owner,
                now=now,
            )
            return state

        # Grade the files on disk again. Another worker may already have
        # published a better capture while this fetch was in flight.
        disk = grade_snap(sid)
        if not is_better(new_grade, disk):
            return _schedule(
                sid,
                snap,
                meta,
                now=now,
                attempts=attempts,
                grade=disk,
                history=_history(
                    now,
                    outcome="kept",
                    reason=new_grade.reason,
                    detail="not better",
                    score=disk.score,
                    new_score=new_grade.score,
                    url_kind=url_kind,
                ),
                unlock=unlock,
                owner=owner,
            )
        # Compare-and-set immediately before any snap write.
        if not _still_owner(sid, owner):
            return "lost"
        disk = grade_snap(sid)
        if not is_better(new_grade, disk):
            return _schedule(
                sid,
                snap,
                meta,
                now=now,
                attempts=attempts,
                grade=disk,
                history=_history(
                    now,
                    outcome="kept",
                    reason=new_grade.reason,
                    detail="not better",
                    score=disk.score,
                    new_score=new_grade.score,
                    url_kind=url_kind,
                ),
                unlock=unlock,
                owner=owner,
            )
        page_bytes = (stage / "page.html").read_bytes()
        text_bytes = (stage / "article.txt").read_bytes()
        _publish_staged(stage, db.snap_dir(sid))
        folder = db.snap_dir(sid)
        same_attempt = (
            (folder / "page.html").read_bytes() == page_bytes
            and (folder / "article.txt").read_bytes() == text_bytes
        )
        if not same_attempt:
            return _schedule(
                sid,
                snap,
                meta,
                now=now,
                attempts=attempts,
                grade=grade,
                history=_history(
                    now,
                    outcome="error",
                    reason=new_grade.reason,
                    detail="error",
                    score=grade.score,
                    new_score=new_grade.score,
                    url_kind=url_kind,
                ),
                unlock=unlock,
                owner=owner,
            )
        if new_grade.label == "good":
            wrote = _remember(
                sid,
                snap,
                staged_meta,
                state=None,
                reason="ok",
                attempts=attempts,
                next_at=None,
                grade=new_grade,
                history=_history(
                    now,
                    outcome="replaced",
                    reason=new_grade.reason,
                    score=grade.score,
                    new_score=new_grade.score,
                    url_kind=url_kind,
                ),
                unlock=unlock,
                write_meta=True,
                owner=owner,
                now=now,
                content=content,
            )
            return "replaced" if wrote else "lost"
        outcome = _schedule(
            sid,
            snap,
            staged_meta,
            now=now,
            attempts=attempts,
            grade=new_grade,
            history=_history(
                now,
                outcome="replaced",
                reason=new_grade.reason,
                score=grade.score,
                new_score=new_grade.score,
                url_kind=url_kind,
            ),
            unlock=unlock,
            owner=owner,
            content=content,
        )
        return "lost" if outcome == "lost" else "replaced"
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def fetch_via_save(url: str) -> dict:
    """One /save capture that does not allocate a snapshot id."""
    from . import capture

    skip = looks_like_unlock(url)
    return asyncio.run(capture.fetch_document(url, skip_referrer_bounce=skip))


def run(*, now: datetime | None = None, fetch: Fetch | None = None) -> dict:
    """Grade new snaps and retry the ones that are due.

    A second call in the same process, or a second process whose rows are
    already claimed, does not fetch those rows again. Pass ``fetch`` in tests.
    The default fetch is :func:`fetch_via_save`.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        report = status()
        report["busy"] = True
        return report
    try:
        db.init_db()
        moment = _clock(now)
        _classify(moment)
        fetcher = fetch or fetch_via_save
        replaced: list[str] = []
        attempted = 0
        seen: set[str] = set()

        def counting(url: str) -> Any:
            nonlocal attempted
            attempted += 1
            return fetcher(url)

        while True:
            claimed = [(sid, token) for sid, token in _claim_due(moment) if sid not in seen]
            if not claimed:
                break
            for sid, token in claimed:
                seen.add(sid)
                try:
                    outcome = _attempt(sid, moment, counting, token)
                except Exception:
                    log.exception("recapture failed for %s", sid)
                    snap = db.get_snapshot(sid) or {"id": sid, "unlock_url": None}
                    _remember(
                        sid,
                        snap,
                        _load_meta(sid),
                        state="pending",
                        reason=(snap.get("recapture_reason") if snap else None),
                        attempts=None,
                        next_at=_iso(moment + timedelta(seconds=BACKOFF_SECONDS[0])),
                        grade=None,
                        history=_history(
                            moment,
                            outcome="error",
                            reason=None,
                            detail="internal error",
                        ),
                        unlock=None,
                        write_meta=True,
                        owner=token,
                        now=moment,
                    )
                    outcome = "error"
                if outcome == "replaced":
                    replaced.append(sid)
        report = status()
        report["attempted"] = attempted
        report["replaced"] = replaced
        return report
    finally:
        _RUN_LOCK.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.recapture")
    parser.add_argument("command", nargs="?", default="run", choices=("run",))
    parser.parse_args(argv)
    json.dump(run(), sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
