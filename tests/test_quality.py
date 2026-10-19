"""Detector: good article, FT wall, thin body, soft CTA, WaPo upsell."""

from pathlib import Path

from app.quality import grade_html, is_better

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _grade(name: str):
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return html, grade_html(html, "https://example.com/story")


def test_good_article_grades_good():
    html, grade = _grade("good_article.html")
    assert "TOKEN_GOOD_ARTICLE" in html
    assert grade.label == "good"
    assert grade.reason == "ok"
    assert grade.score >= 80


def test_ft_wall_grades_bad():
    html, grade = _grade("ft_wall.html")
    lowered = html.lower()
    assert "subscribe to unlock" in lowered
    assert "$75 per month" in lowered
    assert "explore our full range of subscriptions" in lowered
    assert grade.label == "bad"
    assert grade.reason == "ft_subscribe_wall"


def test_pricing_feature_list_without_the_three_phrases_is_an_ft_wall():
    html = """
    <html><head><title>Choose a plan</title></head>
    <body><article>
      <p>Standard Digital includes the weekday edition.</p>
      <p>Premium Digital adds the weekend paper.</p>
      <p>Cancel anytime from your account page.</p>
      <p>Unlimited access on the phone and the web.</p>
    </article></body></html>
    """
    grade = grade_html(html)
    assert grade.label == "bad"
    assert grade.reason == "ft_subscribe_wall"


def test_thin_body_grades_bad():
    html, grade = _grade("thin_body.html")
    assert "TOKEN_THIN_BODY" in html
    assert grade.label == "bad"
    assert grade.reason == "thin_body"
    assert grade.score < 80


def test_ft_soft_cta_grades_good():
    html, grade = _grade("ft_soft_cta.html")
    lowered = html.lower()
    assert "myft" in lowered
    assert "newsletter" in lowered
    assert "subscribe to unlock" not in lowered
    assert "$75 per month" not in lowered
    assert grade.label == "good"
    assert grade.reason == "ok"
    assert grade.score >= 80


def test_wapo_body_with_upsell_grades_good():
    html, grade = _grade("wapo_upsell.html")
    assert "TOKEN_WAPO_BODY" in html
    assert "Subscribe" in html
    assert "unlimited access" in html.lower()
    assert grade.label == "good"
    assert grade.reason == "ok"


def test_subscribe_to_unlock_inside_a_real_article_stays_good():
    body = (FIXTURES / "good_article.html").read_text(encoding="utf-8")
    html = body.replace(
        "</article>",
        "<p>A spokesperson said subscribe to unlock the memo, then took questions.</p></article>",
    )
    grade = grade_html(html)
    assert grade.label == "good"
    assert grade.score >= 80


def test_hard_paywall_and_block_page():
    wall = grade_html(
        """
        <html><head><title>The real story</title></head>
        <body><article>
          <h1>The real story</h1>
          <p>Subscribe to continue reading.</p>
          <p>Already a subscriber?</p>
        </article></body></html>
        """
    )
    assert wall.label == "bad"
    assert wall.reason == "hard_paywall"

    blocked = grade_html(
        """
        <html><head><title>Just a moment...</title></head>
        <body><p>Checking your browser before accessing the site.
        Enable JavaScript and cookies to continue. Cloudflare Ray ID abc.</p></body></html>
        """
    )
    assert blocked.label == "bad"
    assert blocked.reason == "block_page"
    assert blocked.score == 0


def _bridge_words(count: int) -> str:
    seed = (
        "The city council voted last night to fund a new bridge over the river "
        "after years of argument about the long detour"
    ).split()
    words = []
    for index in range(count):
        word = seed[index % len(seed)]
        if index % 21 == 0:
            word = word.capitalize()
        words.append(word)
    for index in range(21, count, 21):
        words[index - 1] = words[index - 1] + "."
    text = " ".join(words)
    if not text.endswith("."):
        text += "."
    return text


def test_article_between_ft_markers_stays_good():
    middle = _bridge_words(167)
    html = f"""
    <html><head><title>City council funds a bridge</title></head>
    <body><article>
      <h1>City council funds a bridge</h1>
      <p>Subscribe to unlock this article.</p>
      <p>{middle}</p>
      <p>The figure printed on the board is $75 per month.</p>
    </article></body></html>
    """
    assert len(middle.split()) == 167
    grade = grade_html(html, "https://www.ft.com/content/bridge")
    assert grade.label == "good"
    assert grade.reason == "ok"
    assert grade.score >= 167


def test_article_between_a_pricing_list_stays_good():
    middle = _bridge_words(167)
    html = f"""
    <html><head><title>City council funds a bridge</title></head>
    <body><article>
      <h1>City council funds a bridge</h1>
      <p>Standard Digital and Premium Digital are the first two rows.</p>
      <p>{middle}</p>
      <p>Cancel anytime. Digital + print is the last row.</p>
    </article></body></html>
    """
    assert len(middle.split()) == 167
    grade = grade_html(html, "https://www.ft.com/content/bridge")
    assert grade.label == "good"
    assert grade.reason == "ok"
    assert grade.score >= 167


def test_long_ft_offer_with_benefit_copy_grades_bad():
    """Markers are checked even when benefit sentences push the raw count past 80."""
    from app.extract import extract_article
    from app.quality import narrative_text

    html, grade = _grade("ft_offer_long.html")
    lowered = html.lower()
    assert "subscribe to unlock" in lowered
    assert "$75 per month" in lowered
    assert "explore our full range of subscriptions" in lowered
    article = extract_article(html, "https://www.ft.com/content/offer")
    words = article.get("article_text") or ""
    # The offer is long enough that ignoring the markers would call it an article.
    assert 150 <= len(words.split()) <= 330
    assert len(narrative_text(words).split()) >= 80
    assert grade.label == "bad"
    assert grade.reason == "ft_subscribe_wall"
    assert grade.score < 80


def _upsell_inside_article(html: str) -> str:
    moved = html.replace('<aside class="subscribe">', "<div>").replace("<aside>", "<div>")
    moved = moved.replace("</article>", "", 1).replace("</div>", "</article>", 1)
    return moved


def test_soft_newsletter_myft_and_wapo_upsell_inside_the_article_stay_good():
    soft = _upsell_inside_article((FIXTURES / "ft_soft_cta.html").read_text(encoding="utf-8"))
    assert "<aside" not in soft
    assert "myft" in soft.lower()
    assert "newsletter" in soft.lower()
    soft_grade = grade_html(soft)
    assert soft_grade.label == "good"
    assert soft_grade.reason == "ok"
    assert soft_grade.score >= 80

    wapo = _upsell_inside_article((FIXTURES / "wapo_upsell.html").read_text(encoding="utf-8"))
    assert "<aside" not in wapo
    assert "unlimited access" in wapo.lower()
    wapo_grade = grade_html(wapo)
    assert wapo_grade.label == "good"
    assert wapo_grade.reason == "ok"
    assert "TOKEN_WAPO_BODY" in wapo


def test_better_never_prefers_a_worse_capture():
    good, _ = _grade("good_article.html")
    thin, _ = _grade("thin_body.html")
    wall, _ = _grade("ft_wall.html")
    good_grade = grade_html(good)
    thin_grade = grade_html(thin)
    wall_grade = grade_html(wall)
    assert is_better(good_grade, thin_grade)
    assert is_better(good_grade, wall_grade)
    assert is_better(wall_grade, thin_grade) is False
    assert is_better(thin_grade, good_grade) is False
    assert is_better(wall_grade, good_grade) is False
    assert is_better(good_grade, good_grade) is False
