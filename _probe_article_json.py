import json
from pathlib import Path

from bs4 import BeautifulSoup

html = Path("data/_googlebot.html").read_text(encoding="utf-8", errors="replace")
soup = BeautifulSoup(html, "lxml")
for s in soup.select("script.js-react-on-rails-component"):
    if s.get("data-component-name") != "Article":
        continue
    data = json.loads(s.string or s.get_text() or "")

    def walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}.{k}" if path else k
                if k.lower() in {
                    "body",
                    "content",
                    "html",
                    "text",
                    "dek",
                    "teaser",
                    "standfirst",
                    "excerpt",
                    "url",
                    "api",
                    "gated",
                    "paywall",
                    "accessible",
                    "locked",
                } or (
                    isinstance(v, str) and len(v) > 80 and k.lower() not in {"image", "src", "href"}
                ):
                    preview = v if not isinstance(v, str) else v[:160]
                    print(p, type(v).__name__, preview)
                walk(v, p)
        elif isinstance(obj, list) and len(obj) < 40:
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")

    print("top keys", list(data.keys()))
    walk(data)
