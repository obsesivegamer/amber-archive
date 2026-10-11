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
from collections import deque

from . import db
from .config import ROOT

MAX_TAGS = 60_000
MAX_DEPTH = 400
MAX_ATTRS_PER_TAG = 1_000
MAX_TOTAL_ATTRS = 400_000
TIMEOUT_S = 45.0
FAILURE_WINDOW_S = 60.0
FAILURES_BEFORE_BLOCK = 3
BLOCK_S = 60.0
MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024

# Quote-aware like a browser's tokenizer, and possessive so an unterminated
# quote cannot send it backtracking across the document.
_TAG_RE = re.compile(
    r"""<!--|<(/?)([A-Za-z][^\s/<>]*+)((?:[^<>"']|"[^"]*+"|'[^']*+')*+)(>?)"""
)
_COMMENT_END_RE = re.compile(r"--!?>")
_ATTR_RE = re.compile(r"""[^\s"'<>/=]++(?:\s*+=\s*+(?:"[^"]*+"|'[^']*+'|[^\s"'<>=]*+))?""")
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


# Snapshot ids whose row is reserved while a child ingests them.
importing: set[str] = set()
_failures: deque[float] = deque()
_blocked_until = 0.0


# sid -> (time, message) for imports that failed, so a client that was handed
# the reserved id by /api/save can still read why instead of getting a 404.
_tombstones: dict[str, tuple[float, str]] = {}
TOMBSTONE_S = 60.0


def _tombstone(sid: str, message: str) -> None:
    now = time.monotonic()
    for key in [k for k, (t, _) in _tombstones.items() if now - t > TOMBSTONE_S]:
        del _tombstones[key]
    _tombstones[sid] = (now, message)


def import_failure(sid: str) -> str | None:
    entry = _tombstones.get(sid)
    if entry and time.monotonic() - entry[0] <= TOMBSTONE_S:
        return entry[1]
    return None


def note_failure() -> None:
    """Record a child that failed or was killed; three in a minute pause imports."""
    global _blocked_until
    now = time.monotonic()
    _failures.append(now)
    while _failures and _failures[0] < now - FAILURE_WINDOW_S:
        _failures.popleft()
    if len(_failures) >= FAILURES_BEFORE_BLOCK:
        _blocked_until = now + BLOCK_S
        _failures.clear()


def blocked_for() -> float:
    """Seconds imports are paused after repeated failures (0 when open)."""
    return max(0.0, _blocked_until - time.monotonic())


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
            # `<!-->` and `<!--->` are complete (empty) comments.
            if html.startswith(">", pos):
                pos += 1
            elif html.startswith("->", pos):
                pos += 2
            else:
                end = _COMMENT_END_RE.search(html, pos)
                if end is None:
                    return
                pos = end.end()
            continue
        closing, name, rest, _gt = m.groups()
        name = name.lower()
        if closing:
            for i in range(1, min(len(stack), 16) + 1):
                if stack[-i] == name:
                    del stack[-i:]
                    break
            continue
        attrs = rest.count("=")
        if attrs <= MAX_ATTRS_PER_TAG and len(rest) > 4 * MAX_ATTRS_PER_TAG:
            # Boolean attributes carry no "=", so count them properly.
            attrs = sum(1 for _ in _ATTR_RE.finditer(rest))
        if attrs > MAX_ATTRS_PER_TAG:
            raise TooComplex(f"a <{name}> tag has too many attributes")
        if name in _VOID or rest.endswith("/"):
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


def measure_parsed_tree(html: str) -> None:
    """Authoritative limits, measured on the tree the real parser builds.

    The parent's text scan is only a fast reject: quoted `<` and `>`, odd
    comments and tag soup can fool it. Runs in the child, so a hostile page can
    only cost the child.
    """
    from bs4 import BeautifulSoup, Tag

    soup = BeautifulSoup(html, "lxml")
    stack = [(soup, 0)]
    tags = 0
    attrs = 0
    while stack:
        node, depth = stack.pop()
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            tags += 1
            if tags > MAX_TAGS:
                raise TooComplex(f"page has too many elements (> {MAX_TAGS})")
            n = len(child.attrs)
            if n > MAX_ATTRS_PER_TAG:
                raise TooComplex(f"a <{child.name}> element has too many attributes")
            attrs += n
            if attrs > MAX_TOTAL_ATTRS:
                raise TooComplex("page has too many attributes")
            if depth + 1 > MAX_DEPTH:
                raise TooComplex(f"page is nested too deeply (> {MAX_DEPTH} levels)")
            stack.append((child, depth + 1))


async def ingest_in_child(
    html: str, url: str, title: str, final_url: str
) -> str:
    """Pre-check, then ingest in a child process. Returns the snapshot id.

    Raises TooComplex for pages that are rejected, time out or exceed the
    memory cap; any other failure surfaces as RuntimeError. A snapshot row is
    inserted up front so a killed child never leaves one behind.
    """
    await asyncio.to_thread(check_complexity, html)
    url = url.strip()
    if "://" not in url:
        url = "https://" + url

    def reserve() -> str:
        sid = db.allocate_id()
        # Before the insert, so no reader ever sees the row as an orphan.
        importing.add(sid)
        try:
            db.insert_snapshot(sid, url, _normalize(url))
        except BaseException:
            importing.discard(sid)
            raise
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
                result = json.loads(out or b"{}")
            except ValueError:
                result = {}
            reason = result.get("error")
            if reason == "memory":
                raise TooComplex("page needed too much memory to process")
            if reason == "complex":
                raise TooComplex(result.get("detail") or "page is too complex")
            raise RuntimeError(reason or f"import worker exited with {proc.returncode}")
        return json.loads(out)["sid"]
    except BaseException as exc:
        if isinstance(exc, (TooComplex, RuntimeError, ValueError, KeyError)):
            note_failure()
        if isinstance(exc, TooComplex):
            _tombstone(sid, f"Page too complex to import: {exc}")
        elif isinstance(exc, asyncio.CancelledError):
            _tombstone(sid, "The import was cancelled.")
        else:
            _tombstone(sid, "Could not turn that HTML into a snapshot.")
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        await asyncio.to_thread(db.delete_snapshot, sid)
        raise
    finally:
        importing.discard(sid)


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
        import signal

        # If the server is killed the child's pipe goes quiet but it would
        # otherwise run on; SIGALRM ends it a little after the parent's own kill.
        signal.alarm(int(TIMEOUT_S) + 5)
    except (AttributeError, ValueError):
        pass
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

        measure_parsed_tree(body.decode("utf-8"))
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
    except TooComplex as exc:
        sys.stdout.write(json.dumps({"error": "complex", "detail": str(exc)}))
        return 4
    except Exception as exc:
        sys.stdout.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]}))
        return 1
    sys.stdout.write(json.dumps({"sid": sid}))
    return 0


if __name__ == "__main__":
    sys.exit(_child_main())
