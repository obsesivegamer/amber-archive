"""Import worker for Dell-PLX.

A separate process from ``python -m app.library_ingest``. It accepts the same
``POST /api/import`` body the Mac does, freezes the HTML in a private scratch
directory, and commits by ``POST``ing to ingest on ``127.0.0.1:8082``.

``library_ingest`` stays the only writer of the shared data directory. This
process opens that database with ``mode=ro`` and never calls
``commit_document``. The scratch tree and the budget ledger live outside
the shared directory.

Dedup must pass the read-only library connection into ``_find_matches``.
The process data directory is the scratch tree, so the default connection
would not see a snapshot the Mac already pushed, and ``409 already archived``
would never fire.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import urllib.error
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config, db
from .api import (
    MAX_IMPORT_BODY_BYTES,
    MAX_IMPORT_BYTES_PER_DAY,
    MAX_IMPORTS_IN_FLIGHT,
    _best_match,
    _find_matches,
    _is_usable,
    _read_body_capped,
    _site_name,
    parse_import_fields,
)
from .config import DELL_ID_PREFIX
from .db import new_snapshot_id
from .import_child import TooComplex, blocked_for, ingest_in_child
from .library_contract import ID_RE, read_token_file, tokens_equal
from .library_push import PushError, PushRejected, push_snapshot
from .security import article_match_key

log = logging.getLogger("amber.library_import")

_ALLOC_TRIES = 8
_THUMBNAIL_TIMEOUT_S = 20.0
_reserved: set[str] = set()
_reserve_lock = threading.Lock()
_budget_lock = threading.Lock()


class ImportHooks:
    """Test seams for the three crash windows. Production leaves these unset."""

    def __init__(
        self,
        before_push: Callable[[], None] | None = None,
        after_commit: Callable[[], None] | None = None,
    ) -> None:
        self.before_push = before_push
        self.after_commit = after_commit


class MatchLocks:
    """One lock per article key, from the dedup read through the ingest commit."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, keys: list[str]):
        acquired: list[asyncio.Lock] = []
        try:
            for key in keys:
                async with self._guard:
                    lock = self._locks.get(key)
                    if lock is None:
                        lock = asyncio.Lock()
                        self._locks[key] = lock
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()


def open_library_ro(db_path: Path) -> sqlite3.Connection:
    """Open the shared library read-only. Same helper as every other library read."""
    return db.open_library_ro(db_path)


