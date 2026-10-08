"""What a camera that reads codes does with each code: report it, accept only listed codes, or reject listed codes.

A product gets one result, decided once: the vision camera's and its code's together.
"""
from __future__ import annotations

import asyncio
import copy

import pytest

import app.services.line_service as line_module
import app.services.settings_persistence_service as persistence_module
from app.services.counting_service import CountingService, counting_service
from app.services.line_config import (
    PRIMARY_LINE_ID,
    REASON_LISTED,
    REASON_NO_CODE,
    REASON_NOT_LISTED,
    REASON_VISION,
    SCHEMA_VERSION,
    code_checks,
    line_warnings,
    normalize_line,
    product_verdict,
    upgrade_state,
)
from app.services.line_service import LineManager
from app.services.plc_dispatcher_service import PLCDispatcherService, _eval_condition, _normalize_card
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

    def __init__(self, is_defect=False, class_name="bottle"):
        self.is_defect, self.class_name = is_defect, class_name
        self.objects, self._recently_counted, self.next_id = {}, set(), 1

    def update(self, **kwargs):
        self.next_id += 1
        return [(self.next_id, self.class_name, self.is_defect, 0.9)]


def _cross(counter, camera_id="vis", is_defect=False):
    counter._trackers[camera_id] = _CrossingTracker(is_defect)
    counter.process_frame([], 640, 480, camera_rotation=90, camera_id=camera_id)


def _reader(action="report", no_read="ignore", **extra):
    return {"camera_id": "qr", "role": "qr", "product_list_id": LIST, "qr_action": action, "qr_no_read": no_read, **extra}


def _line(manager, cameras, name="Checked"):
    # A vision camera is not saved without a model.
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


def _totals(counter):
    return counter.total_inspected, counter.good_count, counter.rejected_count


# ── The rule ──────────────────────────────────────────────────────────────────

CHECK_REJECT_NO_CODE = {"qr": {"action": "accept_listed", "no_read": "reject"}}
CHECK_IGNORE_NO_CODE = {"qr": {"action": "accept_listed", "no_read": "ignore"}}
LISTED_CODE = {"camera_id": "qr", "known": True}
OTHER_CODE = {"camera_id": "qr", "known": False}


@pytest.mark.parametrize("vision_reject, read, checks, expected", [
    # vision good, code check passed -> good
    (False, LISTED_CODE, CHECK_REJECT_NO_CODE, (False, None)),
    # vision good, code check failed -> reject
    (False, OTHER_CODE, CHECK_REJECT_NO_CODE, (True, REASON_NOT_LISTED)),
    # vision reject, any code -> reject
    (True, LISTED_CODE, CHECK_REJECT_NO_CODE, (True, REASON_VISION)),
    (True, OTHER_CODE, CHECK_REJECT_NO_CODE, (True, REASON_VISION)),
    # no code, "Ignore" -> as the vision camera says
    (False, None, CHECK_IGNORE_NO_CODE, (False, None)),
    (True, None, CHECK_IGNORE_NO_CODE, (True, REASON_VISION)),
    # no code, "Reject" -> reject
    (False, None, CHECK_REJECT_NO_CODE, (True, REASON_NO_CODE)),
    (True, None, CHECK_REJECT_NO_CODE, (True, REASON_VISION)),
    # "Reject listed codes" turns the check round
    (False, LISTED_CODE, {"qr": {"action": "reject_listed", "no_read": "ignore"}}, (True, REASON_LISTED)),
    (False, OTHER_CODE, {"qr": {"action": "reject_listed", "no_read": "ignore"}}, (False, None)),
    # "Report only": the code never changes the result
    (False, OTHER_CODE, {}, (False, None)),
    (False, None, {}, (False, None)),
    (True, LISTED_CODE, {}, (True, REASON_VISION)),
])
def test_one_result_per_product(vision_reject, read, checks, expected):
    assert product_verdict(vision_reject, read, checks) == expected


# ── Settings ──────────────────────────────────────────────────────────────────

def test_existing_readers_load_as_report_only():
    line = normalize_line({"name": "A", "cameras": [{"camera_id": "v"}, {"camera_id": "q", "role": "qr"}]})
    vision, reader = line["cameras"]
    assert (reader["qr_action"], reader["qr_no_read"], reader["product_list_id"]) == ("report", "ignore", None)
    assert "qr_action" not in vision and "qr_no_read" not in vision
    assert code_checks(line["cameras"]) == {} and line["sync"]["enabled"] is False


