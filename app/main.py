from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

MIME_BY_EXT = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".json": "application/json",
    ".html": "text/html",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".txt": "text/plain",
}


def mime_for_filename(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in MIME_BY_EXT:
        return MIME_BY_EXT[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"

from . import capture, db
from .api import (
    MAX_IMPORT_BODY_BYTES,
    _read_body_capped,
    check_import_budget,
    claim_import_slot,
    effective_status,
    record_import_bytes,
    release_import_slot,
    reserve_thumbnail,
    router as api_router,
    run_reserved_thumbnail,
)
from .config import HOST, ID_ALPHABET, ID_LENGTH, PORT, ROOT
from .import_child import TooComplex, check_complexity, ingest_in_child
from .ingest import original_url_from_html, screenshot_reader
from .security import normalize_url, validate_public_http_url

log = logging.getLogger("amber")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

templates = Jinja2Templates(directory=str(ROOT / "templates"))
ID_RE = re.compile(rf"^[{re.escape(ID_ALPHABET)}]{{{ID_LENGTH}}}$")
RESERVED = {
    "save",
    "saving",
    "search",
    "about",
    "static",
    "api",
    "exists",
    "saved",
    "delete",
    "favicon.ico",
    "robots.txt",
}


def fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        return iso


def fmt_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.1f} GB"


def fmt_duration(millis: int) -> str:
    if millis < 1000:
        return f"{millis} ms"
    if millis < 60 * 1000:
        seconds = round(millis / 1000, 1)
        return f"{seconds:g} s"
    minutes, remainder = divmod(millis, 60 * 1000)
    seconds = round(remainder / 1000, 1)
    return f"{minutes}m {seconds:g}s"


def host_of(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


templates.env.filters["fmtdate"] = fmt_date
templates.env.filters["filesize"] = fmt_bytes
templates.env.filters["duration"] = fmt_duration
templates.env.filters["host"] = host_of


def _read_capture_stats(sid: str) -> dict:
    try:
        return db.read_json(db.snap_dir(sid) / "meta.json").get("capture_stats", {})
    except (OSError, ValueError):
        return {}


def _allowed_extension_ids() -> set[str]:
    raw = os.environ.get("AMBER_EXTENSION_IDS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if not _allowed_extension_ids():
        log.warning(
            "AMBER_EXTENSION_IDS is not set: any installed Chrome extension with access to "
            "this address may POST JSON to Amber. Set it to your extension id(s) to restrict this."
        )
    capture.job_queue = asyncio.Queue()
    worker = asyncio.create_task(capture.worker())
    yield
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Amber",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", HOST}
if os.environ.get("AMBER_ALLOW_TESTSERVER"):
    ALLOWED_HOSTS.add("testserver")


def _forbidden() -> JSONResponse:
    return JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)


async def reject_cross_site_writes(request: Request, call_next):
    """Refuse state-changing requests that a web page on another site started.

    Browsers attach Sec-Fetch-Site to every request and Origin to every
    cross-site POST, and a web page cannot forge either. A request with neither
    (curl, scripts) is not a browser acting for another site and passes.

    GET /save?url=... is the bookmarklet's endpoint, so it stays reachable, but
    only as a top-level navigation (Sec-Fetch-Mode: navigate, Dest: document):
    <img>, fetch() and <iframe> GETs from another site are refused.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if request.method in {"GET", "HEAD"}:
        if request.url.path == "/save":
            # Speculative loads (prefetch/prerender) send navigate+document too.
            purpose = (request.headers.get("sec-purpose", "") + request.headers.get("purpose", "")).lower()
            if "prefetch" in purpose or "prerender" in purpose:
                return _forbidden()
        if request.url.path == "/save" and fetch_site in {"cross-site", "same-site"}:
            if (
                request.headers.get("sec-fetch-mode") != "navigate"
                or request.headers.get("sec-fetch-dest") != "document"
            ):
                return _forbidden()
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    # <a ping> posts with this type and header. Amber's own pages have no use
    # for it, and a captured page must not be able to fire a state-changing
    # POST (such as /saved/{id}/delete) from inside the viewer's iframe.
    if (
        request.headers.get("content-type", "").split(";")[0].strip().lower() == "text/ping"
        or "ping-to" in request.headers
    ):
        return _forbidden()
    origin = request.headers.get("origin")
    own_origin = origin == f"http://{request.headers.get('host', '')}"
    if origin is not None and origin.startswith("chrome-extension://"):
        allowed_ids = _allowed_extension_ids()
        if allowed_ids and origin[len("chrome-extension://"):].split("/")[0].lower() not in allowed_ids:
            return _forbidden()
        # Only JSON, which forces a CORS preflight unless the extension holds
        # host permission for this address.
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return _forbidden()
    elif not (own_origin or fetch_site == "same-origin"):
        if origin is not None or fetch_site in {"cross-site", "same-site"}:
            return _forbidden()
    return await call_next(request)


# Added before TrustedHostMiddleware so the Host header is validated first.
app.add_middleware(BaseHTTPMiddleware, dispatch=reject_cross_site_writes)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(ALLOWED_HOSTS))
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.get("/favicon.ico")
async def favicon():
    path = ROOT / "static" / "img" / "favicon.svg"
    return FileResponse(path, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"error": error, "recent": db.recent_snapshots(8), "saved_count": db.count_snapshots()},
    )


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse(request, "about.html", {})


def _import_error(message: str) -> RedirectResponse:
    return RedirectResponse("/?error=" + quote(message), status_code=303)


@app.post("/import")
async def import_html(request: Request):
    # Same guards as POST /api/import: streaming body cap, in-flight and daily
    # limits, complexity pre-check and a killable child process.
    try:
        claim_import_slot()
    except HTTPException as exc:
        return _import_error(exc.detail)
    try:
        try:
            raw_body = await _read_body_capped(request, MAX_IMPORT_BODY_BYTES)
        except HTTPException as exc:
            if exc.status_code == 408:
                return _import_error("The upload took too long. Try again.")
            return _import_error("That file is too large to import (12 MB limit).")

        async def _replay():
            return {"type": "http.request", "body": raw_body, "more_body": False}

        form = await Request(request.scope, _replay).form()
        upload = form.get("file")
        url = str(form.get("url") or "").strip()
        if upload is None or not getattr(upload, "read", None):
            return _import_error("Choose an HTML file to import.")
        raw = await upload.read()
        if not raw:
            return _import_error("That file was empty.")
        try:
            check_import_budget(len(raw))
        except HTTPException as exc:
            return _import_error(exc.detail)
        # Charged before parsing and kept charged if the import fails.
        record_import_bytes(len(raw))
        html = raw.decode("utf-8", errors="replace")
        try:
            await asyncio.to_thread(check_complexity, html)
            if not url:
                url = await asyncio.to_thread(original_url_from_html, html)
            sid = await ingest_in_child(html, url, "", "")
        except TooComplex as exc:
            return _import_error(f"Page too complex to import: {exc}")
        except Exception:
            log.exception("HTML import failed")
            return _import_error("Could not turn that HTML into a snapshot.")
    finally:
        release_import_slot()
    if reserve_thumbnail():
        await run_reserved_thumbnail(sid, screenshot_reader)
    return RedirectResponse(f"/{sid}", status_code=303)


@app.api_route("/save", methods=["GET", "POST"])
async def save(request: Request):
    force = int(request.query_params.get("force") or 0)
    if request.method == "POST":
        form = await request.form()
        url = str(form.get("url") or "").strip()
        if form.get("force") not in (None, ""):
            force = int(form.get("force") or 0)
    else:
        url = (request.query_params.get("url") or "").strip()
    if not url:
        return RedirectResponse("/?error=" + quote("Paste a URL first."), status_code=303)
    try:
        clean = validate_public_http_url(url)
    except ValueError as exc:
        return RedirectResponse("/?error=" + quote(str(exc)), status_code=303)

    normalized = normalize_url(clean)
    existing = db.find_by_url(normalized)
    if existing and not force:
        return templates.TemplateResponse(
            request,
            "exists.html",
            {"url": clean, "existing": existing},
        )

    try:
        job_id = capture.enqueue_capture(clean, normalized)
    except capture.QueueFull:
        return RedirectResponse(
            "/?error=" + quote("Too many saves are already queued. Try again shortly."),
            status_code=303,
        )
    return RedirectResponse(f"/saving/{job_id}", status_code=303)


@app.get("/saving/{job_id}", response_class=HTMLResponse)
async def saving(request: Request, job_id: str):
    job = capture.jobs.get(job_id)
    if not job:
        snap = db.get_snapshot(job_id)
        if snap and snap["status"] == "complete":
            return RedirectResponse(f"/{job_id}", status_code=303)
        raise HTTPException(404, "No such job")
    return templates.TemplateResponse(
        request,
        "saving.html",
        {"job": job, "job_id": job_id},
    )


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = capture.jobs.get(job_id)
    if not job:
        snap = db.get_snapshot(job_id)
        if not snap:
            raise HTTPException(404, "No such job")
        status = effective_status(snap)
        error = snap.get("error")
        if status == "failed" and not error:
            error = "Capture was interrupted before it finished."
        return {
            "id": job_id,
            "snapshot_id": job_id,
            "status": status,
            "error": error,
            "resources": [],
            "title": snap.get("title"),
            "url": snap.get("url"),
            "final_url": snap.get("final_url"),
            "capture_stats": _read_capture_stats(job_id),
        }
    return {
        "id": job["id"],
        "snapshot_id": job["snapshot_id"],
        "status": job["status"],
        "error": job.get("error"),
        "resources": job.get("resources", [])[-80:],
        "title": job.get("title"),
        "url": job.get("url"),
        "final_url": job.get("final_url"),
        "capture_stats": job["capture_stats"],
    }


@app.get("/saved", response_class=HTMLResponse)
async def saved(request: Request):
    snaps = db.recent_snapshots(500)
    return templates.TemplateResponse(
        request,
        "saved.html",
        {"snaps": snaps, "saved_count": len(snaps)},
    )


@app.post("/saved/{sid}/delete")
async def delete_saved(sid: str):
    if not ID_RE.fullmatch(sid):
        raise HTTPException(404, "Not found")
    capture.jobs.pop(sid, None)
    if not db.delete_snapshot(sid):
        raise HTTPException(404, "Not found")
    return RedirectResponse("/saved", status_code=303)


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    results = db.search_snapshots(q) if q.strip() else []
    return templates.TemplateResponse(
        request,
        "search.html",
        {"q": q, "results": results},
    )


app.include_router(api_router)


def _require_complete(sid: str) -> dict:
    if not ID_RE.fullmatch(sid):
        raise HTTPException(404, "Not found")
    snap = db.get_snapshot(sid)
    if not snap:
        raise HTTPException(404, "Not found")
    if snap["status"] != "complete":
        if snap["status"] in {"pending", "capturing"}:
            return RedirectResponse(f"/saving/{sid}", status_code=303)  # type: ignore[return-value]
        raise HTTPException(404, snap.get("error") or "Snapshot failed")
    snap["capture_stats"] = _read_capture_stats(sid)
    return snap


@app.get("/{sid}", response_class=HTMLResponse)
async def snapshot(request: Request, sid: str):
    if sid in RESERVED:
        raise HTTPException(404, "Not found")
    snap = _require_complete(sid)
    if isinstance(snap, RedirectResponse):
        return snap
    return templates.TemplateResponse(
        request,
        "snapshot.html",
        {"snap": snap, "mode": "article"},
    )


@app.get("/{sid}/webpage", response_class=HTMLResponse)
async def snapshot_webpage(request: Request, sid: str):
    snap = _require_complete(sid)
    if isinstance(snap, RedirectResponse):
        return snap
    return templates.TemplateResponse(
        request,
        "snapshot.html",
        {"snap": snap, "mode": "webpage"},
    )


@app.get("/{sid}/screenshot", response_class=HTMLResponse)
async def snapshot_screenshot(request: Request, sid: str):
    snap = _require_complete(sid)
    if isinstance(snap, RedirectResponse):
        return snap
    return templates.TemplateResponse(
        request,
        "snapshot.html",
        {"snap": snap, "mode": "screenshot"},
    )


@app.get("/{sid}/text", response_class=HTMLResponse)
async def snapshot_text(request: Request, sid: str):
    snap = _require_complete(sid)
    if isinstance(snap, RedirectResponse):
        return snap
    return RedirectResponse(f"/{sid}", status_code=303)


@app.get("/{sid}/reader")
async def snapshot_reader(sid: str):
    snap = _require_complete(sid)
    if isinstance(snap, RedirectResponse):
        return snap
    path = db.snap_dir(sid) / "reader.html"
    if not path.exists():
        raise HTTPException(404, "No article extract")
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "img-src 'self' data: https:; media-src 'self' data: https:; "
                "font-src 'self' data:; connect-src 'none'; "
                "script-src 'none'; object-src 'none'; frame-src 'none'; "
                "base-uri 'none'; form-action 'none'; " + DOCUMENT_SANDBOX
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/{sid}/raw")
async def snapshot_raw(sid: str):
    snap = _require_complete(sid)
    if isinstance(snap, RedirectResponse):
        return snap
    path = db.snap_dir(sid) / "page.html"
    if not path.exists():
        raise HTTPException(404, "Missing snapshot file")
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; style-src-elem 'self' 'unsafe-inline'; "
                "font-src 'self' data:; media-src 'self' data:; frame-src 'none'; script-src 'none'; "
                "object-src 'none'; base-uri 'none'; form-action 'none'; " + DOCUMENT_SANDBOX
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


@app.get("/{sid}/image.jpg")
async def snapshot_image(sid: str):
    snap = _require_complete(sid)
    if isinstance(snap, RedirectResponse):
        return snap
    path = db.snap_dir(sid) / "screenshot.jpg"
    if not path.exists():
        raise HTTPException(404, "No screenshot")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/{sid}/thumb.jpg")
async def snapshot_thumb(sid: str):
    snap = _require_complete(sid)
    if isinstance(snap, RedirectResponse):
        return snap
    folder = db.snap_dir(sid)
    path = folder / "thumb.jpg"
    if not path.exists():
        path = folder / "screenshot.jpg"
    if not path.exists():
        raise HTTPException(404, "No thumbnail")
    return FileResponse(path, media_type="image/jpeg")


# Stored resources are third-party bytes served on Amber's origin. Only types a
# browser renders passively are shown inline; everything else (including any
# .html/.js/.xml an older capture stored by URL suffix) is a download. The
# sandbox CSP keeps even an SVG with script from running as a document here.
_INLINE_RESOURCE_TYPES = {
    ".css": "text/css",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".tiff": "image/tiff",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
}
RESOURCE_CSP = (
    "sandbox; default-src 'none'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; font-src 'self' data:"
)
# Same sandbox the viewer iframes use (no scripts), for captured markup that
# can also be opened as a top-level page.
DOCUMENT_SANDBOX = (
    "sandbox allow-same-origin allow-popups allow-popups-to-escape-sandbox "
    "allow-top-navigation-by-user-activation"
)


@app.get("/{sid}/r/{filename}")
async def snapshot_resource(sid: str, filename: str):
    if not ID_RE.fullmatch(sid):
        raise HTTPException(404, "Not found")
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(400, "Bad filename")
    path = db.snap_dir(sid) / "res" / filename
    if not path.exists():
        raise HTTPException(404, "Missing resource")
    inline_type = _INLINE_RESOURCE_TYPES.get(Path(filename).suffix.lower())
    return FileResponse(
        path,
        media_type=inline_type or "application/octet-stream",
        headers={
            "Content-Disposition": "inline" if inline_type else "attachment",
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Security-Policy": RESOURCE_CSP,
            "X-Content-Type-Options": "nosniff",
        },
    )


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    run()
