from pathlib import Path

import pytest

from app.extract import extract_article, sanitize_article_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"
POLITICO_RELATED_CARD = (FIXTURES / "politico_related_card.html").read_text(encoding="utf-8")
NYT_BEETS_CHROME = (FIXTURES / "nyt_beets_chrome.html").read_text(encoding="utf-8")
FULL_ARTICLE_TOKEN = "TOKEN_FULL_ARTICLE"
CARD_TEASER_TOKEN = "CARD_TEASER_TOKEN"
BEETS_BODY_TOKEN = "TOKEN_BEETS_BODY"
BEETS_RECIPE_TOKEN = "TOKEN_BEETS_RECIPE"

NEWS = """
<!doctype html>
<html>
<head>
  <title>Ignore</title>
  <meta property="og:title" content="City Council Approves Bridge">
  <meta property="og:site_name" content="The Daily Test">
  <meta property="og:description" content="The city council voted last night to fund a new bridge over the river. Construction starts in May after a decade of debate.">
  <meta name="author" content="Jane Reporter">
  <script type="application/ld+json">
  {"@type":"NewsArticle","headline":"City Council Approves Bridge","author":{"name":"Jane Reporter"},"datePublished":"2026-04-01"}
  </script>
</head>
<body>
  <article>
    <h1>City Council Approves Bridge</h1>
    <p>The city council voted last night to fund a new bridge over the river.</p>
    <p>Construction starts in May after a decade of debate among residents.</p>
  </article>
</body>
</html>
"""

PAYWALL = """
<!doctype html>
<html>
<head>
  <meta property="og:title" content="Exclusive: Elon Musk Asks Tesla Staff to Move to Using Grok">
  <meta property="og:site_name" content="The Information">
  <meta property="og:description" content="Tesla CEO Elon Musk told staff at the carmaker to move to using Grok, the AI model from SpaceXAI, according to a memo sent to staff on Friday. Musk told staff that they should make the change when possible given Grok 4.5 lower token costs.">
  <script type="application/json" class="js-react-on-rails-component" data-component-name="Briefing">
  {"briefing":{"headline":"Exclusive: Elon Musk Asks Tesla Staff to Move to Using Grok","body":null,"dek":"Tesla staff have been testing beta versions of Grok in recent months.","authors":[{"name":"Grace Kay"}],"publishedAt":"Jul 10, 2026","source":{"name":"The Information"}}}
  </script>
</head>
<body>
  <h1>Exclusive: Elon Musk Asks Tesla Staff to Move to Using Grok</h1>
  <p>Read this briefing for free</p>
  <button>Continue with Google</button>
</body>
</html>
"""


def test_extracts_news_article_fields():
    got = extract_article(NEWS, "https://daily.test/bridge")
    assert got["title"] == "City Council Approves Bridge"
    assert got["site_name"] == "The Daily Test"
    assert got["author"] == "Jane Reporter"
    assert "voted last night" in got["article_text"]
    assert got["word_count"] > 10


ARCHIVE_IS_SAVED = """
<!doctype html>
<html>
<head>
  <meta property="og:title" content="Exclusive: Elon Musk Tells Tesla Staff to Move to Using Grok — The In…">
  <meta property="og:site_name" content="archive.is">
</head>
<body>
  <header>
    <h1>Exclusive: Elon Musk Tells Tesla Staff to Move to Using Grok</h1>
    <div>Tesla staff have been testing beta versions of Grok in recent months.</div>
    <div>By Grace Kay</div>
    <time>Jul 10, 2026, 1:02pm PDT</time>
    <div>Source: The Information</div>
  </header>
  <p>Tesla CEO Elon Musk told staff at the carmaker to move to using Grok.</p>
  <p>The move follows Tesla setting a $200 weekly limit on employee's AI spending on Monday.</p>
  <p>SpaceXAI's product lead Andrew Milich has also been working closely with staff at Tesla.</p>
</body>
</html>
"""


