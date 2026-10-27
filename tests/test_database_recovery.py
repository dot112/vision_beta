"""Start-up recovery: a damaged database is moved aside and rebuilt from the newest backup and the archive."""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db import recovery
from app.db.models.production_record import ProductRecord
from app.events.alarm_events import alarm_manager
from app.services import backup_service, production_records_service, records_archive, storage_state
from app.services.data_keeper import DataKeeper
from app.services.production_records_service import normalize_row
from tests.test_backups import _insert_user, _upgrade
from tests.test_production_records import _product, sessions  # noqa: F401  (sessions is a fixture)

NOW = datetime(2026, 10, 26, 0, 10, tzinfo=timezone.utc)
GARBAGE = b"\x00garbage" * 4096


@pytest.fixture(autouse=True)
def _clean(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "RECORDS_RETENTION_DAYS", 90)
    alarm_manager.reset()
    yield
    alarm_manager.reset()


@pytest.fixture
def good_db(tmp_path):
    return _upgrade(tmp_path, "factory.db")


def _upgrader(path, calls=None):
    """The real upgrade of the file at ``path`` (Alembic, to the newest revision)."""
    async def upgrade():
        if calls is not None:
            calls.append("upgrade")
        await asyncio.to_thread(_upgrade, Path(path).parent, Path(path).name)
    return upgrade


async def _noop():
    return None


def _users(path):
    with closing(sqlite3.connect(path)) as db:
        return sorted(row[0] for row in db.execute("SELECT username FROM users"))


def _table_names(path):
    with closing(sqlite3.connect(path)) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _active(code):
    return alarm_manager.get(code, storage_state.STORAGE_SOURCE) is not None


# ── The marker and the checks ─────────────────────────────────────────────────

def test_the_marker_tells_an_unclean_end():
    assert not recovery.unclean_end()
    recovery.mark_running()
    assert recovery.unclean_end()
    recovery.mark_stopped()
    assert not recovery.unclean_end()
    recovery.mark_stopped()          # again: nothing to remove, no error


def test_checks(tmp_path, good_db):
    assert recovery.check(str(tmp_path / "missing.db"), full=True)
    assert recovery.check(good_db, full=False) and recovery.check(good_db, full=True)
    garbage = tmp_path / "g.db"
    garbage.write_bytes(GARBAGE)
    assert not recovery.check(str(garbage), full=False) and not recovery.check(str(garbage), full=True)


def test_a_check_changes_nothing(good_db):
    before = Path(good_db).read_bytes()
    recovery.check(good_db, full=True)
    assert Path(good_db).read_bytes() == before


def test_damage_errors_are_told_from_others():
    assert recovery.is_damage(sqlite3.DatabaseError("database disk image is malformed"))
    assert recovery.is_damage(DBAPIError("x", {}, sqlite3.DatabaseError("file is not a database")))
    try:
        try:
            raise sqlite3.DatabaseError("database disk image is malformed")
        except sqlite3.DatabaseError as inner:
            raise RuntimeError("upgrade failed") from inner
    except RuntimeError as outer:
        assert recovery.is_damage(outer)
    assert not recovery.is_damage(sqlite3.OperationalError("database is locked"))
    assert not recovery.is_damage(ValueError("not a database"))


# ── The rebuild ───────────────────────────────────────────────────────────────

def test_rebuild_moves_the_files_aside_and_copies_the_newest_backup(data_dir, good_db):
    entry = backup_service.make_backup("nightly", good_db, NOW)
    for suffix in ("-wal", "-shm"):
        Path(good_db + suffix).write_bytes(b"journal")
    Path(good_db).write_bytes(GARBAGE)
    info = recovery.rebuild(good_db, now=NOW)
    broken = data_dir / "broken" / "20261026T001000Z"
    assert info == {"broken_dir": str(broken), "backup": entry["name"]}
    assert sorted(p.name for p in broken.iterdir()) == ["factory.db", "factory.db-shm", "factory.db-wal"]
    assert (broken / "factory.db").read_bytes() == GARBAGE
    assert Path(good_db).read_bytes() == (data_dir / "backups" / entry["name"] / "config.db").read_bytes()
    assert not Path(good_db + "-wal").exists() and not Path(good_db + "-shm").exists()
    state = storage_state.load()["rebuild"]
    assert state["pending"] is True and state["backup"] == entry["name"] and state["database"] == str(broken / "factory.db")


