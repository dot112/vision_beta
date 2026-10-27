"""The free-space floor: alarms, and the order in which the oldest data is deleted to make room."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db.models.detection import DetectionLog
from app.db.models.kept_total import KeptTotal
from app.db.models.production_record import ProductRecord
from app.events.alarm_events import alarm_manager
from app.services import records_archive, storage_state
from app.services.production_records_service import ProductionRecorder
from app.services.storage_guard import DiskReading, StorageGuard, room_in_database
from tests.test_kept_totals import _old_products, _restart_totals
from tests.test_production_records import _product, lines, sessions  # noqa: F401  (lines and sessions are fixtures)

MB = 1024 * 1024
GB = 1024 * MB
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
TARGET = int(2048 * MB * 1.25)


def _active(code):
    return alarm_manager.get(code, storage_state.STORAGE_SOURCE) is not None


@pytest.fixture(autouse=True)
def _floor(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_MIN_FREE_MB", 2048)
    monkeypatch.setattr(settings, "STORAGE_WARN_PERCENT", 85.0)
    alarm_manager.reset()
    yield
    alarm_manager.reset()


async def _create(engine, *models):
    async with engine.begin() as conn:
        for model in models:
            await conn.run_sync(lambda sync, table=model.__table__: table.create(sync, checkfirst=True))


@pytest.fixture
def guard_db(tmp_path):
    """A throwaway database with the records, kept totals and detections tables. Gives its session factory."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'guard.db').as_posix()}", poolclass=NullPool)
    asyncio.run(_create(engine, ProductRecord, KeptTotal, DetectionLog))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    asyncio.run(engine.dispose())


async def _rows_left(factory):
    async with factory() as db:
        records = (await db.execute(sa.select(sa.func.count(ProductRecord.id)))).scalar_one()
        detections = (await db.execute(sa.select(sa.func.count(DetectionLog.id)))).scalar_one()
    return records + detections


class FakeDisk:
    """The disk: ``free`` grows by ``per_file`` for each archive file deleted since ``files_at_start``."""

    def __init__(self):
        self.total, self.free, self.used_percent, self.per_file, self.files_at_start = 32 * GB, 10 * GB, 50.0, 0, 0

    def __call__(self):
        gone = max(0, self.files_at_start - len(records_archive.list_days()))
        return DiskReading(total=self.total, free=self.free + self.per_file * gone, used_percent=self.used_percent)


class FakeRoom:
    """The room inside the database: ``value``, plus ``per_row`` for each record or detection deleted."""

    def __init__(self, factory):
        self.factory, self.value, self.per_row, self.rows_at_start = factory, 0, 0, 0

    async def __call__(self):
        return self.value + self.per_row * max(0, self.rows_at_start - await _rows_left(self.factory))


@pytest.fixture
def disk():
    return FakeDisk()


@pytest.fixture
def room(guard_db):
    return FakeRoom(guard_db)


@pytest.fixture
def guard(guard_db, disk, room, data_dir):
    return StorageGuard(guard_db, ProductionRecorder(guard_db), disk=disk, room=room)


def _insert(factory, records=(), detections=()):
    async def run():
        async with factory() as db:
            if records:
                await db.execute(sa.insert(ProductRecord), list(records))
            if detections:
                await db.execute(sa.insert(DetectionLog), [
                    {"id": str(uuid.uuid4()), "model_name": "m", "total_detections": 1, "detections": [],
                     "inference_time_ms": 1.0, "created_at": when.replace(tzinfo=None)} for when in detections])
            await db.commit()
    asyncio.run(run())


def _record_times(factory):
    async def run():
        async with factory() as db:
            return [row[0] for row in (await db.execute(sa.select(ProductRecord.recorded_at).order_by(ProductRecord.recorded_at))).all()]
    return asyncio.run(run())


@pytest.fixture
def full_db(guard_db, disk, room, data_dir):
    """3 detections; a record on each of days 0 and 1 (both archived); 2 records 5 days ago (no file); 2 in the last hour."""
    day0, day1 = (NOW - timedelta(days=10)).date(), (NOW - timedelta(days=9)).date()
    records = [_product(n=0, recorded_at=NOW - timedelta(days=10)), _product(n=1, recorded_at=NOW - timedelta(days=9)),
               _product(n=2, recorded_at=NOW - timedelta(days=5)), _product(n=3, recorded_at=NOW - timedelta(days=5, minutes=-1)),
               _product(n=4, recorded_at=NOW - timedelta(minutes=30)), _product(n=5, recorded_at=NOW - timedelta(minutes=20))]
    _insert(guard_db, records, [NOW - timedelta(days=d) for d in (40, 20, 2)])
    for day in (day0, day1):
        records_archive.file_for(day).parent.mkdir(parents=True, exist_ok=True)
        records_archive.file_for(day).write_bytes(b"archived")
    disk.files_at_start = 2
    room.rows_at_start = 9
    return guard_db


# ── Alarms ────────────────────────────────────────────────────────────────────

def test_low_is_raised_at_the_warn_level_and_clears_two_points_below(guard, disk):
    disk.used_percent = 85
    assert asyncio.run(guard.check(NOW))["state"] == "low" and _active("storage.low")
    disk.used_percent = 84
    asyncio.run(guard.check(NOW))
    assert _active("storage.low")
    disk.used_percent = 82.9
    assert asyncio.run(guard.check(NOW))["state"] == "ok" and not _active("storage.low")