def test_reader_action_validation():
    cams = lambda **reader: [{"camera_id": "v"}, {"camera_id": "q", "role": "qr", **reader}]  # noqa: E731
    with pytest.raises(ValueError, match="action must be"):
        normalize_line({"name": "A", "cameras": cams(qr_action="save")})
    with pytest.raises(ValueError, match="choose the product list"):
        normalize_line({"name": "A", "cameras": cams(qr_action="accept_listed")})
    with pytest.raises(ValueError, match="'reject' or 'ignore'"):
        normalize_line({"name": "A", "cameras": cams(qr_no_read="maybe")})

    # A check beside a vision camera needs Sync: it is switched on.
    checked = normalize_line({"name": "A", "sync": {"enabled": False},
                              "cameras": cams(qr_action="reject_listed", product_list_id=LIST, qr_no_read="reject")})
    assert checked["sync"]["enabled"] is True
    assert code_checks(checked["cameras"]) == {"q": {"action": "reject_listed", "no_read": "reject"}}

    # A vision camera that reads codes itself has the same choices.
    one = normalize_line({"name": "A", "cameras": [{"camera_id": "v", "read_codes": True, "qr_action": "accept_listed", "product_list_id": LIST}]})
    assert one["sync"]["enabled"] is True and code_checks(one["cameras"])["v"]["action"] == "accept_listed"

    # Without a vision camera there is no "no code was read", and no Sync.
    alone = normalize_line({"name": "A", "cameras": [{"camera_id": "q", "role": "qr", "qr_action": "accept_listed",
                                                      "product_list_id": LIST, "qr_no_read": "reject"}]})
    assert alone["cameras"][0]["qr_no_read"] == "ignore" and alone["sync"]["enabled"] is False


def test_an_upgraded_settings_file_behaves_as_before(lines):
    state = {"schema_version": 2, "camera_auto_connect": True, "lines": [
        {"id": PRIMARY_LINE_ID, "name": "Line 1", "enabled": True, "auto_connect": True,
         "sync": {"enabled": True, "window_ms": WINDOW_MS}, "cameras": [
             {"camera_id": "vis", "role": "vision", "counting": True, "qr_hold_ms": 1500},
             {"camera_id": "qr", "role": "qr", "counting": False, "qr_hold_ms": 1500, "qr_trigger": "continuous",
              "qr_trigger_delay_ms": 0, "code_type": "all"},
         ]},
    ]}
    assert upgrade_state(state) and state["schema_version"] == SCHEMA_VERSION
    SettingsPersistenceService._state["lines"] = state["lines"]
    lines.apply_state()
    runtime = lines.get(PRIMARY_LINE_ID)
    assert runtime.code_checks == {} and runtime.sync_enabled
    assert runtime.product_list_id("qr") == LIST  # the one list there was
    sent = []
    runtime.counter.dispatch_event = lambda payload, plc_event, is_defect: sent.append((payload, plc_event))

    async def steps():
        runtime.on_qr_read("qr", "NOT-LISTED", "QR_CODE")
        await asyncio.sleep(0.01)
        _cross(runtime.counter)
        await asyncio.sleep(0.05)

    try:
        _run(steps)
    finally:
        del runtime.counter.dispatch_event
    # An unknown code is reported, and the product is still good: nothing is rejected.
    (payload, plc_event), = sent
    assert (payload["qr_status"], payload["result"], payload["reject_reason"]) == ("unknown", "PASSED", None)
    assert plc_event["result"] == "good" and plc_event["delay_from_crossing"] is False
    assert _totals(runtime.counter) == (1, 1, 0)
    assert runtime.recent_reads()[0]["result"] is None


# ── A vision camera and a reader ──────────────────────────────────────────────