def test_open_database_rebuilds_a_garbage_file_and_upgrades(data_dir, good_db):
    _insert_user(good_db, "ahmed")
    backup_service.make_backup("nightly", good_db, NOW)
    Path(good_db).write_bytes(GARBAGE)
    recovery.mark_running()                                   # the last run did not end cleanly: a full check
    info = asyncio.run(recovery.open_database(good_db, upgrade=_upgrader(good_db), dispose=_noop))
    assert info["backup"].endswith("-nightly")
    assert _users(good_db) == ["ahmed"]
    assert recovery.check(good_db, full=True) and recovery.unclean_end()     # running again
    assert _active("storage.database_restored")
    assert [n["code"] for n in storage_state.notices()] == ["storage.database_restored"]


def test_a_healthy_database_is_only_upgraded(data_dir, good_db):
    calls = []
    assert asyncio.run(recovery.open_database(good_db, upgrade=_upgrader(good_db, calls), dispose=_noop)) is None
    assert calls == ["upgrade"] and not (data_dir / "broken").exists() and not _active("storage.database_restored")


def test_open_database_retries_once_when_the_upgrade_finds_damage(data_dir, good_db):
    backup_service.make_backup("nightly", good_db, NOW)
    calls = []

    async def upgrade():
        calls.append("upgrade")
        if len(calls) == 1:
            raise DBAPIError("PRAGMA", {}, sqlite3.DatabaseError("database disk image is malformed"))

    async def dispose():
        calls.append("dispose")

    info = asyncio.run(recovery.open_database(good_db, upgrade=upgrade, dispose=dispose))
    assert calls == ["upgrade", "dispose", "upgrade"] and info["backup"].endswith("-nightly")


def test_an_upgrade_error_that_is_not_damage_is_raised(data_dir, good_db):
    async def upgrade():
        raise RuntimeError("bad revision")
    with pytest.raises(RuntimeError):
        asyncio.run(recovery.open_database(good_db, upgrade=upgrade, dispose=_noop))
    assert not (Path(good_db).parent / "broken").exists()


def test_without_a_backup_the_upgrade_builds_an_empty_database(data_dir, good_db):
    Path(good_db).write_bytes(GARBAGE)
    info = asyncio.run(recovery.open_database(good_db, upgrade=_upgrader(good_db), dispose=_noop))
    assert info["backup"] is None
    assert {"users", "product_records", "kept_totals", "alembic_version"} <= _table_names(good_db)
    assert _users(good_db) == []
    assert "no backup" in storage_state.notices()[0]["message"]


def test_open_database_disposes_before_moving(data_dir, good_db, monkeypatch):
    backup_service.make_backup("nightly", good_db, NOW)
    order = []
    real_move = shutil.move
    monkeypatch.setattr(recovery.shutil, "move", lambda *a, **k: (order.append("move"), real_move(*a, **k))[1])
    engine = create_async_engine(f"sqlite+aiosqlite:///{Path(good_db).as_posix()}")

    async def main():
        async with engine.connect() as conn:          # the server's pool holds the file open
            await conn.execute(sa.text("SELECT 1"))

        async def upgrade():
            if "move" not in order:
                raise DBAPIError("PRAGMA", {}, sqlite3.DatabaseError("database disk image is malformed"))

        async def dispose():
            order.append("dispose")
            await engine.dispose()

        return await recovery.open_database(good_db, upgrade=upgrade, dispose=dispose)

    info = asyncio.run(main())
    assert order[:2] == ["dispose", "move"] and info is not None


# ── Getting the records back ──────────────────────────────────────────────────

def _records_db(path, times):
    """A database file with only product_records, holding one row at each of ``times``."""
    engine = sa.create_engine(f"sqlite:///{Path(path).as_posix()}")
    ProductRecord.__table__.create(engine)
    if times:
        with engine.begin() as conn:
            conn.execute(sa.insert(ProductRecord), [_product(n=i, recorded_at=when, details={"stations": [i]})
                                                     for i, when in enumerate(times)])
    engine.dispose()
    return str(path)


def test_salvage_reads_what_it_can(tmp_path):
    times = [NOW - timedelta(hours=h) for h in (30, 20, 10, 1)]
    path = _records_db(tmp_path / "broken.db", times)
    rows = recovery.salvage_rows(path, after=NOW - timedelta(hours=20))
    assert sorted(r["recorded_at"] for r in rows) == times[1:]
    assert rows[0] == normalize_row(rows[0]) and isinstance(rows[0]["details"], dict) and rows[0]["counted"] in (True, False)
    assert len(recovery.salvage_rows(path, after=None)) == 4
    garbage = tmp_path / "g.db"
    garbage.write_bytes(GARBAGE)
    assert recovery.salvage_rows(str(garbage), after=None) == []
    assert recovery.salvage_rows(str(tmp_path / "missing.db"), after=None) == []


