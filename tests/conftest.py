from __future__ import annotations

import pytest


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    data = tmp_path / "data"
    snaps = data / "snaps"
    db_path = data / "amber.sqlite3"
    monkeypatch.setattr("app.config.DATA_DIR", data)
    monkeypatch.setattr("app.config.SNAPS_DIR", snaps)
    monkeypatch.setattr("app.config.DB_PATH", db_path)
    monkeypatch.setattr("app.db.DATA_DIR", data)
    monkeypatch.setattr("app.db.SNAPS_DIR", snaps)
    monkeypatch.setattr("app.db.DB_PATH", db_path)
    from app import db

    db.init_db()
    return data


@pytest.fixture
def allow_private(monkeypatch):
    monkeypatch.setattr("app.security.host_is_public", lambda host: True)
