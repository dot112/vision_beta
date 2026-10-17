"""The result handshake: capture request -> result -> PLC acknowledge, per line.

idle -> waiting_result (the capture input fired, its sequence noted) -> result_ready (the
product's one result went out) -> idle (the ack input went on). A result that does not
come in time is a reject or a fault, never a pass; an ack that does not come raises an
alarm; a capture that skips the ack is a protocol fault.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

import app.services.line_service as line_module
import app.services.production_records_service as records_module
from app.events.alarm_events import ALARM_CATALOG, alarm_manager
from app.security.api_key_scopes import required_api_key_scopes
from app.services.card_triggers import card_fires, normalize_trigger
from app.services.handshake import Handshake
from app.services.line_config import (
    CARD_TRIGGERS,
    REASON_INSPECTION_TIMEOUT,
    REJECT_REASON_CODES,
    REJECT_REASONS,
    normalize_line,
)
from app.services.line_service import LineManager
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.plc_input_service import PLCInputService
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_joined_cameras import _run, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import signals  # noqa: F401  (a fixture)
from tests.test_product_sequence import (  # noqa: F401  (fixtures)
    CAP, CAPTURE_INPUT, GOOD_QR, READER, SEQ, SEQUENCE_INPUT, VISION, _poll, _Reader, carton,
)
from tests.test_production_records import _Recorder
from tests.test_trigger_inspection import workers  # noqa: F401  (a fixture)

ACK = "DB1.ResultAck"
ACK_INPUT = {"id": "in_ack", "name": "ResultAck", "plc_endpoint_id": "ep1", "address": ACK, "action": "none",
             "data_type": "bool"}
HANDSHAKE = {"enabled": True, "capture_input_id": "in_capture", "ack_input_id": "in_ack",
             "result_timeout_ms": 3000, "ack_timeout_ms": 5000, "on_timeout": "reject"}


# ── The state machine on its own ──────────────────────────────────────────────

class _Timers:
    """A scheduler the test fires by hand."""

    def __init__(self):
        self.pending = []

    def __call__(self, delay, fn):
        timer = _Timer(delay, fn, self)
        self.pending.append(timer)
        return timer

    def fire(self, delay=None):
        due = [t for t in self.pending if delay is None or t.delay == delay]
        assert due, "no timer to fire"
        timer = due[0]
        self.pending.remove(timer)
        timer.fn()


class _Timer:
    def __init__(self, delay, fn, owner):
        self.delay, self.fn, self.owner = delay, fn, owner

    def cancel(self):
        if self in self.owner.pending:
            self.owner.pending.remove(self)


def _machine(**settings):
    timers = _Timers()
    seen = {"timeouts": [], "ack_timeouts": []}
    machine = Handshake(schedule=timers,
                        on_result_timeout=lambda request, policy: seen["timeouts"].append((request.id, request.sequence, policy)),
                        on_ack_timeout=lambda request: seen["ack_timeouts"].append(request.id))
    machine.configure({**HANDSHAKE, **settings})
    return machine, timers, seen


def test_one_carton_goes_round_the_handshake():
    machine, timers, _ = _machine()
    assert machine.view()["state"] == "idle"
    answer = machine.capture(7)
    assert answer["action"] == "accepted" and answer["fault"] is None
    view = machine.view()
    assert (view["state"], view["sequence"]) == ("waiting_result", 7)
    assert [t.delay for t in timers.pending] == [3.0]
    assert machine.result(answer["request_id"], "good") is True
    assert (machine.view()["state"], machine.view()["last_result"]) == ("result_ready", "good")
    assert [t.delay for t in timers.pending] == [5.0]                     # the ack timer replaced the result timer
    assert machine.ack() == "done"
    assert machine.view()["state"] == "idle" and timers.pending == []


def test_a_second_capture_with_the_same_sequence_is_a_duplicate():
    machine, timers, _ = _machine()
    first = machine.capture(7)
    assert machine.capture(7)["action"] == "duplicate"                   # still waiting for its result
    machine.result(first["request_id"], "reject")
    assert machine.capture(7)["action"] == "duplicate"                   # still waiting for its ack
    assert machine.view()["duplicates"] == 2 and machine.view()["state"] == "result_ready"
    machine.ack()
    assert machine.capture(7)["action"] == "accepted"                    # out of the handshake: a new request


def test_an_ack_while_idle_or_waiting_is_stale():
    machine, _, _ = _machine()
    assert machine.ack() == "stale"
    request = machine.capture(1)
    assert machine.ack() == "stale"
    view = machine.view()
    assert (view["state"], view["stale_acks"]) == ("waiting_result", 2)
    machine.result(request["request_id"], "good")
    assert machine.ack() == "done" and machine.view()["stale_acks"] == 2


def test_no_result_in_time_rejects():
    machine, timers, seen = _machine()
    request = machine.capture(4)
    timers.fire(3.0)
    assert seen["timeouts"] == [(request["request_id"], 4, "reject")]
    view = machine.view()
    # The line emits the timeout reject (it comes back as this request's result); here it did not.
    assert (view["state"], view["last_result"], view["timeouts"]) == ("result_ready", "reject", 1)
    assert machine.is_closed(request["request_id"])
    assert machine.result(request["request_id"], "good") is False         # a late result changes nothing


def test_no_result_in_time_can_be_a_fault():
    machine, timers, seen = _machine(on_timeout="fault")
    request = machine.capture(4)
    timers.fire(3.0)
    assert seen["timeouts"] == [(request["request_id"], 4, "fault")]
    assert (machine.view()["state"], machine.view()["last_result"]) == ("result_ready", "fault")
    assert machine.ack() == "done"


def test_the_timeout_reject_is_the_requests_one_result():
    machine, timers, _ = _machine()
    request = machine.capture(4)
    machine.on_result_timeout = lambda req, policy: machine.result(req.id, "reject", timeout=True)
    timers.fire(3.0)
    assert (machine.view()["state"], machine.view()["last_result"]) == ("result_ready", "reject")
    assert machine.is_closed(request["request_id"])


def test_no_ack_in_time_says_so_and_keeps_waiting():
    machine, timers, seen = _machine()
    request = machine.capture(2)
    machine.result(request["request_id"], "good")
    timers.fire(5.0)
    assert seen["ack_timeouts"] == [request["request_id"]]
    assert machine.view()["state"] == "result_ready"
    assert machine.ack() == "done"


def test_a_capture_that_skipped_the_ack_is_a_fault_and_goes_ahead():
    machine, timers, _ = _machine()
    first = machine.capture(1)
    machine.result(first["request_id"], "good")
    second = machine.capture(2)
    assert second["action"] == "accepted" and second["fault"] == "skipped_ack"
    assert (machine.view()["state"], machine.view()["sequence"]) == ("waiting_result", 2)
    assert [t.delay for t in timers.pending] == [3.0]
    third = machine.capture(3)
    assert third["fault"] == "no_result"                                   # before the second's result
    assert machine.result(second["request_id"], "good") is False           # no longer the one waited for
    assert machine.result(third["request_id"], "good") is True


def test_a_product_without_a_request_answers_the_waiting_one():
    machine, _, _ = _machine()
    machine.capture(None)
    assert machine.result(None, "good") is True
    assert machine.result(None, "good") is False                          # not waiting any more


def test_settings_that_change_the_inputs_start_again_from_idle():
    machine, timers, _ = _machine()
    machine.capture(1)
    machine.configure({**HANDSHAKE, "result_timeout_ms": 4000})            # same inputs: keeps going
    assert machine.view()["state"] == "waiting_result"
    machine.configure({**HANDSHAKE, "ack_input_id": "in_other"})
    assert machine.view()["state"] == "idle" and timers.pending == []
    machine.configure(None)
    assert machine.enabled is False and machine.capture(1) is None


# ── The settings ──────────────────────────────────────────────────────────────

def test_the_handshake_settings_are_checked():
    inputs = [CAPTURE_INPUT, SEQUENCE_INPUT, ACK_INPUT]
    line = normalize_line({"name": "L", "plc_inputs": inputs, "handshake": {"enabled": True,
                           "capture_input_id": "in_capture", "ack_input_id": "in_ack"}})
    assert line["handshake"] == {**HANDSHAKE}
    assert "handshake" not in normalize_line({"name": "L"})
    for bad, word in (({"capture_input_id": "in_ack"}, "Take a picture"), ({"ack_input_id": "nope"}, "acknowledge"),
                      ({"ack_input_id": "in_capture"}, "acknowledge"), ({"result_timeout_ms": 50}, "result timeout"),
                      ({"ack_timeout_ms": 70000}, "ack timeout"), ({"on_timeout": "pass"}, "reject")):
        with pytest.raises(ValueError, match=word):
            normalize_line({"name": "L", "plc_inputs": inputs, "handshake": {**HANDSHAKE, **bad}})
    # Switched off, it keeps its settings and is not checked against the inputs.
    off = normalize_line({"name": "L", "plc_inputs": [], "handshake": {**HANDSHAKE, "enabled": False}})
    assert off["handshake"]["enabled"] is False and off["handshake"]["result_timeout_ms"] == 3000


def test_the_new_reason_alarms_and_triggers_are_known():
    assert REASON_INSPECTION_TIMEOUT == "inspection_timeout" and REASON_INSPECTION_TIMEOUT in REJECT_REASONS
    assert REJECT_REASON_CODES["inspection_timeout"] == 8
    catalog = {entry["code"]: entry for entry in ALARM_CATALOG}
    for code in ("line.inspection_timeout", "line.ack_timeout", "line.handshake_fault"):
        assert catalog[code]["scope"] == "line"
    assert "handshake_fault" in CARD_TRIGGERS
    fault = {"event_type": "handshake_fault", "line_id": "line-1", "fault": "inspection_timeout"}
    assert card_fires(normalize_trigger({"trigger": "handshake_fault"}), fault)
    assert not card_fires(normalize_trigger({"trigger": "cross_line", "condition": "any"}), fault)
    assert not card_fires(normalize_trigger({"trigger": "handshake_fault"}),
                          {"event_type": "crossing", "line_id": "line-1", "result": "reject"})


# ── On the carton line, with the fake PLC ─────────────────────────────────────

def _card(card_id, trigger, address, operation="set", **extra):
    return {"id": card_id, "name": card_id, "enabled": True, "plc_endpoint_id": "ep1", "trigger": trigger,
            "operation": operation, "target_address": address, "rearm_lockout_ms": 0, **extra}


OUTPUTS = [
    _card("InspectionDone", "cross_line", "DB1.InspectionDone", condition="any"),
    _card("PassRequest", "cross_line", "DB1.PassRequest", condition="good"),
    _card("RejectRequest", "cross_line", "DB1.RejectRequest", condition="reject"),
    _card("FaultSet", "handshake_fault", "DB1.VisionFault"),
    _card("ResetDone", "input", "DB1.InspectionDone", operation="reset", input_id="in_ack", condition="on"),
    _card("ResetPass", "input", "DB1.PassRequest", operation="reset", input_id="in_ack", condition="on"),
    _card("ResetReject", "input", "DB1.RejectRequest", operation="reset", input_id="in_ack", condition="on"),
]


@pytest.fixture
def line_with_handshake(carton, monkeypatch):  # noqa: F811
    """Make the carton line with its output actions; every card that fires is noted as (card, event)."""
    fired = []

    async def note(cls, card, state, event=None):
        fired.append((card["id"], event))

    monkeypatch.setattr(PLCDispatcherService, "_dispatch", classmethod(note))
    recorder = _Recorder()
    monkeypatch.setattr(records_module, "production_recorder", recorder)
    alarm_manager.reset()

    def make(cameras=(VISION, READER), **handshake):
        line = SettingsPersistenceService.save_line({
            "name": "Carton line", "enabled": True, "cameras": list(cameras),
            "plc_inputs": [CAPTURE_INPUT, SEQUENCE_INPUT, ACK_INPUT], "sync": {"window_ms": 300},
            "sequence_input_id": "in_seq", "handshake": {**HANDSHAKE, **handshake}, "plc_actions": OUTPUTS})
        line_module.line_manager.apply_state()
        PLCInputService.reload()
        PLCDispatcherService.set_cards(SettingsPersistenceService.get_line_plc_actions(line["id"]), line_id=line["id"])
        runtime = line_module.line_manager.get(line["id"])
        runtime.fired, runtime.recorder = fired, recorder
        return runtime

    carton["plc"].values[ACK] = False
    yield make
    alarm_manager.reset()


def _products(runtime):
    """(sequence, result, reason) of the line's counted products, from their records."""
    return [((row["details"] or {}).get("sequence"), row["result"], row["reject_reason"])
            for row in runtime.recorder._rows if row["kind"] == "product" and row["counted"]]


