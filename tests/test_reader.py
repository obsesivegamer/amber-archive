from app.reader import build_reader_html
from tests.test_extract import NYT_BEETS_CHROME

from app.extract import extract_article


def test_reader_layout_has_headline_dek_byline_and_body():
    html = build_reader_html(
        {
            "title": "Exclusive: Elon Musk Tells Tesla Staff to Move to Using Grok",
            "dek": "Tesla staff have been testing beta versions of Grok in recent months.",
            "author": "Grace Kay",
            "published_at": "Jul 10, 2026, 1:02pm PDT",
            "site_name": "The Information",
            "article_text": (
                "Tesla CEO Elon Musk told staff at the carmaker to move to using Grok, "
                "the AI model from SpaceXAI, according to a memo sent to staff on Friday.\n\n"
                "Musk told staff that they should make the change when possible given Grok 4.5’s "
                "lower token costs as compared to competitors, the memo said."
            ),
        },
        "https://www.theinformation.com/briefings/x",
    )
    assert "Exclusive: Elon Musk Tells Tesla Staff" in html
    assert "testing beta versions of Grok" in html
    assert "By Grace Kay" in html
    assert "Jul 10, 2026" in html
    assert "The Information" in html
    assert "<strong>Tesla CEO Elon Musk</strong>" in html
    assert "token costs" in html


def test_paywalled_reader_explains_the_teaser():
    html = build_reader_html(
        {
            "title": "SpaceX Shakes Up Data Center Leadership After Aggressive Build-Out",
            "author": "Grace Kay",
            "site_name": "The Information",
            "article_text": "Elon Musk has shaken up the SpaceX team ...",
            "paywalled": True,
        },
        "https://www.theinformation.com/articles/x",
    )
    assert "short preview" in html
    assert "Import" in html
    assert "retried Google/X referrer" not in html


def test_paywalled_reader_mentions_referrer_retry_when_it_happened():
    html = build_reader_html(
        {
            "title": "Inside Kallas’ fightback",
            "author": "Nicholas Vinocur",
            "site_name": "POLITICO",
            "article_text": "A logged-out teaser sentence.",
            "paywalled": True,
            "referrer_retried": True,
        },
        "https://www.politico.eu/article/example",
    )
    assert "retried Google/X referrer visits" in html
    assert "short preview" in html
    assert "Import" in html


def test_reader_constrains_images_and_drops_publisher_chrome():
    article = extract_article(
        NYT_BEETS_CHROME,
        "https://www.nytimes.com/2026/08/10/well/eat/beets-health-benefits-recipes.html",
    )
    html = build_reader_html(article, article.get("title") or "https://example.com/")
    assert "max-width: 100% !important" in html
    assert "float: none !important" in html
    assert "height: auto !important" in html
    assert "TOKEN_BEETS_BODY" in html
    assert "A pile of roasted beets on a wooden table." in html
    assert "Beet hummus in a bowl" in html
    assert "beets-hero.jpg" in html
    assert "beets-bowl.jpg" in html
    assert "Share full article" not in html
    assert "Listen · 6:27" not in html
    assert "Leer en español" not in html
    assert "width:1600" not in html
    assert "float:left" not in html
    assert "How Healthy Are Beets?" in html
    assert "By Simar Bajaj" in html


def test_reader_sanitizes_dirty_article_html_and_overrides_inline_layout():
    html = build_reader_html(
        {
            "title": "How Healthy Are Beets?",
            "dek": "Their eye-popping colors will give you a hint.",
            "author": "Simar Bajaj",
            "site_name": "The New York Times",
            "article_html": (
                "<p>TOKEN_BEETS_BODY Beets bring folate and nitrate in ordinary food.</p>"
                '<p><button>Listen · 6:27 min</button>'
                '<a href="https://x.com/intent/post/?url=https://daily.test/x">'
                "Share full article</a></p>"
                '<figure><img src="https://static.example/beets.jpg" '
                'style="width:2400px;float:left;" width="2400">'
                "<figcaption>Beet hummus in a bowl.</figcaption></figure>"
                "<p>Roasting concentrates the sugar and keeps the color.</p>"
            ),
        },
        "https://www.nytimes.com/2026/08/10/well/eat/beets-health-benefits-recipes.html",
    )
    assert "TOKEN_BEETS_BODY" in html
    assert "Beet hummus in a bowl." in html
    assert "Share full article" not in html
    assert "Listen · 6:27" not in html
    assert "width:2400" not in html
    assert "float:left" not in html
    assert "max-width: 100% !important" in html
