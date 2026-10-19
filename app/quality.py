"""Label a saved page good or bad.

A snap is bad when the captured body is a block page, a hard subscribe wall,
or thinner than a real article. Soft newsletter and myFT prompts are ignored.
An upsell next to a real article does not make the snap bad: the grade follows
how much article prose is left after those lines are set aside.

Score is that prose word count. A higher score is a better capture. Callers
that replace files must refuse a new capture that does not beat the stored one.

Burned FT wall markers and a pricing-feature list are checked even when the
raw page is long. The page is a wall unless 80 words of prose remain after
the offer block is removed. One mention inside a real article stays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .extract import PAYWALL_WORD_LIMIT, extract_article

# Same line the incomplete flag already uses. Shorter than this, the extract
# is a lede or a stub, not a saved article.
MIN_ARTICLE_WORDS = PAYWALL_WORD_LIMIT

# Burned FT subscribe wall. These phrases, not a newsletter prompt.
_FT_MARKERS = (
    "subscribe to unlock",
    "$75 per month",
    "explore our full range of subscriptions",
)

# A pricing grid standing where the article should be. One phrase in a real
# story is not enough; several of them with no article prose is the wall.
_PRICING_FEATURES = (
    "standard digital",
    "premium digital",
    "cancel anytime",
    "unlimited access",
    "per month",
    "best value",
    "digital + print",
    "full digital access",
)
_PRICING_LIST_MIN = 3

# Pitch lines that sit around the burned wall but are not the three phrases
# and are not a pricing-feature hit. They count only when the page already
# looks like that wall (two markers, or a pricing list). A real article that
# merely says "weekend" is left alone.
_BENEFIT_RE = re.compile(
    r"\b(?:subscriptions?|e-?papers?|briefings?|columnists?|unlimited|"
    r"weekdays?|weekends?|newspapers?|editions?|premium|devices?|"
    r"log(?:\s*|-)?ins?|sign(?:ing)?\s+in|accounts?|journalism|"
    r"trials?|apps?|members?|paywalls?)\b",
    re.I,
)

_SOFT_CTA_RE = re.compile(
    r"\bmyft\b|newsletter|sign up to (?:the|our)|"
    r"subscribe to (?:our|the) newsletter|get the .{0,40}newsletter|"
    r"manage your delivery|follow us on|download the app",
    re.I,
)
_HARD_WALL_RE = re.compile(
    r"subscribe to (?:unlock|continue|read\b)|"
    r"already a subscriber|"
    r"for subscribers only|"
    r"this article is (?:only )?for subscribers|"
    r"available exclusively to subscribers|"
    r"to continue reading|"
    r"register to continue|"
    r"you(?:'|’)ve (?:reached|hit) your .{0,40}limit|"
    r"sign in to (?:continue|keep) reading",
    re.I,
)
_BLOCK_TITLE_RE = re.compile(
    r"just a moment|attention required|access denied|403 forbidden|"
    r"pardon our interruption|are you a robot|"
    r"verify you are a human|sorry, you have been blocked",
    re.I,
)
_BLOCK_BODY_RE = re.compile(
    r"checking your browser|enable javascript and cookies to continue|"
    r"cf-browser-verification|cloudflare ray id|why have i been blocked|"
    r"bot detection|ddos protection by|"
    r"please enable cookies|sorry, you have been blocked",
    re.I,
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\s*[•|·]\s+|\n+")

PAYWALL_REASONS = frozenset({"ft_subscribe_wall", "hard_paywall"})


@dataclass(frozen=True)
class Grade:
    label: str
    reason: str
    score: int
    word_count: int

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "reason": self.reason,
            "score": self.score,
            "word_count": self.word_count,
        }


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _pieces(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    return [part.strip() for part in _SENTENCE_RE.split(raw) if part.strip()]


def _is_offer_piece(piece: str, *, aggressive: bool) -> bool:
    if _HARD_WALL_RE.search(piece) or _ft_marker(piece) or _pricing_hits(piece):
        return True
    if _SOFT_CTA_RE.search(piece):
        return True
    return bool(aggressive and _BENEFIT_RE.search(piece))


def _prose_without_offer(text: str, *, aggressive: bool) -> str:
    """Drop wall lines. On a burned wall, also drop the block they sit in.

    Two or more FT markers, or a pricing-feature list, mark the whole span
    from the first offer sentence through the last one. Leftover pitch lines
    inside that span do not count as the article, and neither do benefit
    lines outside it. A single marker only drops its own sentence.
    """
    pieces = _pieces(text)
    if not pieces:
        return ""
    flags = [_is_offer_piece(piece, aggressive=aggressive) for piece in pieces]
    drop = {index for index, flag in enumerate(flags) if flag}
    if aggressive and drop:
        first = min(drop)
        last = max(drop)
        drop.update(range(first, last + 1))
    return " ".join(piece for index, piece in enumerate(pieces) if index not in drop)


def narrative_text(text: str) -> str:
    """Article prose with soft prompts and wall lines removed."""
    return _prose_without_offer(text, aggressive=False)


def _ft_marker(text: str) -> bool:
    flat = _norm(text)
    return any(marker in flat for marker in _FT_MARKERS)


def _marker_count(text: str) -> int:
    flat = _norm(text)
    return sum(1 for marker in _FT_MARKERS if marker in flat)


def _pricing_hits(text: str) -> int:
    flat = _norm(text)
    return sum(1 for phrase in _PRICING_FEATURES if phrase in flat)


def _is_block(title: str, page_text: str) -> bool:
    if _BLOCK_TITLE_RE.search(title or ""):
        return True
    head = " ".join((page_text or "").split())[:800]
    return bool(_BLOCK_BODY_RE.search(head))


def grade_parts(*, article_text: str, title: str = "", page_text: str = "") -> Grade:
    """Grade extracted article text. `page_text` catches block pages the extractor drops.

    FT wall markers are applied even when the raw word count is past the
    article minimum. What matters is the prose left after the offer block.
    """
    source = article_text or page_text or ""
    word_count = len(source.split())
    blob = _norm("\n".join((title or "", page_text or "", article_text or "")))
    markers = _marker_count(blob)
    pricing_list = _pricing_hits(blob) >= _PRICING_LIST_MIN
    prose = _prose_without_offer(source, aggressive=markers >= 2 or pricing_list)
    score = len(prose.split())
    if score < MIN_ARTICLE_WORDS and _is_block(title, page_text or article_text):
        return Grade("bad", "block_page", 0, word_count)
    if (markers or pricing_list) and score < MIN_ARTICLE_WORDS:
        return Grade("bad", "ft_subscribe_wall", score, word_count)
    if score < MIN_ARTICLE_WORDS and _HARD_WALL_RE.search(blob):
        return Grade("bad", "hard_paywall", score, word_count)
    if score < MIN_ARTICLE_WORDS:
        return Grade("bad", "thin_body", score, word_count)
    return Grade("good", "ok", score, word_count)


def grade_html(html: str, url: str = "") -> Grade:
    article = extract_article(html or "", url or "https://example.invalid/")
    soup = BeautifulSoup(html or "", "lxml")
    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
    title = title or article.get("title") or ""
    page_text = soup.get_text(" ", strip=True)
    return grade_parts(
        article_text=article.get("article_text") or "",
        title=title,
        page_text=page_text,
    )


def is_better(new: Grade, old: Grade) -> bool:
    """True when `new` should replace a stored bad snap.

    A good snap is never replaced. A bad snap is replaced by a good one, or by
    another bad one only when more article prose survived.
    """
    if old.label == "good":
        return False
    if new.label == "good":
        return True
    return new.score > old.score