def _cards(runtime):
    return [card for card, _ in runtime.fired]


async def _request(env, sequence, code=GOOD_QR, names=("carton",), settle=0.08):
    plc = env["plc"]
    env["vision"].names = list(names)
    env["readers"].setdefault("cam1", _Reader("cam1")).code = code
    plc.values.update({SEQ: sequence, CAP: True})
    await _poll()
    await asyncio.sleep(settle)
    plc.values[CAP] = False
    await _poll()


async def _ack(env, settle=0.02):
    env["plc"].values[ACK] = True
    await _poll()
    await asyncio.sleep(settle)
    env["plc"].values[ACK] = False
    await _poll()


def _alarms(runtime):
    return sorted(alarm.code for alarm in alarm_manager.active() if (alarm.details or {}).get("line_id") == runtime.id)


def test_the_six_cartons_go_round_the_handshake(carton, line_with_handshake):
    runtime = line_with_handshake()
    states = []

    async def steps():
        await _poll()
        for sequence, code, names in ((1, GOOD_QR, ["carton"]), (2, GOOD_QR, ["carton", "damage"]),
                                      (3, None, ["carton"]), (4, GOOD_QR, ["wrong_label"]),
                                      (5, GOOD_QR, ["carton"]), (6, "P-999;B-2026-07", ["carton"])):
            await _request(carton, sequence, code, names, settle=0.4 if code is None else 0.08)
            states.append((runtime.handshake.view()["state"], runtime.handshake.view()["sequence"]))
            await _ack(carton)
            states.append(runtime.handshake.view()["state"])

    _run(steps)
    assert _products(runtime) == [(1, "good", None), (2, "reject", "vision_class"), (3, "reject", "no_code"),
                                  (4, "reject", "vision_class"), (5, "good", None), (6, "reject", "code_not_in_list")]
    assert [s for s in states if isinstance(s, tuple)] == [("result_ready", n) for n in range(1, 7)]
    assert [s for s in states if not isinstance(s, tuple)] == ["idle"] * 6
    counter = runtime.counter
    assert (counter.total_inspected, counter.good_count, counter.rejected_count) == (6, 2, 4)
    # Exactly one of pass / reject per product; every product sets InspectionDone; each ack resets the three.
    by_event = {}
    for card, event in runtime.fired:
        by_event.setdefault(event.get("event_id"), []).append(card)
    products = [cards for cards in by_event.values() if "InspectionDone" in cards]
    assert len(products) == 6
    assert all(("PassRequest" in cards) != ("RejectRequest" in cards) for cards in products)
    assert _cards(runtime).count("PassRequest") == 2 and _cards(runtime).count("RejectRequest") == 4
    assert _cards(runtime).count("ResetDone") == 6
    view = runtime.handshake.view()
    assert (view["stale_acks"], view["timeouts"], view["duplicates"], view["last_error"]) == (0, 0, 0, None)
    assert _alarms(runtime) == []


