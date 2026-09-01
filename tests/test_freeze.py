from bs4 import BeautifulSoup

from app.freeze import freeze_html, rewrite_css


SAMPLE = """
<!doctype html>
<html>
<head>
  <title>Story</title>
  <link rel="stylesheet" href="https://cdn.example.com/app.css">
  <link rel="preconnect" href="https://fonts.example.com">
  <script>alert('xss')</script>
</head>
<body>
  <h1 onclick="steal()">Headline</h1>
  <img src="https://cdn.example.com/hero.jpg" srcset="https://cdn.example.com/hero.jpg 1x">
  <a href="javascript:alert(1)">bad</a>
  <form action="/login"><input name="pw"><button>go</button></form>
  <iframe src="https://evil.example/ad"></iframe>
  <div style="background:url('https://cdn.example.com/hero.jpg')"></div>
</body>
</html>
"""

MAP = {
    "https://cdn.example.com/app.css": "aaaa.css",
    "https://cdn.example.com/hero.jpg": "bbbb.jpg",
}


def test_freeze_strips_scripts_and_handlers():
    out = freeze_html(SAMPLE, "https://news.example.com/a", MAP, "AbC12")
    soup = BeautifulSoup(out, "lxml")
    assert soup.find("script") is None
    assert soup.find("iframe") is None
    assert "onclick" not in str(soup.find("h1"))
    assert soup.find("a")["href"] == "#"
    form = soup.find("form")
    assert form["action"] == ""
    assert form.find("input").has_attr("disabled")
    assert soup.find("p", class_="amber-removed")


def test_freeze_rewrites_assets_to_local_snapshot_urls():
    out = freeze_html(SAMPLE, "https://news.example.com/a", MAP, "AbC12")
    soup = BeautifulSoup(out, "lxml")
    assert soup.find("link", rel="stylesheet")["href"] == "/AbC12/r/aaaa.css"
    assert soup.find("link", rel="preconnect") is None
    assert soup.find("img")["src"] == "/AbC12/r/bbbb.jpg"
    assert "/AbC12/r/bbbb.jpg" in soup.find("img")["srcset"]
    assert "/AbC12/r/bbbb.jpg" in soup.find("div")["style"]


def test_freeze_adds_csp_and_marker():
    out = freeze_html(SAMPLE, "https://news.example.com/a", MAP, "AbC12")
    soup = BeautifulSoup(out, "lxml")
    csp = soup.find("meta", attrs={"http-equiv": "Content-Security-Policy"})
    assert "script-src 'none'" in csp["content"]
    assert soup.find("meta", attrs={"name": "amber-snapshot"})["content"] == "AbC12"


def test_rewrite_css_keeps_data_and_fragments():
    css = "x{background:url(data:image/png;base64,aaa);mask:url(#clip)}"
    assert rewrite_css(css, "https://x/", {}, "id") == css
