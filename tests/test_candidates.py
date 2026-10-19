"""Gift candidate queue: CLI, expiry, save, dismiss, and the write guards."""

from __future__ import annotations

import asyncio
import io
import json
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import capture, db
from app.candidates import PUBLISHER_TTL, add_finds, get_candidates_payload
from app.capture import _should_referrer_retry
from app.config import ID_ALPHABET, ID_LENGTH
from app.main import app
from app.security import article_match_key

ROOT = Path(__file__).resolve().parents[1]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _live_find(url: str = "http://93.184.216.34/story", **extra) -> dict:
    find = {
        "unlock_url": url,
        "publisher": "nyt",
        "title": "Cities after the storm",
        "source_url": "http://8.8.8.8/post/1",
        "source_author": "@ada",
        "found_at": _iso(_now()),
    }
    find.update(extra)
    return find


def _run(monkeypatch, capsys, argv: list[str], stdin: str = ""):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    from app.candidates import main

    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _queue(url: str = "http://93.184.216.34/story", **extra) -> dict:
    lines = add_finds([_live_find(url, **extra)])
    assert lines == ["added"], lines
    return db.list_gift_candidates()[0]


def _make_usable(sid: str, url: str) -> None:
    db.update_snapshot(
        sid,
        status="complete",
        title="Cities after the storm",
        site_name="Example",
        word_count=220,
        final_url=url,
        paywalled=0,
    )
    folder = db.snap_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "article.txt").write_text("city " * 160, encoding="utf-8")


@pytest.fixture
def api(tmp_data, monkeypatch):
    async def _noop(job_id: str) -> None:
        return None

    monkeypatch.setattr("app.capture.run_job", _noop)
    capture.jobs.clear()
    with TestClient(app) as client:
        yield client
    capture.jobs.clear()


def test_cli_add_and_refresh_is_idempotent(tmp_data, monkeypatch, capsys):
    payload = json.dumps({"finds": [_live_find()], "ignored": True})
    code, out, err = _run(monkeypatch, capsys, ["add"], payload)
    assert code == 0, err
    assert out.strip() == "added"
    code, out, err = _run(monkeypatch, capsys, ["add"], payload)
    assert code == 0, err
    assert out.strip() == "refreshed"
    rows = db.list_gift_candidates()
    assert len(rows) == 1
    assert len(rows[0]["id"]) == 8
    assert set(rows[0]["id"]) <= set(ID_ALPHABET)
    code, out, err = _run(monkeypatch, capsys, ["list"])
    assert code == 0, err
    assert rows[0]["id"] in out
    assert "  new  " in out


def test_overlapping_adds_insert_one_row(tmp_data):
    find = _live_find("http://93.184.216.34/dup")
    barrier = threading.Barrier(2)
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def _add() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(add_finds([find]))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_add) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == []
    assert sorted(line for batch in results for line in batch) == ["added", "refreshed"]
    rows = db.list_gift_candidates()
    assert len(rows) == 1
    assert rows[0]["match_key"] == article_match_key("http://93.184.216.34/dup")


def test_cli_rejects_bad_finds_and_keeps_the_good_one(tmp_data, monkeypatch, capsys):
    found = _iso(_now())
    payload = json.dumps(
        {
            "finds": [
                _live_find("http://93.184.216.34/good", found_at=found),
                {"unlock_url": "http://127.0.0.1/secret", "found_at": found},
                {"unlock_url": "http://93.184.216.34/nope", "publisher": "nope", "found_at": found},
                {"unlock_url": "http://93.184.216.34/missing-time"},
                "nope",
            ]
        }
    )
    code, out, err = _run(monkeypatch, capsys, ["add"], payload)
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == "added"
    assert lines[1].startswith("rejected:")
    assert "Private" in lines[1]
    assert lines[2] == "rejected: unknown publisher"
    assert lines[3] == "rejected: found_at is required"
    assert lines[4] == "rejected: find must be an object"
    assert len(db.list_gift_candidates()) == 1