def test_a_duplicate_capture_takes_no_picture(carton, line_with_handshake):
    runtime = line_with_handshake()

    async def steps():
        await _poll()
        await _request(carton, 7)
        await _request(carton, 7)                     # the PLC asks again before it acknowledged
        await _ack(carton)

    _run(steps)
    assert len(carton["readers"]["cam1"].requests) == 1
    assert _products(runtime) == [(7, "good", None)]
    assert runtime.handshake.view()["duplicates"] == 1 and runtime.handshake.view()["state"] == "idle"


def test_a_stale_ack_is_ignored_and_counted(carton, line_with_handshake):
    runtime = line_with_handshake()

    async def steps():
        await _poll()
        await _ack(carton)                            # nothing to acknowledge
        await _request(carton, 1)
        await _ack(carton)

    _run(steps)
    view = runtime.handshake.view()
    assert (view["state"], view["stale_acks"]) == ("idle", 1)
    assert _products(runtime) == [(1, "good", None)]
    # The ack input's own cards still fire: resetting flags that are off does no harm.
    assert _cards(runtime).count("ResetPass") == 2


def test_no_result_in_time_is_one_reject_counted_once(carton, line_with_handshake):
    runtime = line_with_handshake(result_timeout_ms=150)

    async def steps():
        await _poll()
        await _request(carton, 4, names=[], settle=0.5)           # Camera 2 finds nothing: no product
        assert runtime.handshake.view()["state"] == "result_ready"
        await _ack(carton)

    _run(steps)
    assert _products(runtime) == [(4, "reject", REASON_INSPECTION_TIMEOUT)]
    counter = runtime.counter
    assert (counter.total_inspected, counter.good_count, counter.rejected_count) == (1, 0, 1)
    assert _cards(runtime).count("RejectRequest") == 1 and "PassRequest" not in _cards(runtime)
    event = next(event for card, event in runtime.fired if card == "RejectRequest")
    assert (event["reject_reason"], event["sequence"]) == (REASON_INSPECTION_TIMEOUT, 4)
    assert runtime.handshake.view()["timeouts"] == 1


