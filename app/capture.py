"""Playwright capture: graphical screenshot + frozen HTML + text copy."""

from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from .config import (
    BLOCKED_HOST_SNIPPETS,
    EXTRA_HEADERS,
    GOOGLEBOT_UA,
    MAX_RESOURCE_BYTES,
    MAX_TOTAL_RESOURCE_BYTES,
    NAV_TIMEOUT_MS,
    NETWORK_IDLE_MS,
    REFERRER_BOUNCE_ORIGINS,
    REFERRER_BOUNCE_RETRY_ORIGINS,
    RENDER_WAIT_MS,
    USER_AGENT,
    VIEWPORT,
)
from . import db
from .extract import extract_article
from .freeze import freeze_html, rewrite_css
from .reader import build_reader_html
from .security import validate_public_http_url

jobs: dict[str, dict] = {}
job_queue: asyncio.Queue[str] | None = None

SAVE_TYPES = {
    "text/css",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "image/avif",
    "image/bmp",
    "image/x-icon",
    "image/vnd.microsoft.icon",
    "font/woff",
    "font/woff2",
    "font/ttf",
    "font/otf",
    "application/font-woff",
    "application/font-woff2",
    "application/vnd.ms-fontobject",
    "application/x-font-ttf",
    "application/x-font-woff",
}

EXT_FOR_TYPE = {
    "text/css": ".css",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
}

CLEAN_PAGE_JS = """
() => {
  const selectors = [
    '[id*="paywall" i]', '[class*="paywall" i]',
    '.tp-modal', '.tp-backdrop', '.tp-iframe-wrapper',
    '[class*="piano-"]', '[id*="piano-"]', '#piano_inline',
    '[data-testid="paywall"]', '#gateway-content',
    '[class*="regwall" i]', '[class*="subscribe-wall" i]',
    '[class*="subscription-wall" i]',
    '#onetrust-banner-sdk', '#onetrust-consent-sdk', '#onetrust-pc-sdk',
    '.cc-window', '#cookie-banner', '[id*="sp_message_container"]',
    '.fc-consent-root', '.zephr-overlay', '[class*="zephr-"]',
    '.pelcro-prefix-modal', '[class*="gdpr-banner" i]',
    '.modal-scrollable', '[class*="fancybox-overlay"]',
    '.top-sticky-banner', '.awareness-bar',
    '[id^="StickyBannerTop"]', '[id^="MarketingModal"]',
    '[id^="BannerAdvertisement"]',
    '#onetrust-consent-sdk'
  ];
  for (const sel of selectors) {
    document.querySelectorAll(sel).forEach((el) => el.remove());
  }
  document.documentElement.style.setProperty('--nav-awareness-bar-height', '0px');
  document.documentElement.style.setProperty('--nav-ad-banner-height', '0px');
  document.documentElement.style.overflow = 'auto';
  document.body.style.overflow = 'auto';
  document.body.style.position = 'static';
  document.body.style.height = 'auto';
  document.body.style.filter = 'none';
  document.querySelectorAll('article, [itemprop="articleBody"], .article-body, .story-body, main').forEach((el) => {
    el.style.filter = 'none';
    el.style.maxHeight = 'none';
    el.style.overflow = 'visible';
  });
  document.querySelectorAll('[style*="blur"]').forEach((el) => {
    el.style.filter = 'none';
  });
}
"""

REVEAL_LEDE_JS = """
() => {
  const desc = document.querySelector('meta[property="og:description"], meta[name="description"]');
  const text = (desc && desc.getAttribute('content') || '').trim();
  if (text.length < 80) return false;
  if (document.getElementById('amber-extracted-lede')) return true;
  const host = document.querySelector('#ti-content h1, article h1, h1');
  if (!host) return false;
  const box = document.createElement('div');
  box.id = 'amber-extracted-lede';
  box.setAttribute('data-amber', 'lede');
  box.style.cssText = 'max-width:42rem;margin:1.25rem 0 2rem;font-size:1.12rem;line-height:1.65;color:inherit;';
  text.split(/\\n+/).forEach((para) => {
    const t = para.trim();
    if (!t) return;
    const p = document.createElement('p');
    p.textContent = t;
    box.appendChild(p);
  });
  const header = host.closest('header') || host;
  header.after(box);
  return true;
}
"""

