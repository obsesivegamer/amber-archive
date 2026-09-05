"""Opt-in probe for soft-meter extract. Not run by pytest / default CI.

Compare readability's pick to Amber's extract on saved HTML, or capture a
live URL with Playwright (same path as Amber's first visit).

  python scripts/probe_soft_meter.py --html tests/fixtures/politico_related_card.html
  python scripts/probe_soft_meter.py --live https://www.politico.eu/article/...

Live capture is cookieless and does not clone a Chrome profile. Bounce is
not the unlock this script is checking — if readability misses the article
body, a Google referrer retry of the same HTML will miss it too.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from readability import Document

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.extract import extract_article  # noqa: E402


def _report(html: str, url: str) -> int:
    read = BeautifulSoup(
        Document(html).summary(html_partial=True) or "", "lxml"
    ).get_text(" ", strip=True)
    got = extract_article(html, url)
    print("url", url)
    print("readability_words", len(read.split()), "has_full_lead", "Kaja Kallas, the EU" in read or "TOKEN_FULL_ARTICLE" in read)
    print("readability_text", read[:220].replace("\n", " "))
    print("extract_words", got.get("word_count"), "paywalled", got.get("paywalled"))
    print("extract_text", (got.get("article_text") or "")[:220])
    if got.get("paywalled"):
        print("RESULT paywalled teaser — extract still looks incomplete")
        return 1
    print("RESULT not paywalled")
    return 0


async def _live(url: str) -> str:
    from playwright.async_api import async_playwright

    from app.capture import _capture_visit

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            visit = await _capture_visit(browser, {"resources": []}, url)
            try:
                return visit.html
            finally:
                await visit.close()
        finally:
            await browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--html", type=Path, help="Recorded HTML file")
    group.add_argument("--live", metavar="URL", help="Playwright first visit (opt-in)")
    args = parser.parse_args(argv)
    if args.html:
        html = args.html.read_text(encoding="utf-8", errors="replace")
        url = "https://daily.test/probe"
    else:
        html = asyncio.run(_live(args.live))
        url = args.live
    return _report(html, url)


if __name__ == "__main__":
    raise SystemExit(main())