def test_a_late_result_is_not_counted_again(carton, line_with_handshake):
    runtime = line_with_handshake(result_timeout_ms=120)
    carton["readers"]["cam1"] = reader = _Reader("cam1")
    reader.delay = 0.6                                            # the code comes after the timeout and the Sync window

    async def steps():
        await _poll()
        await _request(carton, 5, settle=0.9)

    _run(steps)
    assert _products(runtime) == [(5, "reject", REASON_INSPECTION_TIMEOUT)]
    assert runtime.counter.total_inspected == 1
    assert runtime.handshake.view()["late"] == 1
    assert "PassRequest" not in _cards(runtime)


def test_no_result_in_time_can_be_a_fault(carton, line_with_handshake):
    runtime = line_with_handshake(result_timeout_ms=150, on_timeout="fault")

    async def steps():
        await _poll()
        await _request(carton, 4, names=[], settle=0.4)
        assert "line.inspection_timeout" in _alarms(runtime)
        await _ack(carton)

    _run(steps)
    assert _products(runtime) == [] and runtime.counter.total_inspected == 0
    assert "FaultSet" in _cards(runtime)
    assert not {"PassRequest", "RejectRequest"} & set(_cards(runtime))
    fault = next(event for card, event in runtime.fired if card == "FaultSet")
    assert (fault["fault"], fault["sequence"]) == ("inspection_timeout", 4)
    assert runtime.handshake.view()["state"] == "idle"