DISMISS_JS = """
() => {
  const labels = ['accept', 'accept all', 'agree', 'i agree', 'got it', 'ok',
                  'allow', 'allow all', 'continue', 'i understand'];
  const clickables = document.querySelectorAll('button, [role="button"], a');
  for (const el of clickables) {
    const t = (el.innerText || el.textContent || '').trim().toLowerCase();
    if (t && t.length < 40 && labels.some((l) => t === l || t.startsWith(l))) {
      try { el.click(); } catch (e) {}
    }
  }
}
"""

# archive.today freezes the rendered page by copying getComputedStyle()
# onto every element, then dropping stylesheets. That is why their HTML
# is a wall of inline style="" and still looks right with scripts off.
INLINE_COMPUTED_CSS_JS = """
() => {
  const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'LINK', 'META', 'HEAD', 'TITLE', 'BR', 'WBR']);
  const nodes = document.querySelectorAll('body *');
  for (const el of nodes) {
    if (skip.has(el.tagName)) continue;
    const cs = getComputedStyle(el);
    let css = '';
    for (let i = 0; i < cs.length; i++) {
      const prop = cs.item(i);
      if (!prop || prop.startsWith('--')) continue;
      css += prop + ':' + cs.getPropertyValue(prop) + ';';
    }
    if (css) el.setAttribute('style', css);
  }
  const htmlCs = getComputedStyle(document.documentElement);
  const bodyCs = getComputedStyle(document.body);
  const pack = (cs) => {
    let css = '';
    for (let i = 0; i < cs.length; i++) {
      const prop = cs.item(i);
      if (!prop || prop.startsWith('--')) continue;
      css += prop + ':' + cs.getPropertyValue(prop) + ';';
    }
    return css;
  };
  document.documentElement.setAttribute('style', pack(htmlCs));
  document.body.setAttribute('style', pack(bodyCs));
  document.querySelectorAll('link[rel="stylesheet"], style').forEach((n) => n.remove());
}
"""

SCROLL_JS = """
async () => {
  await new Promise((resolve) => {
    let total = 0;
    const dist = 500;
    const timer = setInterval(() => {
      window.scrollBy(0, dist);
      total += dist;
      if (total >= Math.min(document.body.scrollHeight, 12000)) {
        clearInterval(timer);
        window.scrollTo(0, 0);
        resolve();
      }
    }, 100);
  });
}
"""


