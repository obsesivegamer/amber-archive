import json
from pathlib import Path

from bs4 import BeautifulSoup

html = Path("data/_googlebot.html").read_text(encoding="utf-8", errors="replace")
soup = BeautifulSoup(html, "lxml")
for s in soup.select("script.js-react-on-rails-component"):
    if s.get("data-component-name") != "Article":
        continue
    data = json.loads(s.string or s.get_text() or "")
    art = data["article"]
    print("article keys:")
    for k, v in art.items():
        kind = type(v).__name__
        extra = ""
        if isinstance(v, str):
            extra = f" len={len(v)}"
        elif v is None:
            extra = " None"
        elif isinstance(v, bool):
            extra = f" {v}"
        print(f"  {k}: {kind}{extra}")
    print("showUpsellPaywall", data.get("showUpsellPaywall"))
    print("initialCurrentUser keys", list((data.get("initialCurrentUser") or {}).keys())[:20])
    user = data.get("initialCurrentUser") or {}
    print("logged in?", bool(user.get("id") or user.get("email")))
    print("user email/id", user.get("email"), user.get("id"), user.get("subscriber"))