def test_accept_only_listed_codes(lines):
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("accept_listed", no_read="reject")])
    counter = runtime.counter
    assert runtime.sync_enabled and runtime.code_checks == {"qr": {"action": "accept_listed", "no_read": "reject"}}
    seen = []

    async def steps():
        # 1. Good product, listed code -> good.
        runtime.on_qr_read("qr", "LISTED", "QR_CODE")
        await asyncio.sleep(0.01)
        _cross(counter)
        await asyncio.sleep(0.01)
        seen.append(_totals(counter))
        # 2. Good product, code not in the list -> reject.
        runtime.on_qr_read("qr", "OTHER", "QR_CODE")
        await asyncio.sleep(0.01)
        _cross(counter)
        await asyncio.sleep(0.01)
        seen.append(_totals(counter))
        # 3. Defect, listed code -> reject (the vision camera's reason).
        runtime.on_qr_read("qr", "LISTED", "QR_CODE")
        await asyncio.sleep(0.01)
        _cross(counter, is_defect=True)
        await asyncio.sleep(0.01)
        seen.append(_totals(counter))
        # 4. Good product, no code. It is not counted while the line waits for the code...
        _cross(counter)
        await asyncio.sleep(0.02)
        seen.append(_totals(counter))
        # ...and is rejected when the Sync window ends.
        await asyncio.sleep(WINDOW_MS / 1000 + 0.1)
        seen.append(_totals(counter))

    _run(steps)
    assert seen == [(1, 1, 0), (2, 1, 1), (3, 1, 2), (3, 1, 2), (4, 1, 3)]
    results = [(p["result"], p["reject_reason"], p["vision_result"], p["qr_status"]) for p, _ in runtime.sent]
    assert results == [
        ("PASSED", None, "PASSED", "known"),
        ("REJECTED", REASON_NOT_LISTED, "PASSED", "unknown"),
        ("REJECTED", REASON_VISION, "REJECTED", "known"),
        ("REJECTED", REASON_NO_CODE, "PASSED", "no_read"),
    ]
    # One event per product; the message's totals are the totals after that product.
    assert [p["rejected_count"] for p, _ in runtime.sent] == [0, 1, 2, 3]
    assert [e["result"] for _, e in runtime.sent] == ["good", "reject", "reject", "reject"]
    assert all(e["delay_from_crossing"] for _, e in runtime.sent)
    # A reject by code fires the same PLC cards as a reject by the vision camera.
    reject_card = _normalize_card({"trigger": "cross_line", "condition": "reject"})
    good_card = _normalize_card({"trigger": "cross_line", "condition": "good"})
    assert [_eval_condition(reject_card, e) for _, e in runtime.sent] == [False, True, True, True]
    assert [_eval_condition(good_card, e) for _, e in runtime.sent] == [True, False, False, False]
    recent = [(r["result"], r["reject_reason"]) for r in reversed(runtime.recent_reads())]
    assert recent == [("good", None), ("reject", REASON_NOT_LISTED), ("reject", REASON_VISION), ("reject", REASON_NO_CODE)]
    assert runtime.qr_stats["code_rejects"] == 2 and counter.yield_percentage == 25.0


def test_no_code_can_be_ignored(lines):
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("accept_listed", no_read="ignore")])

    async def steps():
        _cross(runtime.counter)                    # good, no code
        _cross(runtime.counter, is_defect=True)    # defect, no code
        await asyncio.sleep(WINDOW_MS / 1000 + 0.1)

    _run(steps)
    assert [(p["result"], p["reject_reason"]) for p, _ in runtime.sent] == [("PASSED", None), ("REJECTED", REASON_VISION)]
    assert _totals(runtime.counter) == (2, 1, 1)


def test_reject_listed_codes(lines):
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("reject_listed")])

    async def steps():
        for code in ("LISTED", "OTHER"):
            runtime.on_qr_read("qr", code, "QR_CODE")
            await asyncio.sleep(0.01)
            _cross(runtime.counter)
            await asyncio.sleep(0.01)

    _run(steps)
    assert [(p["result"], p["reject_reason"]) for p, _ in runtime.sent] == [("REJECTED", REASON_LISTED), ("PASSED", None)]
    assert _totals(runtime.counter) == (2, 1, 1)


def test_one_camera_that_inspects_and_checks_codes(lines):
    runtime = _line(lines, [{"camera_id": "vis", "read_codes": True, "product_list_id": LIST, "qr_action": "accept_listed"}])
    assert runtime.sync_enabled and "vis" in runtime.code_checks

    async def steps():
        runtime.on_qr_read("vis", "OTHER", "EAN13")
        await asyncio.sleep(0.01)
        _cross(runtime.counter)
        await asyncio.sleep(0.01)

    _run(steps)
    (payload, _), = runtime.sent
    assert (payload["result"], payload["reject_reason"], payload["qr_code"]) == ("REJECTED", REASON_NOT_LISTED, "OTHER")