def test_cli_archived_skips_the_queue(tmp_data, monkeypatch, capsys):
    canonical = "http://93.184.216.34/story"
    sid = db.allocate_id()
    db.insert_snapshot(sid, canonical, canonical)
    _make_usable(sid, canonical)
    payload = json.dumps(
        {"finds": [_live_find(canonical + "?unlocked_article_code=abc", canonical_url=canonical)]}
    )
    code, out, err = _run(monkeypatch, capsys, ["add"], payload)
    assert code == 0, err
    assert out.strip() == "archived"
    assert db.list_gift_candidates() == []
    code, out, err = _run(monkeypatch, capsys, ["add"], payload)
    assert out.strip() == "archived"
    assert db.list_gift_candidates() == []


def test_unparseable_input_is_the_only_add_failure(tmp_data, monkeypatch, capsys):
    code, out, err = _run(monkeypatch, capsys, ["add"], "not json")
    assert code == 1
    assert out == ""
    assert "parse" in err
    code, out, err = _run(monkeypatch, capsys, ["add"], '{"finds": "no"}')
    assert code == 1
    assert db.list_gift_candidates() == []


def test_later_expiry_and_canonical(tmp_data, allow_private):
    found = _now()
    unlock = "https://giftarticle.ft.com/giftarticle/actions/redeem/TOKEN"
    canonical = "https://www.ft.com/content/abc123"
    short = _iso(found + timedelta(days=1))
    later = _iso(found + timedelta(days=3))
    base = {"publisher": "ft", "title": "Markets", "found_at": _iso(found)}
    lines = add_finds(
        [
            {**base, "unlock_url": unlock, "expires_at": short},
            {**base, "unlock_url": unlock, "canonical_url": canonical, "expires_at": later},
            {
                "unlock_url": "https://giftarticle.ft.com/giftarticle/actions/redeem/OTHER",
                "canonical_url": canonical,
                "publisher": "ft",
                "found_at": _iso(found + timedelta(days=2)),
            },
        ]
    )
    assert lines == ["added", "refreshed", "refreshed"]
    rows = db.list_gift_candidates()
    assert len(rows) == 1
    assert rows[0]["canonical_url"] == canonical
    assert rows[0]["expires_at"] == later
    assert rows[0]["expires_estimated"] is False
    # The third find has no reported expiry, so it must not slide the deadline
    # or replace the unlock URL.
    assert rows[0]["unlock_url"] == unlock
    assert rows[0]["found_at"] == _iso(found)


def test_expiry_cap_differs_by_publisher(tmp_data):
    found = _now()
    lines = add_finds(
        [
            _live_find("http://93.184.216.34/nyt", found_at=_iso(found)),
            _live_find("http://93.184.216.34/wsj", publisher="wsj", found_at=_iso(found)),
            _live_find(
                "http://93.184.216.34/capped",
                found_at=_iso(found),
                expires_at=_iso(found + timedelta(days=30)),
            ),
            _live_find(
                "http://93.184.216.34/sooner",
                found_at=_iso(found),
                expires_at=_iso(found + timedelta(hours=5)),
            ),
        ]
    )
    assert lines == ["added", "added", "added", "added"]
    rows = {row["unlock_url"]: row for row in db.list_gift_candidates()}
    assert rows["http://93.184.216.34/nyt"]["expires_at"] == _iso(found + PUBLISHER_TTL["nyt"])
    assert rows["http://93.184.216.34/nyt"]["expires_estimated"] is True
    assert rows["http://93.184.216.34/wsj"]["expires_at"] == _iso(found + PUBLISHER_TTL["wsj"])
    assert rows["http://93.184.216.34/nyt"]["expires_at"] != rows["http://93.184.216.34/wsj"]["expires_at"]
    assert rows["http://93.184.216.34/capped"]["expires_at"] == _iso(found + PUBLISHER_TTL["nyt"])
    assert rows["http://93.184.216.34/capped"]["expires_estimated"] is True
    sooner = rows["http://93.184.216.34/sooner"]
    assert sooner["expires_at"] == _iso(found + timedelta(hours=5))
    assert sooner["expires_estimated"] is False


