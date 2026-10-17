"""A camera that reads codes Continuously only shows them; a triggered picture feeds the line.

Shown means the marks on the camera's live picture and the line's recent reads.
Nothing else: no pairing, product, count, output action, message or record.
"""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import numpy as np
import pytest

import app.services.line_service as line_module
import app.services.settings_persistence_service as persistence_module
from app.services.counting_service import CountingService, counting_service
from app.services.line_config import upgrade_state
from app.services.line_service import LineManager
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService

LIST = "list-1"
WINDOW_MS = 80


@pytest.fixture
def lines(monkeypatch, tmp_path):
    """Throwaway settings file, dispatcher and line manager; the product list holds LISTED."""
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
    """Stands in for WirelineTracker: every update reports one crossing."""

    def __init__(self):
        self.objects, self._recently_counted, self.next_id = {}, set(), 1

    def update(self, **kwargs):
        self.next_id += 1
        return [(self.next_id, "bottle", False, 0.9)]


def _cross(counter, camera_id="vis"):
    counter._trackers[camera_id] = _CrossingTracker()
    counter.process_frame([], 640, 480, camera_rotation=90, camera_id=camera_id)


def _line(manager, cameras, name="Shown"):
    cameras = [cam if cam.get("role", "vision") != "vision" else {"model_id": "model-test", **cam} for cam in cameras]
    line = SettingsPersistenceService.save_line({"name": name, "cameras": cameras, "sync": {"window_ms": WINDOW_MS}, "enabled": True})
    manager.apply_state()
    runtime = manager.get(line["id"])
    runtime.sent = []
    runtime.counter.dispatch_event = lambda payload, plc_event, is_defect: runtime.sent.append((payload, plc_event))
    return runtime


def _run(steps):
    """Run ``steps`` (an async function) with the line runtimes' event loop set, as in the server."""
    async def main():
        CountingService.set_event_loop(asyncio.get_running_loop())
        try:
            await steps()
        finally:
            CountingService.set_event_loop(None)

    asyncio.run(main())


async def _record(events, event):
    events.append(event)


def _watch(monkeypatch, runtime):
    """Everything a read could send or save: PLC events, messages, records."""
    seen = {"events": [], "messages": [], "records": []}
    monkeypatch.setattr(PLCDispatcherService, "evaluate", classmethod(lambda cls, event: _record(seen["events"], event)))
    monkeypatch.setattr(runtime.counter, "send", lambda event, payload: seen["messages"].append(payload) or 0)
    monkeypatch.setattr("app.services.production_records_service.record_event",
                        lambda *args, **kwargs: seen["records"].append(args))
    return seen


def _worker_with_codes(monkeypatch, camera_id, codes):
    """A continuous reader whose every frame holds ``codes``."""
    from app.services import qr_service

    worker = qr_service._QRReaderWorker(camera_id)
    found = [SimpleNamespace(data=code, code_type="QR_CODE", polygon=[]) for code in codes]
    monkeypatch.setattr(worker._engine, "decode",
                        lambda mat, hard_budget_ms=0.0, code_type="all": SimpleNamespace(codes=found, decode_time_ms=1.0))
    return worker


def _process_twice(worker):
    async def steps():
        frame = np.zeros((10, 10, 3), np.uint8)
        worker.process(frame)
        await asyncio.sleep(0.01)
        worker.process(frame)
        await asyncio.sleep(0.05)
    return steps


VISION = {"camera_id": "vis", "role": "vision", "counting": True}
READER = {"camera_id": "qr", "role": "qr", "product_list_id": LIST, "qr_trigger": "continuous"}


def test_continuous_reads_are_only_shown(lines, monkeypatch):
    runtime = _line(lines, [VISION, READER])
    seen = _watch(monkeypatch, runtime)
    worker = _worker_with_codes(monkeypatch, "qr", ["LISTED"])
    try:
        _run(_process_twice(worker))
    finally:
        worker.stop()
    reads = runtime.recent_reads()
    assert [(r["code"], r["status"], r.get("shown_only")) for r in reads] == [("LISTED", "known", True)]
    assert reads[0]["product_name"] == "Widget" and reads[0]["camera_id"] == "qr"
    assert seen == {"events": [], "messages": [], "records": []} and runtime.sent == []
    assert runtime.qr_stats["codes_read"] == 0 and runtime.counter.total_inspected == 0