def test_extracts_full_archive_is_saved_article():
    got = extract_article(ARCHIVE_IS_SAVED, "https://archive.is/5QUGY")
    assert got["title"] == "Exclusive: Elon Musk Tells Tesla Staff to Move to Using Grok"
    assert "beta versions of Grok" in (got.get("dek") or "")
    assert got["author"] == "Grace Kay"
    assert "Jul 10, 2026" in (got.get("published_at") or "")
    assert got["site_name"] == "The Information"
    assert "weekly limit" in got["article_text"]
    assert "Andrew Milich" in got["article_text"]
    assert got["word_count"] > 30


def test_paywall_falls_back_to_lede_not_cta():
    got = extract_article(PAYWALL, "https://www.theinformation.com/briefings/x")
    assert "Elon Musk" in got["title"]
    assert got["author"] == "Grace Kay"
    assert got["site_name"] == "The Information"
    assert "token costs" in got["article_text"]
    assert "Continue with Google" not in got["article_text"]
    assert got["word_count"] > 20
    assert got["paywalled"] is True
    assert "beta versions of Grok" in (got.get("dek") or "")


LOCKED_ARTICLE = """
<!doctype html>
<html>
<head>
  <meta property="og:title" content="SpaceX Shakes Up Data Center Leadership After Aggressive Build-Out">
  <meta property="og:site_name" content="The Information">
  <meta property="og:description" content="Elon Musk has shaken up the SpaceX team building data centers.">
  <script type="application/json" class="js-react-on-rails-component" data-component-name="Article">
  {"article":{"title":"SpaceX Shakes Up Data Center Leadership After Aggressive Build-Out","subTitle":"The shuffle follows a year of rapid construction.","access":"locked","fullText":null,"freeBlurb":"<p>Elon Musk has shaken up the SpaceX team building data centers after an aggressive expansion that stretched crews and budgets. FREEBLURB_TOKEN_UKrA8 The company moved several program leads in recent weeks, according to people familiar with the changes, and asked remaining managers to slow new site starts until power contracts catch up.</p><p>Colleagues said the reshuffle was meant to keep construction from outrunning the electrical interconnects that each campus needs. Several planned buildings in the southern U.S. are still waiting on utility upgrades, and the free blurb in this fixture is long enough to stand in for the teaser The Information already ships in the page JSON.</p><p>A locked visitor still receives this freeBlurb even when fullText is null, so Amber should keep these paragraphs instead of the short Open Graph line above.</p>"}}
  </script>
</head>
<body>
  <h1>SpaceX Shakes Up Data Center Leadership After Aggressive Build-Out</h1>
  <p>Sign in</p>
  <button>Subscribe</button>
</body>
</html>
"""


def test_react_fulltext_aside_stays_in_the_extract():
    """Sanitize must not drop a unique aside from rails fullText under 80 words."""
    aside = " ".join(
        f"Aside sentence {i} about the interconnect queue and crew rest."
        for i in range(10)
    )
    assert len(aside.split()) >= 40
    html = f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="SpaceX Shakes Up Data Center Leadership">
  <script type="application/json" class="js-react-on-rails-component" data-component-name="Article">
  {{"article":{{"title":"SpaceX Shakes Up Data Center Leadership","fullText":"<p>Lead sentence about the campus TOKEN_ASIDE_LEAD.</p><aside><p>{aside}</p></aside><p>Closing sentence TOKEN_ASIDE_CLOSE.</p>"}}}}
  </script>