def test_no_ack_in_time_raises_an_alarm_until_it_comes(carton, line_with_handshake):
    runtime = line_with_handshake(ack_timeout_ms=150)

    async def steps():
        await _poll()
        await _request(carton, 1, settle=0.3)
        assert "line.ack_timeout" in _alarms(runtime)
        await _ack(carton)
        assert "line.ack_timeout" not in _alarms(runtime)

    _run(steps)
    assert runtime.handshake.view()["state"] == "idle"


def test_a_capture_that_skipped_the_ack_is_a_protocol_fault(carton, line_with_handshake):
    runtime = line_with_handshake()

    async def steps():
        await _poll()
        await _request(carton, 1)
        await _request(carton, 2)                     # no ack in between
        assert "line.handshake_fault" in _alarms(runtime)
        await _ack(carton)
        assert "line.handshake_fault" not in _alarms(runtime)

    _run(steps)
    assert _products(runtime) == [(1, "good", None), (2, "good", None)]
    fault = next(event for card, event in runtime.fired if card == "FaultSet")
    assert fault["fault"] == "skipped_ack"


def test_a_plc_that_drops_out_still_gets_the_timeout(carton, line_with_handshake):
    runtime = line_with_handshake(result_timeout_ms=150)

    async def steps():
        await _poll()
        carton["vision"].names = []
        carton["plc"].values.update({SEQ: 9, CAP: True})
        await _poll()
        carton["plc"].reachable = False               # the link goes down while the line waits
        await _poll()
        await asyncio.sleep(0.4)

    _run(steps)
    assert _products(runtime) == [(9, "reject", REASON_INSPECTION_TIMEOUT)]


