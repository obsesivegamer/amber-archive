from __future__ import annotations

import asyncio
import logging
import mimetypes
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
from .config import HOST, ID_ALPHABET, ID_LENGTH, PORT, ROOT
from .ingest import ingest_html, original_url_from_html, screenshot_reader
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


def host_of(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


templates.env.filters["fmtdate"] = fmt_date
templates.env.filters["host"] = host_of


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
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


@app.post("/import")
async def import_html(request: Request):
    form = await request.form()
    upload = form.get("file")
    url = str(form.get("url") or "").strip()
    if upload is None or not getattr(upload, "read", None):
        return RedirectResponse("/?error=" + quote("Choose an HTML file to import."), status_code=303)
    raw = await upload.read()
    if not raw:
        return RedirectResponse("/?error=" + quote("That file was empty."), status_code=303)
    html = raw.decode("utf-8", errors="replace")
    if not url:
        url = original_url_from_html(html)
    sid = await asyncio.to_thread(ingest_html, html, url)
    try:
        await screenshot_reader(sid)
    except Exception:
        log.exception("reader screenshot failed for %s", sid)
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

    sid = db.allocate_id()
    job_id = sid
    db.insert_snapshot(sid, clean, normalized)
    capture.jobs[job_id] = {
        "id": job_id,
        "snapshot_id": sid,
        "url": clean,
        "status": "queued",
        "resources": [],
        "error": None,
        "title": None,
    }
    assert capture.job_queue is not None
    await capture.job_queue.put(job_id)
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
        return {
            "id": job_id,
            "snapshot_id": job_id,
            "status": snap["status"],
            "error": snap.get("error"),
            "resources": [],
            "title": snap.get("title"),
        }
    return {
        "id": job["id"],
        "snapshot_id": job["snapshot_id"],
        "status": job["status"],
        "error": job.get("error"),
        "resources": job.get("resources", [])[-80:],
        "title": job.get("title"),
        "url": job.get("url"),
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
        headers={"X-Content-Type-Options": "nosniff"},
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
                "font-src 'self' data:; media-src 'self' data:; frame-src 'none'; script-src 'none';"
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


@app.get("/{sid}/r/{filename}")
async def snapshot_resource(sid: str, filename: str):
    if not ID_RE.fullmatch(sid):
        raise HTTPException(404, "Not found")
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(400, "Bad filename")
    path = db.snap_dir(sid) / "res" / filename
    if not path.exists():
        raise HTTPException(404, "Missing resource")
    return FileResponse(
        path,
        media_type=mime_for_filename(filename),
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    run()