</head>
<body><h1>SpaceX Shakes Up Data Center Leadership</h1><p>Sign in</p></body>
</html>
"""
    got = extract_article(html, "https://www.theinformation.com/articles/x")
    assert "TOKEN_ASIDE_LEAD" in got["article_text"]
    assert "Aside sentence 0" in got["article_text"]
    assert "TOKEN_ASIDE_CLOSE" in got["article_text"]
    assert got["word_count"] >= 80
    assert got["paywalled"] is False


def test_react_article_uses_free_blurb_not_short_og():
    got = extract_article(LOCKED_ARTICLE, "https://www.theinformation.com/articles/x")
    assert got["title"] == "SpaceX Shakes Up Data Center Leadership After Aggressive Build-Out"
    assert "rapid construction" in (got.get("dek") or "")
    assert "FREEBLURB_TOKEN_UKrA8" in got["article_text"]
    assert "short Open Graph line" in got["article_text"]
    assert got["word_count"] >= 90
    assert got["word_count"] > len(
        "Elon Musk has shaken up the SpaceX team building data centers.".split()
    )
    assert got["paywalled"] is False




def test_extract_uses_article_body_not_related_card():
    got = extract_article(POLITICO_RELATED_CARD, "https://daily.test/kallas")
    assert FULL_ARTICLE_TOKEN in got["article_text"]
    assert got["paywalled"] is False
    assert got["word_count"] >= 80
    assert got["title"].startswith("Inside the fightback")
    assert got["site_name"] == "Daily Test"
    assert got["author"] == "Jane Reporter"
    assert "Listen" not in got["article_text"]
    assert "Share via email" not in got["article_text"]
    assert "Share on X" not in got["article_text"]


def test_nyt_shaped_chrome_and_images_are_sanitized():
    got = extract_article(
        NYT_BEETS_CHROME,
        "https://www.nytimes.com/2026/08/10/well/eat/beets-health-benefits-recipes.html",
    )
    text = got["article_text"]
    html = got["article_html"]
    assert got["title"] == "How Healthy Are Beets?"
    assert got["author"] == "Simar Bajaj"
    assert got["site_name"] == "The New York Times"
    assert BEETS_BODY_TOKEN in text
    assert BEETS_RECIPE_TOKEN in text
    assert "eye-popping colors" in (got.get("dek") or text)
    assert "By Simar Bajaj" in text
    assert "David Chow" in text
    assert "roasted beets on a wooden table" in text
    assert "Beet hummus in a bowl" in text
    assert "beets-hero.jpg" in html
    assert "beets-bowl.jpg" in html
    assert 'src="https://static.example/beets-bowl.jpg"' in html
    assert "Share full article" not in text
    assert "Share full article" not in html
    assert "Listen" not in text
    assert "6:27" not in text
    assert "Leer en español" not in text
    assert "Gift this article" not in text
    assert "More in Well" not in text
    assert "sleep trackers" not in text
    assert "width:1600" not in html
    assert "float:left" not in html
    assert "float:right" not in html
    assert 'style="' not in html
    assert got["paywalled"] is False
    assert got["word_count"] >= 80
    assert text.lower().count("share") == 0


def test_sanitize_is_idempotent_on_nyt_extract():
    got = extract_article(
        NYT_BEETS_CHROME,
        "https://www.nytimes.com/2026/08/10/well/eat/beets-health-benefits-recipes.html",
    )
    again = sanitize_article_html(got["article_html"])
    assert BEETS_BODY_TOKEN in again
    assert "Share full article" not in again
    assert "beets-bowl.jpg" in again


def test_sanitize_keeps_long_aside_nav_video_and_footer():
    quote = " ".join(
        f"Pull quote word{i} about nitrate and blood pressure in beets."
        for i in range(8)
    )
    toc = " ".join(
        f"Section heading {i} covering soil, harvest, and roasting."
        for i in range(25)
    )
    correction = " ".join(
        f"Correction word{i} on the date of the garden trial."
        for i in range(8)
    )
    assert len(quote.split()) >= 40
    assert len(toc.split()) >= 40
    assert len(correction.split()) >= 40
    html = f"""
    <article>
      <p>Lead paragraph about beets TOKEN_BEETS_BODY in ordinary food.</p>
      <aside><p>{quote}</p></aside>
      <nav><p>{toc}</p></nav>
      <video src="https://static.example/beets.mp4"></video>
      <footer><p>{correction}</p></footer>
      <aside><p>More in Well</p></aside>
      <button>Listen · 6:27 min</button>
      <div role="toolbar"><a href="/share">Share full article</a></div>
      <p>Closing paragraph TOKEN_BEETS_RECIPE stays in the extract.</p>
    </article>
    """
    got = sanitize_article_html(html)
    assert "Pull quote word0" in got
    assert "Section heading 0" in got
    assert "beets.mp4" in got
    assert "Correction word0" in got
    assert "TOKEN_BEETS_BODY" in got
    assert "TOKEN_BEETS_RECIPE" in got
    assert "More in Well" not in got
    assert "Listen · 6:27" not in got
    assert "Share full article" not in got


def test_sanitize_keeps_rails_aside_prose_above_paywall_line():
    """A JSON fullText aside with unique prose must not be stripped under 80 words."""
    aside = " ".join(
        f"Aside sentence {i} about the interconnect queue and crew rest."
        for i in range(10)
    )
    assert len(aside.split()) >= 40
    html = (
        "<p>Lead sentence about the campus.</p>"
        f"<aside><p>{aside}</p></aside>"
        "<p>Closing sentence.</p>"
    )
    got = sanitize_article_html(html)
    assert "Aside sentence 0" in got
    from app.extract import _prose_word_count

    assert _prose_word_count(got) >= 40


def test_sanitize_promotes_spacer_gif_to_data_src():
    html = (
        '<p>TOKEN_BEETS_BODY</p>'
        '<img src="https://static.example/spacer.gif" '
        'data-src="https://static.example/beets-real.jpg" alt="Beets">'
    )
    got = sanitize_article_html(html)
    assert "beets-real.jpg" in got
    assert 'src="https://static.example/beets-real.jpg"' in got
    assert "spacer.gif" not in got


def test_sanitize_keeps_svg_chart_outside_figure():
    slices = "".join(f'<path d="M{i} 0 L{i} 10"></path>' for i in range(10))
    html = (
        "<p>TOKEN_BEETS_BODY</p>"
        f'<svg viewBox="0 0 100 40"><title>Beet yield</title>{slices}</svg>'
        '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M0 0h16v16H0z"></path></svg>'
    )
    got = sanitize_article_html(html)
    assert "Beet yield" in got
    assert got.count("<svg") == 1


def test_share_listen_chrome_does_not_inflate_word_count():
    """Toolbar copy must not push a short teaser over the paywall line."""
    chrome = """
    <button>Listen · 6:27 min</button>
    <div role="toolbar">
      <a href="https://x.com/intent/post/?url=https://daily.test/x">Share full article</a>
      <button aria-label="Gift this article">Gift</button>
    </div>
    <p>Listen</p>
    <p>Share full article</p>
    <p>Leer en español</p>
    """
    body = (
        "Beets are a root vegetable with pigment that stains the board. "
        "TOKEN_TEASER_ONLY Doctors mention nitrate and blood pressure in passing."
    )
    html = f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="How Healthy Are Beets?">
  <meta property="og:description" content="A short dek for a locked page.">
</head>
<body>
  <article>
    <h1>How Healthy Are Beets?</h1>
    {chrome}
    <p>{body}</p>
  </article>
</body>
</html>
"""
    assert len(body.split()) < 80
    got = extract_article(html, "https://daily.test/beets")
    assert got["paywalled"] is True
    assert got["word_count"] < 80
    assert "TOKEN_TEASER_ONLY" in got["article_text"]
    assert "Share full article" not in got["article_text"]
    assert "Listen" not in got["article_text"]
    assert "Leer en español" not in got["article_text"]