def test_expired_rows_leave_the_live_list(tmp_data):
    old = _live_find(
        "http://93.184.216.34/old",
        found_at=_iso(_now() - timedelta(days=40)),
        canonical_url="http://93.184.216.34/old-canonical",
    )
    assert add_finds([_live_find("http://93.184.216.34/live"), old]) == ["added", "added"]
    live = get_candidates_payload("live")
    assert [item["unlock_url"] for item in live["candidates"]] == ["http://93.184.216.34/live"]
    statuses = {item["unlock_url"]: item["status"] for item in get_candidates_payload("all")["candidates"]}
    assert statuses["http://93.184.216.34/old"] == "expired"
    assert statuses["http://93.184.216.34/live"] == "new"


def test_purge_keeps_a_recent_expiry_so_repeats_match(tmp_data, monkeypatch, capsys):
    ancient = _live_find("http://93.184.216.34/ancient", found_at=_iso(_now() - timedelta(days=80)))
    recent = _live_find("http://93.184.216.34/recent", found_at=_iso(_now() - timedelta(days=10)))
    live = _live_find("http://93.184.216.34/live")
    assert add_finds([ancient, recent, live]) == ["added", "added", "added"]
    code, out, err = _run(monkeypatch, capsys, ["purge"])
    assert code == 0, err
    assert out.strip() == "purged 1"
    left = {row["unlock_url"] for row in db.list_gift_candidates()}
    assert left == {"http://93.184.216.34/recent", "http://93.184.216.34/live"}
    again = add_finds([recent])
    assert again == ["refreshed"]
    assert len(db.list_gift_candidates()) == 2


def test_atlantic_gift_parameter_is_not_part_of_the_key(tmp_data, allow_private):
    base = "https://www.theatlantic.com/ideas/archive/2026/10/story/"
    assert article_match_key(base) == article_match_key(base + "?gift=aaa")
    assert article_match_key("https://example.com/story?gift=aaa") != article_match_key(
        "https://example.com/story"
    )
    found = _iso(_now())
    lines = add_finds(
        [
            {"unlock_url": base + "?gift=aaa", "publisher": "atlantic", "found_at": found, "title": "A"},
            {"unlock_url": base + "?gift=bbb", "publisher": "atlantic", "found_at": found, "title": "A"},
        ]
    )
    assert lines == ["added", "refreshed"]
    assert len(db.list_gift_candidates()) == 1


_ARTICLE_HOSTS = (
    ("nyt", "nytimes.com"),
    ("wsj", "wsj.com"),
    ("ft", "ft.com"),
    ("wapo", "washingtonpost.com"),
    ("atlantic", "theatlantic.com"),
    ("newyorker", "newyorker.com"),
)
_NOT_AN_ARTICLE = "rejected: unlock_url is not an allowed article URL"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.newyorker.com/magazine/2026/10/19/city-bridge",
        "https://newyorker.com/magazine/2026/10/19/city-bridge",
    ],
)
def test_newyorker_article_urls_are_accepted(tmp_data, allow_private, url):
    assert add_finds([_live_find(url, publisher="newyorker")]) == ["added"]
    row = db.list_gift_candidates()[0]
    assert row["publisher"] == "newyorker"
    assert row["unlock_url"] == url


def test_newyorker_www_and_bare_host_are_one_article(tmp_data, allow_private):
    bare = "https://newyorker.com/magazine/2026/10/19/city-bridge"
    www = "https://www.newyorker.com/magazine/2026/10/19/city-bridge"
    assert add_finds(
        [
            _live_find(bare, publisher="newyorker"),
            _live_find(www, publisher="newyorker"),
        ]
    ) == ["added", "refreshed"]
    assert len(db.list_gift_candidates()) == 1
    assert db.list_gift_candidates()[0]["match_key"] == article_match_key(www)