def test_the_listed_code_of_a_picture_decides(lines):
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("accept_listed", qr_trigger="line1")])
    codes = [{"code": "BATCH-77", "format": "CODE128", "known": False}, {"code": "LISTED", "format": "EAN13", "known": True}]
    assert [c["code"] for c in runtime._deciding_first("qr", codes)] == ["LISTED", "BATCH-77"]
    # A camera that only reports keeps the order the codes were found in.
    plain = _line(lines, [{"camera_id": "vis2"}, {"camera_id": "qr2", "role": "qr", "product_list_id": LIST}], name="Plain")
    assert [c["code"] for c in plain._deciding_first("qr2", codes)] == ["BATCH-77", "LISTED"]


# ── A reader alone ────────────────────────────────────────────────────────────

def test_on_a_line_with_only_a_reader_each_code_is_one_product(lines, monkeypatch):
    runtime = _line(lines, [_reader("accept_listed")])
    counter = runtime.counter
    assert not runtime.sync_enabled and counter.event_sink is None
    events, results = [], []
    monkeypatch.setattr(PLCDispatcherService, "evaluate", classmethod(lambda cls, event: _record(events, event)))
    # One message per product goes to the line's send cards, with the same event the PLC cards get.
    monkeypatch.setattr(counter, "send", lambda event, payload: results.append(payload) or 0)

    async def steps():
        for code in ("LISTED", "OTHER", "LISTED"):
            runtime.on_qr_read("qr", code, "QR_CODE")
        await asyncio.sleep(0.05)

    _run(steps)
    assert _totals(counter) == (3, 2, 1) and counter.yield_percentage == pytest.approx(66.67)
    assert counter.counts_by_class == {"widget": 2, "code not in list": 1}
    assert [(p["result"], p["reject_reason"], p["code"], p["total_inspected"]) for p in results] == [
        ("PASSED", None, "LISTED", 1), ("REJECTED", REASON_NOT_LISTED, "OTHER", 2), ("PASSED", None, "LISTED", 3),
    ]
    # One PLC event per product: it fires the reject card like a vision reject, and the code cards once.
    assert len(events) == 3
    reject_card = _normalize_card({"trigger": "cross_line", "condition": "reject"})
    unknown_card = _normalize_card({"trigger": "qr_unknown"})
    counter_card = _normalize_card({"trigger": "reject_counter", "condition": "counter_reach", "set_value": "1"})
    assert [_eval_condition(reject_card, e) for e in events] == [False, True, False]
    assert [_eval_condition(unknown_card, e) for e in events] == [False, True, False]
    assert [_eval_condition(counter_card, e) for e in events] == [False, True, True]
    assert [(r["result"], r["reject_reason"]) for r in reversed(runtime.recent_reads())] == [
        ("good", None), ("reject", REASON_NOT_LISTED), ("good", None),
    ]


async def _record(events, event):
    events.append(event)


def test_a_reader_alone_that_only_reports_counts_nothing(lines, monkeypatch):
    runtime = _line(lines, [_reader("report")])
    sent = []
    monkeypatch.setattr(runtime.counter, "send", lambda event, payload: sent.append(payload))

    async def steps():
        runtime.on_qr_read("qr", "OTHER", "QR_CODE")
        await asyncio.sleep(0.03)

    _run(steps)
    assert _totals(runtime.counter) == (0, 0, 0)
    assert sent[0]["event"] == "QR_CODE_READ" and "result" not in sent[0]
    assert runtime.recent_reads()[0]["result"] is None


# ── Timing and warnings ───────────────────────────────────────────────────────

def test_travel_delay_runs_from_the_crossing_when_the_result_waited_for_a_code(monkeypatch):
    import time as _time

    from app.services import plc_dispatcher_service as dispatcher
    from app.services.plc_dispatcher_service import _CardState

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(dispatcher.asyncio, "sleep", fake_sleep)
    card = {"id": "gate", "name": "Reject gate", "travel_delay_ms": 400}  # no endpoint: stops after the delay

    def dispatch(event):
        slept.clear()
        asyncio.run(PLCDispatcherService._dispatch(card, _CardState(card_id="gate"), event))
        return slept[0] if slept else 0.0

    crossed = _time.monotonic() - 0.25  # the result came 250 ms after the crossing
    assert dispatch({"crossed_at": crossed, "delay_from_crossing": True}) == pytest.approx(0.15, abs=0.03)
    # A line that only reports codes keeps the full delay from the moment of the event, as before.
    assert dispatch({"crossed_at": crossed, "delay_from_crossing": False}) == 0.4
    assert dispatch({}) == 0.4
    # A result that came after the whole travel delay fires at once (and is logged as late).
    assert dispatch({"crossed_at": _time.monotonic() - 0.6, "delay_from_crossing": True}) == 0.0


