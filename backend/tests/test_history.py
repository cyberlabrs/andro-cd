"""Sync-history timeline: duration + images persist and round-trip."""
import pytest

from app import db
from app.config import settings


@pytest.fixture()
def sqlite_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/hist.db"
    monkeypatch.setattr(settings, "database_url", url)
    # reset module-level session so init_db rebuilds against the temp file
    monkeypatch.setattr(db, "_Session", None)
    monkeypatch.setattr(db, "_engine", None)
    assert db.init_db() is True
    yield
    monkeypatch.setattr(db, "_Session", None)


def test_record_and_read_timeline_fields(sqlite_db):
    db.record_sync("web", "abc123def456", "Succeeded",
                   ["registered task definition web:5", "updated service 'web'"],
                   "ok", duration_ms=3200, images=["nginx:1.27", "sidecar:v2"])
    hist = db.get_history("web")
    assert len(hist) == 1
    entry = hist[0]
    assert entry["durationMs"] == 3200
    assert entry["images"] == ["nginx:1.27", "sidecar:v2"]
    assert entry["commit"] == "abc123def456"
    assert entry["status"] == "Succeeded"


def test_defaults_when_omitted(sqlite_db):
    db.record_sync("web", None, "Error", [], "boom")
    entry = db.get_history("web")[0]
    assert entry["durationMs"] == 0
    assert entry["images"] == []


def test_history_ordered_newest_first(sqlite_db):
    for i in range(3):
        db.record_sync("web", f"c{i}", "Succeeded", [], "", duration_ms=i * 100)
    hist = db.get_history("web", limit=2)
    assert [h["commit"] for h in hist] == ["c2", "c1"]


def test_migration_adds_columns_to_legacy_table(tmp_path, monkeypatch):
    """A sync_history table created before the timeline columns gets them via _migrate."""
    from sqlalchemy import create_engine, inspect, text

    path = f"{tmp_path}/legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text(
            'CREATE TABLE sync_history (id INTEGER PRIMARY KEY, app_name VARCHAR(255), '
            '"commit" VARCHAR(64), status VARCHAR(32), message TEXT, actions TEXT, created_at DATETIME)'
        ))
    engine.dispose()

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{path}")
    monkeypatch.setattr(db, "_Session", None)
    monkeypatch.setattr(db, "_engine", None)
    assert db.init_db() is True

    cols = {c["name"] for c in inspect(db._engine).get_columns("sync_history")}
    assert {"duration_ms", "images"} <= cols
    # and it's usable
    db.record_sync("x", "c", "Succeeded", [], "", duration_ms=50, images=["i:1"])
    assert db.get_history("x")[0]["durationMs"] == 50
    monkeypatch.setattr(db, "_Session", None)
