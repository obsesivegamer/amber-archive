#!/usr/bin/env python3
"""Rebuild reader.html from a stored extract (no live fetch).

Prefers snaps/<id>/article.html (the extract capture/import already
wrote, including JSON-backed bodies). Re-extracts frozen page.html only
when article.html is missing, and refuses to write if that extract is
worse (word count collapses or paywalled flips true).

Does not rewrite page.html or screenshots.

From the repo root, with Amber's venv and the same AMBER_DATA_DIR the
server uses (unset = ./data):

    .venv/bin/python scripts/rebuild_reader.py V5yMo
    .venv/Scripts/python.exe scripts/rebuild_reader.py V5yMo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.ingest import rebuild_reader  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "ids",
        nargs="+",
        help="snapshot ids (the short /{id} codes)",
    )
    args = parser.parse_args()
    db.init_db()
    failed = 0
    for sid in args.ids:
        try:
            article = rebuild_reader(sid)
        except (ValueError, FileNotFoundError) as exc:
            print(f"{sid}: error: {exc}", file=sys.stderr)
            failed += 1
            continue
        if article.get("rebuild_refused"):
            print(f"{sid}: refused: {article.get('rebuild_reason')}", file=sys.stderr)
            failed += 1
            continue
        print(
            f"{sid}: source={article.get('rebuild_source')} "
            f"words={article.get('word_count')} "
            f"paywalled={article.get('paywalled')} "
            f"title={article.get('title')}"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
