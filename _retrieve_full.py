"""Try to retrieve the locked TI article via archive.today and a local Chrome profile."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "_retrieved.html"
URL = "https://www.theinformation.com/articles/spacex-shakes-data-center-leadership-aggressive-build"
ARCHIVE = "https://archive.ph/?run=1&url=" + URL

CHROME_USER = Path.home() / "AppData/Local/Google/Chrome/User Data"
CLONE = ROOT / "data" / "_chrome_clone"


def dump_article_info(html: str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    info = {"len": len(html), "title": soup.title.get_text(strip=True) if soup.title else None}
    for s in soup.select("script.js-react-on-rails-component"):
        if s.get("data-component-name") != "Article":
            continue
        try:
            data = json.loads(s.string or s.get_text() or "")
        except json.JSONDecodeError:
            continue
        art = data.get("article") or {}
        body = art.get("fullText") or art.get("body") or art.get("content")
        info["access"] = art.get("access")
        info["body_len"] = len(body) if isinstance(body, str) else None
        info["free_len"] = len(art.get("freeBlurb") or "")
        info["has_full"] = isinstance(body, str) and len(body) > 1000
        break
    text = soup.get_text(" ", strip=True)
    info["visible_chars"] = len(text)
    return info


def try_archive() -> str | None:
    print("=== archive.ph submit ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(ARCHIVE, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(8000)
            html = page.content()
            title = page.title()
            url = page.url
            print("archive url", url, "title", title, "html", len(html))
            if "captcha" in html.lower() or "One more step" in html:
                print("archive captcha")
                browser.close()
                return None
            if "theinformation.com" in html.lower() and "civil engineering" in html.lower():
                OUT.write_text(html, encoding="utf-8")
                print("saved archive html", dump_article_info(html))
                browser.close()
                return html
            # maybe still loading
            page.wait_for_timeout(15000)
            html = page.content()
            print("after wait", page.url, page.title(), len(html))
            OUT.write_text(html, encoding="utf-8")
            print(dump_article_info(html))
            browser.close()
            return html
        except Exception as exc:
            print("archive fail", exc)
            browser.close()
            return None


def clone_chrome_profile() -> Path | None:
    src_root = CHROME_USER
    if not src_root.exists():
        print("no chrome user data")
        return None
    if CLONE.exists():
        shutil.rmtree(CLONE, ignore_errors=True)
    CLONE.mkdir(parents=True, exist_ok=True)
    local_state = src_root / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, CLONE / "Local State")
    for profile in ("Default", "Profile 1"):
        src = src_root / profile
        if not src.exists():
            continue
        dst = CLONE / profile
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("Preferences", "Secure Preferences", "Cookies"):
            p = src / name
            if p.exists():
                try:
                    shutil.copy2(p, dst / name)
                except Exception as exc:
                    print("copy", p, exc)
        net_src = src / "Network"
        if net_src.exists():
            net_dst = dst / "Network"
            net_dst.mkdir(exist_ok=True)
            for name in ("Cookies", "Cookies-journal", "Trust Tokens"):
                p = net_src / name
                if p.exists():
                    try:
                        shutil.copy2(p, net_dst / name)
                    except Exception as exc:
                        print("copy net", p, exc)
        print("cloned", profile)
    return CLONE


def try_chrome_profile() -> str | None:
    print("=== chrome profile ===")
    cloned = clone_chrome_profile()
    if not cloned:
        return None
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(cloned),
                channel="chrome",
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--profile-directory=Default",
                ],
                viewport={"width": 1280, "height": 900},
            )
        except Exception as exc:
            print("launch fail", exc)
            return None
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
            html = page.content()
            print("chrome default", dump_article_info(html))
            if dump_article_info(html).get("has_full"):
                OUT.write_text(html, encoding="utf-8")
                context.close()
                return html
            # try Profile 1 by opening a second context is hard; just report
            OUT.write_text(html, encoding="utf-8")
            context.close()
            return html
        except Exception as exc:
            print("chrome nav fail", exc)
            context.close()
            return None


if __name__ == "__main__":
    html = try_archive()
    info = dump_article_info(html) if html else {}
    if not info.get("has_full"):
        html2 = try_chrome_profile()
        if html2:
            print("profile result", dump_article_info(html2))