def test_room_inside_the_database_counts_as_free(guard, disk, room):
    disk.free = 1500 * MB
    room.value = 600 * MB
    status = asyncio.run(guard.check(NOW))
    assert status["state"] == "ok" and status["free"] == 1500 * MB and status["room"] == 600 * MB
    assert status["floor"] == 2048 * MB and status["warn_percent"] == 85.0 and status["total"] == 32 * GB
    assert not _active("storage.full")


def test_full_raises_and_clears_at_the_floor(guard, disk):
    disk.free = 100 * MB
    status = asyncio.run(guard.check(NOW))
    assert status["state"] == "full" and _active("storage.full")
    assert "nothing more the server can delete" in alarm_manager.get("storage.full", "storage").message
    disk.free = 2048 * MB
    assert asyncio.run(guard.check(NOW))["state"] == "ok" and not _active("storage.full")


def test_floor_zero_turns_it_off(guard, disk, full_db, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_MIN_FREE_MB", 0)
    disk.free = 0
    assert asyncio.run(guard.check(NOW))["state"] == "ok"
    assert asyncio.run(_rows_left(full_db)) == 9 and not _active("storage.full")


# ── What goes first ───────────────────────────────────────────────────────────

def test_space_is_freed_in_order_until_1_25_times_the_floor(guard, disk, room, full_db):
    disk.free, room.per_row, disk.per_file = 1000 * MB, 200 * MB, 100 * MB   # 1000+600+400+200+400 = 2600 >= 2560
    steps = asyncio.run(guard.free_space(TARGET, NOW))
    assert steps == [{"step": "detections", "deleted": 3}, {"step": "archived_records", "deleted": 2},
                     {"step": "archive_files", "deleted": 2}, {"step": "records", "deleted": 2}]
    assert _record_times(full_db) == [(NOW - timedelta(minutes=30)).replace(tzinfo=None),
                                      (NOW - timedelta(minutes=20)).replace(tzinfo=None)]
    assert _active("storage.records_lost")
    notice = storage_state.notices()[0]
    assert notice["code"] == "storage.records_lost" and notice["details"]["deleted"] == 2


def test_records_of_archived_days_go_before_archive_files_even_after_a_gap(guard, disk, room, full_db):
    # An older day that has no archive file (its archive failed) must not keep the archived days' records in
    # the database while the archive files, perhaps the only copy of older days, are deleted.
    _insert(full_db, [_product(n=9, recorded_at=NOW - timedelta(days=20)), _product(n=10, recorded_at=NOW - timedelta(days=20, hours=-1))])
    room.rows_at_start = 11
    disk.free, room.per_row, disk.per_file = 1000 * MB, 200 * MB, 100 * MB
    steps = asyncio.run(guard.free_space(TARGET, NOW))
    assert steps[:2] == [{"step": "detections", "deleted": 3}, {"step": "archived_records", "deleted": 2}]


def test_freeing_stops_as_soon_as_there_is_enough(guard, disk, room, full_db):
    disk.free, room.per_row = 2400 * MB, 200 * MB
    assert asyncio.run(guard.free_space(TARGET, NOW)) == [{"step": "detections", "deleted": 3}]
    assert asyncio.run(_rows_left(full_db)) == 6 and not _active("storage.records_lost")


def test_a_check_under_the_floor_frees_space_and_clears(guard, disk, room, full_db):
    disk.free, room.per_row = 2000 * MB, 200 * MB
    status = asyncio.run(guard.check(NOW))
    assert status["state"] == "ok" and not _active("storage.full")
    assert asyncio.run(_rows_left(full_db)) == 6


def test_the_newest_24_hours_are_never_deleted(guard, disk, guard_db, data_dir):
    _insert(guard_db, [_product(n=i, recorded_at=NOW - timedelta(hours=i + 1)) for i in range(3)])
    disk.free = 100 * MB
    status = asyncio.run(guard.check(NOW))
    assert status["state"] == "full" and len(_record_times(guard_db)) == 3
    assert "nothing more the server can delete" in alarm_manager.get("storage.full", "storage").message
    assert not _active("storage.records_lost")


def test_a_notice_that_cannot_be_saved_does_not_stop_the_freeing(guard, disk, room, full_db, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(storage_state, "add_notice", broken)
    disk.free, room.per_row = 1000 * MB, 200 * MB
    assert [s["step"] for s in asyncio.run(guard.free_space(TARGET, NOW))][-1] == "records"
    assert _active("storage.records_lost")


def test_totals_stay_exact_when_space_deletes(lines, sessions, monkeypatch, data_dir):
    rec, runtime = _old_products(lines, sessions, monkeypatch)
    asyncio.run(_create(sessions.engine, DetectionLog))
    before = _restart_totals(sessions, runtime.id, monkeypatch)

    async def nothing():
        return 0
    guard = StorageGuard(sessions, ProductionRecorder(sessions), disk=lambda: DiskReading(32 * GB, 0, 99.0), room=nothing)
    steps = asyncio.run(guard.free_space(TARGET, datetime.now(timezone.utc)))
    assert steps == [{"step": "records", "deleted": 4}]
    assert _restart_totals(sessions, runtime.id, monkeypatch) == before == ((3, 2, 1), {"bottle": 2, "can": 1}, (1, 0, 1))


def test_room_in_database_reads_the_free_pages(guard_db):
    _insert(guard_db, [_product(n=i, recorded_at=NOW, code="x" * 400) for i in range(200)])
    assert asyncio.run(room_in_database(guard_db)) == 0

    async def drop():
        async with guard_db() as db:
            await db.execute(sa.delete(ProductRecord))
            await db.commit()
    asyncio.run(drop())
    assert asyncio.run(room_in_database(guard_db)) > 0