def test_warnings_for_a_short_travel_delay_and_for_a_reader_alone():
    gate = {"id": "g", "name": "Reject gate", "trigger": "cross_line", "condition": "reject", "delay_ms": 300}
    lamp = {"id": "l", "name": "Good lamp", "trigger": "cross_line", "condition": "good", "delay_ms": 0}
    checked = normalize_line({"name": "A", "sync": {"window_ms": 500}, "cameras": [
        {"camera_id": "v"}, {"camera_id": "q", "role": "qr", "qr_action": "accept_listed", "product_list_id": LIST}]})
    warnings = line_warnings({**checked, "plc_actions": [gate, lamp]})
    assert len(warnings) == 1 and "'Reject gate'" in warnings[0] and "300 ms" in warnings[0] and "500 ms" in warnings[0]
    assert line_warnings({**checked, "plc_actions": [{**gate, "delay_ms": 800}, lamp]}) == []
    assert line_warnings({**checked, "plc_actions": [{**gate, "enabled": False}]}) == []

    plain = normalize_line({"name": "A", "cameras": [{"camera_id": "v"}, {"camera_id": "q", "role": "qr"}]})
    assert line_warnings({**plain, "plc_actions": [gate]}) == []  # codes are only reported: nothing waits

    alone = normalize_line({"name": "A", "cameras": [{"camera_id": "q", "role": "qr", "qr_action": "reject_listed",
                                                      "product_list_id": LIST, "qr_hold_ms": 2000}]})
    (warning,) = line_warnings(alone)
    assert "no vision camera" in warning and "2000 ms" in warning


# ── API ───────────────────────────────────────────────────────────────────────

def test_line_api_saves_the_reader_action(client, admin_headers, vision_model):
    from app.services.product_service import product_catalog

    made = client.post("/api/v1/products/lists", json={"name": "Reader action list"}, headers=admin_headers)
    assert made.status_code == 201, made.text
    list_id = made.json()["id"]
    line = client.post("/api/v1/lines", json={"name": "Action line"}, headers=admin_headers).json()
    try:
        saved = client.put(f"/api/v1/lines/{line['id']}", json={
            "sync": {"enabled": False, "window_ms": 400},
            "cameras": [
                {"camera_id": "act-vis", "model_id": vision_model},
                {"camera_id": "act-qr", "role": "qr", "qr_action": "accept_listed", "product_list_id": list_id, "qr_no_read": "reject"},
            ],
            "plc_actions": [{"id": "act-gate", "name": "Gate", "trigger": "cross_line", "condition": "reject", "delay_ms": 100}],
        }, headers=admin_headers)
        assert saved.status_code == 200, saved.text
        body = saved.json()
        reader = body["cameras"][1]
        assert (reader["qr_action"], reader["product_list_id"], reader["qr_no_read"]) == ("accept_listed", list_id, "reject")
        assert body["sync"] == {"enabled": True, "window_ms": 400}
        assert "Sync was switched on" in body["warning"]
        assert len(body["warnings"]) == 1 and "'Gate'" in body["warnings"][0]
        status = body["status"]["cameras"][1]
        assert (status["qr_action"], status["qr_no_read"], status["product_list_id"]) == ("accept_listed", "reject", list_id)

        no_list = client.put(f"/api/v1/lines/{line['id']}", json={
            "cameras": [{"camera_id": "act-qr", "role": "qr", "qr_action": "reject_listed", "product_list_id": None}],
        }, headers=admin_headers)
        assert no_list.status_code == 422 and "product list" in no_list.json()["detail"]
    finally:
        client.delete(f"/api/v1/lines/{line['id']}", headers=admin_headers)
        client.delete(f"/api/v1/products/lists/{list_id}", headers=admin_headers)
    assert not product_catalog.has_list(list_id)
