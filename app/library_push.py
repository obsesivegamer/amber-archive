"""Push one complete snapshot to Dell-PLX ingest.

Reads ``snaps/<id>/`` and the ``snapshots`` row already on disk, ``PUT``s each
file to ``/v1/blobs/{sha256}``, then ``POST``s revision 1 to ``/v1/revisions``.
A second run of the same id sends the same revision and the server answers 200.

Capture is not involved. Set ``AMBER_DATA_DIR`` to the Mac archive, or pass
``--data-dir``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .config import DATA_DIR
from .library_contract import ID_RE, TokenFileError, read_token_file, sha256_hex, snapshot_relpath
from .security import article_match_key


class PushError(Exception):
    """The snapshot on disk cannot be pushed."""


class PushRejected(Exception):
    """Ingest refused the push."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class PushResult:
    status: int
    revision: int
    change_seq: int


@dataclass(frozen=True)
class BackfillItem:
    sid: str
    ok: bool
    status: int | None
    detail: str
    result: PushResult | None = None


class UrllibTransport:
    def round_trip(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> tuple[int, dict]:
        request = urllib.request.Request(
            url,
            data=body if body else None,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        if not raw:
            return status, {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PushError("ingest returned a body that is not JSON") from exc
        if not isinstance(payload, dict):
            raise PushError("ingest returned a body that is not a JSON object")
        return status, payload


def snapshot_push(
    source: Path,
    sid: str,
    *,
    revision: int = 1,
) -> tuple[dict, list[tuple[str, bytes]]]:
    """The revision document and the blob bodies for one complete snapshot."""
    if not ID_RE.fullmatch(sid):
        raise PushError("snapshot id must be 5 characters from the archive alphabet")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise PushError("revision must be a positive integer")
    snap = source / "snaps" / sid
    if snap.is_symlink() or not snap.is_dir():
        raise PushError(f"no snapshot directory for {sid}")
    row = _read_row(source / "amber.sqlite3", sid)
    files: list[dict] = []
    blobs: dict[str, bytes] = {}
    for path in sorted(snap.rglob("*")):
        rel = path.relative_to(snap).as_posix()
        if path.is_symlink():
            raise PushError(f"{rel} is a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PushError(f"{rel} is not a regular file")
        if snapshot_relpath(rel) != rel:
            raise PushError(f"{rel} is outside the snapshot layout")
        data = path.read_bytes()
        digest = sha256_hex(data)
        files.append({"path": rel, "sha256": digest, "size": len(data)})
        blobs.setdefault(digest, data)
    if not any(item["path"] == "meta.json" for item in files):
        raise PushError("snapshot is missing meta.json")
    document = {
        "id": sid,
        "revision": revision,
        "op": "upsert",
        "match_key": article_match_key(row["final_url"] or row["url"]),
        "row": row,
        "files": files,
    }
    return document, list(blobs.items())


def push_snapshot(
    source: Path,
    sid: str,
    ingest_url: str,
    token: str,
    *,
    transport: UrllibTransport | None = None,
) -> PushResult:
    """Upload one complete snapshot. Revision is 1, which is this slice's push."""
    document, blobs = snapshot_push(source, sid, revision=1)
    client = transport or UrllibTransport()
    base = ingest_url.rstrip("/")
    auth = {"Authorization": f"Bearer {token}"}
    for digest, data in blobs:
        status, payload = client.round_trip(
            "PUT",
            f"{base}/v1/blobs/{digest}",
            data,
            {**auth, "Content-Length": str(len(data))},
        )
        if status not in (200, 201):
            raise PushRejected(status, _detail(payload))
    encoded = json.dumps(document).encode("utf-8")
    status, payload = client.round_trip(
        "POST",
        f"{base}/v1/revisions",
        encoded,
        {
            **auth,
            "Content-Length": str(len(encoded)),
            "Content-Type": "application/json",
        },
    )
    if status not in (200, 201):
        raise PushRejected(status, _detail(payload))
    try:
        return PushResult(
            status=status,
            revision=int(payload["revision"]),
            change_seq=int(payload["change_seq"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PushError("ingest accepted the push without a change_seq") from exc


def _detail(payload: dict) -> str:
    detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        revision = payload.get("revision")
        if isinstance(revision, int) and not isinstance(revision, bool):
            return f"{detail} (stored revision {revision})"
        return detail
    return "ingest rejected the push"


def _read_row(db_path: Path, sid: str) -> dict:
    if not db_path.is_file():
        raise PushError("archive database is missing")
    uri = "file:{}?mode=ro".format(quote(str(db_path), safe="/:"))
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise PushError("could not read the snapshot row") from exc
    conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute("SELECT * FROM snapshots WHERE id = ?", (sid,)).fetchone()
        except sqlite3.Error as exc:
            raise PushError("could not read the snapshot row") from exc
    finally:
        conn.close()
    if row is None:
        raise PushError("snapshot row is missing")
    keys = set(row.keys())
    if row["status"] != "complete":
        raise PushError("snapshot status is not complete")
    for key in ("url", "url_normalized", "created_at"):
        if key not in keys or not isinstance(row[key], str) or row[key] == "":
            raise PushError(f"snapshot row is missing {key}")

    def optional(name: str) -> str | None:
        if name not in keys or row[name] is None:
            return None
        value = row[name]
        if not isinstance(value, str):
            raise PushError(f"snapshot row {name} is not text")
        return value

    def optional_int(name: str) -> int | None:
        if name not in keys or row[name] is None:
            return None
        value = row[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise PushError(f"snapshot row {name} is not an integer")
        return value

    error = optional("error") if "error" in keys else None
    return {
        "url": row["url"],
        "url_normalized": row["url_normalized"],
        "final_url": optional("final_url"),
        "title": optional("title"),
        "site_name": optional("site_name"),
        "author": optional("author"),
        "published_at": optional("published_at"),
        "description": optional("description"),
        "created_at": row["created_at"],
        "http_status": optional_int("http_status"),
        "word_count": optional_int("word_count"),
        "paywalled": bool(row["paywalled"]) if "paywalled" in keys and row["paywalled"] is not None else False,
        "status": "complete",
        "error": error,
    }


def complete_ids(source: Path) -> list[str]:
    """Complete snapshot ids on disk, oldest first."""
    db_path = source / "amber.sqlite3"
    if not db_path.is_file():
        raise PushError("archive database is missing")
    uri = "file:{}?mode=ro".format(quote(str(db_path), safe="/:"))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id FROM snapshots
            WHERE status = 'complete'
            ORDER BY created_at ASC, rowid ASC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise PushError("could not list complete snapshots") from exc
    finally:
        conn.close()
    found = []
    for row in rows:
        sid = row["id"]
        if isinstance(sid, str) and ID_RE.fullmatch(sid):
            found.append(sid)
    return found


def stored_ids(ingest_url: str, token: str, *, transport: UrllibTransport | None = None) -> set[str]:
    """Ids ingest has already stored. The reader lists these rows, not bare folders."""
    client = transport or UrllibTransport()
    status, payload = client.round_trip(
        "GET",
        ingest_url.rstrip("/") + "/v1/revisions",
        None,
        {"Authorization": f"Bearer {token}"},
    )
    if status != 200:
        raise PushRejected(status, _detail(payload))
    revisions = payload.get("revisions")
    if not isinstance(revisions, list):
        raise PushError("ingest revisions response has no list")
    found = set()
    for item in revisions:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            found.add(item["id"])
    return found


def backfill(
    source: Path,
    ingest_url: str,
    token: str,
    *,
    transport: UrllibTransport | None = None,
) -> list[BackfillItem]:
    """Push every complete id that ingest has not stored.

    A tree rsync can land snaps/<id>/ without a snapshots row. The reader
    lists rows only, so those folders stay off the phone until this runs.
    Ids are oldest first. One rejected id is recorded and the rest still run,
    so a bad old snapshot cannot wedge every newer capture.
    """
    client = transport or UrllibTransport()
    have = stored_ids(ingest_url, token, transport=client)
    outcome: list[BackfillItem] = []
    for sid in complete_ids(source):
        if sid in have:
            continue
        try:
            folder = source / "snaps" / sid
            if folder.is_symlink() or not folder.is_dir():
                raise PushError(f"no snapshot directory for {sid}")
            result = push_snapshot(source, sid, ingest_url, token, transport=client)
        except PushRejected as exc:
            outcome.append(BackfillItem(sid, False, exc.status, exc.detail))
            continue
        except (PushError, urllib.error.URLError, TimeoutError, OSError) as exc:
            outcome.append(BackfillItem(sid, False, None, str(exc)))
            continue
        outcome.append(BackfillItem(sid, True, result.status, "", result))
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.library_push",
        description="Push one complete snapshot to Dell-PLX ingest.",
    )
    parser.add_argument("snapshot_id", nargs="?", metavar="id")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Push every complete id ingest has not stored.",
    )
    parser.add_argument("--ingest-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Archive data directory. Defaults to AMBER_DATA_DIR or the repo data directory.",
    )
    args = parser.parse_args(argv)
    if args.backfill == (args.snapshot_id is not None):
        parser.error("pass an id or --backfill")
    try:
        token = read_token_file(args.token_file)
        source = args.data_dir if args.data_dir is not None else DATA_DIR
        if args.backfill:
            items = backfill(source, args.ingest_url, token)
        else:
            result = push_snapshot(source, args.snapshot_id, args.ingest_url, token)
            items = [BackfillItem(args.snapshot_id, True, result.status, "", result)]
    except (TokenFileError, PushError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except PushRejected as exc:
        print(exc.detail, file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"push failed: {exc}", file=sys.stderr)
        return 1
    ok = 0
    failed = 0
    for item in items:
        if item.ok and item.result is not None:
            ok += 1
            print(
                f"{item.result.status} {item.sid} "
                f"revision={item.result.revision} change_seq={item.result.change_seq}"
            )
            continue
        failed += 1
        status = "-" if item.status is None else str(item.status)
        print(f"FAIL {item.sid} {status} {item.detail}", file=sys.stderr)
    if args.backfill:
        print(f"backfill {ok} ok, {failed} failed")
        return 1 if failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