def test_teaser_plus_related_cards_stays_paywalled():
    """Safety for the enumerated strip list only (`.article-card`, `.content-listing`).

    Off-list names like `story-card` / `related-stories` can still inflate —
    that limit is pre-existing on main (readability picks those cards too).
    Do not widen `_RECIRC_SELECTORS` without a tight, tested reason.
    """
    cards = "\n".join(
        f'<div class="article-card"><div class="card__excerpt">'
        f"<p>{CARD_TEASER_TOKEN} Related blurb {i} about a different story "
        f"that is long enough to look like body copy if cards are counted.</p>"
        f"</div></div>"
        for i in range(8)
    )
    html = f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="Locked teaser only">
  <meta property="og:description" content="A short dek for a locked page.">
</head>
<body>
  <article>
    <h1>Locked teaser only</h1>
    <p>Subscribe to continue reading this piece.</p>
    <div class="content-listing">{cards}</div>
  </article>
</body>
</html>
"""
    got = extract_article(html, "https://daily.test/locked")
    assert got["paywalled"] is True
    assert FULL_ARTICLE_TOKEN not in got["article_text"]
    assert got["word_count"] < 80


def test_teaser_plus_link_dense_list_stays_paywalled():
    """Regression: a whole-`<article>` dump must not beat readability on link chrome.

    A teaser plus a "Most read" `<a>` list and a newsletter blurb (no classes
    on `_RECIRC_SELECTORS`) stayed paywalled on main (~35 words) so the PR #3
    bounce could fire. Raw-word-count `_prefer_richer_html` flipped it to a
    complete 142-word chrome dump and skipped the bounce.
    """
    chrome = "".join(
        f"<li><a href='/x{i}'>Most read headline number {i} covering an unrelated "
        f"political development in some member state this particular week</a></li>"
        for i in range(6)
    )
    html = f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="Locked teaser only">
  <meta property="og:description" content="A short dek for a locked page.">
</head>
<body>
  <article>
    <h1>Locked teaser only</h1>
    <p>Subscribe to continue reading this piece.</p>
    <div class="most-read"><h2>Most read</h2><ul>{chrome}</ul></div>
    <div class="newsletter-signup"><p>Sign up for our daily newsletter to get the most
    important stories delivered straight to your inbox every morning, curated by our
    editors across every bureau in the world.</p></div>
  </article>
</body>
</html>
"""
    got = extract_article(html, "https://daily.test/locked")
    assert got["paywalled"] is True
    assert got["word_count"] < 80
    assert FULL_ARTICLE_TOKEN not in got["article_text"]
    assert "Most read headline number 0" not in got["article_text"]