def _should_block(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(snip in host or snip in url.lower() for snip in BLOCKED_HOST_SNIPPETS)


def _ext_for(content_type: str, url: str) -> str:
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in EXT_FOR_TYPE:
        return EXT_FOR_TYPE[mime]
    guessed = mimetypes.guess_extension(mime) if mime else None
    if guessed:
        return guessed
    path = urlparse(url).path
    suffix = Path(path).suffix
    return suffix if suffix and len(suffix) < 8 else ".bin"


def _should_save(content_type: str) -> bool:
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in SAVE_TYPES:
        return True
    return mime.startswith("image/") or mime.startswith("font/")


def log_resource(job: dict, **entry) -> None:
    job.setdefault("resources", []).append(entry)


def _http_get(url: str, user_agent: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


async def fetch_crawler_html(url: str) -> str | None:
    for ua in (GOOGLEBOT_UA, USER_AGENT):
        try:
            html = await asyncio.to_thread(_http_get, url, ua)
        except Exception:
            continue
        if html and len(html) > 2000 and "Attention Required" not in html:
            return html
    return None


def require_bounce_origin(origin: str) -> str:
    if origin not in REFERRER_BOUNCE_ORIGINS:
        raise ValueError("bounce origin is not allowlisted")
    return origin


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def extra_headers_for_visit(*, bounce: bool) -> dict[str, str]:
    """Pin Google Referer only on the first (direct) visit.

    Playwright extra_http_headers override the click-through Referer, so a
    bounce context that still carries EXTRA_HEADERS would send Google on
    every retry — including x.com.
    """
    if not bounce:
        return dict(EXTRA_HEADERS)
    return {k: v for k, v in EXTRA_HEADERS.items() if k.lower() != "referer"}


def bounce_origins_for(*urls: str) -> tuple[str, ...]:
    """Retry origins whose host is not the article. You cannot bounce a site off itself."""
    skip = {_host(u) for u in urls if u}
    skip.discard("")
    return tuple(origin for origin in REFERRER_BOUNCE_RETRY_ORIGINS if _host(origin) not in skip)


def intercept_bounce_host(req_url: str, bounce_origin: str | None, *, landed: bool) -> bool:
    """Stub/abort the bounce host only until the click-through starts."""
    if not bounce_origin or landed:
        return False
    return _host(req_url) == _host(bounce_origin)


def _richer_article(candidate: dict, current: dict) -> bool:
    # Word-count-only win is safe only while paywalled ⇔ word_count < 80
    # (article_is_paywalled). Prefer not-paywalled if that invariant diverges.
    if candidate.get("paywalled") is False and current.get("paywalled") is True:
        return True
    if candidate.get("paywalled") is True and current.get("paywalled") is False:
        return False
    return (candidate.get("word_count") or 0) > (current.get("word_count") or 0)


def _merge_crawler_article(article: dict, crawler_article: dict) -> dict:
    if _richer_article(crawler_article, article):
        for key in ("dek", "author", "author_image", "published_at", "site_name", "title"):
            if article.get(key) and not crawler_article.get(key):
                crawler_article[key] = article[key]
        return crawler_article
    for key in ("dek", "author", "author_image", "published_at"):
        if not article.get(key) and crawler_article.get(key):
            article[key] = crawler_article[key]
    return article


BOUNCE_STUB_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="referrer" content="origin">
  <title>Amber</title>
</head>
<body><p>Amber referrer bounce</p></body>
</html>
"""


@dataclass
class _Visit:
    html: str
    article: dict
    final_url: str
    title: str
    http_status: int | None
    resource_map: dict[str, str]
    resource_bodies: dict[str, tuple[bytes, str]]
    page: Any
    context: Any
    bounce_origin: str | None = None

    async def close(self) -> None:
        try:
            await self.context.close()
        except Exception:
            pass


async def _settle_page(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
    except PlaywrightTimeout:
        pass
    try:
        await page.evaluate(DISMISS_JS)
    except Exception:
        pass
    await page.wait_for_timeout(400)
    try:
        await page.evaluate(CLEAN_PAGE_JS)
    except Exception:
        pass
    try:
        await page.evaluate(SCROLL_JS)
    except Exception:
        pass
    await page.wait_for_timeout(RENDER_WAIT_MS)
    try:
        await page.evaluate(CLEAN_PAGE_JS)
    except Exception:
        pass
    try:
        await page.evaluate(REVEAL_LEDE_JS)
    except Exception:
        pass
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(200)
    try:
        await page.evaluate(INLINE_COMPUTED_CSS_JS)
    except Exception:
        pass
    await page.wait_for_timeout(100)


async def _goto_article(page, url: str, bounce_origin: str | None, *, on_bounce_landed=None):
    """Land on the article.

    A direct ``page.goto`` is an address-bar navigation (``Sec-Fetch-Site:
    none``). Chromium may still attach EXTRA_HEADERS Referer and fill
    ``document.referrer``, but Piano-style meters treat that as a typed URL.
    A bounce lands on an allowlisted origin, then clicks through so the
    article request is a real cross-site navigation with that referrer.
    """
    if bounce_origin is None:
        return await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    origin = require_bounce_origin(bounce_origin)
    await page.goto(origin, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    if on_bounce_landed is not None:
        on_bounce_landed()
    await page.evaluate(
        """(href) => {
          let a = document.getElementById('amber-bounce');
          if (!a) {
            a = document.createElement('a');
            a.id = 'amber-bounce';
            document.body.appendChild(a);
          }
          a.setAttribute('href', href);
          a.textContent = 'continue';
        }""",
        url,
    )
    async with page.expect_navigation(
        wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
    ) as info:
        await page.click("#amber-bounce")
    return await info.value


async def _capture_visit(
    browser,
    job: dict,
    url: str,
    *,
    bounce_origin: str | None = None,
) -> _Visit:
    if bounce_origin is not None:
        bounce_origin = require_bounce_origin(bounce_origin)

    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport=VIEWPORT,
        locale="en-US",
        java_script_enabled=True,
        bypass_csp=True,
        extra_http_headers=extra_headers_for_visit(bounce=bounce_origin is not None),
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    page = await context.new_page()

    resource_map: dict[str, str] = {}
    resource_bodies: dict[str, tuple[bytes, str]] = {}
    total_bytes = 0
    bounce_landed = False

    def _mark_bounce_landed() -> None:
        nonlocal bounce_landed
        bounce_landed = True

    async def on_route(route):
        req_url = route.request.url
        if intercept_bounce_host(req_url, bounce_origin, landed=bounce_landed):
            if route.request.resource_type == "document":
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=BOUNCE_STUB_HTML,
                    headers={"Referrer-Policy": "origin"},
                )
                return
            await route.abort()
            return
        if _should_block(req_url):
            await route.abort()
            return
        await route.continue_()

    async def on_response(response):
        nonlocal total_bytes
        req_url = response.url
        status = response.status
        ctype = response.headers.get("content-type", "")
        try:
            body = await response.body()
        except Exception:
            body = b""
        size = len(body)
        log_resource(
            job,
            url=req_url,
            status=status,
            mime=(ctype or "").split(";")[0],
            size=size,
        )
        if status >= 400 or not body:
            return
        if not _should_save(ctype):
            return
        if size > MAX_RESOURCE_BYTES or total_bytes + size > MAX_TOTAL_RESOURCE_BYTES:
            return
        digest = hashlib.sha256(body).hexdigest()[:16]
        filename = digest + _ext_for(ctype, req_url)
        if filename not in resource_bodies:
            resource_bodies[filename] = (body, (ctype or "application/octet-stream").split(";")[0])
            total_bytes += size
        resource_map[req_url] = filename
        try:
            request_url = response.request.url
            if request_url != req_url:
                resource_map[request_url] = filename
        except Exception:
            pass

    await page.route("**/*", on_route)
    page.on("response", on_response)

    log_resource(job, url=url, status=0, mime="navigation", size=0)
    try:
        response = await _goto_article(
            page, url, bounce_origin, on_bounce_landed=_mark_bounce_landed
        )
    except PlaywrightTimeout as exc:
        await context.close()
        raise RuntimeError(f"Timed out loading {url}") from exc

    http_status = response.status if response else None
    await _settle_page(page)

    final_url = page.url
    try:
        validate_public_http_url(final_url)
    except ValueError as exc:
        await context.close()
        raise RuntimeError(f"Redirected to a blocked address: {exc}") from exc

    title = await page.title()
    html = await page.content()
    article = extract_article(html, final_url)
    return _Visit(
        html=html,
        article=article,
        final_url=final_url,
        title=title,
        http_status=http_status,
        resource_map=resource_map,
        resource_bodies=resource_bodies,
        page=page,
        context=context,
        bounce_origin=bounce_origin,
    )


async def worker() -> None:
    assert job_queue is not None
    while True:
        job_id = await job_queue.get()
        try:
            await run_job(job_id)
        except Exception as exc:
            job = jobs.get(job_id)
            if job:
                job["status"] = "failed"
                job["error"] = str(exc)
                db.update_snapshot(job["snapshot_id"], status="failed", error=str(exc)[:500])
        finally:
            job_queue.task_done()


async def run_job(job_id: str) -> None:
    job = jobs[job_id]
    sid = job["snapshot_id"]
    url = job["url"]
    job["status"] = "capturing"
    db.update_snapshot(sid, status="capturing")

    folder = db.snap_dir(sid)
    res_dir = folder / "res"
    folder.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)

    visit: _Visit | None = None
    html = ""
    final_url = url
    title = ""
    http_status = None
    article: dict = {}
    resource_map: dict[str, str] = {}
    resource_bodies: dict[str, tuple[bytes, str]] = {}
    screenshot = b""
    bounce_used: str | None = None
    retried = False
    reader = ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            visit = await _capture_visit(browser, job, url)
            article = visit.article
            crawler_html = await fetch_crawler_html(visit.final_url or url)
            if crawler_html:
                crawler_article = extract_article(crawler_html, visit.final_url or url)
                article = _merge_crawler_article(article, crawler_article)
                visit.article = article

            if article.get("paywalled"):
                for origin in bounce_origins_for(url, visit.final_url):
                    retried = True
                    try:
                        attempt = await _capture_visit(
                            browser, job, url, bounce_origin=origin
                        )
                    except Exception:
                        continue
                    if _richer_article(attempt.article, visit.article):
                        await visit.close()
                        visit = attempt
                    else:
                        await attempt.close()
                    if not visit.article.get("paywalled"):
                        break

            article = visit.article
            if retried:
                article["referrer_retried"] = True
            html = visit.html
            final_url = visit.final_url
            title = visit.title
            http_status = visit.http_status
            resource_map = visit.resource_map
            resource_bodies = visit.resource_bodies
            bounce_used = visit.bounce_origin
            reader = build_reader_html(article, url)
            if article.get("paywalled"):
                await visit.page.set_content(reader, wait_until="domcontentloaded")
                await visit.page.wait_for_timeout(250)
            try:
                screenshot = await visit.page.screenshot(full_page=True, type="jpeg", quality=82)
            except Exception:
                screenshot = await visit.page.screenshot(full_page=False, type="jpeg", quality=82)
        finally:
            if visit is not None:
                await visit.close()
            await browser.close()

    for filename, (body, _ctype) in resource_bodies.items():
        if filename.endswith(".css"):
            css_url = next((u for u, n in resource_map.items() if n == filename), final_url)
            try:
                text = body.decode("utf-8", errors="replace")
                text = rewrite_css(text, css_url, resource_map, sid)
                (res_dir / filename).write_text(text, encoding="utf-8")
                continue
            except Exception:
                pass
        (res_dir / filename).write_bytes(body)

    frozen = freeze_html(html, final_url, resource_map, sid)

    (folder / "page.html").write_text(frozen, encoding="utf-8")
    (folder / "reader.html").write_text(reader, encoding="utf-8")
    (folder / "article.html").write_text(article.get("article_html") or "", encoding="utf-8")
    (folder / "article.txt").write_text(article.get("article_text") or "", encoding="utf-8")
    (folder / "screenshot.jpg").write_bytes(screenshot)

    try:
        im = Image.open(io.BytesIO(screenshot))
        im.thumbnail((480, 360))
        im.convert("RGB").save(folder / "thumb.jpg", "JPEG", quality=70)
    except Exception:
        pass

    meta = {
        "id": sid,
        "url": url,
        "final_url": final_url,
        "http_status": http_status,
        "title": article.get("title") or title,
        "site_name": article.get("site_name"),
        "author": article.get("author"),
        "published_at": article.get("published_at"),
        "description": article.get("description"),
        "word_count": article.get("word_count"),
        "dek": article.get("dek"),
        "author_image": article.get("author_image"),
        "paywalled": article.get("paywalled"),
        "referrer_retried": bool(retried),
        "referrer_bounce": bounce_used,
        "resources": resource_map,
        "created_at": db.now_iso(),
    }
    db.write_json(folder / "meta.json", meta)
    db.update_snapshot(
        sid,
        final_url=final_url,
        title=meta["title"],
        site_name=meta["site_name"],
        author=meta["author"],
        published_at=meta["published_at"],
        description=meta["description"],
        http_status=http_status,
        word_count=meta["word_count"],
        paywalled=1 if meta.get("paywalled") else 0,
        status="complete",
        error=None,
    )
    job["status"] = "complete"
    job["title"] = meta["title"]