@pytest.mark.parametrize("path", ["/", "/newsletters", "/newsletters/"])
def test_newyorker_non_article_paths_match_other_publications(tmp_data, allow_private, path):
    finds = []
    for publisher, host in _ARTICLE_HOSTS:
        finds.append(_live_find(f"https://{host}{path}", publisher=publisher))
        finds.append(_live_find(f"https://www.{host}{path}", publisher=publisher))
    lines = add_finds(finds)
    assert lines == [_NOT_AN_ARTICLE] * len(finds)
    assert db.list_gift_candidates() == []


def test_unrelated_domain_is_rejected(tmp_data, allow_private):
    lines = add_finds(
        [
            _live_find("https://example.com/2026/10/19/story", publisher="other"),
            _live_find(
                "https://notnewyorker.com/magazine/2026/10/19/story",
                publisher="newyorker",
            ),
            _live_find(
                "https://newyorker.com.evil.example/magazine/2026/10/19/story",
                publisher="newyorker",
            ),
        ]
    )
    assert lines == [_NOT_AN_ARTICLE] * 3
    assert db.list_gift_candidates() == []


@pytest.mark.parametrize(
    "source",
    [
        "https://x.com/ada/status/1",
        "https://bsky.app/profile/ada/post/1",
        "https://mastodon.social/@ada/1",
        "https://www.threads.net/@ada/post/1",
        "https://example.com/notes/1",
    ],
)
def test_source_posts_are_accepted(tmp_data, allow_private, source):
    lines = add_finds(
        [
            {
                "unlock_url": "https://www.nytimes.com/2026/10/16/us/cities.html?unlocked_article_code=abc",
                "publisher": "nyt",
                "found_at": _iso(_now()),
                "title": "Cities",
                "x_post_url": source,
                "x_author": "@ada",
            }
        ]
    )
    assert lines == ["added"]
    row = db.list_gift_candidates()[0]
    assert row["source_url"] == source
    assert row["source_author"] == "@ada"
    assert "x_post_url" not in row


def test_candidates_table_is_idempotent_and_has_no_status(tmp_data):
    db.init_db()
    with db.connect() as conn:
        names = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    assert "status" not in names
    assert "clicked_at" in names and "dismissed_at" in names
    db.init_db()
    with db.connect() as conn:
        conn.execute("DROP TABLE candidates")
        conn.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY, unlock_url TEXT)")
        conn.commit()
    db.init_db()
    with db.connect() as conn:
        names = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    assert {"source_url", "source_author", "clicked_at", "dismissed_at", "expires_at", "match_key"} <= names
    assert "status" not in names
    with db.connect() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_candidates_match_key'"
        ).fetchone()["sql"]
    assert "UNIQUE" in sql.upper()


def test_match_key_index_upgrades_to_unique(tmp_data):
    with db.connect() as conn:
        conn.execute("DROP INDEX idx_candidates_match_key")
        conn.execute("CREATE INDEX idx_candidates_match_key ON candidates(match_key)")
        conn.execute(
            """
            INSERT INTO candidates (
                id, unlock_url, canonical_url, match_key, publisher, title,
                source_url, source_author, found_at, expires_at,
                expires_estimated, created_at
            ) VALUES
            ('abcdefg2', 'http://93.184.216.34/dup', NULL, '93.184.216.34/dup', 'nyt', NULL,
             NULL, NULL, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', 1, '2026-01-01T00:00:00Z'),
            ('abcdefg3', 'http://93.184.216.34/dup', NULL, '93.184.216.34/dup', 'nyt', NULL,
             NULL, NULL, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', 1, '2026-01-01T00:00:00Z')
            """
        )
        conn.commit()
    db.init_db()
    rows = db.list_gift_candidates()
    assert len(rows) == 1
    assert rows[0]["id"] == "abcdefg3"
    with db.connect() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_candidates_match_key'"
        ).fetchone()["sql"]
    assert "UNIQUE" in sql.upper()
    db.init_db()
    assert [row["id"] for row in db.list_gift_candidates()] == ["abcdefg3"]


