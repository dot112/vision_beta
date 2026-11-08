"""Kept totals: a line's counters stay exact after its old product records are deleted."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import command

import app.services.line_service as line_module
import app.services.production_records_service as records_module
from app.db.models.kept_total import KeptTotal
from app.db.models.production_record import ProductRecord
from app.routes.v1.counting import reset_line_counters
from app.services import storage_state
from app.services.line_service import LineManager
from app.services.production_records_service import (
    ProductionRecorder,
    prune_kept_totals,
    reset_tag,
    restore_line_counts,
)
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_production_records import (  # noqa: F401  (lines and sessions are fixtures)
    _alembic,
    _count,
    _cross,
    _line,
    _product,
    _Recorder,
    _rows,
    _totals,
    lines,
    sessions,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _add(sessions, rows):
    async def run():
        async with sessions() as db:
            await db.execute(sa.insert(ProductRecord), rows)
            await db.commit()
    asyncio.run(run())


def _kept(sessions):
    async def run():
        async with sessions() as db:
            k = KeptTotal
            rows = (await db.execute(sa.select(k.line_id, k.camera_id, k.class_name, k.result, k.reset_tag, k.count))).all()
            return sorted(tuple(row) for row in rows)
    return asyncio.run(run())


def _age_all(sessions, line_id, days):
    """Every record ``days`` older, and the line's reset time a day before them."""
    async def run():
        async with sessions() as db:
            # Row by row: SQLite cannot take a time from a stored time in SQL.
            for row_id, recorded_at in (await db.execute(sa.select(ProductRecord.id, ProductRecord.recorded_at))).all():
                await db.execute(sa.update(ProductRecord).where(ProductRecord.id == row_id)
                                 .values(recorded_at=recorded_at - timedelta(days=days)))
            await db.commit()
    asyncio.run(run())
    line = SettingsPersistenceService._find_line(line_id)
    line["counts_reset_at"] = (datetime.now(timezone.utc) - timedelta(days=days + 1)).isoformat()
    SettingsPersistenceService.save()


def _restart_totals(sessions, runtime_id, monkeypatch):
    """New counters rebuilt from the database, as at a restart: (line totals, by class, own-station totals)."""
    fresh = LineManager()
    monkeypatch.setattr(line_module, "line_manager", fresh)
    fresh.apply_state()
    asyncio.run(restore_line_counts(sessions))
    rt = fresh.get(runtime_id)
    return _totals(rt.counter), dict(rt.counter.counts_by_class), _totals(rt.aux_counters["own"])


def _old_products(lines, sessions, monkeypatch, crossings=(("bottle", False), ("bottle", True), ("can", False))):
    """A line with an own-station camera, its products (and one own-station reject) recorded 100 days ago."""
    rec = _Recorder(sessions)
    monkeypatch.setattr(records_module, "production_recorder", rec)
    runtime = _line(lines, [{"camera_id": "top"}, {"camera_id": "own", "station": "own"}])
    for name, defect in crossings:
        _cross(runtime.counter, "top", is_defect=defect, class_name=name)
    _cross(runtime.aux_counters["own"], "own", is_defect=True)
    asyncio.run(rec.flush())
    _age_all(sessions, runtime.id, 100)
    return rec, runtime


# ── The totals stay exact ─────────────────────────────────────────────────────

def test_totals_are_the_same_after_old_records_are_deleted(lines, sessions, monkeypatch):
    rec, runtime = _old_products(lines, sessions, monkeypatch)
    before = _restart_totals(sessions, runtime.id, monkeypatch)
    assert asyncio.run(rec.prune(days=90)) == 4
    assert _count(sessions) == 0
    assert _restart_totals(sessions, runtime.id, monkeypatch) == before == ((3, 2, 1), {"bottle": 2, "can": 1}, (1, 0, 1))


def test_kept_counts_add_up_over_chunks(lines, sessions, monkeypatch):
    rec, runtime = _old_products(lines, sessions, monkeypatch, crossings=[("bottle", False)] * 3)
    monkeypatch.setattr(ProductionRecorder, "DELETE_CHUNK", 2)
    assert asyncio.run(rec.prune(days=90)) == 4
    tag = SettingsPersistenceService.get_line(runtime.id)["counts_reset_at"]
    assert _kept(sessions) == [(runtime.id, "", "bottle", "good", tag, 3), (runtime.id, "own", "scratch", "reject", tag, 1)]
    assert _restart_totals(sessions, runtime.id, monkeypatch) == ((3, 3, 0), {"bottle": 3}, (1, 0, 1))


def test_a_line_reset_stops_old_kept_totals_counting(lines, sessions, monkeypatch):
    rec, runtime = _old_products(lines, sessions, monkeypatch)
    asyncio.run(rec.prune(days=90))
    reset_line_counters(runtime, None)
    assert _restart_totals(sessions, runtime.id, monkeypatch) == ((0, 0, 0), {}, (0, 0, 0))


