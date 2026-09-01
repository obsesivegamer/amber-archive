from app.reader import build_reader_html


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
