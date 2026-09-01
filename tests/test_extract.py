from app.extract import extract_article

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