def test_triggered_reads_still_reach_the_line(lines, monkeypatch):
    runtime = _line(lines, [VISION, {**READER, "qr_trigger": "signal"}])
    seen = _watch(monkeypatch, runtime)
    capture = {"codes": [{"code": "LISTED", "format": "QR_CODE", "known": True}], "t": None, "timestamp": None}

    async def steps():
        runtime.on_qr_capture("qr", capture, None)
        await asyncio.sleep(0.05)

    _run(steps)
    assert [e["qr_code"] for e in seen["events"]] == ["LISTED"]
    assert [m["code"] for m in seen["messages"]] == ["LISTED"] and len(seen["records"]) == 1
    assert runtime.qr_stats["codes_read"] == 1
    assert "shown_only" not in runtime.recent_reads()[0]


def test_a_vision_camera_that_reads_codes_still_counts(lines, monkeypatch):
    runtime = _line(lines, [{**VISION, "read_codes": True, "product_list_id": LIST, "qr_trigger": "continuous"}])
    seen = _watch(monkeypatch, runtime)
    worker = _worker_with_codes(monkeypatch, "vis", ["LISTED"])

    async def steps():
        await _process_twice(worker)()
        _cross(runtime.counter)
        await asyncio.sleep(0.05)

    try:
        _run(steps)
    finally:
        worker.stop()
    assert runtime.counter.total_inspected == 1 and len(runtime.sent) == 1
    payload, _ = runtime.sent[0]
    assert payload.get("qr_code") is None                      # the shown code did not pair with the product
    assert [r.get("shown_only") for r in runtime.recent_reads()] == [True]
    assert [e for e in seen["events"] if e.get("event_type") == "qr_read"] == []


# ── Save checks, the flag and the warnings ────────────────────────────────────

from app.services.line_config import line_warnings, normalize_camera, normalize_line  # noqa: E402

CHECK = {"name": "Wrong", "only_when": {"rules": [{"left": "class", "op": "=", "right": "x"}]}}


def test_a_code_check_needs_a_trigger():
    with pytest.raises(ValueError, match=r"Camera 2 checks codes, but Read codes 'Continuously' only shows them\. "
                                         r"Choose a wire line or 'When an input asks'\."):
        normalize_line({"name": "L", "cameras": [VISION, {**READER, "qr_action": "accept_listed"}]})
    with pytest.raises(ValueError, match="Camera 1 checks codes"):
        normalize_line({"name": "L", "cameras": [{"camera_id": "qr", "role": "qr", "product_list_id": LIST,
                                                  "qr_action": "reject_listed"}]})     # no qr_trigger: continuous
    line = normalize_line({"name": "L", "cameras": [VISION, {**READER, "qr_action": "accept_listed", "qr_trigger": "line1"}]})
    assert line["sync"]["enabled"] is True


def test_sync_needs_a_triggered_reader():
    with pytest.raises(ValueError, match=r"Sync needs a camera that reads codes when a product crosses a wire line "
                                         r"or when an input asks\."):
        normalize_line({"name": "L", "cameras": [VISION, READER], "sync": {"enabled": True}})
    line = normalize_line({"name": "L", "cameras": [VISION, READER, {**READER, "camera_id": "qr2", "qr_trigger": "signal"}],
                           "sync": {"enabled": True}})
    assert line["sync"]["enabled"] is True


def test_product_checks_force_sync_only_with_a_triggered_reader():
    line = normalize_line({"name": "L", "cameras": [VISION, READER], "product_checks": [CHECK]})
    assert line["sync"]["enabled"] is False
    line = normalize_line({"name": "L", "cameras": [VISION, {**READER, "qr_trigger": "signal"}], "product_checks": [CHECK]})
    assert line["sync"]["enabled"] is True


@pytest.mark.parametrize("camera, kept", [
    ({**READER, "qr_trigger_review": True}, True),
    ({**READER, "qr_trigger": "signal", "qr_trigger_review": True}, False),
    ({**READER, "qr_trigger_review": False}, False),
    ({**READER, "qr_trigger_review": "yes"}, False),
    ({**VISION, "qr_trigger_review": True}, False),                       # reads no codes
    ({**VISION, "read_codes": True, "qr_trigger_review": True}, True),
])
def test_the_flag_is_kept_only_on_a_continuous_reader(camera, kept):
    assert normalize_camera(camera, 0).get("qr_trigger_review") is (True if kept else None)


