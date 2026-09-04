"""Allowlisted Google/X bounce origins — never a user-supplied Referer."""

import pytest

from app.capture import (
    _merge_crawler_article,
    _richer_article,
    bounce_origins_for,
    extra_headers_for_visit,
    intercept_bounce_host,
    require_bounce_origin,
)
from app.config import (
    REFERRER_BOUNCE_ORIGINS,
    REFERRER_BOUNCE_RETRY_ORIGINS,
    referrer_is_allowlisted,
)
from app.extract import PAYWALL_WORD_LIMIT, extract_article

from tests.test_e2e_metered_paywall import FULL_ARTICLE_HTML, TEASER_ARTICLE_HTML


def test_allowlist_accepts_google_news_and_x():
    assert referrer_is_allowlisted("https://www.google.com/")
    assert referrer_is_allowlisted("https://news.google.com/articles/abc")
    assert referrer_is_allowlisted("https://x.com/i/status/1")
    assert referrer_is_allowlisted("https://t.co/abc")
    assert set(REFERRER_BOUNCE_ORIGINS) == {
        "https://www.google.com/",
        "https://news.google.com/",
        "https://x.com/",
        "https://t.co/",
    }
    assert REFERRER_BOUNCE_RETRY_ORIGINS == (
        "https://www.google.com/",
        "https://x.com/",
    )
    for origin in REFERRER_BOUNCE_RETRY_ORIGINS:
        assert origin in REFERRER_BOUNCE_ORIGINS


def test_allowlist_rejects_lookalikes_and_user_urls():
    assert not referrer_is_allowlisted("")
    assert not referrer_is_allowlisted(None)
    assert not referrer_is_allowlisted("https://evil.example/")
    assert not referrer_is_allowlisted("https://www.google.com.evil.example/")
    assert not referrer_is_allowlisted("https://notgoogle.com/")
    assert not referrer_is_allowlisted("https://www.politico.eu/")
    assert not referrer_is_allowlisted("https://archive.ph/abc")


def test_require_bounce_origin_rejects_arbitrary_referer():
    for origin in REFERRER_BOUNCE_ORIGINS:
        assert require_bounce_origin(origin) == origin
    with pytest.raises(ValueError, match="not allowlisted"):
        require_bounce_origin("https://evil.example/from-user")
    with pytest.raises(ValueError, match="not allowlisted"):
        require_bounce_origin("https://www.politico.eu/")


def test_extract_teaser_is_paywalled_full_is_not():
    teaser = extract_article(TEASER_ARTICLE_HTML, "https://daily.test/kallas")
    full = extract_article(FULL_ARTICLE_HTML, "https://daily.test/kallas")
    assert teaser["paywalled"] is True
    assert teaser["word_count"] < PAYWALL_WORD_LIMIT
    assert "METERED_FULL_TOKEN_AMBER" not in teaser["article_text"]
    assert full["paywalled"] is False
    assert full["word_count"] >= PAYWALL_WORD_LIMIT
    assert "METERED_FULL_TOKEN_AMBER" in full["article_text"]


def test_richer_article_keeps_higher_word_count():
    assert _richer_article({"word_count": 200}, {"word_count": 20}) is True
    assert _richer_article({"word_count": 20}, {"word_count": 20}) is False
    merged = _merge_crawler_article(
        {"word_count": 20, "title": "A", "author": "Jane"},
        {"word_count": 200, "title": None, "author": None},
    )
    assert merged["word_count"] == 200
    assert merged["title"] == "A"
    assert merged["author"] == "Jane"


def test_richer_article_prefers_not_paywalled():
    assert _richer_article(
        {"word_count": 40, "paywalled": False},
        {"word_count": 90, "paywalled": True},
    ) is True
    assert _richer_article(
        {"word_count": 90, "paywalled": True},
        {"word_count": 40, "paywalled": False},
    ) is False


def test_bounce_origins_skip_article_host():
    assert bounce_origins_for("https://www.politico.eu/article/x") == (
        "https://www.google.com/",
        "https://x.com/",
    )
    assert bounce_origins_for("https://x.com/i/status/1") == ("https://www.google.com/",)
    assert bounce_origins_for("https://www.google.com/amp/s/story") == ("https://x.com/",)
    assert bounce_origins_for(
        "https://x.com/i/status/1", "https://www.google.com/amp/s/story"
    ) == ()


def test_bounce_visit_headers_drop_google_referer():
    direct = extra_headers_for_visit(bounce=False)
    bounced = extra_headers_for_visit(bounce=True)
    assert direct.get("Referer") == "https://www.google.com/"
    assert "Referer" not in bounced
    assert bounced.get("Accept-Language") == direct.get("Accept-Language")


def test_intercept_bounce_host_stops_after_landing():
    origin = "https://x.com/"
    article = "https://x.com/i/status/1"
    recaptcha = "https://www.google.com/recaptcha/api.js"
    assert intercept_bounce_host(origin, origin, landed=False) is True
    assert intercept_bounce_host(article, origin, landed=False) is True
    assert intercept_bounce_host(article, origin, landed=True) is False
    assert intercept_bounce_host(recaptcha, "https://www.google.com/", landed=True) is False
    assert intercept_bounce_host(recaptcha, "https://www.google.com/", landed=False) is True
    assert intercept_bounce_host(article, None, landed=False) is False
