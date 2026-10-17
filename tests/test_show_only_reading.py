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
