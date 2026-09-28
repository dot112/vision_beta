"""Tests for the alarm model, alarm manager and event-bus publishing."""
from __future__ import annotations

import asyncio
import os
import threading

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production-use-0123456789")

import pytest  # noqa: E402

from app.events import system_events
from app.events.alarm_events import AlarmManager, AlarmSeverity, AlarmState, alarm_manager
from app.events.detection_events import DetectionEvent, LineCrossEvent, publish_event
from app.events.event_bus import EventBus


@pytest.fixture(autouse=True)
def _reset_alarms():
    alarm_manager.reset()
    yield
    alarm_manager.reset()


def test_repeated_raise_updates_one_alarm():
    mgr = AlarmManager()
    first = mgr.raise_alarm("plc.connect_failed", "plc:ep1", "refused")
    second = mgr.raise_alarm("plc.connect_failed", "plc:ep1", "timed out")

    assert first is second
    assert second.count == 2
    assert second.message == "timed out"
    assert len(mgr.active()) == 1
    assert mgr.total_raised == 1


def test_same_code_different_source_are_separate():
    mgr = AlarmManager()
    mgr.raise_alarm("plc.connect_failed", "plc:ep1", "down")
    mgr.raise_alarm("plc.connect_failed", "plc:ep2", "down")
    assert len(mgr.active()) == 2


def test_severity_escalates_but_never_downgrades():
    mgr = AlarmManager()
    mgr.raise_alarm("x", "s", "m", AlarmSeverity.WARNING)
    mgr.raise_alarm("x", "s", "m", AlarmSeverity.CRITICAL)
    assert mgr.get("x", "s").severity == AlarmSeverity.CRITICAL
    mgr.raise_alarm("x", "s", "m", AlarmSeverity.INFO)
    assert mgr.get("x", "s").severity == AlarmSeverity.CRITICAL


def test_clear_moves_alarm_to_history():
    mgr = AlarmManager()
    mgr.raise_alarm("x", "s", "m")
    cleared = mgr.clear_alarm("x", "s", "fixed")

    assert cleared.state == AlarmState.CLEARED
    assert cleared.cleared_at is not None
    assert mgr.active() == []
    assert [a.code for a in mgr.history()] == ["x"]
    assert mgr.clear_alarm("x", "s") is None  # clearing twice is a no-op


def test_raise_after_clear_starts_a_new_alarm():
    mgr = AlarmManager()
    old = mgr.raise_alarm("x", "s", "m")
    mgr.clear_alarm("x", "s")
    new = mgr.raise_alarm("x", "s", "m")
    assert new.id != old.id
    assert new.count == 1


def test_acknowledge_keeps_alarm_active_until_cleared():
    mgr = AlarmManager()
    alarm = mgr.raise_alarm("x", "s", "m", AlarmSeverity.CRITICAL)
    acked = mgr.acknowledge(alarm.id, "operator1")

    assert acked.state == AlarmState.ACKNOWLEDGED
    assert acked.acknowledged_by == "operator1"
    assert mgr.counts()["critical"] == 1
    assert mgr.counts()["unacknowledged"] == 0
    assert mgr.acknowledge("missing-id") is None


def test_active_sorted_most_severe_first_and_filterable():
    mgr = AlarmManager()
    mgr.raise_alarm("a", "camera:1", "m", AlarmSeverity.INFO)
    mgr.raise_alarm("b", "plc:1", "m", AlarmSeverity.CRITICAL)
    mgr.raise_alarm("c", "plc:2", "m", AlarmSeverity.WARNING)

    assert [a.code for a in mgr.active()] == ["b", "c", "a"]
    assert {a.code for a in mgr.active(source_prefix="plc:")} == {"b", "c"}


def test_clear_source_clears_every_alarm_for_that_source():
    mgr = AlarmManager()
    mgr.raise_alarm("a", "plc:1", "m")
    mgr.raise_alarm("b", "plc:1", "m")
    mgr.raise_alarm("a", "plc:2", "m")
    assert mgr.clear_source("plc:1") == 2
    assert [a.source for a in mgr.active()] == ["plc:2"]


def test_history_is_bounded():
    mgr = AlarmManager(history_size=3)
    for i in range(5):
        mgr.raise_alarm("x", f"s{i}", "m")
        mgr.clear_alarm("x", f"s{i}")
    assert [a.source for a in mgr.history()] == ["s4", "s3", "s2"]


def test_to_dict_is_json_friendly():
    import json
    alarm = AlarmManager().raise_alarm("x", "s", "m", details={"port": 502})
    data = json.loads(json.dumps(alarm.to_dict()))
    assert data["severity"] == "warning"
    assert data["state"] == "active"
    assert data["details"] == {"port": 502}


def test_concurrent_raises_from_threads_dedupe():
    mgr = AlarmManager()

    def worker():
        for _ in range(200):
            mgr.raise_alarm("camera.stalled", "camera:1", "stalled")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(mgr.active()) == 1
    assert mgr.get("camera.stalled", "camera:1").count == 1600


def test_alarm_lifecycle_is_published_on_event_bus(monkeypatch):
    bus = EventBus()
    monkeypatch.setattr("app.events.event_bus.event_bus", bus)
    received = []
    for name in ("alarm_raised", "alarm_acknowledged", "alarm_cleared"):
        bus.subscribe(name, lambda data, _n=name: received.append((_n, data["code"])))

    async def run():
        alarm = alarm_manager.raise_alarm("x", "s", "m")
        alarm_manager.raise_alarm("x", "s", "m")  # repeat: not re-announced
        alarm_manager.acknowledge(alarm.id, "op")
        alarm_manager.clear_alarm("x", "s")
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert received == [("alarm_raised", "x"), ("alarm_acknowledged", "x"), ("alarm_cleared", "x")]


def test_publish_from_worker_thread_reaches_loop(monkeypatch):
    bus = EventBus()
    monkeypatch.setattr("app.events.event_bus.event_bus", bus)
    received = []
    bus.subscribe("alarm_raised", lambda data: received.append(data["source"]))

    async def run():
        system_events.set_event_loop(asyncio.get_running_loop())
        try:
            thread = threading.Thread(target=lambda: alarm_manager.raise_alarm("x", "camera:thread", "m"))
            thread.start()
            await asyncio.to_thread(thread.join)
            await asyncio.sleep(0.05)
        finally:
            system_events.set_event_loop(None)

    asyncio.run(run())
    assert received == ["camera:thread"]


def test_publish_without_loop_is_a_safe_noop():
    system_events.set_event_loop(None)
    assert system_events.publish_threadsafe("alarm_raised", {}) is False
    alarm_manager.raise_alarm("x", "s", "m")  # must not raise
    assert alarm_manager.get("x", "s") is not None


def test_detection_events_publish_with_existing_event_names(monkeypatch):
    bus = EventBus()
    monkeypatch.setattr("app.events.event_bus.event_bus", bus)
    received = []
    bus.subscribe("detection", lambda d: received.append(("detection", d["class_name"])))
    bus.subscribe("wireline_cross", lambda d: received.append(("wireline_cross", d["line_index"])))

    async def run():
        assert publish_event(DetectionEvent(camera_id="c1", class_name="defect", confidence=0.9, is_defect=True))
        assert publish_event(LineCrossEvent(camera_id="c1", line_index=1, class_name="bottle"))
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert received == [("detection", "defect"), ("wireline_cross", 1)]
