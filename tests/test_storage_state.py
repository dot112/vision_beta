"""Data safety foundations: the new settings, the storage alarms, the storage state file and the WAL size limit."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db.session import _sqlite_pragmas
from app.events.alarm_events import ALARM_CATALOG
from app.services import storage_state

CATALOG = {"storage.low": "warning", "storage.full": "critical", "storage.records_lost": "critical",
           "storage.clock_check": "warning", "storage.settings_restored": "critical",
           "storage.database_restored": "critical", "storage.archive_failed": "warning",
           "storage.backup_failed": "warning", "storage.copy_failed": "warning"}


def test_settings_defaults_and_limits():
    s = Settings()
    assert (s.STORAGE_MIN_FREE_MB, s.STORAGE_WARN_PERCENT, s.DETECTIONS_RETENTION_DAYS, s.ARCHIVE_RECORDS,
            s.ARCHIVE_RETENTION_DAYS, s.BACKUP_KEEP_NIGHTLY, s.BACKUP_COPY_DIR, s.AUDIT_RETENTION_DAYS) == \
           (2048, 85, 30, True, 365, 7, "", 365)
    for name, bad in (("STORAGE_MIN_FREE_MB", -1), ("STORAGE_WARN_PERCENT", 0), ("STORAGE_WARN_PERCENT", 101),
                      ("DETECTIONS_RETENTION_DAYS", -1), ("ARCHIVE_RETENTION_DAYS", -1),
                      ("BACKUP_KEEP_NIGHTLY", 0), ("AUDIT_RETENTION_DAYS", 0)):
        with pytest.raises(ValidationError):
            Settings(**{name: bad})


def test_the_storage_alarms_are_in_the_catalogue():
    entries = {e["code"]: e for e in ALARM_CATALOG if e["code"].startswith("storage.")}
    assert {c: e["severity"] for c, e in entries.items()} == CATALOG
    assert all(e["group"] == "Storage" and e["scope"] == "server" and e["label"] and e["description"]
               for e in entries.values())


def test_state_round_trip_and_atomic_write(data_dir):
    storage_state.update(lambda s: s.setdefault("last", {}).update(archive="2026-10-26T00:10:00+00:00"))
    assert storage_state.load()["last"]["archive"] == "2026-10-26T00:10:00+00:00"
    assert sorted(p.name for p in data_dir.iterdir()) == ["storage_state.json"]


def test_a_damaged_state_file_starts_empty(data_dir, caplog):
    (data_dir / "storage_state.json").write_text("{cut", encoding="utf-8")
    assert storage_state.load() == {}
    assert "storage_state.json" in caplog.text


def test_notices_replace_by_code_and_close(data_dir):
    storage_state.add_notice("storage.full", "first")
    storage_state.add_notice("storage.full", "second", {"n": 2})
    assert [(n["code"], n["message"], n["details"]) for n in storage_state.notices()] == [("storage.full", "second", {"n": 2})]
    assert storage_state.close_notice("storage.full") is True and storage_state.notices() == []
    assert storage_state.close_notice("storage.full") is False


def test_stamps():
    t = datetime(2026, 10, 26, 0, 10, 0, 123456, tzinfo=timezone.utc)
    assert storage_state.stamp(t) == "20261026T001000Z"
    assert storage_state.stamp(t, micro=True) == "20261026T001000123456Z"


def test_the_wal_file_is_limited_to_64_mb(tmp_path):
    con = sqlite3.connect(tmp_path / "x.db")
    _sqlite_pragmas(con, None)
    assert con.execute("PRAGMA journal_size_limit").fetchone()[0] == 64 * 1024 * 1024
