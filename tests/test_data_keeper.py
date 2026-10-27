"""The data keeper: the schedule of cleanup, archive, backups and copies, and the clock guard."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.events.alarm_events import alarm_manager
from app.services import (
    audit_log,
    backup_service,
    detections_cleanup,
    production_records_service,
    records_archive,
    storage_state,
)
from app.services.backup_service import mirror as real_mirror
from app.services.data_keeper import ClockGuard, DataKeeper
from app.services.production_records_service import ProductionRecorder
from tests.test_production_records import sessions  # noqa: F401  (a fixture)

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 25, tzinfo=timezone.utc)


def _active(code):
    return alarm_manager.get(code, storage_state.STORAGE_SOURCE) is not None


@pytest.fixture(autouse=True)
def _clean(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_RECORDS", True)
    monkeypatch.setattr(settings, "BACKUP_COPY_DIR", "")
    monkeypatch.setattr(settings, "RECORDS_RETENTION_DAYS", 90)
    alarm_manager.reset()
    yield
    alarm_manager.reset()


# ── The clock guard ───────────────────────────────────────────────────────────

def test_a_cleanup_that_would_delete_every_record_pauses():
    g = ClockGuard()
    assert g.check_start(newest=NOW - timedelta(days=120), now=NOW, days=90) == "would_delete_everything"
    assert g.paused() and _active("storage.clock_check")
    assert g.check_start(newest=NOW - timedelta(days=10), now=NOW, days=90) is None and g.paused()   # stays until confirmed


def test_confirming_with_the_same_newest_record_does_not_pause_again():
    g = ClockGuard()
    newest = NOW - timedelta(days=120)
    g.check_start(newest, NOW, 90)
    g.confirm(newest, NOW)
    assert not g.paused() and not _active("storage.clock_check")
    assert g.check_start(newest, NOW + timedelta(hours=1), 90) is None and not g.paused()
    # A newer record that is too old again is a new question.
    assert g.check_start(newest + timedelta(days=1), NOW + timedelta(days=200), 90) == "would_delete_everything"


def test_records_inside_the_kept_days_and_no_records_do_not_pause():
    g = ClockGuard()
    assert g.check_start(NOW - timedelta(days=89), NOW, 90) is None
    assert g.check_start(None, NOW, 90) is None
    assert g.check_start(NOW - timedelta(days=400), NOW, 0) is None      # kept for ever: nothing is deleted by age
    assert not g.paused()


def test_a_forward_jump_while_running_pauses():
    g = ClockGuard()
    assert g.check_jump(NOW, 1000.0, newest=NOW) is None
    assert g.check_jump(NOW + timedelta(days=3), 1060.0, newest=NOW) == "clock_jumped"
    assert g.paused() and _active("storage.clock_check")


def test_small_corrections_do_not_pause():
    g = ClockGuard()
    g.check_jump(NOW, 1000.0, newest=NOW)
    assert g.check_jump(NOW + timedelta(hours=5), 1060.0, newest=NOW) is None       # a big NTP step, still under a day
    assert g.check_jump(NOW - timedelta(days=2), 1120.0, newest=NOW) is None        # backwards deletes less, not more
    assert not g.paused()


def test_a_jump_that_lands_near_the_newest_record_does_not_pause():
    g = ClockGuard()
    boot = datetime(2000, 1, 1, tzinfo=timezone.utc)
    g.check_jump(boot, 10.0, newest=NOW)
    assert g.check_jump(NOW + timedelta(hours=2), 70.0, newest=NOW) is None and not g.paused()


def test_the_pause_survives_a_restart():
    ClockGuard().check_start(NOW - timedelta(days=120), NOW, 90)
    g = ClockGuard()
    assert g.paused()
    assert storage_state.load()["clock"]["reason"] == "would_delete_everything"


def test_a_pause_holds_even_when_the_state_file_cannot_be_written(monkeypatch):
    def broken(change):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(storage_state, "update", broken)
    g = ClockGuard()
    assert g.check_start(NOW - timedelta(days=120), NOW, 90) == "would_delete_everything"
    assert g.paused() and _active("storage.clock_check")


# ── The keeper ────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, now=NOW):
        self.now, self.mono = now, 1000.0

    def wall(self):
        return self.now

    def monotonic(self):
        return self.mono

    def advance(self, **kwargs):
        step = timedelta(**kwargs)
        self.now += step
        self.mono += step.total_seconds()


class FakeRecorder:
    def __init__(self, calls):
        self.calls = calls

    async def prune(self, days=None, now=None, before=None):
        self.calls.append(("records.prune", (), {"days": days, "now": now, "before": before}))
        return 0


class FakeGuard:
    def __init__(self, calls):
        self.calls = calls

    async def check(self, now):
        self.calls.append(("guard.check", (now,), {}))
        return {"state": "ok"}

    async def status(self):
        return {"total": 1, "free": 1, "room": 0, "used_percent": 1.0, "floor": 0, "warn_percent": 85.0, "state": "ok"}


@pytest.fixture
def backups():
    """What backup_service.list_backups answers."""
    return []


@pytest.fixture
def spies(monkeypatch, backups):
    calls = []

    def spy(name, result=None):
        async def run(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result
        return run

    def sync_spy(name, result=None):
        def run(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result
        return run

    monkeypatch.setattr(detections_cleanup, "prune", spy("detections.prune", 0))
    monkeypatch.setattr(records_archive, "archive_pending", spy("archive_pending", []))
    monkeypatch.setattr(records_archive, "archived_until", spy("archived_until", UNTIL))
    monkeypatch.setattr(records_archive, "prune_files", sync_spy("prune_files", 0))
    monkeypatch.setattr(backup_service, "make_backup", sync_spy("make_backup", {"name": "b", "kind": "nightly"}))
    monkeypatch.setattr(backup_service, "list_backups", lambda: list(backups))
    monkeypatch.setattr(backup_service, "mirror", sync_spy("mirror", {"copied": 0, "removed": 0}))
    monkeypatch.setattr(audit_log, "prune", sync_spy("audit.prune", 0))
    monkeypatch.setattr(production_records_service, "prune_kept_totals", spy("prune_kept_totals", 0))
    return calls


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def keeper(spies, clock, sessions):
    return DataKeeper(session_factory=sessions, recorder=FakeRecorder(spies), guard=FakeGuard(spies),
                      clock=clock.wall, monotonic=clock.monotonic)


def _names(calls):
    return [call[0] for call in calls]


def _skip_first_run(keeper):
    async def nothing():
        return None
    keeper.first_run = nothing


AGE_JOBS = {"records.prune", "detections.prune", "prune_files", "audit.prune", "prune_kept_totals"}


def test_hourly_runs_every_age_cleanup_with_its_setting(keeper, spies, monkeypatch):
    monkeypatch.setattr(settings, "DETECTIONS_RETENTION_DAYS", 30)
    monkeypatch.setattr(settings, "ARCHIVE_RETENTION_DAYS", 365)
    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 365)
    asyncio.run(keeper.hourly(NOW))
    by_name = {name: (args, kwargs) for name, args, kwargs in spies}
    assert AGE_JOBS <= set(by_name)
    assert by_name["records.prune"][1]["before"] == UNTIL and by_name["records.prune"][1]["now"] == NOW
    assert by_name["detections.prune"][0][1:] == (30, NOW)
    assert by_name["prune_files"][0] == (365, NOW) and by_name["audit.prune"][0] == (365, NOW)
    assert storage_state.load()["last"]["cleanup"] == NOW.isoformat()


def test_hourly_prunes_records_only_up_to_the_archived_days(keeper, spies, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_RECORDS", False)
    asyncio.run(keeper.hourly(NOW))
    assert [c[2]["before"] for c in spies if c[0] == "records.prune"] == [None]
    # With the archive on and no records at all, nothing is deleted (None is not "no limit").
    monkeypatch.setattr(settings, "ARCHIVE_RECORDS", True)
    monkeypatch.setattr(records_archive, "archived_until", lambda *a: asyncio.sleep(0, result=None))
    spies.clear()
    asyncio.run(keeper.hourly(NOW))
    assert "records.prune" not in _names(spies) and "detections.prune" in _names(spies)


def test_hourly_skips_age_cleanup_while_paused_but_the_floor_still_runs(keeper, spies):
    keeper.clock_guard.check_start(NOW - timedelta(days=120), NOW, 90)
    asyncio.run(keeper.hourly(NOW))
    assert not AGE_JOBS & set(_names(spies))
    _skip_first_run(keeper)
    asyncio.run(keeper.tick())
    assert "guard.check" in _names(spies)
    assert not AGE_JOBS & set(_names(spies))


def test_a_settings_restore_notice_holds_the_record_cleanup(keeper, spies):
    storage_state.add_notice("storage.settings_restored", "loaded from a copy")
    asyncio.run(keeper.hourly(NOW))
    names = set(_names(spies))
    assert "records.prune" not in names and "prune_kept_totals" not in names
    assert {"detections.prune", "prune_files", "audit.prune"} <= names


def test_a_failing_cleanup_step_does_not_stop_the_others(keeper, spies, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(detections_cleanup, "prune", boom)
    asyncio.run(keeper.hourly(NOW))
    assert {"records.prune", "prune_files", "audit.prune"} <= set(_names(spies))


def test_nightly_runs_once_per_utc_day_after_00_10(keeper, spies, clock):
    clock.now = datetime(2026, 10, 26, 0, 5, tzinfo=timezone.utc)
    _skip_first_run(keeper)
    asyncio.run(keeper.tick())
    assert "archive_pending" not in _names(spies)
    for step in ({"minutes": 6}, {"hours": 5}, {"hours": 19}):     # 00:11, 05:11, the next day 00:11
        clock.advance(**step)
        asyncio.run(keeper.tick())
    assert _names(spies).count("archive_pending") == 2
    assert [c[1][0] for c in spies if c[0] == "make_backup"] == ["nightly", "nightly"]


def test_with_the_archive_off_no_file_is_written(keeper, spies, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_RECORDS", False)
    asyncio.run(keeper.nightly(NOW))
    assert "archive_pending" not in _names(spies) and "make_backup" in _names(spies)


def test_first_run_archives_and_backs_up_only_without_a_nightly_backup_today(keeper, spies, backups):
    backups.append({"name": "20261026T001000Z-nightly", "kind": "nightly", "made_at": "2026-10-26T00:10:00+00:00", "size": 1})
    asyncio.run(keeper.first_run())
    assert "archive_pending" in _names(spies) and "make_backup" not in _names(spies)
    backups[0] = {"name": "20261025T001000Z-nightly", "kind": "nightly", "made_at": "2026-10-25T00:10:00+00:00", "size": 1}
    spies.clear()
    asyncio.run(keeper.first_run())
    assert [c[1][0] for c in spies if c[0] == "make_backup"] == ["nightly"]


def test_a_failing_job_raises_its_alarm_and_the_next_still_runs(keeper, spies, monkeypatch):
    async def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    real = records_archive.archive_pending
    monkeypatch.setattr(records_archive, "archive_pending", full)
    asyncio.run(keeper.nightly(NOW))
    assert _active("storage.archive_failed") and "make_backup" in _names(spies)
    monkeypatch.setattr(records_archive, "archive_pending", real)
    asyncio.run(keeper.nightly(NOW + timedelta(days=1)))
    assert not _active("storage.archive_failed")


def test_a_failing_backup_raises_and_clears(keeper, spies, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")
    real = backup_service.make_backup
    monkeypatch.setattr(backup_service, "make_backup", broken)
    asyncio.run(keeper.nightly(NOW))
    assert _active("storage.backup_failed")
    monkeypatch.setattr(backup_service, "make_backup", real)
    asyncio.run(keeper.nightly(NOW + timedelta(days=1)))
    assert not _active("storage.backup_failed")


def test_copy_failed_raises_and_clears(keeper, spies, tmp_path, monkeypatch):
    monkeypatch.setattr(backup_service, "mirror", real_mirror)
    blocker = tmp_path / "a file"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "BACKUP_COPY_DIR", str(blocker))
    asyncio.run(keeper.hourly(NOW))
    assert _active("storage.copy_failed")
    monkeypatch.setattr(settings, "BACKUP_COPY_DIR", str(tmp_path / "usb"))
    asyncio.run(keeper.hourly(NOW))
    assert not _active("storage.copy_failed")
    assert storage_state.load()["last"]["copy"] == NOW.isoformat()


def test_first_run_raises_the_alarms_of_open_notices(keeper):
    storage_state.add_notice("storage.records_lost", "2 records deleted")
    asyncio.run(keeper.first_run())
    alarm = alarm_manager.get("storage.records_lost", storage_state.STORAGE_SOURCE)
    assert alarm is not None and alarm.severity == "critical" and alarm.message == "2 records deleted"


def test_the_clock_pause_alarm_comes_back_after_a_restart(keeper):
    ClockGuard().check_start(NOW - timedelta(days=120), NOW, 90)
    alarm_manager.reset()
    asyncio.run(keeper.first_run())
    assert _active("storage.clock_check")


def test_confirming_the_clock_lets_the_cleanup_run_again(keeper, spies):
    keeper.clock_guard.check_start(NOW - timedelta(days=120), NOW, 90)
    asyncio.run(keeper.confirm_clock())
    asyncio.run(keeper.hourly(NOW))
    assert not keeper.clock_guard.paused() and "records.prune" in _names(spies)


def test_backup_now_makes_a_manual_backup_and_copies_it(keeper, spies, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_COPY_DIR", str(tmp_path / "usb"))
    entry = asyncio.run(keeper.backup_now())
    assert entry["name"] == "b"
    assert [c[1][0] for c in spies if c[0] == "make_backup"] == ["manual"] and "mirror" in _names(spies)


def test_status_has_every_part(keeper):
    status = asyncio.run(keeper.status())
    assert set(status) == {"disk", "database", "archive", "backups", "copy", "clock", "notices", "last"}
    assert status["clock"] == {"paused": False, "reason": None, "since": None}
    assert status["copy"] == {"dir": "", "ok": None, "last": None}
    assert set(status["archive"]) == {"enabled", "days", "size", "oldest", "newest", "retention_days"}


def test_start_and_stop(keeper):
    async def main():
        keeper.start()
        assert keeper.running
        await keeper.stop()
        assert not keeper.running
    asyncio.run(main())


def test_the_records_writer_no_longer_prunes():
    assert not hasattr(ProductionRecorder, "RETENTION_SECONDS")
    assert not hasattr(ProductionRecorder, "FIRST_RETENTION_SECONDS")
