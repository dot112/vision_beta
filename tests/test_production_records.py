"""Product records: every product and code read kept in the database, the Records API and its exports."""
from __future__ import annotations

import asyncio
import copy
import csv
import io
import os
import threading
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.services.line_service as line_module
import app.services.production_records_service as records_module
import app.services.records_export as export_module
import app.services.settings_persistence_service as persistence_module
from app.db.models.production_record import ProductRecord
from app.events.alarm_events import AlarmCode, alarm_manager
from app.services.counting_service import CountingService, counting_service
from app.services.line_config import PRIMARY_LINE_ID, REASON_NOT_LISTED, REASON_VISION, upgrade_state
from app.services.line_service import LineManager
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.production_records_service import (
    ProductionRecorder,
    RecordFilter,
    build_row,
    counted_totals,
    restore_line_counts,
)
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService

ROOT = Path(__file__).resolve().parents[1]
API = "/api/v1"
LIST = "list-1"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sessions(tmp_path):
    """A throwaway database with only the product_records table. Gives its session factory."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'records.db').as_posix()}", poolclass=NullPool)

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: ProductRecord.__table__.create(sync))

    asyncio.run(create())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    factory.engine = engine
    yield factory
    asyncio.run(engine.dispose())


def _count(sessions, *where):
    async def run():
        async with sessions() as db:
            return (await db.execute(sa.select(sa.func.count(ProductRecord.id)).where(*where))).scalar_one()
    return asyncio.run(run())


def _rows(sessions):
    async def run():
        async with sessions() as db:
            return list((await db.execute(sa.select(ProductRecord).order_by(ProductRecord.id))).scalars().all())
    return asyncio.run(run())


class _Recorder(ProductionRecorder):
    """A recorder that only queues (the tests write the queue themselves)."""

    def __init__(self, sessions=None, limit=None):
        super().__init__(sessions, limit)
        self.running = True


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(records_module, "production_recorder", rec)
    return rec


@pytest.fixture
def lines(monkeypatch, tmp_path):
    """Throwaway settings file and line manager; the product list holds LISTED."""
    from app.services.product_service import product_catalog

    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(tmp_path / "system_state.json"))
    state = copy.deepcopy(DEFAULT_STATE)
    upgrade_state(state)
    monkeypatch.setattr(SettingsPersistenceService, "_state", state)
    monkeypatch.setattr(SettingsPersistenceService, "_recent_changes", [])
    monkeypatch.setattr(PLCDispatcherService, "_cards", [])
    monkeypatch.setattr(PLCDispatcherService, "_states", {})
    monkeypatch.setattr(product_catalog, "_lists", {LIST: {"LISTED": {"code": "LISTED", "name": "Widget"}}})
    monkeypatch.setattr(product_catalog, "_names", {LIST: "List 1"})
    manager = LineManager()
    monkeypatch.setattr(line_module, "line_manager", manager)
    saved_config = counting_service.config
    counting_service.reset_counts()
    yield manager
    counting_service.event_sink = None
    counting_service.update_config(saved_config)
    counting_service.reset_counts()


class _CrossingTracker:
    def __init__(self, is_defect=False, class_name=None):
        self.is_defect = is_defect
        self.class_name = class_name or ("scratch" if is_defect else "bottle")
        self.objects, self._recently_counted, self.next_id = {}, set(), 1

    def update(self, **kwargs):
        self.next_id += 1
        return [(self.next_id, self.class_name, self.is_defect, 0.87, (10, 20, 50, 80))]


def _cross(counter, camera_id, is_defect=False, class_name=None):
    counter._trackers[camera_id] = _CrossingTracker(is_defect, class_name)
    counter.process_frame([], 640, 480, camera_id=camera_id)


def _line(manager, cameras, name="Records", sync=None):
    cameras = [cam if cam.get("role", "vision") != "vision" else {"model_id": "model-test", **cam} for cam in cameras]
    line = SettingsPersistenceService.save_line({"name": name, "cameras": cameras, "enabled": True,
                                                 "sync": sync or {"window_ms": 80}})
    manager.apply_state()
    runtime = manager.get(line["id"])
    for counter in [runtime.counter, *runtime.aux_counters.values()]:
        counter.dispatch_event = lambda payload, plc_event, is_defect: None
    runtime._publish = lambda topics, payload, plc_event: None
    return runtime


def _run(steps):
    async def main():
        CountingService.set_event_loop(asyncio.get_running_loop())
        try:
            await steps()
        finally:
            CountingService.set_event_loop(None)

    asyncio.run(main())


def _totals(counter):
    return counter.total_inspected, counter.good_count, counter.rejected_count


# ── One row per product and per code read ─────────────────────────────────────

def test_a_counted_product_and_an_own_station_product(lines, recorder):
    runtime = _line(lines, [{"camera_id": "top"}, {"camera_id": "own", "station": "own"}])
    _cross(runtime.counter, "top", is_defect=True)
    _cross(runtime.aux_counters["own"], "own")
    counted, own = recorder._rows
    assert (counted["kind"], counted["counted"], counted["result"], counted["reject_reason"]) == ("product", True, "reject", REASON_VISION)
    assert (counted["line_id"], counted["line_name"], counted["camera_id"]) == (runtime.id, "Records", "top")
    assert (counted["class_name"], counted["confidence"], counted["track_id"]) == ("scratch", 0.87, 2)
    assert counted["details"]["vision_result"] == "reject" and counted["details"]["bbox"]["x2"] == 50
    assert counted["code"] is None and counted["code_status"] is None and counted["batch"] is None
    assert isinstance(counted["recorded_at"], datetime) and counted["recorded_at"].tzinfo is not None
    assert (own["kind"], own["counted"], own["result"], own["camera_id"]) == ("product", False, "good", "own")


def test_a_product_paired_with_its_code_keeps_the_code(lines, recorder):
    runtime = _line(lines, [
        {"camera_id": "top"},
        {"camera_id": "qr", "role": "qr", "product_list_id": LIST, "qr_action": "accept_listed", "qr_no_read": "reject"},
    ])

    async def steps():
        runtime.on_qr_read("qr", "LISTED", "QR_CODE")
        await asyncio.sleep(0.01)
        _cross(runtime.counter, "top")
        await asyncio.sleep(0.02)
        runtime.on_qr_read("qr", "=HYPERLINK()", "QR_CODE")
        await asyncio.sleep(0.01)
        _cross(runtime.counter, "top")
        await asyncio.sleep(0.02)

    _run(steps)
    good, bad = recorder._rows
    assert (good["result"], good["code"], good["code_format"], good["code_status"], good["product_name"]) == (
        "good", "LISTED", "QR_CODE", "known", "Widget")
    assert good["product_list_id"] == LIST and good["counted"] is True
    assert (bad["result"], bad["reject_reason"], bad["code"], bad["code_status"]) == ("reject", REASON_NOT_LISTED, "=HYPERLINK()", "unknown")


def test_a_code_only_product_an_unpaired_code_and_a_no_read(lines, recorder):
    reader = _line(lines, [{"camera_id": "qr", "role": "qr", "product_list_id": LIST, "qr_action": "accept_listed"}],
                   name="Codes")

    async def steps():
        reader.on_qr_read("qr", "LISTED", "QR_CODE")
        reader.on_qr_read("qr", "NOPE", "EAN_13")
        await asyncio.sleep(0.01)

    _run(steps)
    listed, other = recorder._rows
    assert (listed["kind"], listed["counted"], listed["result"], listed["class_name"]) == ("product", True, "good", "Widget")
    assert (listed["code"], listed["code_status"], listed["product_list_id"]) == ("LISTED", "known", LIST)
    assert (other["result"], other["reject_reason"], other["class_name"], other["code_format"]) == (
        "reject", REASON_NOT_LISTED, "code not in list", "EAN_13")
    assert _totals(reader.counter) == (2, 1, 1)

    recorder._rows.clear()
    report = _line(lines, [{"camera_id": "top"}, {"camera_id": "qr2", "role": "qr", "product_list_id": LIST}], name="Report")

    async def more():
        report.on_qr_read("qr2", "LISTED", "QR_CODE")
        report._emit_no_read("qr2", {"track_id": 7, "class_name": "bottle"})
        await asyncio.sleep(0.01)

    _run(more)
    code, no_read = recorder._rows
    assert (code["kind"], code["counted"], code["result"], code["code"], code["code_status"]) == ("code", False, None, "LISTED", "known")
    assert code["camera_id"] == "qr2" and code["line_id"] == report.id
    assert (no_read["kind"], no_read["counted"], no_read["code"], no_read["code_status"], no_read["track_id"]) == (
        "code", False, None, "no_read", 7)
    assert _totals(report.counter) == (0, 0, 0)


def test_a_joined_cameras_stations_go_into_the_details(lines, recorder):
    runtime = _line(lines, [{"camera_id": "top"}, {"camera_id": "side", "station": "join", "join_offset_ms": 50,
                                                   "join_window_ms": 100}])

    async def steps():
        _cross(runtime.counter, "top")
        await asyncio.sleep(0.05)
        _cross(runtime.aux_counters["side"], "side", is_defect=True)
        await asyncio.sleep(0.02)

    _run(steps)
    (row,) = recorder._rows
    assert row["result"] == "reject" and row["camera_id"] == "top" and row["counted"] is True
    assert row["details"]["reject_camera_id"] == "side"
    assert [s["camera_id"] for s in row["details"]["stations"]] == ["side"]


def test_counters_that_send_nothing_record_nothing(recorder):
    counter = CountingService(dispatch_telemetry=False)
    _cross(counter, "cam")
    assert not recorder._rows and counter.total_inspected == 1
    recorder.running = False
    _cross(CountingService(), "cam")
    assert not recorder._rows


def test_a_row_keeps_to_its_columns():
    row = build_row({"line_id": "x" * 80, "class_name": "c" * 300, "code": "q" * 600, "batch": "B-7",
                     "is_defect": False, "confidence": "high", "track_id": "12"}, "product", True)
    assert len(row["line_id"]) == 48 and len(row["class_name"]) == 128 and len(row["code"]) == 512
    assert row["confidence"] is None and row["track_id"] == 12 and row["batch"] == "B-7"


# ── The writer ────────────────────────────────────────────────────────────────

def _product(line_id="line-w", n=0, **extra):
    return records_module.normalize_row({"line_id": line_id, "kind": "product", "counted": True, "result": "good",
                                         "class_name": "bottle", "track_id": n, **extra})


def test_the_writer_under_load(sessions):
    rec = ProductionRecorder(sessions)

    async def main():
        rec.start()
        threads = [threading.Thread(target=lambda: [rec.record(_product(n=i)) for i in range(2500)]) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        await asyncio.sleep(1.3)
        await rec.stop()

    asyncio.run(main())
    assert _count(sessions) == 5000 and rec.written == 5000
    # 500 rows wake the writer early; it never writes one row at a time.
    assert 1 <= rec.transactions <= 15
    assert rec.dropped_total == 0


def test_the_queue_drops_the_oldest_and_raises_then_clears_its_alarm(sessions):
    alarm_manager.clear_alarm(AlarmCode.RECORDS_DROPPED, records_module.SOURCE)
    rec = _Recorder(sessions, limit=10)
    for i in range(15):
        rec.record(_product(n=i))
    assert rec.waiting() == 10 and rec.dropped_total == 5
    assert [r["track_id"] for r in rec._rows] == list(range(5, 15))
    alarm = alarm_manager.get(AlarmCode.RECORDS_DROPPED, records_module.SOURCE)
    assert alarm is not None and alarm.severity.value == "warning"

    assert asyncio.run(rec.flush()) == 10
    # Rows were dropped since the last write: the alarm stays until a write without drops.
    assert alarm_manager.get(AlarmCode.RECORDS_DROPPED, records_module.SOURCE) is not None
    rec.record(_product(n=99))
    assert asyncio.run(rec.flush()) == 1
    assert alarm_manager.get(AlarmCode.RECORDS_DROPPED, records_module.SOURCE) is None
    assert [r.track_id for r in _rows(sessions)] == list(range(5, 15)) + [99]


def test_a_database_error_keeps_the_rows_and_retries(sessions):
    alarm_manager.clear_alarm(AlarmCode.RECORDS_WRITE_FAILED, records_module.SOURCE)
    broken = {"on": True}

    def factory():
        if broken["on"]:
            raise sa.exc.OperationalError("INSERT", {}, Exception("database is locked"))
        return sessions()

    rec = _Recorder(factory)
    for i in range(3):
        rec.record(_product(n=i))
    for attempt in range(1, 4):
        assert asyncio.run(rec.flush()) == 0
        rec.record(_product(n=10 + attempt))
        assert rec.failures == attempt
        assert (alarm_manager.get(AlarmCode.RECORDS_WRITE_FAILED, records_module.SOURCE) is not None) == (attempt >= 3)
    assert alarm_manager.get(AlarmCode.RECORDS_WRITE_FAILED, records_module.SOURCE).severity.value == "critical"
    broken["on"] = False
    assert asyncio.run(rec.flush()) == 6
    assert alarm_manager.get(AlarmCode.RECORDS_WRITE_FAILED, records_module.SOURCE) is None
    # Kept in their order, the new rows after them.
    assert [r.track_id for r in _rows(sessions)] == [0, 1, 2, 11, 12, 13]


def test_retention_deletes_only_rows_older_than_the_limit(sessions, monkeypatch):
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    rows = [_product(n=i, recorded_at=now - timedelta(days=d)) for i, d in enumerate((100, 91, 90.5, 89, 1, 0))]

    async def insert():
        async with sessions() as db:
            await db.execute(sa.insert(ProductRecord), rows)
            await db.commit()

    asyncio.run(insert())
    rec = ProductionRecorder(sessions)
    monkeypatch.setattr(ProductionRecorder, "DELETE_CHUNK", 2)  # several chunks
    assert asyncio.run(rec.prune(days=0, now=now)) == 0  # 0 = keep for ever
    assert asyncio.run(rec.prune(days=90, now=now)) == 3
    assert sorted(r.track_id for r in _rows(sessions)) == [3, 4, 5]


# ── Counts survive a restart ──────────────────────────────────────────────────

def test_totals_come_back_after_a_restart(lines, sessions, monkeypatch):
    rec = _Recorder(sessions)
    monkeypatch.setattr(records_module, "production_recorder", rec)
    runtime = _line(lines, [{"camera_id": "top"}, {"camera_id": "own", "station": "own"}])
    _cross(runtime.counter, "top", class_name="bottle")
    _cross(runtime.counter, "top", class_name="can")
    asyncio.run(rec.flush())

    # Reset: nothing from before counts again.
    from app.routes.v1.counting import reset_line_counters
    reset_line_counters(runtime, None)
    saved = SettingsPersistenceService.get_line(runtime.id)
    assert datetime.fromisoformat(saved["counts_reset_at"]) <= datetime.now(timezone.utc)
    for name, defect in (("bottle", False), ("bottle", True), ("can", False), ("scratch", True), ("bottle", False)):
        _cross(runtime.counter, "top", is_defect=defect, class_name=name)
    _cross(runtime.aux_counters["own"], "own", is_defect=True)
    _cross(runtime.aux_counters["own"], "own")
    asyncio.run(rec.flush())
    before = (_totals(runtime.counter), dict(runtime.counter.counts_by_class), dict(runtime.counter.rejects_by_class))
    assert before[0] == (5, 3, 2)

    # A "restart": new counters, rebuilt from the records.
    fresh = LineManager()
    monkeypatch.setattr(line_module, "line_manager", fresh)
    fresh.apply_state()
    restarted = fresh.get(runtime.id)
    assert restarted.counter is not runtime.counter and _totals(restarted.counter) == (0, 0, 0)
    restored = asyncio.run(restore_line_counts(sessions))
    assert restored[runtime.id] == 5
    after = (_totals(restarted.counter), dict(restarted.counter.counts_by_class), dict(restarted.counter.rejects_by_class))
    assert after == before
    assert _totals(restarted.aux_counters["own"]) == (2, 1, 1)

    # A single class reset is kept too.
    restarted.counter.reset_counts(reset_all=False, classes_to_reset=["bottle"])
    SettingsPersistenceService.mark_counts_reset(runtime.id, ["Bottle"])
    expected = (_totals(restarted.counter), dict(restarted.counter.counts_by_class))
    counter = CountingService(runtime.id, dispatch_telemetry=False)
    since = records_module.parse_time(SettingsPersistenceService.get_line(runtime.id)["counts_reset_at"])
    classes = records_module._reset_times(SettingsPersistenceService.get_line(runtime.id))[1]

    async def totals():
        async with sessions() as db:
            return await counted_totals(db, runtime.id, since, classes)

    counter.restore(asyncio.run(totals()))
    assert (_totals(counter), counter.counts_by_class) == expected == ((2, 1, 1), {"can": 1, "scratch": 1})


def test_restore_gives_good_plus_rejected_equal_to_the_total():
    counter = CountingService(dispatch_telemetry=False)
    counter.restore({"counts_by_class": {"Bottle": 4, "can": 2}, "rejects_by_class": {"bottle": 1, "ghost": 3}})
    assert _totals(counter) == (6, 5, 1)
    assert counter.counts_by_class == {"bottle": 4, "can": 2} and counter.rejects_by_class == {"bottle": 1}
    counter.count_product("can", True)
    assert _totals(counter) == (7, 5, 2)


def test_the_version_8_step_and_new_lines_set_the_reset_time(lines):
    state = {"schema_version": 7, "lines": [{"id": "line-1", "name": "Line 1", "cameras": []},
                                            {"id": "old", "name": "Old", "cameras": [], "counts_reset_at": "2026-01-01T00:00:00+00:00"}]}
    upgrade_state(state)
    assert datetime.fromisoformat(state["lines"][0]["counts_reset_at"]) > datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert state["lines"][1]["counts_reset_at"] == "2026-01-01T00:00:00+00:00"

    runtime = _line(lines, [{"camera_id": "top"}], name="New")
    saved = SettingsPersistenceService.get_line(runtime.id)
    assert saved["counts_reset_at"]
    # A client cannot set it; saving the line again keeps it.
    again = SettingsPersistenceService.save_line({**saved, "counts_reset_at": "2000-01-01T00:00:00+00:00", "name": "Renamed"})
    assert again["counts_reset_at"] == saved["counts_reset_at"] and again["name"] == "Renamed"
    assert SettingsPersistenceService.get_line(PRIMARY_LINE_ID)["counts_reset_at"]


# ── The API ───────────────────────────────────────────────────────────────────

def _test_db_engine():
    url = os.environ["DATABASE_URL"].replace("sqlite+aiosqlite", "sqlite")
    return sa.create_engine(url)


def _insert(rows):
    engine = _test_db_engine()
    try:
        with engine.begin() as conn:
            conn.execute(ProductRecord.__table__.insert(), [records_module.normalize_row(r) for r in rows])
    finally:
        engine.dispose()


T0 = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture
def seeded(client):
    """Records of two lines over two hours on 2026-03-02, under ids no other test uses."""
    a, b = f"rec-a-{uuid.uuid4().hex[:6]}", f"rec-b-{uuid.uuid4().hex[:6]}"
    rows = []
    for i in range(10):
        rows.append({"recorded_at": T0 + timedelta(minutes=10 * i), "line_id": a, "line_name": "A", "camera_id": "cam-a",
                     "camera_name": "Top", "kind": "product", "counted": True, "result": "reject" if i % 3 == 0 else "good",
                     "reject_reason": "vision_class" if i % 3 == 0 else None, "class_name": "scratch" if i % 3 == 0 else "bottle",
                     "confidence": 0.9, "track_id": i, "code": f"SKU-{i:03d}", "code_status": "known",
                     "batch": "B1" if i < 5 else "B2"})
    rows.append({"recorded_at": T0 + timedelta(minutes=5), "line_id": a, "camera_id": "own", "kind": "product",
                 "counted": False, "result": "reject", "reject_reason": "vision_class", "class_name": "dent"})
    rows.append({"recorded_at": T0 + timedelta(minutes=6), "line_id": a, "camera_id": "qr", "kind": "code",
                 "counted": False, "code": "=cmd|' /C calc'!A0", "code_status": "unknown"})
    rows.append({"recorded_at": T0 + timedelta(minutes=30), "line_id": b, "camera_id": "cam-b", "kind": "product",
                 "counted": True, "result": "good", "class_name": "bottle"})
    # Outside the range.
    rows.append({"recorded_at": T0 - timedelta(hours=1), "line_id": a, "camera_id": "cam-a", "kind": "product",
                 "counted": True, "result": "good", "class_name": "bottle"})
    _insert(rows)
    return a, b


def _range(**extra):
    return {"start": _iso(T0), "end": _iso(T0 + timedelta(hours=2)), **extra}


def test_records_filters_and_paging(client, admin_headers, seeded):
    a, b = seeded
    res = client.get(f"{API}/records", headers=admin_headers, params=_range(line_id=a))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 12
    times = [r["recorded_at"] for r in body["rows"]]
    assert times == sorted(times, reverse=True) and times[0].endswith("Z")

    def total(**params):
        res = client.get(f"{API}/records", headers=admin_headers, params=_range(line_id=a, **params))
        assert res.status_code == 200, res.text
        return res.json()["total"]

    assert total(result="reject") == 5
    assert total(kind="code") == 1
    assert total(batch="B1") == 5
    assert total(code="SKU-00") == 10
    assert total(code="sku-005") == 1
    assert total(counted="true") == 10
    assert total(camera_id="own") == 1
    # A narrower range.
    narrow = client.get(f"{API}/records", headers=admin_headers, params={
        "line_id": a, "start": _iso(T0 + timedelta(minutes=20)), "end": "2026-03-02T09:50:00+01:00"})
    assert narrow.json()["total"] == 3  # 08:20, 08:30 and 08:40 UTC; the end is left out
    both = client.get(f"{API}/records", headers=admin_headers, params=_range()).json()["total"]
    assert both >= 13

    page1 = client.get(f"{API}/records", headers=admin_headers, params=_range(line_id=a, limit=5)).json()
    page3 = client.get(f"{API}/records", headers=admin_headers, params=_range(line_id=a, limit=5, offset=10)).json()
    assert len(page1["rows"]) == 5 and len(page3["rows"]) == 2 and page3["total"] == 12
    assert not {r["id"] for r in page1["rows"]} & {r["id"] for r in page3["rows"]}


@pytest.mark.parametrize("params,message", [
    ({"start": "2026-03-02T08:00:00", "end": "2026-03-02T09:00:00Z"}, "time zone"),
    ({"start": "2026-03-02T09:00:00Z", "end": "2026-03-02T08:00:00Z"}, "after start"),
    ({"start": "2025-01-01T00:00:00Z", "end": "2026-03-02T00:00:00Z"}, "366 days"),
    ({"start": "2026-03-02T08:00:00Z"}, None),
])
def test_records_range_is_checked(client, admin_headers, params, message):
    res = client.get(f"{API}/records", headers=admin_headers, params=params)
    assert res.status_code == 422
    if message:
        assert message in res.json()["detail"]


def test_records_summary(client, admin_headers, seeded):
    a, _ = seeded
    res = client.get(f"{API}/records/summary", headers=admin_headers, params=_range(line_id=a, tz="Europe/Berlin"))
    assert res.status_code == 200, res.text
    body = res.json()
    # The line totals: the counted products only.
    assert body["totals"] == {"total": 10, "good": 6, "reject": 4, "yield": 60.0}
    assert {e["class_name"]: (e["total"], e["reject"]) for e in body["by_class"]} == {"bottle": (6, 0), "scratch": (4, 4)}
    assert body["by_reject_reason"] == [{"reject_reason": "vision_class", "count": 4}]
    cameras = {e["camera_id"]: e for e in body["by_camera"]}
    assert (cameras["cam-a"]["total"], cameras["cam-a"]["counted"], cameras["cam-a"]["camera_name"]) == (10, True, "Top")
    assert (cameras["own"]["total"], cameras["own"]["reject"], cameras["own"]["counted"]) == (1, 1, False)
    assert body["codes"] == {"known": 10, "unknown": 1}
    # Hours in Berlin time (UTC+1 in March).
    assert [(e["start"], e["total"], e["reject"]) for e in body["buckets"]] == [
        ("2026-03-02T09:00:00+01:00", 6, 2), ("2026-03-02T10:00:00+01:00", 4, 2)]
    day = client.get(f"{API}/records/summary", headers=admin_headers, params=_range(line_id=a, bucket="day")).json()
    assert [(e["start"], e["total"]) for e in day["buckets"]] == [("2026-03-02T00:00:00+00:00", 10)]
    assert day["tz"] == "UTC"
    # Filtering by camera counts that camera's products.
    own = client.get(f"{API}/records/summary", headers=admin_headers, params=_range(line_id=a, camera_id="own")).json()
    assert own["totals"] == {"total": 1, "good": 0, "reject": 1, "yield": 0.0}
    empty = client.get(f"{API}/records/summary", headers=admin_headers, params=_range(line_id="nothing-here")).json()
    assert empty["totals"] == {"total": 0, "good": 0, "reject": 0, "yield": None}


def _csv(text):
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:])))


def test_csv_export(client, admin_headers, seeded):
    a, _ = seeded
    res = client.get(f"{API}/records/export", headers=admin_headers,
                     params=_range(line_id=a, format="csv", tz="America/New_York"))
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/csv")
    assert res.headers["content-disposition"] == f'attachment; filename="records_{a}_20260302-0300_20260302-0500.csv"'
    rows = _csv(res.text)
    header, body = rows[0], rows[1:]
    assert header[:2] == ["time (America/New_York)", "time_utc"] and "code" in header and "stations" in header
    assert len(body) == 12
    first = dict(zip(header, body[0]))
    # Oldest first; New York is UTC-5 on 2 March.
    assert first["time (America/New_York)"] == "2026-03-02 03:00:00" and first["time_utc"] == "2026-03-02T08:00:00.000000Z"
    assert (first["line_id"], first["result"], first["counted"], first["code"]) == (a, "reject", "true", "SKU-000")
    formula = next(dict(zip(header, r)) for r in body if dict(zip(header, r))["kind"] == "code")
    assert formula["code"] == "'=cmd|' /C calc'!A0"  # cannot run as a formula

    unknown = client.get(f"{API}/records/export", headers=admin_headers, params=_range(line_id=a, tz="Mars/Base"))
    assert unknown.headers["x-records-timezone"] == "UTC" and "Mars/Base" in unknown.headers["x-records-timezone-note"]
    assert _csv(unknown.text)[0][0] == "time (UTC)"


def test_a_large_csv_export_is_read_in_chunks(client, admin_headers, monkeypatch):
    line = f"rec-big-{uuid.uuid4().hex[:6]}"
    # Three rows share each time, so chunks also end in the middle of a time.
    _insert([{"recorded_at": T0 + timedelta(seconds=i // 3), "line_id": line, "kind": "product", "counted": True,
              "result": "good", "class_name": "bottle", "track_id": i} for i in range(1234)])
    monkeypatch.setattr(export_module, "CHUNK_ROWS", 100)
    chunks = []
    real = export_module.iter_chunks

    async def spy(*args, **kwargs):
        async for rows in real(*args, **kwargs):
            chunks.append(len(rows))
            yield rows

    monkeypatch.setattr(export_module, "iter_chunks", spy)
    with client.stream("GET", f"{API}/records/export", headers=admin_headers, params=_range(line_id=line)) as res:
        assert res.status_code == 200
        parts = list(res.iter_bytes())
    assert chunks == [100] * 12 + [34]
    text = b"".join(parts).decode("utf-8")
    body = _csv(text)[1:]
    assert len(body) == 1234 and [int(r[-1]) for r in body] == sorted(int(r[-1]) for r in body)
    assert len({r[-1] for r in body}) == 1234


def _sheet_rows(xml):
    root = ElementTree.fromstring(xml)
    out = []
    for row in root.find("m:sheetData", NS):
        cells = []
        for cell in row:
            text = cell.find("m:is/m:t", NS)
            value = cell.find("m:v", NS)
            cells.append((cell.get("r"), cell.get("t"), cell.get("s"), text.text if text is not None else (value.text if value is not None else None)))
        out.append(cells)
    return out


def test_xlsx_export(client, admin_headers, seeded):
    a, _ = seeded
    res = client.get(f"{API}/records/export", headers=admin_headers,
                     params=_range(line_id=a, format="xlsx", tz="Europe/Berlin"))
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert res.headers["content-disposition"].endswith('.xlsx"')
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = set(zf.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml",
                "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"} <= names
        workbook = ElementTree.fromstring(zf.read("xl/workbook.xml"))
        assert [s.get("name") for s in workbook.find("m:sheets", NS)] == ["Records", "Summary"]
        styles = zf.read("xl/styles.xml").decode()
        assert 'formatCode="yyyy-mm-dd hh:mm:ss"' in styles
        records = _sheet_rows(zf.read("xl/worksheets/sheet1.xml"))
        summary = _sheet_rows(zf.read("xl/worksheets/sheet2.xml"))

    header = [c[3] for c in records[0]]
    assert header[:2] == ["time (Europe/Berlin)", "time_utc"] and len(records) == 13
    local, utc_cell = records[1][0], records[1][1]
    # 08:00 UTC = 09:00 in Berlin: Excel's day number of 2026-03-02 09:00.
    assert local[0] == "A2" and local[2] == "1" and local[1] is None
    assert float(local[3]) == pytest.approx((datetime(2026, 3, 2, 9) - datetime(1899, 12, 30)).total_seconds() / 86400)
    assert float(utc_cell[3]) == pytest.approx(float(local[3]) - 1 / 24)
    by_header = {header[i]: cell for i, cell in enumerate(records[1])}
    assert by_header["confidence"][1] is None and float(by_header["confidence"][3]) == 0.9  # a number
    assert by_header["code"][1] == "inlineStr" and by_header["code"][3] == "SKU-000"
    assert by_header["counted"][1] == "b" and by_header["counted"][3] == "1"

    labels = {row[0][3]: [c[3] for c in row[1:]] for row in summary if row}
    assert labels["Line"] == [a]
    totals_at = next(i for i, row in enumerate(summary) if row and row[0][3] == "Total")
    assert [c[3] for c in summary[totals_at + 1]] == ["10", "6", "4", "0.6"]
    assert summary[totals_at + 1][3][2] == "3"  # yield as a percentage
    assert labels["bottle"] == ["6", "6", "0"] and labels["vision_class"] == ["4"]


def test_xlsx_refuses_more_rows_than_a_sheet_holds(client, admin_headers, seeded, monkeypatch):
    a, _ = seeded
    monkeypatch.setattr(export_module, "XLSX_MAX_ROWS", 11)
    res = client.get(f"{API}/records/export", headers=admin_headers, params=_range(line_id=a, format="xlsx"))
    assert res.status_code == 413 and "CSV" in res.json()["detail"]
    monkeypatch.setattr(export_module, "XLSX_MAX_ROWS", 12)
    assert client.get(f"{API}/records/export", headers=admin_headers, params=_range(line_id=a, format="xlsx")).status_code == 200


def test_xlsx_text_is_valid_xml():
    xml = export_module.sheet_row(3, ["a\x1db", "x_x0041_y", "<&>", 2, 1.5, True, None])
    root = ElementTree.fromstring(f'<sheetData xmlns="{NS["m"]}">{xml}</sheetData>')
    texts = [t.text for t in root.iter(f'{{{NS["m"]}}}t')]
    assert texts == ["a_x001D_b", "x_x005F_x0041_y", "<&>"]
    assert export_module.column_letter(0) == "A" and export_module.column_letter(27) == "AB"


def test_records_need_the_records_scope(client, admin_headers, seeded):
    from app.security.api_key_scopes import API_KEY_SCOPES, required_api_key_scopes

    assert API_KEY_SCOPES["records:read"]["min_clearance"] == 1
    for path in ("/api/v1/records", "/api/v1/records/summary", "/api/v1/records/export"):
        assert required_api_key_scopes("GET", path) == {"records:read"}
        assert required_api_key_scopes("POST", path) is None
    me = client.get(f"{API}/auth/me", headers=admin_headers).json()

    def key(scopes):
        created = client.post(f"{API}/auth/api-keys", headers=admin_headers, json={
            "user_id": me["id"], "name": f"records {uuid.uuid4().hex[:4]}", "scopes": scopes, "expires_in_days": 1})
        assert created.status_code == 201, created.text
        return {"X-API-Key": created.json()["api_key"]}

    assert client.get(f"{API}/records", headers=key(["monitor:read"]), params=_range()).status_code == 403
    allowed = key(["records:read"])
    assert client.get(f"{API}/records", headers=allowed, params=_range()).status_code == 200
    assert client.get(f"{API}/records/export", headers=allowed, params=_range(line_id=seeded[0])).status_code == 200
    assert client.get(f"{API}/records", params=_range()).status_code == 401


# ── The database upgrade ──────────────────────────────────────────────────────

def _alembic(tmp_path, name):
    from alembic.config import Config

    engine = sa.create_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return engine, config


def _shape(conn):
    inspector = sa.inspect(conn)
    columns = sorted((c["name"], str(c["type"]), bool(c["nullable"])) for c in inspector.get_columns("product_records"))
    indexes = sorted((i["name"], tuple(i["column_names"])) for i in inspector.get_indexes("product_records"))
    return columns, indexes


def test_a_database_at_0005_upgrades_and_a_new_one_builds(tmp_path):
    from alembic import command

    engine, config = _alembic(tmp_path, "old.db")
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "0005_product_lists")
            assert "product_records" not in sa.inspect(conn).get_table_names()
            command.upgrade(config, "head")
            upgraded = _shape(conn)
            conn.execute(ProductRecord.__table__.insert(), [records_module.normalize_row(_product())])
        with engine.begin() as conn:  # again: nothing changes
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            assert conn.execute(sa.text("SELECT COUNT(*) FROM product_records")).scalar_one() == 1
            command.downgrade(config, "0005_product_lists")
            assert "product_records" not in sa.inspect(conn).get_table_names()
    finally:
        engine.dispose()
    assert upgraded[1] == [
        ("ix_product_records_batch", ("batch",)),
        ("ix_product_records_code", ("code",)),
        ("ix_product_records_line_time", ("line_id", "recorded_at")),
        ("ix_product_records_recorded_at", ("recorded_at",)),
    ]

    engine, config = _alembic(tmp_path, "new.db")
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            assert _shape(conn) == upgraded
    finally:
        engine.dispose()


def test_filters_read_utc_whatever_zone_they_were_given():
    flt = RecordFilter(start=records_module.parse_time("2026-03-02T09:00:00+01:00"),
                       end=records_module.parse_time("2026-03-02 10:00:00 01:00"))
    assert flt.start == T0 and flt.end == T0 + timedelta(hours=1)
    assert records_module.parse_time("2026-03-02T08:00:00") is None
    assert records_module.parse_time("yesterday") is None