def test_unique_index_upgrade_merges_the_older_click(tmp_data):
    """A pre-fix file can hold two rows for one key. The older one has the history."""
    with db.connect() as conn:
        conn.execute("DROP INDEX idx_candidates_match_key")
        conn.execute("CREATE INDEX idx_candidates_match_key ON candidates(match_key)")
        conn.execute(
            """
            INSERT INTO candidates (
                id, unlock_url, canonical_url, match_key, publisher, title,
                source_url, source_author, found_at, expires_at,
                expires_estimated, dismissed_at, clicked_at, snapshot_id, created_at
            ) VALUES
            (
                'clicked1', 'http://93.184.216.34/dup', 'http://93.184.216.34/canon',
                '93.184.216.34/dup', 'nyt', 'Cities after the storm',
                NULL, NULL, '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z',
                0, '2026-01-04T00:00:00Z', '2026-01-03T00:00:00Z', 'K7mQp',
                '2026-01-01T00:00:00Z'
            ),
            (
                'bare2222', 'http://93.184.216.34/dup', NULL,
                '93.184.216.34/dup', 'nyt', NULL,
                'http://8.8.8.8/post/1', '@ada', '2026-01-01T00:00:00Z',
                '2026-01-02T00:00:00Z',
                1, NULL, '2026-01-05T00:00:00Z', NULL,
                '2026-01-02T00:00:00Z'
            ),
            (
                'otherrow', 'http://93.184.216.34/other', NULL,
                '93.184.216.34/other', 'wsj', 'Other',
                NULL, NULL, '2026-01-01T00:00:00Z', '2026-01-03T00:00:00Z',
                1, NULL, NULL, NULL,
                '2026-01-01T00:00:00Z'
            )
            """
        )
        conn.commit()
    db.init_db()
    rows = {row["match_key"]: row for row in db.list_gift_candidates()}
    assert set(rows) == {"93.184.216.34/dup", "93.184.216.34/other"}
    kept = rows["93.184.216.34/dup"]
    assert kept["clicked_at"] == "2026-01-03T00:00:00Z"
    assert kept["dismissed_at"] == "2026-01-04T00:00:00Z"
    assert kept["snapshot_id"] == "K7mQp"
    assert kept["canonical_url"] == "http://93.184.216.34/canon"
    assert kept["title"] == "Cities after the storm"
    assert kept["expires_at"] == "2026-02-01T00:00:00Z"
    assert kept["expires_estimated"] is False
    assert kept["source_url"] == "http://8.8.8.8/post/1"
    assert kept["source_author"] == "@ada"
    other = rows["93.184.216.34/other"]
    assert other["id"] == "otherrow"
    assert other["title"] == "Other"
    assert other["clicked_at"] is None
    with db.connect() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_candidates_match_key'"
        ).fetchone()["sql"]
    assert "UNIQUE" in sql.upper()
    db.init_db()
    assert {row["id"] for row in db.list_gift_candidates()} == {kept["id"], "otherrow"}
    again = db.get_gift_candidate(kept["id"])
    assert again["snapshot_id"] == "K7mQp"
    assert again["clicked_at"] == "2026-01-03T00:00:00Z"
    assert again["dismissed_at"] == "2026-01-04T00:00:00Z"


def test_referrer_retry_is_off_for_a_gift_job():
    assert _should_referrer_retry({"skip_referrer_bounce": True}, {"paywalled": True}) is False
    assert _should_referrer_retry({}, {"paywalled": True}) is True
    assert _should_referrer_retry({}, {"paywalled": False}) is False


