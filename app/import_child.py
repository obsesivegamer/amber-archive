"""Run ingest_html for POST /api/import in a throwaway child process.

Pasted HTML is untrusted and parsers can be made to burn minutes of CPU or
gigabytes of memory on a few megabytes of input. The parent therefore rejects
the cheap-to-detect shapes up front (check_complexity) and runs the rest in a
child it can kill on a wall-clock deadline; the child also caps its own memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time

from . import db
from .config import ROOT

MAX_TAGS = 60_000
MAX_DEPTH = 400
MAX_ATTRS_PER_TAG = 1_000
TIMEOUT_S = 45.0
MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024

_TAG_RE = re.compile(r"<!--|<(/?)([A-Za-z][^\s/<>]*)([^<>]*)(>?)")
_VOID = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
_RAW_TEXT = {
    name: re.compile(r"</" + name, re.IGNORECASE)
    for name in ("script", "style", "textarea", "title")
}
# <p>, <li>... are closed by a following sibling of the same name.
_IMPLIED_SIBLING_CLOSE = frozenset("p li dt dd tr td th option thead tbody tfoot".split())


class TooComplex(ValueError):
    """The page is shaped to make a parser expensive; nothing was stored."""


def check_complexity(html: str) -> None:
    """Linear scan that refuses tag floods, deep nesting and attribute floods."""
    tags = html.count("<")
    if tags > MAX_TAGS:
        raise TooComplex(f"page has too many tags ({tags} > {MAX_TAGS})")
    stack: list[str] = []
    pos = 0
    search = _TAG_RE.search
    while True:
        m = search(html, pos)
        if m is None:
            return
        pos = m.end()
        if m.group(0) == "<!--":
            end = html.find("-->", pos)
            if end < 0:
                return
            pos = end + 3
            continue
        closing, name, rest, _gt = m.groups()
        name = name.lower()
        if closing:
            for i in range(1, min(len(stack), 16) + 1):
                if stack[-i] == name:
                    del stack[-i:]
                    break
            continue
        if rest.count("=") > MAX_ATTRS_PER_TAG:
            raise TooComplex(f"a <{name}> tag has too many attributes")
        if name in _VOID or rest.rstrip().endswith("/"):
            continue
        raw_close = _RAW_TEXT.get(name)
        if raw_close is not None:
            end = raw_close.search(html, pos)
            if end is None:
                return
            pos = end.start()
            continue
        if name in _IMPLIED_SIBLING_CLOSE and stack and stack[-1] == name:
            stack.pop()
        stack.append(name)
        if len(stack) > MAX_DEPTH:
            raise TooComplex(f"page is nested too deeply (> {MAX_DEPTH} levels)")


async def ingest_in_child(
    html: str, url: str, title: str, final_url: str
) -> str:
    """Pre-check, then ingest in a child process. Returns the snapshot id.

    Raises TooComplex for pages that are rejected, time out or exceed the
    memory cap; any other failure surfaces as RuntimeError. A snapshot row is
    inserted up front so a killed child never leaves one behind.
    """
    await asyncio.to_thread(check_complexity, html)

    def reserve() -> str:
        sid = db.allocate_id()
        db.insert_snapshot(sid, url, _normalize(url))
        return sid

    sid = await asyncio.to_thread(reserve)
    header = json.dumps({"sid": sid, "url": url, "title": title, "final_url": final_url})
    payload = header.encode("utf-8") + b"\n" + html.encode("utf-8")
    env = dict(os.environ, AMBER_DATA_DIR=str(db.DATA_DIR))
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.import_child",
            cwd=str(ROOT), env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(payload), TIMEOUT_S)
        except asyncio.TimeoutError:
            raise TooComplex("page took too long to process") from None
        if proc.returncode != 0:
            try:
                reason = json.loads(out or b"{}").get("error")
            except ValueError:
                reason = None
            if reason == "memory":
                raise TooComplex("page needed too much memory to process")
            raise RuntimeError(reason or f"import worker exited with {proc.returncode}")
        return json.loads(out)["sid"]
    except BaseException:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        await asyncio.to_thread(db.delete_snapshot, sid)
        raise


def _normalize(url: str) -> str:
    from .security import normalize_url

    return normalize_url(url)


def _memory_watchdog() -> None:
    import resource

    scale = 1 if sys.platform == "darwin" else 1024
    while True:
        time.sleep(0.1)
        if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale > MEMORY_LIMIT_BYTES:
            sys.stdout.write(json.dumps({"error": "memory"}))
            sys.stdout.flush()
            os._exit(3)


def _child_main() -> int:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    except Exception:
        pass
    try:
        threading.Thread(target=_memory_watchdog, daemon=True).start()
    except Exception:
        pass
    raw = sys.stdin.buffer.read()
    header, _, body = raw.partition(b"\n")
    meta = json.loads(header)
    try:
        from .ingest import ingest_html

        sid = ingest_html(
            body.decode("utf-8"),
            meta["url"],
            title_hint=meta["title"],
            final_url=meta["final_url"],
            sid=meta["sid"],
        )
    except MemoryError:
        sys.stdout.write(json.dumps({"error": "memory"}))
        return 3
    except Exception as exc:
        sys.stdout.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]}))
        return 1
    sys.stdout.write(json.dumps({"sid": sid}))
    return 0


if __name__ == "__main__":
    sys.exit(_child_main())