def test_the_flag_survives_a_client_that_leaves_it_out(lines):
    line = SettingsPersistenceService.save_line({"name": "Kept", "cameras": [{**READER, "qr_trigger_review": True}]})
    again = SettingsPersistenceService.save_line({"id": line["id"], "name": "Kept", "cameras": [READER]})
    assert again["cameras"][0]["qr_trigger_review"] is True
    cleared = SettingsPersistenceService.save_line({"id": line["id"], "name": "Kept",
                                                    "cameras": [{**READER, "qr_trigger_review": False}]})
    assert "qr_trigger_review" not in cleared["cameras"][0]


def test_hardware_trigger_on_continuous_is_warned():
    warnings = line_warnings({"cameras": [{**READER, "camera_id": "g"}]}, trigger_modes={"g": "hardware"})
    assert "Camera 1 takes a picture on its own input, but Read codes 'Continuously' only shows the codes. Set Read " \
           "codes to 'When an input asks': every picture it takes is then read." in warnings
    assert not any("own input" in w for w in line_warnings({"cameras": [{**READER, "camera_id": "g"}]}))


def test_no_hold_time_warning_for_a_code_only_line():
    line = {"cameras": [{**READER, "qr_action": "accept_listed", "qr_trigger": "signal"}]}
    assert not any("hold time" in w for w in line_warnings(line))


# ── Upgrade to version 9 and the alarm ────────────────────────────────────────

from app.events.alarm_events import alarm_manager  # noqa: E402
from app.services.line_config import PRIMARY_LINE_ID, REASON_NO_CODE, SCHEMA_VERSION  # noqa: E402

SHOW_ONLY = "line.codes_show_only"


@pytest.fixture
def alarms():
    yield
    for alarm in alarm_manager.active("line:"):
        if alarm.code == SHOW_ONLY:
            alarm_manager.clear_alarm(alarm.code, alarm.source, "test over")


def _show_only_alarms():
    return [a for a in alarm_manager.active("line:") if a.code == SHOW_ONLY]


def _v8_state(*lines_):
    return {"schema_version": 8, "camera_auto_connect": True, "lines": [copy.deepcopy(line) for line in lines_]}


def test_upgrade_flags_every_continuous_reader():
    cameras = [VISION, READER, {"camera_id": "qr2", "role": "qr"}, {**READER, "camera_id": "qr3", "qr_trigger": "signal"},
               {"camera_id": "vis2", "role": "vision", "read_codes": True}, {"camera_id": "vis3", "role": "vision"}]
    state = _v8_state({"id": PRIMARY_LINE_ID, "name": "Line 1", "cameras": cameras},
                      {"id": "other", "name": "Other", "cameras": [READER]})
    before = copy.deepcopy(state)
    assert SCHEMA_VERSION == 9
    assert upgrade_state(state) and state["schema_version"] == 9
    assert [c.get("qr_trigger_review") for c in state["lines"][0]["cameras"]] == [None, True, True, None, True, None]
    assert state["lines"][1]["cameras"][0]["qr_trigger_review"] is True
    for line in state["lines"]:
        for camera in line["cameras"]:
            camera.pop("qr_trigger_review", None)
    before["schema_version"] = 9
    assert state == before                                   # nothing else changed
    assert not upgrade_state(state)                          # once only


def test_an_upgraded_line_runs_on_its_no_read_setting(lines, monkeypatch, alarms):
    state = _v8_state({"id": PRIMARY_LINE_ID, "name": "Line 1", "enabled": True, "sync": {"enabled": True, "window_ms": WINDOW_MS},
                       "cameras": [{**VISION, "model_id": "model-test"},
                                   {**READER, "qr_action": "accept_listed", "qr_no_read": "reject"}]})
    upgrade_state(state)
    SettingsPersistenceService._state["lines"] = state["lines"]
    lines.apply_state()
    runtime = lines.get(PRIMARY_LINE_ID)
    sent = []
    monkeypatch.setattr(runtime.counter, "dispatch_event", lambda payload, plc_event, is_defect: sent.append(payload))

    async def steps():
        _cross(runtime.counter)
        await asyncio.sleep(0.25)

    _run(steps)
    assert [(p["result"], p["reject_reason"]) for p in sent] == [("REJECTED", REASON_NO_CODE)]
    (alarm,) = _show_only_alarms()
    assert alarm.source == f"line:{PRIMARY_LINE_ID}" and alarm.severity.value == "warning"
    assert "Camera 2" in alarm.message and "every product is rejected" in alarm.message