def test_gift_join_sets_skip_referrer_bounce(tmp_data):
    previous = capture.job_queue
    capture.jobs.clear()
    capture.job_queue = asyncio.Queue()
    try:
        url = "http://93.184.216.34/join"
        first = capture.enqueue_capture(url, url)
        assert "skip_referrer_bounce" not in capture.jobs[first]
        second = capture.enqueue_capture(
            url + "?unlocked_article_code=abc",
            url + "?unlocked_article_code=abc",
            skip_referrer_bounce=True,
        )
        assert second == first
        job = capture.jobs[first]
        assert job["skip_referrer_bounce"] is True
        assert _should_referrer_retry(job, {"paywalled": True}) is False
        third = capture.enqueue_capture(url, url)
        assert third == first
        assert capture.jobs[first]["skip_referrer_bounce"] is True
    finally:
        capture.jobs.clear()
        capture.job_queue = previous


def test_save_links_the_snapshot_and_skips_the_bounce(api):
    url = "http://93.184.216.34/story"
    row = _queue(url, canonical_url=url)
    saved = api.post(
        f"/api/candidates/{row['id']}/save",
        json={"url": "http://127.0.0.1/secret", "unlock_url": "http://169.254.169.254/"},
    )
    assert saved.status_code == 202
    body = saved.json()
    assert body["candidate"] == row["id"]
    assert body["status"] == "queued"
    assert len(body["id"]) == ID_LENGTH
    job = capture.jobs[body["id"]]
    assert job["skip_referrer_bounce"] is True
    assert job["url"] == url
    stored = db.get_gift_candidate(row["id"])
    assert stored["snapshot_id"] == body["id"]
    assert stored["clicked_at"]
    again = api.post(f"/api/candidates/{row['id']}/save", json={})
    assert again.status_code == 202
    assert again.json()["id"] == body["id"]
    saving = api.get("/api/candidates").json()["candidates"]
    assert saving[0]["status"] == "saving"
    assert saving[0]["id"] == row["id"]

    _make_usable(body["id"], url)
    exists = api.post(f"/api/candidates/{row['id']}/save", json={})
    assert exists.status_code == 200
    assert exists.json()["status"] == "exists"
    assert exists.json()["usable"] is True
    listed = api.get("/api/candidates")
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json()["candidates"][0]["status"] == "saved"
    assert add_finds([_live_find(url)]) == ["archived"]

    plain = capture.enqueue_capture("http://93.184.216.34/other", "http://93.184.216.34/other")
    assert "skip_referrer_bounce" not in capture.jobs[plain]

    db.update_snapshot(body["id"], status="failed", error="nope")
    failed = api.get("/api/candidates").json()["candidates"]
    match = next(item for item in failed if item["id"] == row["id"])
    assert match["status"] == "failed"
    with db.connect() as conn:
        cols = {col[1] for col in conn.execute("PRAGMA table_info(candidates)")}
    assert "status" not in cols


def test_expired_save_returns_410_and_does_not_capture(api):
    canonical = "http://93.184.216.34/old-canonical"
    row = _queue(
        "http://93.184.216.34/old",
        found_at=_iso(_now() - timedelta(days=40)),
        canonical_url=canonical,
    )
    live = api.get("/api/candidates")
    assert live.status_code == 200
    assert live.json()["candidates"] == []
    everyone = api.get("/api/candidates?include=all").json()["candidates"]
    assert everyone[0]["status"] == "expired"
    assert everyone[0]["expires_estimated"] is True
    saved = api.post(f"/api/candidates/{row['id']}/save", json={})
    assert saved.status_code == 410
    assert saved.json() == {"detail": "gift link expired", "canonical_url": canonical}
    assert db.get_gift_candidate(row["id"])["snapshot_id"] is None
    assert db.get_gift_candidate(row["id"])["clicked_at"] is None
    assert api.get("/api/candidates?include=bogus").status_code == 400