COMMENT_BODY = (
    "Reader comment {i} about the diplomats and the reform plan that should "
    "not count as article body copy at all."
)


def _teaser_plus_module(class_name: str) -> str:
    comments = " ".join(COMMENT_BODY.format(i=i) for i in range(8))
    return f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="Locked teaser only">
  <meta property="og:description" content="A short dek for a locked page.">
</head>
<body>
  <article>
    <h1>Locked teaser only</h1>
    <p>Subscribe to continue reading this piece about the diplomatic service overhaul.</p>
    <div class="{class_name}">
      <h2>Comments</h2>
      <p>{comments}</p>
    </div>
  </article>
</body>
</html>
"""


def test_teaser_plus_reader_comments_stays_paywalled():
    """Whole-`<article>` fallback must not count comments as body copy.

    On main, readability keeps the teaser (~56 words) so bounce can fire.
    Counting every non-anchor word in `<article>` stored the comments and
    skipped the bounce (Astra: 155 words / not paywalled).
    """
    for class_name in (
        "comments",
        "reader-comments",
        "promo",
        "footer",
        "related-stories",
    ):
        got = extract_article(
            _teaser_plus_module(class_name), "https://daily.test/locked"
        )
        assert got["paywalled"] is True, class_name
        assert got["word_count"] < 80, (class_name, got["word_count"])
        assert "Reader comment 0" not in got["article_text"], class_name
        assert FULL_ARTICLE_TOKEN not in got["article_text"]


NESTED_TEASER = (
    "Subscribe to continue reading this piece about the diplomatic service "
    "overhaul after capitals pushed back on the draft this autumn. Officials "
    "said the rewrite would move desk officers into the Commission and leave "
    "only a thin coordination layer across from the roundabout this week after "
    "the ambassadors met in the building near the square."
)


def test_nested_article_content_does_not_double_count():
    """Parent + child `.article__content` must not join the same prose twice."""
    assert 50 <= len(NESTED_TEASER.split()) < 80
    html = f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="Locked teaser only">
  <meta property="og:description" content="A short dek for a locked page.">
</head>
<body>
  <article>
    <h1>Locked teaser only</h1>
    <div class="article__content">
      <div class="article__content">
        <p>{NESTED_TEASER}</p>
      </div>
    </div>
  </article>
</body>
</html>
"""
    got = extract_article(html, "https://daily.test/locked")
    text = got["article_text"] or ""
    assert text.count("Subscribe to continue") <= 1
    assert got["paywalled"] is True
    assert got["word_count"] < 80
    assert got["word_count"] < 2 * len(NESTED_TEASER.split())


SPONSOR_FILLER = " ".join(
    f"Sponsor blurb {i} promoting an unrelated product that is not the article "
    f"body and must not count toward the extract word count at all today."
    for i in range(12)
)