def test_after_a_restart_the_handshake_starts_idle(carton, line_with_handshake, monkeypatch):
    runtime = line_with_handshake()

    async def steps():
        await _poll()
        await _request(carton, 1)

    _run(steps)
    assert runtime.handshake.view()["state"] == "result_ready"
    restarted = LineManager()
    monkeypatch.setattr(line_module, "line_manager", restarted)
    restarted.apply_state()
    assert restarted.get(runtime.id).handshake.view()["state"] == "idle"


def test_a_plc_that_drops_its_request_as_the_ack(carton, line_with_handshake, monkeypatch):
    """The ack input's own edge decides: an "On to off" input on the capture request's own address
    acknowledges when the PLC drops the request."""
    import tests.test_handshake as this

    dropped = {**ACK_INPUT, "address": CAP, "edge": "falling"}
    monkeypatch.setattr(this, "ACK_INPUT", dropped)
    runtime = line_with_handshake()
    states = []

    async def steps():
        await _poll()
        carton["plc"].values.update({SEQ: 1, CAP: True})
        await _poll()
        await asyncio.sleep(0.08)
        states.append(runtime.handshake.view()["state"])
        carton["plc"].values[CAP] = False                  # the PLC drops its request: the acknowledge
        await _poll()
        states.append(runtime.handshake.view()["state"])

    _run(steps)
    assert states == ["result_ready", "idle"]
    assert runtime.handshake.view()["stale_acks"] == 0


def test_a_line_without_a_handshake_does_what_it_did(carton, line_with_handshake):
    runtime = line_with_handshake(enabled=False)

    async def steps():
        await _poll()
        await _request(carton, 1)
        await _request(carton, 1)                     # no handshake: the same sequence is a new carton
        await _ack(carton)

    _run(steps)
    assert _products(runtime) == [(1, "good", None), (1, "good", None)]
    assert runtime.handshake.view()["state"] == "idle" and runtime.handshake.view()["enabled"] is False


# ── The view ──────────────────────────────────────────────────────────────────

def test_the_handshake_endpoint(client, admin_headers):
    inputs = [{**CAPTURE_INPUT, "plc_endpoint_id": ""}, {**ACK_INPUT, "plc_endpoint_id": ""}]
    created = client.post("/api/v1/lines", headers=admin_headers, json={
        "name": f"Handshake {uuid.uuid4().hex[:6]}", "plc_inputs": inputs,
        "handshake": {"enabled": True, "capture_input_id": "in_capture", "ack_input_id": "in_ack"}})
    assert created.status_code == 201, created.text
    line_id = created.json()["id"]
    try:
        assert created.json()["handshake"]["result_timeout_ms"] == 3000
        view = client.get(f"/api/v1/lines/{line_id}/handshake", headers=admin_headers)
        assert view.status_code == 200, view.text
        body = view.json()
        assert (body["enabled"], body["state"], body["sequence"], body["stale_acks"]) == (True, "idle", None, 0)
        assert {"since", "last_result", "timeouts", "duplicates", "last_error", "settings"} <= set(body)
        assert client.get("/api/v1/lines/nope/handshake", headers=admin_headers).status_code == 404
        assert required_api_key_scopes("GET", f"/api/v1/lines/{line_id}/handshake") == {"monitor:read"}
    finally:
        client.delete(f"/api/v1/lines/{line_id}", headers=admin_headers)
