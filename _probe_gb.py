from pathlib import Path
import json
import re

from bs4 import BeautifulSoup

from app.extract import extract_article

html = Path("data/_googlebot.html").read_text(encoding="utf-8", errors="replace")
got = extract_article(
    html,
    "https://www.theinformation.com/articles/spacex-shakes-data-center-leadership-aggressive-build",
)
print("title", got.get("title"))
print("words", got.get("word_count"), "paywalled", got.get("paywalled"))
print("dek", got.get("dek"))
print("text", (got.get("article_text") or "")[:1500])
print("--- html p ---")
print((got.get("article_html") or "")[:800])

soup = BeautifulSoup(html, "lxml")
print("ror count", len(soup.select("script.js-react-on-rails-component")))
for s in soup.select("script.js-react-on-rails-component"):
    raw = s.string or s.get_text() or ""
    print("component", s.get("data-component-name"), "len", len(raw))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        continue
    briefing = data.get("briefing") or data.get("article") or data
    if isinstance(briefing, dict) and ("body" in briefing or "headline" in briefing or "title" in briefing):
        body = briefing.get("body") or briefing.get("content") or briefing.get("html")
        print("headline", briefing.get("headline") or briefing.get("title"))
        print("body type", type(body).__name__, "len", len(body) if isinstance(body, str) else body)

# search for long text nodes
texts = [t.strip() for t in soup.stripped_strings if len(t.strip()) > 120]
print("long strings", len(texts))
for t in texts[:12]:
    print(" S:", t[:200])