def _teaser_content_root_then_sponsor(*, wrap_in_footer: bool, extra_class: str = "") -> str:
    cls = "article__content" + (f" {extra_class}" if extra_class else "")
    content = f'<div class="{cls}"><p>{NESTED_TEASER}</p></div>'
    if wrap_in_footer:
        content = f"<footer>{content}</footer>"
    return f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="Locked teaser only">
  <meta property="og:description" content="A short dek for a locked page.">
</head>
<body>
  <article>
    <h1>Locked teaser only</h1>
    <p>Subscribe to continue reading this piece.</p>
    {content}
    <div class="sponsor-rail">{SPONSOR_FILLER}</div>
  </article>
</body>
</html>
"""


def test_stripped_content_root_does_not_widen_to_whole_article():
    """If chrome strip removes every `.article__content`, do not dump `<article>`.

    At 9a85147 a 52-word teaser nested in `<footer>` (or classed
    `article__content comments`) left `roots` empty, so the fallback counted
    off-list sponsor filler and flipped Complete (~282 words). Main and
    703e71e stayed paywalled because they scoped to the content root.
    """
    assert len(SPONSOR_FILLER.split()) >= 200
    for html in (
        _teaser_content_root_then_sponsor(wrap_in_footer=True),
        _teaser_content_root_then_sponsor(wrap_in_footer=False, extra_class="comments"),
    ):
        got = extract_article(html, "https://daily.test/locked")
        assert got["paywalled"] is True
        assert got["word_count"] < 80
        assert "Sponsor blurb 0" not in got["article_text"]
        assert FULL_ARTICLE_TOKEN not in got["article_text"]


@pytest.mark.parametrize("word_count", [79, 80, 81])
def test_stripped_sibling_does_not_inflate_article(word_count):
    """Removed content roots must not add words or flip the incomplete flag."""
    body = " ".join(f"word{i}" for i in range(word_count))
    html = f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="Inside the fightback">
  <meta property="og:site_name" content="Daily Test">
</head>
<body>
  <article>
    <div class="article__content"><p>{body}</p></div>
    <div class="comments">
      <div class="article__content"><p>{NESTED_TEASER}</p></div>
    </div>
  </article>
</body>
</html>
"""
    got = extract_article(html, "https://daily.test/kallas")
    assert got["article_text"] == body
    assert got["word_count"] == word_count
    assert got["paywalled"] is (word_count < 80)
    assert "<>" not in got["article_html"]
    assert "&lt;&gt;" not in got["article_html"]


def _outer_teaser_plus_nested_article(wrapper_open: str, wrapper_close: str, inner: str) -> str:
    """Fable P4 shape: teaser on the outer article; filler only in a nested `<article>`."""
    return f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="Locked teaser only">
  <meta property="og:description" content="A short dek for a locked page.">
</head>
<body>
  <article>
    <h1>Locked teaser only</h1>
    <p>{NESTED_TEASER}</p>
    {wrapper_open}
      <article>{inner}</article>
    {wrapper_close}
  </article>
</body>
</html>
"""


def test_nested_article_in_footer_or_comments_stays_paywalled():
    """An inner `<article>` inside chrome must not re-enter as its own host.

    P3 drops that chrome from the outer host, then `find_all("article")`
    clones the inner node in isolation — nothing left to strip — so
    sponsor filler becomes the extract (Fable: ~300 / Complete; main ~54).
    """
    inner = f"<p>{SPONSOR_FILLER}</p>"
    for html in (
        _outer_teaser_plus_nested_article("<footer>", "</footer>", inner),
        _outer_teaser_plus_nested_article('<div class="comments">', "</div>", inner),
    ):
        got = extract_article(html, "https://daily.test/locked")
        assert got["paywalled"] is True
        assert got["word_count"] < 80
        assert "Sponsor blurb 0" not in got["article_text"]
        assert FULL_ARTICLE_TOKEN not in got["article_text"]


def test_comment_article_inside_comments_section_stays_paywalled():
    """`<section class="comments"><article>` with an 80+ word comment stays a teaser."""
    comment = " ".join(
        f"Reader remark {i} about an unrelated diplomatic fight that is not the story."
        for i in range(10)
    )
    assert len(comment.split()) >= 80
    html = _outer_teaser_plus_nested_article(
        '<section class="comments">',
        "</section>",
        f"<p>{comment}</p>",
    )
    got = extract_article(html, "https://daily.test/locked")
    assert got["paywalled"] is True
    assert got["word_count"] < 80
    assert "Reader remark 0" not in got["article_text"]


def _teaser_plus_unlikely_chrome(open_tag: str, close_tag: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="Locked teaser only">
  <meta property="og:description" content="A short dek for a locked page.">
</head>
<body>
  <article>
    <h1>Locked teaser only</h1>
    <p>{NESTED_TEASER}</p>
    {open_tag}<p>{SPONSOR_FILLER}</p>{close_tag}
  </article>
</body>
</html>
"""