def test_gift_save_joining_an_inflight_job_skips_the_bounce(api):
    url = "http://93.184.216.34/join"
    plain = api.post("/api/save", json={"url": url})
    assert plain.status_code == 202
    job_id = plain.json()["id"]
    assert "skip_referrer_bounce" not in capture.jobs[job_id]
    row = _queue(url)
    saved = api.post(f"/api/candidates/{row['id']}/save", json={})
    assert saved.status_code == 202
    assert saved.json()["id"] == job_id
    job = capture.jobs[job_id]
    assert job["skip_referrer_bounce"] is True
    assert _should_referrer_retry(job, {"paywalled": True}) is False
    again = capture.enqueue_capture(url, url)
    assert again == job_id
    assert capture.jobs[job_id]["skip_referrer_bounce"] is True


def test_dismissed_and_expired_stays_dismissed(api):
    canonical = "http://93.184.216.34/old-canonical"
    row = _queue(
        "http://93.184.216.34/old-dismissed",
        found_at=_iso(_now() - timedelta(days=40)),
        canonical_url=canonical,
    )
    dismissed = api.post(f"/api/candidates/{row['id']}/dismiss", json={})
    assert dismissed.status_code == 200
    saved = api.post(f"/api/candidates/{row['id']}/save", json={})
    assert saved.status_code == 409
    assert saved.json()["detail"] == "dismissed"
    stored = db.get_gift_candidate(row["id"])
    assert stored["snapshot_id"] is None
    assert stored["clicked_at"] is None
    listed = api.get("/api/candidates?include=all").json()["candidates"]
    match = next(item for item in listed if item["id"] == row["id"])
    assert match["status"] == "dismissed"


def test_cross_site_post_is_forbidden(api):
    row = _queue()
    headers = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    saved = api.post(f"/api/candidates/{row['id']}/save", json={}, headers=headers)
    dismissed = api.post(f"/api/candidates/{row['id']}/dismiss", json={}, headers=headers)
    assert saved.status_code == 403
    assert dismissed.status_code == 403
    assert saved.json()["detail"] == "Cross-site requests are not allowed."
    stored = db.get_gift_candidate(row["id"])
    assert stored["clicked_at"] is None
    assert stored["dismissed_at"] is None
    assert stored["snapshot_id"] is None
    reading = api.get("/api/candidates", headers=headers)
    assert reading.status_code == 200


def test_writes_are_json_only_and_capped(api):
    row = _queue()
    plain = api.post(
        f"/api/candidates/{row['id']}/save",
        content=b"{}",
        headers={"Content-Type": "text/plain"},
    )
    assert plain.status_code == 415
    huge = b'{"pad":"' + (b"x" * 5000) + b'"}'
    capped = api.post(
        f"/api/candidates/{row['id']}/dismiss",
        content=huge,
        headers={"Content-Type": "application/json"},
    )
    assert capped.status_code == 413
    stored = db.get_gift_candidate(row["id"])
    assert stored["clicked_at"] is None
    assert stored["dismissed_at"] is None
    missing = api.post("/api/candidates/not-an-id/dismiss", json={})
    assert missing.status_code == 404


def test_host_allowlist_still_rejects_other_hosts(api):
    rejected = api.get("/api/candidates", headers={"Host": "evil.example"})
    assert rejected.status_code == 400


def test_save_revalidates_a_private_stored_url(api):
    row = db.insert_gift_candidate(
        unlock_url="http://127.0.0.1/secret",
        canonical_url=None,
        match_key="127.0.0.1/secret",
        publisher="other",
        title="nope",
        source_url=None,
        source_author=None,
        found_at=_iso(_now()),
        expires_at=_iso(_now() + timedelta(days=1)),
        expires_estimated=False,
    )
    saved = api.post(f"/api/candidates/{row['id']}/save", json={})
    assert saved.status_code == 400
    assert db.get_gift_candidate(row["id"])["snapshot_id"] is None
    assert db.get_gift_candidate(row["id"])["clicked_at"] is None