@pytest.mark.parametrize("cameras, sync, sensor, effect", [
    ([VISION, {**READER, "qr_action": "accept_listed", "qr_no_read": "ignore"}], True, False, "pass without a code"),
    ([{**READER, "qr_action": "accept_listed"}], False, False, "counts nothing"),    # it counted by codes
    ([READER], False, False, "only shown"),                                          # it only reported
    ([VISION, READER], False, False, "only shown"),
])
def test_the_alarm_says_what_the_line_does_meanwhile(lines, alarms, cameras, sync, sensor, effect):
    flagged = [{**c, "qr_trigger_review": True} if c.get("role") == "qr" else {**c, "model_id": "model-test"} for c in cameras]
    state = _v8_state({"id": "l2", "name": "L2", "enabled": True, "sync": {"enabled": sync, "window_ms": WINDOW_MS},
                       "cameras": flagged})
    SettingsPersistenceService._state["lines"] = SettingsPersistenceService._state["lines"] + state["lines"]
    lines.apply_state()
    (alarm,) = _show_only_alarms()
    assert alarm.source == "line:l2" and effect in alarm.message


def test_the_alarm_clears_when_the_flag_goes(lines, alarms):
    line = SettingsPersistenceService.save_line({"name": "Flagged", "enabled": True,
                                                 "cameras": [{**READER, "qr_trigger_review": True}]})
    lines.apply_state()
    assert [a.source for a in _show_only_alarms()] == [f"line:{line['id']}"]
    SettingsPersistenceService.save_line({"id": line["id"], "name": "Flagged", "enabled": False, "cameras": [READER]})
    lines.apply_state()
    assert _show_only_alarms() == []                         # a stopped line raises none
    SettingsPersistenceService.save_line({"id": line["id"], "name": "Flagged", "enabled": True,
                                          "cameras": [{**READER, "qr_trigger": "signal"}]})
    lines.apply_state()
    assert _show_only_alarms() == []
    assert "qr_trigger_review" not in SettingsPersistenceService.get_line(line["id"])["cameras"][0]


def test_keep_show_only_clears_the_flag(lines, alarms):
    line = SettingsPersistenceService.save_line({"name": "Kept", "enabled": True, "cameras": [{**READER, "qr_trigger_review": True}]})
    lines.apply_state()
    assert _show_only_alarms()
    saved = SettingsPersistenceService.save_line({"id": line["id"], "name": "Kept",
                                                  "cameras": [{**READER, "qr_trigger_review": False}]})
    lines.apply_state()
    assert "qr_trigger_review" not in saved["cameras"][0] and _show_only_alarms() == []


def test_an_upgraded_line_can_still_be_started_stopped_and_renamed(lines, alarms):
    # The line of the Review Focus: a code check on a Continuous reader, Sync on, no code: reject.
    state = _v8_state({"id": "l9", "name": "Upgraded", "enabled": True, "sync": {"enabled": True, "window_ms": WINDOW_MS},
                       "cameras": [{**VISION, "model_id": "model-test"},
                                   {**READER, "qr_action": "accept_listed", "qr_no_read": "reject"}]},
                      {"id": "l10", "name": "Synced", "enabled": True, "sync": {"enabled": True, "window_ms": WINDOW_MS},
                       "cameras": [{"camera_id": "vis2", "role": "vision", "counting": True, "model_id": "model-test"},
                                   {**READER, "camera_id": "qr2"}]})
    upgrade_state(state)
    SettingsPersistenceService._state["lines"] = SettingsPersistenceService._state["lines"] + state["lines"]
    for line_id in ("l9", "l10"):
        # Stop and start (the dashboard, a PLC input, Sparkplug: each saves {"id", "enabled"}), rename, inputs.
        assert SettingsPersistenceService.save_line({"id": line_id, "enabled": False})["enabled"] is False
        assert SettingsPersistenceService.save_line({"id": line_id, "enabled": True})["enabled"] is True
        assert SettingsPersistenceService.save_line({"id": line_id, "name": f"Renamed {line_id}"})["name"] == f"Renamed {line_id}"
        SettingsPersistenceService.save_line({"id": line_id, "plc_inputs": []})
    # Line setup sends the cameras: there the user has to choose a trigger first.
    saved = SettingsPersistenceService.get_line("l9", with_logic=False)
    with pytest.raises(ValueError, match="Camera 2 checks codes"):
        SettingsPersistenceService.save_line({"id": "l9", "cameras": saved["cameras"]})
    saved = SettingsPersistenceService.get_line("l10", with_logic=False)
    with pytest.raises(ValueError, match="Sync needs a camera that reads codes when"):
        SettingsPersistenceService.save_line({"id": "l10", "cameras": saved["cameras"], "sync": {"enabled": True}})
