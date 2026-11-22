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
    """Open the shared library read-only. Caller closes it on every path."""
    uri = "file:{}?mode=ro".format(quote(str(db_path), safe="/:"))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _library_has_id(conn: sqlite3.Connection, sid: str) -> bool:
    for sql in (
        "SELECT 1 FROM snapshots WHERE id = ?",
        "SELECT 1 FROM library_revisions WHERE id = ?",
    ):
        try:
            if conn.execute(sql, (sid,)).fetchone():
                return True
        except sqlite3.OperationalError:
            continue
    return False


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
    conn = open_library_ro(db_path) if db_path.is_file() else None
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
    try:
        found = list(_find_matches(address, conn=conn))
    except sqlite3.OperationalError:
        found = []
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
    except sqlite3.OperationalError:
        return found
    have = {snap["id"] for snap in found}
    for row in rows:
        snap = db._as_snap(row)
        if snap["id"] not in have:
            found.append(snap)
            have.add(snap["id"])
    return found


def archived_copy(library_dir: Path, addresses: list[str]) -> dict | None:
    """A complete usable snapshot of one of ``addresses`` in the shared library."""
    db_path = library_dir / "amber.sqlite3"
    if not db_path.is_file():
        return None
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


def charge_budget(state_dir: Path, nbytes: int) -> None:
    """Same 300 MiB / 24 h cap as the Mac, stored outside the shared library."""
    path = _ledger_path(state_dir)
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
        rows.append((time.time(), nbytes))
        try:
            _write_ledger(path, rows)
        except OSError as exc:
            raise HTTPException(503, "Import budget ledger cannot be read.") from exc


def _separate(left: Path, right: Path, left_name: str, right_name: str) -> None:
    a = left.resolve()
    b = right.resolve()
    if a == b or a in b.parents or b in a.parents:
        raise SystemExit(
            f"amber import: {left_name} and {right_name} must be separate directories"
        )


def _wipe_scratch() -> None:
    """Drop unpublished scratch snapshots left by a crash. The library is not here."""
    snaps = db.SNAPS_DIR
    if snaps.exists():
        for child in list(snaps.iterdir()):
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child)
    with db.connect() as conn:
        conn.execute("DELETE FROM snapshots")
        conn.commit()


def bind_scratch(scratch: Path) -> None:
    """Point this process's archive database at the scratch tree."""
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
            except RuntimeError:
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
                if exc.status == 429 or exc.status >= 500:
                    raise HTTPException(503, "Library ingest is unavailable.") from None
                raise HTTPException(502, "Library ingest rejected the snapshot.") from None
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
                if not force:
                    existing = await asyncio.to_thread(
                        archived_copy,
                        request.app.state.library_dir,
                        list(dict.fromkeys((clean, final))),
                    )
                    if existing is not None:
                        return JSONResponse(
                            {"detail": "already archived", "id": existing["id"]},
                            status_code=409,
                        )
                await asyncio.to_thread(
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
