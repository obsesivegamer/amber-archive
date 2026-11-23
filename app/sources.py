"""Import and export ``sources.json``.

In ingest mode the file lives in the ``AMBER_APP_STATE`` directory. Otherwise
it lives in the data directory, next to the library.
"""

from __future__ import annotations

import argparse
import json
import sys

from .web import _read_json, _write_json_atomic


def _document() -> dict | None:
    stored = _read_json("sources.json")
    if stored is None:
        return None
    sources = stored.get("sources")
    if not isinstance(sources, list):
        return None
    return {"sources": sources, "updated_at": stored.get("updated_at") or ""}


def _nonempty(stored: dict | None) -> bool:
    return bool(stored and stored.get("sources"))


def import_exact(raw: str, *, replace: bool) -> int:
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        print("rejected: could not parse JSON", file=sys.stderr)
        return 1
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        print('rejected: expected {"sources": [...]}', file=sys.stderr)
        return 1
    current = _document()
    if _nonempty(current) and not replace:
        print("rejected: the sources store is not empty", file=sys.stderr)
        return 1
    updated = payload.get("updated_at")
    if not isinstance(updated, str) or not updated:
        updated = ""
    _write_json_atomic(
        "sources.json",
        {"sources": payload["sources"], "updated_at": updated},
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.sources")
    sub = parser.add_subparsers(dest="command", required=True)
    importing = sub.add_parser("import")
    importing.add_argument("--exact", action="store_true", required=True)
    importing.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "import":
        return import_exact(sys.stdin.read(), replace=args.replace)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