def test_a_class_reset_stops_that_class_only(lines, sessions, monkeypatch):
    rec, runtime = _old_products(lines, sessions, monkeypatch)
    asyncio.run(rec.prune(days=90))
    SettingsPersistenceService.mark_counts_reset(runtime.id, ["bottle"])
    # The own-station camera counts from the line's reset only.
    assert _restart_totals(sessions, runtime.id, monkeypatch) == ((1, 1, 0), {"can": 1}, (1, 0, 1))


def test_records_from_before_a_reset_are_not_kept(lines, sessions, monkeypatch):
    rec, runtime = _old_products(lines, sessions, monkeypatch)
    reset_line_counters(runtime, None)
    assert asyncio.run(rec.prune(days=90)) == 4
    assert _kept(sessions) == []


def test_stale_kept_rows_are_cleaned(lines, sessions, monkeypatch):
    rec, runtime = _old_products(lines, sessions, monkeypatch)
    asyncio.run(rec.prune(days=90))
    assert len(_kept(sessions)) == 4
    assert asyncio.run(prune_kept_totals(sessions)) == 0  # still counting
    reset_line_counters(runtime, None)
    assert asyncio.run(prune_kept_totals(sessions)) == 4
    assert _kept(sessions) == []

    # A deleted line's rows go too.
    current = reset_tag(SettingsPersistenceService.get_line(runtime.id), "bottle")

    async def keep():
        async with sessions() as db:
            await db.execute(sa.insert(KeptTotal), [{"line_id": runtime.id, "camera_id": "", "class_name": "bottle",
                                                     "result": "good", "reset_tag": current, "count": 5}])
            await db.commit()

    asyncio.run(keep())
    assert asyncio.run(prune_kept_totals(sessions)) == 0
    assert SettingsPersistenceService.delete_line(runtime.id)
    assert asyncio.run(prune_kept_totals(sessions)) == 1
    assert _kept(sessions) == []


# ── Deleting ──────────────────────────────────────────────────────────────────

def test_prune_stops_before_the_given_time(sessions, data_dir):
    _add(sessions, [_product(n=i, recorded_at=NOW - timedelta(days=d)) for i, d in enumerate((100, 95))])
    rec = ProductionRecorder(sessions)
    assert asyncio.run(rec.prune(days=90, now=NOW, before=NOW - timedelta(days=97))) == 1
    assert [r.track_id for r in _rows(sessions)] == [1]


def test_a_delete_notes_the_newest_deleted_time(sessions, data_dir):
    _add(sessions, [_product(n=i, recorded_at=NOW - timedelta(days=d)) for i, d in enumerate((100, 95, 10))])
    rec = ProductionRecorder(sessions)
    old = ProductRecord.recorded_at < NOW - timedelta(days=90)
    assert asyncio.run(rec.delete_chunk(old, {})) == (2, NOW - timedelta(days=95))
    assert storage_state.load()["deleted_through"] == (NOW - timedelta(days=95)).isoformat()
    # Rows of a line that is not saved any more are not kept.
    assert _kept(sessions) == []
    # An older delete (a late row, say) leaves the newest time as it was.
    _add(sessions, [_product(n=3, recorded_at=NOW - timedelta(days=120))])
    assert asyncio.run(rec.delete_chunk(old, {})) == (1, NOW - timedelta(days=120))
    assert storage_state.load()["deleted_through"] == (NOW - timedelta(days=95)).isoformat()
    assert asyncio.run(rec.delete_chunk(old, {})) == (0, None)


def test_a_state_file_that_cannot_be_written_does_not_stop_the_delete(sessions, data_dir, monkeypatch):
    _add(sessions, [_product(recorded_at=NOW - timedelta(days=100))])

    def broken(change):
        raise OSError("disk full")

    monkeypatch.setattr(storage_state, "update", broken)
    assert asyncio.run(ProductionRecorder(sessions).delete_chunk(sa.true(), {}))[0] == 1
    assert _count(sessions) == 0


def test_reset_tag():
    line = {"counts_reset_at": "2026-10-01T00:00:00+00:00", "counts_reset_classes": {"can": "2026-10-05T00:00:00+00:00"}}
    assert reset_tag(line, "can") == "2026-10-05T00:00:00+00:00"
    assert reset_tag(line, "bottle") == reset_tag(line, "can", own_station=True) == "2026-10-01T00:00:00+00:00"
    assert reset_tag({}, "can") == ""


# ── The database upgrade ──────────────────────────────────────────────────────

def test_a_database_at_0008_upgrades_to_0009_and_back(tmp_path):
    engine, config = _alembic(tmp_path, "old.db")
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0008_code_format")
        # A database made before the model had the index.
        conn.execute(sa.text("DROP INDEX IF EXISTS ix_detections_created_at"))
        command.upgrade(config, "head")
        # Run again over what is there: nothing changes.
        command.stamp(config, "0008_code_format")
        command.upgrade(config, "head")
        insp = sa.inspect(conn)
        assert "kept_totals" in insp.get_table_names()
        assert "ix_detections_created_at" in {i["name"] for i in insp.get_indexes("detections")}
        command.downgrade(config, "0008_code_format")
        insp = sa.inspect(conn)
        assert "kept_totals" not in insp.get_table_names()
        assert "ix_detections_created_at" not in {i["name"] for i in insp.get_indexes("detections")}
    engine.dispose()