def _library_has_id(conn: sqlite3.Connection, sid: str) -> bool:
    """True when the live store already has this id.

    A missing ``snapshots`` table, a locked database, or a file that is not a
    database raises. Treating that as "id is free" would let a new Z id
    overwrite a legacy row. A missing ``library_revisions`` table only means
    ingest has not created it yet; the snapshots query is the legacy-id check.
    """
    if conn.execute("SELECT 1 FROM snapshots WHERE id = ?", (sid,)).fetchone():
        return True
    try:
        row = conn.execute(
            "SELECT 1 FROM library_revisions WHERE id = ?",
            (sid,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise
    return row is not None


def _scratch_has(sid: str) -> bool:
    if (db.SNAPS_DIR / sid).exists():
        return True
    try:
        return db.get_snapshot(sid) is not None
    except sqlite3.Error:
        return False


def release_dell_id(sid: str) -> None:
    with _reserve_lock:
        _reserved.discard(sid)


def allocate_dell_id(
    library_dir: Path,
    *,
    new_id: Callable[[], str] | None = None,
    scratch_has: Callable[[str], bool] | None = None,
) -> str:
    """A Z-prefixed id that is not already in the live library.

    Legacy Mac ids may already start with Z, so the character is not proof
    the id is free. ``scratch_has`` defaults to this process's scratch tree.
    """
    mint = new_id or (lambda: new_snapshot_id("dell"))
    taken_in_scratch = scratch_has or _scratch_has
    db_path = library_dir / "amber.sqlite3"
    if not db_path.is_file():
        raise RuntimeError("library database is missing")
    conn = open_library_ro(db_path)
    try:
        with _reserve_lock:
            for _ in range(_ALLOC_TRIES * 4):
                sid = mint()
                if (
                    not isinstance(sid, str)
                    or not ID_RE.fullmatch(sid)
                    or not sid.startswith(DELL_ID_PREFIX)
                ):
                    continue
                if sid in _reserved:
                    continue
                if conn is not None and _library_has_id(conn, sid):
                    continue
                snap_dir = library_dir / "snaps" / sid
                if snap_dir.exists() or snap_dir.is_symlink():
                    continue
                if taken_in_scratch(sid):
                    continue
                _reserved.add(sid)
                return sid
    finally:
        if conn is not None:
            conn.close()
    raise RuntimeError("Could not allocate a snapshot id.")


def _matches(address: str, conn: sqlite3.Connection) -> list[dict]:
    """Candidates for ``address`` on the connection the caller opened.

    ``_find_matches`` is given ``conn`` on purpose. Omitting it searches the
    scratch database, which does not hold snapshots the Mac already pushed.
    ``library_revisions.match_key`` covers a row the LIKE candidate query
    missed; the snapshots table on Dell-PLX has the URL columns either way.
    """
    found = list(_find_matches(address, conn=conn))
    key = article_match_key(address)
    if not key:
        return found
    try:
        rows = conn.execute(
            """
            SELECT s.* FROM snapshots AS s
            JOIN library_revisions AS r ON r.id = s.id
            WHERE r.match_key = ?
            ORDER BY s.created_at DESC, s.rowid DESC
            """,
            (key,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return found
    have = {snap["id"] for snap in found}
    for row in rows:
        snap = db._as_snap(row)
        if snap["id"] not in have:
            found.append(snap)
            have.add(snap["id"])
    return found


def archived_copy(library_dir: Path, addresses: list[str]) -> dict | None:
    """A complete usable snapshot of one of ``addresses`` in the shared library.

    A short or teaser row is not "already archived". Gift Links posts the full
    article for URLs the Mac only captured as a teaser, and a 409 would make
    it drop that HTML. A missing database raises, so dedup does not turn off.
    """
    db_path = library_dir / "amber.sqlite3"
    if not db_path.is_file():
        raise RuntimeError("library database is missing")
    conn = open_library_ro(db_path)
    try:
        snaps = library_dir / "snaps"
        ordered: list[dict] = []
        seen: set[str] = set()
        for address in addresses:
            for snap in _matches(address, conn):
                if snap["id"] in seen:
                    continue
                seen.add(snap["id"])
                ordered.append(snap)
        if not ordered:
            return None
        found, status, usable = _best_match(ordered, snaps_dir=snaps)
        if found is not None and status == "complete" and usable:
            return found
        return None
    finally:
        conn.close()


_RECENT_S = 24 * 3600
_recent_lock = threading.Lock()


def _recent_path(state_dir: Path) -> Path:
    return state_dir / "recent.json"


def _read_recent(path: Path) -> list[tuple[str, str, int, int]]:
    """Commits this worker made, newest last. Raises OSError when unreadable."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OSError("recent import ledger cannot be read") from exc
    if not isinstance(raw, list):
        raise OSError("recent import ledger cannot be read")
    cutoff = int(time.time()) - _RECENT_S
    rows: list[tuple[str, str, int, int]] = []
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 4
            or not isinstance(item[0], str)
            or not item[0]
            or not isinstance(item[1], str)
            or not ID_RE.fullmatch(item[1])
            or isinstance(item[2], bool)
            or not isinstance(item[2], int)
            or isinstance(item[3], bool)
            or not isinstance(item[3], int)
            or item[3] < 0
        ):
            raise OSError("recent import ledger cannot be read")
        if item[2] >= cutoff:
            rows.append((item[0], item[1], item[2], item[3]))
    return rows


def _write_recent(path: Path, rows: list[tuple[str, str, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps([list(row) for row in rows], separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def recent_replay(state_dir: Path, keys: list[str], nbytes: int) -> str | None:
    """Id of a recent commit by this worker of the same URL and body size.

    A different body, or a snapshot this worker did not commit, is not a replay.
    """
    wanted = {key for key in keys if key}
    if not wanted:
        return None
    path = _recent_path(state_dir)
    with _recent_lock:
        try:
            rows = _read_recent(path)
        except OSError as exc:
            raise HTTPException(503, "Recent import ledger cannot be read.") from exc
    for key, sid, _stamp, size in reversed(rows):
        if key in wanted and size == nbytes:
            return sid
    return None


def remember_commit(state_dir: Path, keys: list[str], sid: str, nbytes: int) -> None:
    """Record a commit so a retry of this same body returns 409."""
    if not ID_RE.fullmatch(sid):
        return
    fresh = [key for key in keys if isinstance(key, str) and key]
    if not fresh:
        return
    path = _recent_path(state_dir)
    stamp = int(time.time())
    with _recent_lock:
        rows = _read_recent(path)
        for key in fresh:
            rows.append((key, sid, stamp, nbytes))
        _write_recent(path, rows)


def _ledger_path(state_dir: Path) -> Path:
    return state_dir / "budget.json"


def _read_ledger(path: Path) -> list[tuple[float, int]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OSError("budget ledger cannot be read") from exc
    if not isinstance(raw, list):
        raise OSError("budget ledger cannot be read")
    cutoff = time.time() - 24 * 3600
    rows: list[tuple[float, int]] = []
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or isinstance(item[0], bool)
            or not isinstance(item[0], (int, float))
            or isinstance(item[1], bool)
            or not isinstance(item[1], int)
            or item[1] < 0
        ):
            raise OSError("budget ledger cannot be read")
        if item[0] >= cutoff:
            rows.append((float(item[0]), int(item[1])))
    return rows


def _write_ledger(path: Path, rows: list[tuple[float, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps([[stamp, size] for stamp, size in rows], separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def charge_budget(state_dir: Path, nbytes: int) -> int:
    """Same 300 MiB / 24 h cap as the Mac, stored outside the shared library.

    Returns the ledger stamp. A later 503 refunds that stamp. A 422 keeps it:
    the bytes were still parsed.
    """
    path = _ledger_path(state_dir)
    stamp = int(time.time())
    with _budget_lock:
        try:
            rows = _read_ledger(path)
        except OSError as exc:
            raise HTTPException(503, "Import budget ledger cannot be read.") from exc
        used = sum(size for _, size in rows)
        if used + nbytes > MAX_IMPORT_BYTES_PER_DAY:
            raise HTTPException(
                429,
                f"Daily import budget used up ({MAX_IMPORT_BYTES_PER_DAY // (1024 * 1024)} MB "
                "of HTML per 24 hours). Try again later.",
            )
        rows.append((stamp, nbytes))
        try:
            _write_ledger(path, rows)
        except OSError as exc:
            raise HTTPException(503, "Import budget ledger cannot be read.") from exc
    return stamp


def refund_budget(state_dir: Path, stamp: int, nbytes: int) -> None:
    """Remove one charge. Used when the client will retry the same bytes."""
    path = _ledger_path(state_dir)
    with _budget_lock:
        rows = _read_ledger(path)
        for index in range(len(rows) - 1, -1, -1):
            if rows[index] == (stamp, nbytes):
                del rows[index]
                break
        _write_ledger(path, rows)


def _separate(left: Path, right: Path, left_name: str, right_name: str) -> None:
    a = left.resolve()
    b = right.resolve()
    if a == b or a in b.parents or b in a.parents:
        raise SystemExit(
            f"amber import: {left_name} and {right_name} must be separate directories"
        )


def wipe_scratch_tree(root: Path) -> None:
    """Drop unpublished scratch snapshots. ``root`` is not the library."""
    snaps = root / "snaps"
    if snaps.exists():
        for child in list(snaps.iterdir()):
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child)
    db_path = root / "amber.sqlite3"
    if not db_path.is_file():
        return
    conn = sqlite3.connect(db_path)
    try:
        try:
            conn.execute("DELETE FROM snapshots")
            conn.commit()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
    finally:
        conn.close()


def _wipe_scratch() -> None:
    """Drop unpublished scratch snapshots left by a crash. The library is not here."""
    wipe_scratch_tree(db.DATA_DIR)


def bind_scratch(scratch: Path) -> None:
    """Point this process's archive database at the scratch tree."""
    db.use_scratch_database()
    scratch.mkdir(parents=True, exist_ok=True)
    snaps = scratch / "snaps"
    db_path = scratch / "amber.sqlite3"
    config.DATA_DIR = scratch
    config.SNAPS_DIR = snaps
    config.DB_PATH = db_path
    db.DATA_DIR = scratch
    db.SNAPS_DIR = snaps
    db.DB_PATH = db_path
    db.init_db()
    _wipe_scratch()


def _delete_scratch(sid: str, scratch: Path) -> None:
    if db.DATA_DIR.resolve() != scratch.resolve():
        raise RuntimeError("refusing to delete outside the scratch directory")
    if not ID_RE.fullmatch(sid):
        raise RuntimeError("refusing to delete an id outside the archive alphabet")
    db.delete_snapshot(sid)


def scratch_contains(scratch: Path) -> Callable[[str], bool]:
    root = scratch.resolve()

    def taken(sid: str) -> bool:
        if (root / "snaps" / sid).exists():
            return True
        db_path = root / "amber.sqlite3"
        if not db_path.is_file():
            return False
        conn = sqlite3.connect(db_path)
        try:
            try:
                row = conn.execute(
                    "SELECT 1 FROM snapshots WHERE id = ?",
                    (sid,),
                ).fetchone()
            except sqlite3.Error:
                return False
            return row is not None
        finally:
            conn.close()

    return taken


_AMBER_URLS = (
    'href="/{old}/',
    "href='/{old}/",
    'src="/{old}/',
    "src='/{old}/",
    "url(/{old}/",
    "url('/{old}/",
    'url("/{old}/',
    'srcset="/{old}/',
    "srcset='/{old}/",
)


def _rewrite_snapshot_id(folder: Path, old: str, new: str) -> None:
    """Rewrite Amber's own asset URLs. Leave the captured page's text alone."""
    for path in folder.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name
        if name not in {"page.html", "reader.html", "article.html", "meta.json"} and not (
            path.parent.name == "res" and path.suffix.lower() in {".css", ".js"}
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        updated = text.replace(f"/{old}/r/", f"/{new}/r/")
        for pattern in _AMBER_URLS:
            updated = updated.replace(pattern.format(old=old), pattern.format(old=new))
        updated = updated.replace(
            f'name="amber-snapshot" content="{old}"',
            f'name="amber-snapshot" content="{new}"',
        )
        updated = updated.replace(
            f"name='amber-snapshot' content='{old}'",
            f"name='amber-snapshot' content='{new}'",
        )
        updated = updated.replace(
            f'content="{old}" name="amber-snapshot"',
            f'content="{new}" name="amber-snapshot"',
        )
        if path.name == "meta.json":
            try:
                meta = json.loads(updated)
            except json.JSONDecodeError:
                meta = None
            if isinstance(meta, dict) and meta.get("id") == old:
                meta["id"] = new
                updated = json.dumps(meta, indent=2, ensure_ascii=False)
        if updated != text:
            path.write_text(updated, encoding="utf-8", errors="surrogateescape")


def _retarget_scratch(scratch: Path, old: str, new: str) -> None:
    if not ID_RE.fullmatch(old) or not ID_RE.fullmatch(new):
        raise RuntimeError("refusing to retarget an id outside the archive alphabet")
    src = scratch / "snaps" / old
    dest = scratch / "snaps" / new
    if src.is_symlink() or not src.is_dir():
        raise RuntimeError("scratch snapshot is missing")
    if dest.exists() or dest.is_symlink():
        raise RuntimeError("scratch snapshot already exists")
    src.rename(dest)
    _rewrite_snapshot_id(dest, old, new)
    conn = sqlite3.connect(scratch / "amber.sqlite3")
    try:
        conn.execute("UPDATE snapshots SET id = ? WHERE id = ?", (new, old))
        conn.commit()
    finally:
        conn.close()


class UnconfirmedPush(Exception):
    """The push response was lost and the library could not be checked."""

    def __init__(self, sid: str):
        super().__init__(sid)
        self.sid = sid


def _published(library_dir: Path, sid: str) -> bool | None:
    """Whether ingest stored ``sid``. None when the library cannot be read."""
    db_path = library_dir / "amber.sqlite3"
    try:
        conn = open_library_ro(db_path)
    except db.LibraryUnavailable:
        return None
    try:
        try:
            row = conn.execute("SELECT 1 FROM snapshots WHERE id = ?", (sid,)).fetchone()
        except sqlite3.Error:
            return None
        return row is not None
    finally:
        conn.close()


def _push_confirmed(
    scratch: Path,
    sid: str,
    ingest_url: str,
    token: str,
    library_dir: Path,
    *,
    transport,
) -> None:
    """Push once, and again if the transport drops the response.

    The second push sends the same id and bytes. Scratch is only discarded
    by the caller after this returns. If both responses are lost, scratch
    stays unless a read shows the id is absent.
    """
    try:
        push_snapshot(scratch, sid, ingest_url, token, transport=transport)
        return
    except (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected) as first:
        try:
            push_snapshot(scratch, sid, ingest_url, token, transport=transport)
            return
        except (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected):
            present = _published(library_dir, sid)
            if present is True:
                return
            if present is False:
                raise first
            raise UnconfirmedPush(sid) from first


def commit_captured_snapshot(
    library_dir: Path,
    scratch: Path,
    sid: str,
    ingest_url: str,
    token: str,
    *,
    transport=None,
    on_id: Callable[[str], None] | None = None,
) -> str:
    """Push one captured snapshot. A 409 id collision allocates again and retries.

    Returns the id ingest stored. The caller deletes the scratch snapshot.
    """
    current = sid
    reserved = sid
    try:
        for _attempt in range(_ALLOC_TRIES):
            if on_id is not None:
                on_id(current)
            db.fill_scratch_paywalled(scratch, current)
            try:
                _push_confirmed(
                    scratch,
                    current,
                    ingest_url,
                    token,
                    library_dir,
                    transport=transport,
                )
                return current
            except PushRejected as exc:
                if not _id_collision(exc) or _attempt + 1 == _ALLOC_TRIES:
                    raise
                release_dell_id(current)
                reserved = ""
                scratch_taken = scratch_contains(scratch)

                def taken(candidate: str, _scratch_taken=scratch_taken) -> bool:
                    return _scratch_taken(candidate) or db.job_id_taken(candidate)

                fresh = allocate_dell_id(library_dir, scratch_has=taken)
                reserved = fresh
                if on_id is not None:
                    on_id(fresh)
                try:
                    _retarget_scratch(scratch, current, fresh)
                except Exception:
                    shutil.rmtree(scratch / "snaps" / fresh, ignore_errors=True)
                    raise
                current = fresh
    finally:
        if reserved:
            release_dell_id(reserved)


def _id_collision(exc: PushRejected) -> bool:
    if exc.status != 409:
        return False
    return (
        "revision is not stored + 1" in exc.detail
        or "stored revision does not match" in exc.detail
    )


def _public_body(sid: str, snap: dict, usable: bool) -> dict:
    snap_title = snap.get("title")
    title = (
        None
        if not snap_title or snap_title in {snap["url"], "[no-title]"}
        else snap_title
    )
    return {
        "id": sid,
        "status": "complete",
        "usable": usable,
        "word_count": int(snap.get("word_count") or 0),
        "title": title,
    }


async def _maybe_thumbnail(sid: str, enabled: bool) -> None:
    if not enabled:
        return
    try:
        from .ingest import screenshot_reader

        await asyncio.wait_for(screenshot_reader(sid), _THUMBNAIL_TIMEOUT_S)
    except Exception as exc:
        log.warning("thumbnail skipped for %s (%s)", sid, type(exc).__name__)


async def _freeze_and_commit(
    *,
    library_dir: Path,
    scratch: Path,
    ingest_url: str,
    ingest_token: str,
    transport,
    hooks: ImportHooks,
    thumbnails: bool,
    html: str,
    clean: str,
    title: str,
    final: str,
) -> dict:
    cleanup: list[str] = []
    try:
        for _attempt in range(_ALLOC_TRIES):
            try:
                sid = await asyncio.to_thread(allocate_dell_id, library_dir)
            except (RuntimeError, sqlite3.Error):
                raise HTTPException(503, "Could not allocate a snapshot id.") from None
            cleanup.append(sid)

            def _allocate(chosen: str = sid) -> str:
                return chosen

            try:
                frozen = await ingest_in_child(
                    html, clean, title, final, allocate=_allocate
                )
            except TooComplex:
                raise
            except Exception as exc:
                log.error("import freeze failed (%s)", type(exc).__name__)
                raise HTTPException(
                    422, "Could not turn that HTML into a snapshot."
                ) from None
            if frozen != sid:
                raise HTTPException(422, "Could not turn that HTML into a snapshot.")
            try:
                if hooks.before_push is not None:
                    hooks.before_push()
                await _maybe_thumbnail(sid, thumbnails)
                await asyncio.to_thread(
                    push_snapshot,
                    scratch,
                    sid,
                    ingest_url,
                    ingest_token,
                    transport=transport,
                )
                if hooks.after_commit is not None:
                    hooks.after_commit()
            except PushRejected as exc:
                if _id_collision(exc):
                    await asyncio.to_thread(_delete_scratch, sid, scratch)
                    release_dell_id(sid)
                    cleanup.remove(sid)
                    continue
                if exc.status in {401, 403, 408, 429} or exc.status >= 500:
                    # 401 and 403 are the ingest token, not the client's import
                    # token. Gift Links retries 503; a 422 would drop the HTML.
                    raise HTTPException(503, "Library ingest is unavailable.") from None
                # 400, 413 and any other 4xx will not succeed on retry.
                raise HTTPException(422, "Library ingest rejected the snapshot.") from None
            except (PushError, urllib.error.URLError, TimeoutError, OSError):
                raise HTTPException(503, "Library ingest is unavailable.") from None
            snap = await asyncio.to_thread(db.get_snapshot, sid)
            if snap is None:
                raise HTTPException(500, "Could not import that page.")
            usable = await asyncio.to_thread(
                lambda current=snap: _is_usable(current, _site_name(current))
            )
            return _public_body(sid, snap, usable)
        raise HTTPException(503, "Could not allocate a snapshot id.")
    finally:
        for sid in cleanup:
            try:
                await asyncio.to_thread(_delete_scratch, sid, scratch)
            except Exception as exc:
                log.error("scratch cleanup for %s failed (%s)", sid, type(exc).__name__)
            release_dell_id(sid)


def _claim(app: FastAPI) -> None:
    paused = blocked_for()
    if paused > 0:
        raise HTTPException(
            429,
            f"Imports are paused for {int(paused) + 1} s after repeated failures.",
            headers={"Retry-After": str(int(paused) + 1)},
        )
    with app.state.flight_lock:
        if app.state.in_flight >= MAX_IMPORTS_IN_FLIGHT:
            raise HTTPException(
                429, "Too many imports are already running. Try again shortly."
            )
        app.state.in_flight += 1


def _release(app: FastAPI) -> None:
    with app.state.flight_lock:
        app.state.in_flight = max(0, app.state.in_flight - 1)


def _authorize(request: Request, expected: str) -> None:
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or presented == "" or presented != presented.strip():
        raise HTTPException(
            401, "unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        ok = tokens_equal(presented, expected)
    except UnicodeError:
        ok = False
    if not ok:
        raise HTTPException(
            401, "unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )


def create_app(
    *,
    library_dir: Path,
    scratch_dir: Path,
    state_dir: Path,
    import_token: str,
    ingest_token: str,
    ingest_url: str = "http://127.0.0.1:8082",
    transport=None,
    hooks: ImportHooks | None = None,
    thumbnails: bool = False,
) -> FastAPI:
    """The import worker. ``thumbnails`` defaults off so tests do not launch Chromium.

    The systemd unit also defaults thumbnails off until Chromium is installed.
    Pass ``thumbnails=True`` only when the browser is there: the shot is taken
    before the push, or skipped, and never after the commit.
    """
    if not import_token or not ingest_token:
        raise SystemExit("amber import: import token and ingest token are required")
    if tokens_equal(import_token, ingest_token):
        raise SystemExit("amber import: import token and ingest token must differ")
    library_dir = library_dir.resolve()
    scratch_dir = scratch_dir.resolve()
    state_dir = state_dir.resolve()
    _separate(library_dir, scratch_dir, "library", "scratch")
    _separate(library_dir, state_dir, "library", "state")
    _separate(scratch_dir, state_dir, "scratch", "state")
    if not (library_dir / "amber.sqlite3").is_file():
        raise SystemExit("amber import: library database is missing")
    state_dir.mkdir(parents=True, exist_ok=True)
    bind_scratch(scratch_dir)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.library_dir = library_dir
    app.state.scratch_dir = scratch_dir
    app.state.state_dir = state_dir
    app.state.import_token = import_token
    app.state.ingest_token = ingest_token
    app.state.ingest_url = ingest_url.rstrip("/")
    app.state.transport = transport
    app.state.hooks = hooks or ImportHooks()
    app.state.thumbnails = thumbnails
    app.state.locks = MatchLocks()
    app.state.flight_lock = threading.Lock()
    app.state.in_flight = 0

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/api/import")
    async def import_page(request: Request):
        """Same contract as the Mac ``POST /api/import``, including detail strings.

        The bearer token is the import token. It cannot call ingest. Cookies
        are ignored. There is no CORS middleware: a browser on the reader's
        origin must not be able to call this port with the reader cookie.
        """
        _authorize(request, request.app.state.import_token)
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(415, "Send the request as application/json.")
        _claim(request.app)
        try:
            raw = await _read_body_capped(request, MAX_IMPORT_BODY_BYTES)
            try:
                body = json.loads(raw)
            except (ValueError, RecursionError) as exc:
                raise HTTPException(400, "Body must be valid JSON.") from exc
            del raw
            clean, final, html, html_bytes, title, force = await asyncio.to_thread(
                parse_import_fields, body
            )
            keys = sorted(
                {article_match_key(clean), article_match_key(final)} - {""}
            ) or [clean]
            async with request.app.state.locks.hold(keys):
                charged: int | None = None
                try:
                    if not force:
                        try:
                            existing = await asyncio.to_thread(
                                archived_copy,
                                request.app.state.library_dir,
                                list(dict.fromkeys((clean, final))),
                            )
                        except (RuntimeError, sqlite3.Error) as exc:
                            log.error("library read failed (%s)", type(exc).__name__)
                            raise HTTPException(
                                503, "Could not read the library."
                            ) from None
                        if existing is not None:
                            return JSONResponse(
                                {"detail": "already archived", "id": existing["id"]},
                                status_code=409,
                            )
                        replay = await asyncio.to_thread(
                            recent_replay,
                            request.app.state.state_dir,
                            keys,
                            html_bytes,
                        )
                        if replay is not None:
                            return JSONResponse(
                                {"detail": "already archived", "id": replay},
                                status_code=409,
                            )
                    charged = await asyncio.to_thread(
                        charge_budget, request.app.state.state_dir, html_bytes
                    )
                    try:
                        payload = await _freeze_and_commit(
                            library_dir=request.app.state.library_dir,
                            scratch=request.app.state.scratch_dir,
                            ingest_url=request.app.state.ingest_url,
                            ingest_token=request.app.state.ingest_token,
                            transport=request.app.state.transport,
                            hooks=request.app.state.hooks,
                            thumbnails=request.app.state.thumbnails,
                            html=html,
                            clean=clean,
                            title=title,
                            final=final,
                        )
                    except TooComplex as exc:
                        raise HTTPException(
                            422, f"Page too complex to import: {exc}"
                        ) from None
                    except HTTPException:
                        raise
                    except Exception as exc:
                        log.error("import failed (%s)", type(exc).__name__)
                        raise HTTPException(
                            422, "Could not turn that HTML into a snapshot."
                        ) from None
                    try:
                        await asyncio.to_thread(
                            remember_commit,
                            request.app.state.state_dir,
                            keys,
                            payload["id"],
                            html_bytes,
                        )
                    except OSError as exc:
                        log.error(
                            "recent import record failed (%s)",
                            type(exc).__name__,
                        )
                except HTTPException as exc:
                    if exc.status_code == 503 and charged is not None:
                        try:
                            await asyncio.to_thread(
                                refund_budget,
                                request.app.state.state_dir,
                                charged,
                                html_bytes,
                            )
                        except Exception as refund_exc:
                            log.error(
                                "budget refund failed (%s)",
                                type(refund_exc).__name__,
                            )
                    raise
            return JSONResponse(payload, status_code=201)
        finally:
            _release(request.app)

    return app


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"amber import: set {name}")
    return value


def main() -> None:
    host = os.environ.get("AMBER_IMPORT_HOST", "127.0.0.1")
    if host != "127.0.0.1":
        raise SystemExit("amber import: AMBER_IMPORT_HOST must be 127.0.0.1")
    raw_port = os.environ.get("AMBER_IMPORT_PORT", "8083")
    try:
        port = int(raw_port)
    except ValueError:
        raise SystemExit("amber import: AMBER_IMPORT_PORT must be an integer") from None
    if port in (8081, 8082) or not 1 <= port <= 65535:
        raise SystemExit("amber import: AMBER_IMPORT_PORT must be 1-65535 and not 8081 or 8082")
    library = Path(_required("AMBER_LIBRARY_DIR"))
    scratch = Path(_required("AMBER_IMPORT_SCRATCH_DIR"))
    state = Path(_required("AMBER_IMPORT_STATE_DIR"))
    try:
        import_token = read_token_file(Path(_required("AMBER_IMPORT_TOKEN_FILE")))
        ingest_token = read_token_file(Path(_required("AMBER_INGEST_TOKEN_FILE")))
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f"amber import: {exc}") from exc
    ingest_url = os.environ.get("AMBER_INGEST_URL", "http://127.0.0.1:8082").strip()
    thumbnails = os.environ.get("AMBER_IMPORT_THUMBNAILS", "0") == "1"
    app = create_app(
        library_dir=library,
        scratch_dir=scratch,
        state_dir=state,
        import_token=import_token,
        ingest_token=ingest_token,
        ingest_url=ingest_url,
        thumbnails=thumbnails,
    )
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