def test_id_comments_and_substring_chrome_stay_paywalled():
    """Dump path must drop chrome readability already rejects by class+id.

    Exact-token `_ARTICLE_CHROME_SELECTORS` miss `id="comments"`,
    `comment-list`, `id="footer"`, `sponsored`, `sidebar`, `commentbox`.
    Those score ~257+ and beat the teaser (main stays ~57 / paywalled).
    """
    shapes = (
        ('<div id="comments">', "</div>"),
        ('<ol class="comment-list">', "</ol>"),
        ('<div id="footer">', "</div>"),
        ('<div class="sponsored">', "</div>"),
        ('<div class="sidebar">', "</div>"),
        ('<div class="commentbox">', "</div>"),
    )
    for open_tag, close_tag in shapes:
        got = extract_article(
            _teaser_plus_unlikely_chrome(open_tag, close_tag),
            "https://daily.test/locked",
        )
        assert got["paywalled"] is True, open_tag
        assert got["word_count"] < 80, (open_tag, got["word_count"])
        assert "Sponsor blurb 0" not in got["article_text"], open_tag


def _wp_comment_articles() -> str:
    """WordPress wraps each comment in `<article class="comment-body">`."""
    return (
        '<ol class="comment-list">'
        f'<li class="comment"><article class="comment-body"><p>{SPONSOR_FILLER}</p></article></li>'
        "</ol>"
    )


def _teaser_page(body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta property="og:title" content="Locked teaser only">
  <meta property="og:description" content="A short dek for a locked page.">
</head>
<body>
{body}
</body>
</html>
"""


def test_wp_comment_articles_inside_unlikely_chrome_stay_paywalled():
    """Unlikely chrome that wraps `<article>` / `<main>` must still be dropped.

    `_is_unlikely_chrome` used to spare those wrappers (an escape
    readability-lxml does not have). WordPress comments then survived,
    beat the teaser, and skipped bounce (Fable: Complete with junk;
    main ~57 / paywalled).
    """
    comments = _wp_comment_articles()
    promo = f'<article class="promo-card"><p>{SPONSOR_FILLER}</p></article>'
    shapes = {
        "nested": _teaser_page(
            f"<article><h1>Locked teaser only</h1><p>{NESTED_TEASER}</p>"
            f'<div id="comments">{comments}</div></article>'
        ),
        "sibling": _teaser_page(
            f"<article><h1>Locked teaser only</h1><p>{NESTED_TEASER}</p></article>"
            f'<div id="comments">{comments}</div>'
        ),
        "sidebar": _teaser_page(
            f"<article><h1>Locked teaser only</h1><p>{NESTED_TEASER}</p></article>"
            f'<div class="sidebar">{promo}{promo}</div>'
        ),
        "wraps-main": _teaser_page(
            f"<article><h1>Locked teaser only</h1><p>{NESTED_TEASER}</p>"
            f'<div id="comments"><main><p>{SPONSOR_FILLER}</p></main></div></article>'
        ),
    }
    for name, html in shapes.items():
        got = extract_article(html, "https://daily.test/locked")
        assert got["paywalled"] is True, name
        assert got["word_count"] < 80, (name, got["word_count"])
        assert "Sponsor blurb 0" not in got["article_text"], name
        assert FULL_ARTICLE_TOKEN not in got["article_text"], name
