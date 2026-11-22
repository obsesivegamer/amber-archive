"""Import rendered HTML on Dell-PLX, then converge the Mac copy.

The reader open is the one the push tests use: ``amber.sqlite3`` with
``mode=ro`` and no ``immutable`` flag, complete rows only. That is the list
amber-reader serves after the mode=ro patch.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from app.library_contract import sha256_hex
from app.library_ingest import create_app
from app.library_pull import pull
from app.library_push import backfill, push_snapshot
from tests.test_library_push import SID, reader_rows, write_mac, write_token

INGEST = "test-ingest-token"
IMPORT = "test-import-token"
URL = "https://1.1.1.1/stories/bridge"
MARK = "fund a new bridge over the river"


def article(title: str, marker: str) -> str:
    paragraph = (
        f"<p>The city council voted last night to {marker}. "
        "Residents argued about tolls, repairs, and the bus route.</p>"
    )
    body = "\n".join(paragraph for _ in range(15))
    return f"""<!doctype html>
<html>
<head>
  <title>Ignore</title>
  <meta property="og:title" content="{title}">
  <meta property="og:site_name" content="The Daily Test">
  <meta name="author" content="Jane Reporter">
</head>
<body>
  <article>
    <h1>{title}</h1>
    {body}
  </article>
</body>
</html>
"""


def payload(html: str | None = None, url: str = URL, **extra) -> dict:
    body = {
        "url": url,
        "final_url": url,
        "title": "City Council Approves Bridge",
        "html": html if html is not None else article("City Council Approves Bridge", MARK),
    }
    body.update(extra)
    return body


class ClientTransport:
    def __init__(self, client: TestClient):
        self.client = client

    def _target(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.query:
            return f"{parsed.path}?{parsed.query}"
        return parsed.path

    def round_trip(self, method, url, body, headers):
        response = self.client.request(
            method, self._target(url), content=body, headers=headers
        )
        if not response.content:
            return response.status_code, {}
        return response.status_code, response.json()

    def round_trip_bytes(self, method, url, body, headers):
        response = self.client.request(
            method, self._target(url), content=body, headers=headers
        )
        return response.status_code, response.content


def boot(tmp_path: Path):
    ingest = write_token(tmp_path / "ingest.token", INGEST)
    importer = write_token(tmp_path / "import.token", IMPORT)
    library = tmp_path / "library"
    app = create_app(library, ingest, importer)
    client = TestClient(app)
    client.__enter__()
    return library, client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def change_seqs(data_dir: Path) -> list[int]:
    conn = sqlite3.connect(data_dir / "amber.sqlite3")
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT change_seq FROM library_changes ORDER BY change_seq"
            )
        ]
    finally:
        conn.close()


def file_hashes(data_dir: Path, sid: str) -> dict[str, str]:
    snap = data_dir / "snaps" / sid
    found = {}
    for path in sorted(snap.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        found[path.relative_to(snap).as_posix()] = sha256_hex(path.read_bytes())
    return found


def blob_files(data_dir: Path) -> list[Path]:
    root = data_dir / "blobs"
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def test_import_rejects_a_missing_or_wrong_token(tmp_path):
    _library, client = boot(tmp_path)
    body = payload()
    missing = client.post("/v1/import", json=body)
    wrong = client.post("/v1/import", json=body, headers=auth("nope"))
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json()["detail"] == "unauthorized"
    denied = client.post(
        "/v1/revisions",
        content=b"{}",
        headers={**auth(IMPORT), "Content-Type": "application/json"},
    )
    changes = client.get("/v1/changes", headers=auth(IMPORT))
    escaped = client.get(
        "/v1/snapshots/K7mQp/files/..%2F..%2Fingest-store-id",
        headers=auth(INGEST),
    )
    assert denied.status_code == 401
    assert changes.status_code == 401
    assert escaped.status_code in {400, 404}
    assert b"ingest-store" not in escaped.content


def test_import_stores_a_snapshot_the_reader_lists(tmp_path):
    library, client = boot(tmp_path)
    response = client.post("/v1/import", json=payload(), headers=auth(IMPORT))
    assert response.status_code == 201
    body = response.json()
    sid = body["id"]
    assert body["status"] == "complete"
    assert body["title"] == "City Council Approves Bridge"
    assert body["word_count"] >= 120
    assert body["usable"] is True
    assert body["revision"] == 1
    assert body["change_seq"] == 1
    rows = reader_rows(library)
    assert [row["id"] for row in rows] == [sid]
    assert rows[0]["title"] == "City Council Approves Bridge"
    assert rows[0]["status"] == "complete"
    assert rows[0]["url"] == URL
    text = (library / "snaps" / sid / "article.txt").read_text(encoding="utf-8")
    assert MARK in text
    assert (library / "snaps" / sid / "reader.html").is_file()
    assert blob_files(library) == []
    assert change_seqs(library) == [1]

    again = client.post(
        "/v1/import",
        json=payload(),
        headers=auth(INGEST),
    )
    assert again.status_code == 200
    assert again.json()["id"] == sid
    assert again.json()["change_seq"] == 1
    tracked = payload(url=URL + "?utm_source=gift")
    tracked["final_url"] = URL + "?utm_source=gift"
    repeat = client.post("/v1/import", json=tracked, headers=auth(IMPORT))
    assert repeat.status_code == 200
    assert repeat.json()["id"] == sid
    assert change_seqs(library) == [1]
    assert len([path for path in (library / "snaps").iterdir() if path.is_dir()]) == 1


def test_import_of_different_html_for_the_same_url_conflicts(tmp_path):
    library, client = boot(tmp_path)
    first = client.post("/v1/import", json=payload(), headers=auth(IMPORT))
    assert first.status_code == 201
    sid = first.json()["id"]
    other = article("City Council Approves Bridge", "close the ferry instead")
    second = client.post("/v1/import", json=payload(html=other), headers=auth(IMPORT))
    assert second.status_code == 409
    assert second.json()["detail"] == "already archived"
    assert second.json()["id"] == sid
    assert change_seqs(library) == [1]
    assert [row["id"] for row in reader_rows(library)] == [sid]


def test_import_rejects_a_private_url_and_a_different_final_site(tmp_path):
    _library, client = boot(tmp_path)
    private = client.post(
        "/v1/import",
        json=payload(url="http://127.0.0.1/secret"),
        headers=auth(IMPORT),
    )
    assert private.status_code == 400
    other = payload(url="https://1.1.1.1/stories/bridge")
    other["final_url"] = "https://8.8.8.8/stories/bridge"
    crossed = client.post("/v1/import", json=other, headers=auth(IMPORT))
    assert crossed.status_code == 400
    assert crossed.json()["detail"] == "final_url is a different site"


def test_import_then_pull_then_push_does_not_drift(tmp_path):
    library, client = boot(tmp_path)
    created = client.post("/v1/import", json=payload(), headers=auth(IMPORT))
    assert created.status_code == 201
    sid = created.json()["id"]
    assert reader_rows(library)[0]["id"] == sid
    assert MARK in (library / "snaps" / sid / "article.txt").read_text(encoding="utf-8")
    assert change_seqs(library) == [1]

    mac = tmp_path / "mac"
    write_mac(mac)
    mac_before = file_hashes(mac, SID)
    transport = ClientTransport(client)

    first_pull = pull(mac, "http://ingest", INGEST, transport=transport)
    assert [(item.sid, item.action) for item in first_pull.items] == [(sid, "stored")]
    assert file_hashes(mac, sid) == file_hashes(library, sid)
    assert {row["id"] for row in reader_rows(mac)} == {SID, sid}
    assert {row["id"] for row in reader_rows(library)} == {sid}

    sent = backfill(mac, "http://ingest", INGEST, transport=transport)
    assert [(item.sid, item.ok, item.status) for item in sent] == [(SID, True, 201)]
    assert change_seqs(library) == [1, 2]
    assert {row["id"] for row in reader_rows(mac)} == {row["id"] for row in reader_rows(library)}

    replay = push_snapshot(mac, sid, "http://ingest", INGEST, transport=transport)
    assert replay.status == 200
    assert replay.change_seq == 1
    assert change_seqs(library) == [1, 2]
    assert file_hashes(mac, sid) == file_hashes(library, sid)

    second_pull = pull(mac, "http://ingest", INGEST, transport=transport)
    assert [(item.sid, item.action) for item in second_pull.items] == [(SID, "skipped")]
    assert file_hashes(mac, SID) == mac_before

    assert backfill(mac, "http://ingest", INGEST, transport=transport) == []
    seqs = change_seqs(library)
    assert seqs == [1, 2]
    assert all(later > earlier for earlier, later in zip(seqs, seqs[1:]))
    assert {row["id"] for row in reader_rows(mac)} == {row["id"] for row in reader_rows(library)}
    assert json.loads((library / "snaps" / sid / "meta.json").read_text(encoding="utf-8"))["imported"] is True
