"""Recapture queue: same snap id, no downgrade, gift and import stops."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import db, recapture
from app.extract import extract_article
from app.reader import build_reader_html
from app.security import article_match_key, normalize_url

FIXTURES = Path(__file__).resolve().parent / "fixtures"
START = datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc)
GUARDIAN = "https://www.theguardian.com/world/2026/oct/01/bridge"
NYT = "https://www.nytimes.com/2026/10/01/us/cities.html"
UNLOCK = NYT + "?unlocked_article_code=abc"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _seed(sid: str, url: str, html: str, *, imported: bool = False, unlock_url: str | None = None) -> Path:
    db.insert_snapshot(sid, url, normalize_url(url))
    folder = db.snap_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "res").mkdir(exist_ok=True)
    (folder / "res" / "keep.txt").write_text("keep", encoding="utf-8")
    (folder / "screenshot.jpg").write_bytes(b"old-screenshot")
    article = extract_article(html, url)
    (folder / "page.html").write_text(html, encoding="utf-8")
    (folder / "article.txt").write_text(article.get("article_text") or "", encoding="utf-8")
    (folder / "article.html").write_text(article.get("article_html") or "", encoding="utf-8")
    (folder / "reader.html").write_text(build_reader_html(article, url), encoding="utf-8")
    meta = {
        "id": sid,
        "url": url,
        "final_url": url,
        "title": article.get("title"),
        "word_count": article.get("word_count"),
        "paywalled": article.get("paywalled"),
        "created_at": "2026-10-18T00:00:00Z",
    }
    if imported:
        meta["imported"] = True
    if unlock_url:
        meta["unlock_url"] = unlock_url
    db.write_json(folder / "meta.json", meta)
    db.update_snapshot(
        sid,
        status="complete",
        title=article.get("title"),
        final_url=url,
        word_count=article.get("word_count"),
        paywalled=1 if article.get("paywalled") else 0,
        unlock_url=unlock_url,
    )
    return folder


def _files(folder: Path) -> dict[str, bytes]:
    saved = {}
    for path in folder.rglob("*"):
        if path.is_file() and not path.name.endswith(".recapture-new"):
            saved[str(path.relative_to(folder))] = path.read_bytes()
    return saved


def test_grading_a_good_snap_does_not_touch_its_files(tmp_data):
    folder = _seed("abcde", GUARDIAN, _html("good_article.html"))
    before = _files(folder)

    def refuse(url: str) -> str:
        raise AssertionError(url)

    report = recapture.run(now=START, fetch=refuse)
    assert report["attempted"] == 0
    assert report["pending"] == []
    assert report["needs_browser_capture"] == []
    assert report["needs_fresh_gift"] == []
    assert _files(folder) == before
    snap = db.get_snapshot("abcde")
    assert snap["recapture_state"] is None
    assert snap["recapture_reason"] == "ok"
    assert snap["unlock_url"] is None


def test_plain_ft_is_retried_but_nyt_wsj_and_wapo_are_not(tmp_data):
    _seed("abcde", "https://www.ft.com/content/bridge-story", _html("thin_body.html"))
    calls: list[str] = []

    def still_thin(url: str) -> str:
        calls.append(url)
        return _html("thin_body.html")

    recapture.run(now=START, fetch=still_thin)
    assert calls == ["https://www.ft.com/content/bridge-story"]
    assert db.get_snapshot("abcde")["recapture_state"] == "pending"

    def refuse(url: str) -> str:
        raise AssertionError(url)

    for sid, url in (
        ("fghij", "https://www.wsj.com/articles/bridge-story"),
        ("kmnpq", "https://www.washingtonpost.com/world/bridge-story/"),
    ):
        _seed(sid, url, _html("thin_body.html"))
    recapture.run(now=START, fetch=refuse)
    assert db.get_snapshot("fghij")["recapture_state"] == "needs_fresh_gift"
    assert db.get_snapshot("kmnpq")["recapture_state"] == "needs_fresh_gift"
    assert calls == ["https://www.ft.com/content/bridge-story"]


def test_existing_snap_has_no_unlock_until_one_is_stored(tmp_data):
    db.insert_snapshot("abcde", GUARDIAN, normalize_url(GUARDIAN))
    db.update_snapshot("abcde", status="complete", title="Hello", word_count=200)
    db.init_db()
    snap = db.get_snapshot("abcde")
    assert snap is not None
    assert snap["title"] == "Hello"
    assert snap["unlock_url"] is None
    assert snap["recapture_state"] is None
    assert snap["recapture_attempts"] is None


def test_recapture_swap_keeps_id_and_never_downgrades(tmp_data):
    folder = _seed("abcde", GUARDIAN, _html("thin_body.html"))
    meta = db.read_json(folder / "meta.json")
    meta["archive_note"] = "keep-me"
    db.write_json(folder / "meta.json", meta)
    before_names = sorted(p.name for p in folder.iterdir())
    calls: list[str] = []

    def worse(url: str) -> str:
        calls.append(url)
        return "<html><head><title>Nope</title><body><p>No.</p></body></html>"

    first = recapture.run(now=START, fetch=worse)
    assert calls == [GUARDIAN]
    assert first["replaced"] == []
    text = (folder / "article.txt").read_text(encoding="utf-8")
    assert "TOKEN_THIN_BODY" in text
    assert (folder / "screenshot.jpg").read_bytes() == b"old-screenshot"
    assert (folder / "res" / "keep.txt").read_text(encoding="utf-8") == "keep"
    snap = db.get_snapshot("abcde")
    assert snap["id"] == "abcde"
    assert snap["recapture_state"] == "pending"
    assert snap["recapture_attempts"] == 1
    meta = db.read_json(folder / "meta.json")
    assert meta["id"] == "abcde"
    assert meta["recapture"]["history"][-1]["outcome"] == "kept"
    assert meta["recapture"]["attempts"] == 1

    due = datetime.strptime(snap["recapture_next_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )

    def better(url: str) -> str:
        calls.append(url)
        return _html("good_article.html")

    second = recapture.run(now=due, fetch=better)
    assert second["replaced"] == ["abcde"]
    assert calls[-1] == GUARDIAN
    assert db.get_snapshot("abcde")["id"] == "abcde"
    assert folder.name == "abcde"
    article = (folder / "article.txt").read_text(encoding="utf-8")
    assert "TOKEN_GOOD_ARTICLE" in article
    assert "TOKEN_THIN_BODY" not in article
    assert "TOKEN_GOOD_ARTICLE" in (folder / "page.html").read_text(encoding="utf-8")
    assert (folder / "screenshot.jpg").read_bytes() == b"old-screenshot"
    assert (folder / "res" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert sorted(p.name for p in folder.iterdir()) == before_names
    assert db.get_snapshot("abcde")["recapture_state"] is None
    meta = db.read_json(folder / "meta.json")
    assert meta["id"] == "abcde"
    assert meta["url"] == GUARDIAN
    assert meta["archive_note"] == "keep-me"
    assert any(item["outcome"] == "replaced" for item in meta["recapture"]["history"])

    saved = _files(folder)
    calls_before = len(calls)

    def refuse(url: str) -> str:
        raise AssertionError(url)

    db.update_snapshot(
        "abcde",
        recapture_state="pending",
        recapture_next_at="2000-01-01T00:00:00Z",
    )
    third = recapture.run(now=due + timedelta(days=3), fetch=refuse)
    assert third["replaced"] == []
    assert len(calls) == calls_before
    assert _files(folder) == saved
    assert db.get_snapshot("abcde")["id"] == "abcde"
    assert db.get_snapshot("abcde")["recapture_state"] is None


def test_import_is_not_retried_with_save(tmp_data):
    # An imported NYT page is still a browser capture, not a fresh-gift retry.
    _seed("abcde", NYT, _html("thin_body.html"), imported=True)

    def refuse(url: str) -> str:
        raise AssertionError(url)

    report = recapture.run(now=START, fetch=refuse)
    assert report["attempted"] == 0
    assert report["needs_fresh_gift"] == []
    assert report["needs_browser_capture"][0]["id"] == "abcde"
    snap = db.get_snapshot("abcde")
    assert snap["recapture_state"] == "needs_browser_capture"
    again = recapture.run(now=START + timedelta(days=5), fetch=refuse)
    assert again["attempted"] == 0
    assert "TOKEN_THIN_BODY" in (db.snap_dir("abcde") / "article.txt").read_text(encoding="utf-8")


def test_ssrf_or_private_redirect_stops_as_needs_browser_capture(tmp_data):
    _seed("abcde", GUARDIAN, _html("thin_body.html"))
    calls = {"n": 0}

    def blocked(url: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("Private or local network URLs cannot be archived.")
        raise RuntimeError(
            "Redirected to a blocked address: Private or local network URLs cannot be archived."
        )

    report = recapture.run(now=START, fetch=blocked)
    assert report["needs_browser_capture"][0]["id"] == "abcde"
    assert calls["n"] == 1
    recapture.run(now=START + timedelta(days=2), fetch=blocked)
    assert calls["n"] == 1
    assert "TOKEN_THIN_BODY" in (db.snap_dir("abcde") / "article.txt").read_text(encoding="utf-8")

    _seed("fghij", "https://www.theguardian.com/world/2026/oct/02/other", _html("thin_body.html"))
    # The row above is already stopped. A second snap hits the redirect path.
    def redirect(url: str) -> str:
        calls["n"] += 1
        raise RuntimeError(
            "Redirected to a blocked address: Private or local network URLs cannot be archived."
        )

    report = recapture.run(now=START, fetch=redirect)
    assert db.get_snapshot("fghij")["recapture_state"] == "needs_browser_capture"
    assert any(item["id"] == "fghij" for item in report["needs_browser_capture"])


def test_paywall_on_a_plain_retry_stops(tmp_data):
    folder = _seed("abcde", GUARDIAN, _html("thin_body.html"))
    calls = {"n": 0}

    def walled(url: str) -> str:
        calls["n"] += 1
        return _html("ft_wall.html")

    recapture.run(now=START, fetch=walled)
    assert calls["n"] == 1
    assert db.get_snapshot("abcde")["recapture_state"] == "needs_browser_capture"
    assert "TOKEN_THIN_BODY" in (folder / "article.txt").read_text(encoding="utf-8")
    recapture.run(now=START + timedelta(days=3), fetch=walled)
    assert calls["n"] == 1


def test_nyt_without_unlock_needs_a_fresh_gift(tmp_data):
    _seed("abcde", NYT, _html("thin_body.html"))

    def refuse(url: str) -> str:
        raise AssertionError(url)

    report = recapture.run(now=START, fetch=refuse)
    assert report["attempted"] == 0
    assert report["needs_fresh_gift"][0]["id"] == "abcde"
    assert db.get_snapshot("abcde")["recapture_state"] == "needs_fresh_gift"
    meta = db.read_json(db.snap_dir("abcde") / "meta.json")
    assert "unlock_url" not in meta or meta.get("unlock_url") in (None, "")


def test_retry_uses_unlock_url_and_a_burned_gift_stops(tmp_data):
    folder = _seed("abcde", NYT, _html("thin_body.html"), unlock_url=UNLOCK)
    calls: list[str] = []

    def walled(url: str) -> str:
        calls.append(url)
        return "<html><head><title>Subscribe</title><body><article><p>Subscribe to continue reading.</p><p>Already a subscriber?</p></article></body></html>"

    report = recapture.run(now=START, fetch=walled)
    assert calls == [UNLOCK]
    assert report["needs_fresh_gift"][0]["id"] == "abcde"
    assert db.get_snapshot("abcde")["unlock_url"] == UNLOCK
    assert "TOKEN_THIN_BODY" in (folder / "article.txt").read_text(encoding="utf-8")
    meta = db.read_json(folder / "meta.json")
    assert meta["unlock_url"] == UNLOCK
    assert meta["recapture"]["history"][-1]["detail"] == "gift link burned"
    assert meta["recapture"]["history"][-1]["url_kind"] == "unlock"
    assert "unlocked_article_code" not in str(meta["recapture"]["history"])
    recapture.run(now=START + timedelta(days=4), fetch=walled)
    assert calls == [UNLOCK]


def test_live_candidate_unlock_is_used_and_an_expired_one_stops(tmp_data):
    _seed("abcde", NYT, _html("thin_body.html"))
    row = db.insert_gift_candidate(
        unlock_url=UNLOCK,
        canonical_url=NYT,
        match_key=article_match_key(NYT),
        publisher="nyt",
        title="Cities",
        source_url=None,
        source_author=None,
        found_at="2026-10-18T00:00:00Z",
        expires_at="2026-12-01T00:00:00Z",
        expires_estimated=False,
    )
    db.update_gift_candidate(row["id"], snapshot_id="abcde")
    calls: list[str] = []

    def good(url: str) -> str:
        calls.append(url)
        return _html("good_article.html")

    report = recapture.run(now=START, fetch=good)
    assert calls == [UNLOCK]
    assert report["replaced"] == ["abcde"]
    assert db.get_snapshot("abcde")["unlock_url"] == UNLOCK
    assert db.get_snapshot("abcde")["id"] == "abcde"
    meta = db.read_json(db.snap_dir("abcde") / "meta.json")
    assert meta["unlock_url"] == UNLOCK
    assert meta["id"] == "abcde"

    _seed("fghij", "https://www.nytimes.com/2026/10/02/us/other.html", _html("thin_body.html"))
    expired = db.insert_gift_candidate(
        unlock_url="https://www.nytimes.com/2026/10/02/us/other.html?unlocked_article_code=old",
        canonical_url="https://www.nytimes.com/2026/10/02/us/other.html",
        match_key=article_match_key("https://www.nytimes.com/2026/10/02/us/other.html"),
        publisher="nyt",
        title="Other",
        source_url=None,
        source_author=None,
        found_at="2026-09-01T00:00:00Z",
        expires_at="2026-09-08T00:00:00Z",
        expires_estimated=False,
    )
    db.update_gift_candidate(expired["id"], snapshot_id="fghij")

    def refuse(url: str) -> str:
        raise AssertionError(url)

    report = recapture.run(now=START, fetch=refuse)
    assert report["needs_fresh_gift"][0]["id"] == "fghij" or any(
        item["id"] == "fghij" for item in report["needs_fresh_gift"]
    )
    assert db.get_snapshot("fghij")["recapture_state"] == "needs_fresh_gift"


def test_backoff_then_give_up(tmp_data):
    _seed("abcde", GUARDIAN, _html("thin_body.html"))
    calls = {"n": 0}
    moment = START

    def still_thin(url: str) -> str:
        calls["n"] += 1
        return _html("thin_body.html")

    recapture.run(now=moment, fetch=still_thin)
    assert calls["n"] == 1
    assert db.get_snapshot("abcde")["recapture_next_at"] == "2026-10-19T04:00:00Z"
    for expected in (2, 3, 4):
        snap = db.get_snapshot("abcde")
        assert snap["recapture_state"] == "pending"
        moment = datetime.strptime(snap["recapture_next_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        early = recapture.run(now=moment - timedelta(minutes=5), fetch=still_thin)
        assert early["attempted"] == 0
        assert calls["n"] == expected - 1
        recapture.run(now=moment, fetch=still_thin)
        assert calls["n"] == expected
        snap = db.get_snapshot("abcde")
        if expected == 2:
            assert snap["recapture_next_at"] == "2026-10-19T10:00:00Z"
        elif expected == 3:
            assert snap["recapture_next_at"] == "2026-10-20T10:00:00Z"
    assert db.get_snapshot("abcde")["recapture_state"] == "gave_up"
    assert db.get_snapshot("abcde")["recapture_attempts"] == 4
    recapture.run(now=moment + timedelta(days=30), fetch=still_thin)
    assert calls["n"] == 4
    meta = db.read_json(db.snap_dir("abcde") / "meta.json")
    assert meta["recapture"]["state"] == "gave_up"
    assert len(meta["recapture"]["history"]) >= 4


def test_second_run_does_not_fetch_again(tmp_data):
    _seed("abcde", GUARDIAN, _html("thin_body.html"))
    calls = {"n": 0}

    def still_thin(url: str) -> str:
        calls["n"] += 1
        return _html("thin_body.html")

    recapture.run(now=START, fetch=still_thin)
    recapture.run(now=START, fetch=still_thin)
    assert calls["n"] == 1


def test_status_endpoint_and_saved_page(tmp_data):
    _seed("abcde", NYT, _html("thin_body.html"))
    _seed("fghij", GUARDIAN, _html("thin_body.html"), imported=True)
    recapture.run(now=START, fetch=lambda url: (_ for _ in ()).throw(AssertionError(url)))
    from app.main import app

    with TestClient(app) as client:
        status = client.get("/recapture")
        assert status.status_code == 200
        body = status.json()
        assert body["needs_fresh_gift"][0]["id"] == "abcde"
        assert body["needs_browser_capture"][0]["id"] == "fghij"
        saved = client.get("/saved")
        assert saved.status_code == 200
        assert "needs fresh gift" in saved.text
        assert "needs browser capture" in saved.text
        gift = client.get("/abcde")
        assert "Needs a fresh gift link" in gift.text
        imported = client.get("/fghij")
        assert "Needs browser capture" in imported.text
        again = client.post("/recapture/run")
        assert again.status_code == 200
        assert again.json()["attempted"] == 0
        db.update_snapshot("abcde", recapture_state="pending", recapture_reason="thin_body")
        db.update_snapshot("fghij", recapture_state="gave_up", recapture_reason="thin_body")
        queued = client.get("/saved")
        assert "recapture pending" in queued.text
        assert "recapture gave up" in queued.text


def test_cli_run_prints_the_queue(tmp_data, capsys):
    _seed("abcde", GUARDIAN, _html("thin_body.html"), imported=True)
    assert recapture.main([]) == 0
    out = capsys.readouterr().out
    assert "needs_browser_capture" in out
    assert "abcde" in out


def test_save_fetch_skips_bounce_for_an_unlock_url(monkeypatch):
    seen = {}

    async def fake(url, *, skip_referrer_bounce=False):
        seen["url"] = url
        seen["skip"] = skip_referrer_bounce
        return {"html": "<html><title>Hi</title><body><p>x</p></body></html>", "final_url": url}

    monkeypatch.setattr("app.capture.fetch_document", fake)
    recapture.fetch_via_save(UNLOCK)
    assert seen["url"] == UNLOCK
    assert seen["skip"] is True
    recapture.fetch_via_save(GUARDIAN)
    assert seen["skip"] is False