class _Spy:
    def __init__(self):
        self.calls = []

    async def flush(self):
        self.calls.append("flush")
        return 0


@pytest.fixture
def counters(monkeypatch):
    spy = _Spy()

    async def restore(session_factory=None):
        spy.calls.append("restore")
        return {}
    monkeypatch.setattr(production_records_service, "production_recorder", spy)
    monkeypatch.setattr(production_records_service, "restore_line_counts", restore)
    return spy


def _setup_rebuild(sessions, data_dir, tmp_path, salvage_times):
    """Two archived days inside the 90 days (written from ``sessions``, then emptied), and a damaged file to salvage."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    days = [today - timedelta(days=2), today - timedelta(days=1)]
    archived = [_product(n=i, recorded_at=day + timedelta(hours=h)) for i, (day, h) in enumerate(
        [(days[0], 1), (days[0], 2), (days[1], 1), (days[1], 2), (days[1], 3)])]

    async def fill_and_archive():
        async with sessions() as db:
            await db.execute(sa.insert(ProductRecord), archived)
            await db.commit()
        for day in days:
            await records_archive.write_day(sessions, day.date())
        async with sessions() as db:
            await db.execute(sa.delete(ProductRecord))
            await db.commit()
    asyncio.run(fill_and_archive())
    broken = data_dir / "broken" / "20261026T001000Z"
    broken.mkdir(parents=True)
    database = _records_db(broken / "factory.db", [today + timedelta(minutes=m) for m in salvage_times])
    storage_state.update(lambda s: s.__setitem__("rebuild", {"broken_dir": str(broken), "database": database,
                                                                "backup": None, "pending": True}))
    return archived


def _count_rows(sessions):
    async def run():
        async with sessions() as db:
            return (await db.execute(sa.select(sa.func.count(ProductRecord.id)))).scalar_one()
    return asyncio.run(run())


def test_finish_rebuild_puts_records_back_and_restores_the_counters(sessions, data_dir, tmp_path, counters):
    _setup_rebuild(sessions, data_dir, tmp_path, salvage_times=(5, 6, 7))
    result = asyncio.run(recovery.finish_rebuild(sessions))
    assert result == {"salvaged": 3, "reimported": 5}
    assert _count_rows(sessions) == 8
    assert counters.calls == ["flush", "restore"]
    assert storage_state.load()["rebuild"]["pending"] is False
    notice = storage_state.notices()[0]
    assert notice["code"] == "storage.database_restored" and "3 record(s) salvaged" in notice["message"]
    assert "5 put back from the archive" in notice["message"] and _active("storage.database_restored")
    # Again: nothing pending, nothing done twice.
    assert asyncio.run(recovery.finish_rebuild(sessions)) == {"salvaged": 0, "reimported": 0}
    assert _count_rows(sessions) == 8


def test_the_re_import_starts_at_the_newest_deleted_time(sessions, data_dir, tmp_path, counters):
    archived = _setup_rebuild(sessions, data_dir, tmp_path, salvage_times=())
    deleted_through = archived[2]["recorded_at"]           # the first row of the second day was deleted already
    storage_state.update(lambda s: s.__setitem__("deleted_through", deleted_through.isoformat()))
    assert asyncio.run(recovery.finish_rebuild(sessions)) == {"salvaged": 0, "reimported": 3}   # from it, inclusive


def test_a_day_that_cannot_be_read_is_skipped(sessions, data_dir, tmp_path, counters):
    _setup_rebuild(sessions, data_dir, tmp_path, salvage_times=(5,))
    first = sorted(records_archive.list_days(), key=lambda d: d["day"])[0]["day"]
    records_archive.file_for(datetime.fromisoformat(first).date()).write_bytes(b"not gzip")
    assert asyncio.run(recovery.finish_rebuild(sessions)) == {"salvaged": 1, "reimported": 3}


def test_the_keeper_finishes_a_rebuild_at_its_first_run(sessions, monkeypatch):
    calls = []

    async def finish(session_factory):
        calls.append(session_factory)
        return {"salvaged": 0, "reimported": 0}

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(recovery, "finish_rebuild", finish)
    keeper = DataKeeper(session_factory=sessions)
    monkeypatch.setattr(keeper, "nightly", nothing)
    asyncio.run(keeper.first_run())
    assert calls == [sessions]