def test_dismiss_is_idempotent_and_hides_the_row(api):
    row = _queue()
    first = api.post(f"/api/candidates/{row['id']}/dismiss", json={})
    second = api.post(f"/api/candidates/{row['id']}/dismiss", json={})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["dismissed_at"] == second.json()["dismissed_at"]
    assert api.get("/api/candidates").json()["candidates"] == []
    listed = api.get("/api/candidates?include=all").json()["candidates"]
    assert listed[0]["status"] == "dismissed"
    blocked = api.post(f"/api/candidates/{row['id']}/save", json={})
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "dismissed"


def test_events_cli_prints_clicks_and_dismissals(api, monkeypatch, capsys):
    # Bluesky is a hostname. Stub DNS so this test does not open a socket;
    # the unlock URL itself is a public IP literal.
    monkeypatch.setattr("app.security.host_is_public", lambda host: True)
    find = _live_find("http://93.184.216.34/events")
    del find["source_url"]
    del find["source_author"]
    find["x_post_url"] = "https://bsky.app/profile/ada/post/9"
    find["x_author"] = "@ada.bsky"
    assert add_finds([find]) == ["added"]
    row = db.list_gift_candidates()[0]
    saved = api.post(f"/api/candidates/{row['id']}/save", json={})
    assert saved.status_code == 202
    dismissed = api.post(f"/api/candidates/{row['id']}/dismiss", json={})
    assert dismissed.status_code == 200
    since = _iso(_now() - timedelta(days=1))
    code, out, err = _run(monkeypatch, capsys, ["list", "--events", "--since", since])
    assert code == 0, err
    events = json.loads(out)["events"]
    assert [event["event"] for event in events] == ["click", "dismiss"]
    assert events[0]["id"] == row["id"]
    assert events[0]["source_url"] == "https://bsky.app/profile/ada/post/9"
    assert events[0]["source_author"] == "@ada.bsky"
    assert events[0]["publisher"] == "nyt"
    clicked = events[0]["at"]
    code, out, err = _run(monkeypatch, capsys, ["list", "--events", "--since", clicked])
    assert any(event["event"] == "click" for event in json.loads(out)["events"])
    future = _iso(_now() + timedelta(days=1))
    code, out, err = _run(monkeypatch, capsys, ["list", "--events", "--since", future])
    assert json.loads(out)["events"] == []
    code, _out, err = _run(monkeypatch, capsys, ["list", "--events"])
    assert code == 2
    assert "since" in err
    code, _out, err = _run(monkeypatch, capsys, ["list", "--events", "--since", "yesterday"])
    assert code == 2


def test_cli_add_does_not_touch_the_network(tmp_data, monkeypatch, capsys):
    import app.api  # noqa: F401
    import app.main  # noqa: F401

    def _boom(*_args, **_kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    payload = json.dumps({"finds": [_live_find()], "extra": 1})
    code, out, err = _run(monkeypatch, capsys, ["add"], payload)
    assert code == 0, err
    assert out.strip() == "added"


def test_cli_works_with_the_server_down(tmp_path):
    env = {**__import__("os").environ, "AMBER_DATA_DIR": str(tmp_path / "amber")}
    payload = json.dumps({"finds": [_live_find("http://93.184.216.34/offline")]})

    def run():
        return subprocess.run(
            [sys.executable, "-m", "app.candidates", "add"],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            cwd=str(ROOT),
            check=False,
        )

    first = run()
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "added"
    second = run()
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "refreshed"


def test_saved_yesterday_is_not_on_the_live_list(tmp_data):
    row = _queue()
    sid = db.allocate_id()
    db.insert_snapshot(sid, row["unlock_url"], row["unlock_url"])
    db.update_snapshot(sid, status="complete", title="Cities after the storm", word_count=10)
    yesterday = _iso(_now() - timedelta(days=1))
    db.update_gift_candidate(row["id"], snapshot_id=sid, clicked_at=yesterday)
    assert get_candidates_payload("live")["candidates"] == []
    assert get_candidates_payload("all")["candidates"][0]["status"] == "saved"
