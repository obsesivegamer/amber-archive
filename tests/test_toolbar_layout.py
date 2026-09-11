"""The article toolbar keeps the final URL legible beside the open-reader control."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from playwright.async_api import async_playwright

from app import db
from app.main import app

LONG_URL = "https://theintercept.com/2026/09/08/pentagon-openai-military-contract/"
# Widths where the open-reader control and the URL compete for one flex row.
WIDTHS = (1280, 900, 800, 430)
# 6rem still shows a host and an ellipsis. Below that the URL is decoration.
MIN_URL_PX = 96

MEASURE = """() => {
  const url = document.querySelector('.toolbar .orig');
  return {
    url_width: Math.round(url.getBoundingClientRect().width),
    scroll_width: document.documentElement.scrollWidth,
    truncated: url.scrollWidth > url.clientWidth,
    title: url.getAttribute('title'),
  };
}"""


@pytest.fixture
def viewer_url(tmp_data):
    sid = db.allocate_id()
    db.insert_snapshot(sid, LONG_URL, LONG_URL)
    db.update_snapshot(sid, status="complete", title="Story")
    folder = db.snap_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "reader.html").write_text(
        "<!doctype html><title>Story</title><p>Body.</p>", encoding="utf-8"
    )
    # Capture stats widen the meta column, which is what squeezes the URL.
    db.write_json(
        folder / "meta.json",
        {
            "capture_stats": {
                "blocked_resources": 2,
                "saved_resources": 22,
                "bytes_saved": 853_000,
                "duration_ms": 14_400,
            }
        },
    )

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError("viewer server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/{sid}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.browser
def test_final_url_stays_legible_next_to_open_reader(viewer_url):
    async def run() -> dict[int, dict]:
        measured: dict[int, dict] = {}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                for width in WIDTHS:
                    page = await browser.new_page(
                        viewport={"width": width, "height": 400}
                    )
                    await page.goto(viewer_url, wait_until="domcontentloaded")
                    await page.wait_for_selector(".toolbar .open-reader")
                    measured[width] = await page.evaluate(MEASURE)
                    await page.close()
            finally:
                await browser.close()
        return measured

    measured = asyncio.run(run())
    for width, box in measured.items():
        assert box["url_width"] >= MIN_URL_PX, (
            f"{width}px viewport: final URL shrank to {box['url_width']}px"
        )
        assert box["scroll_width"] <= width, (
            f"{width}px viewport: toolbar forced {box['scroll_width']}px of scroll"
        )
    for width, box in measured.items():
        if box["truncated"]:
            assert box["title"] == LONG_URL, (
                f"{width}px viewport: truncated URL has no tooltip"
            )
