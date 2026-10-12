"""PLC signals (inputs): a PLC can send signals into the software, per line.

A signal card names a PLC channel and an address, how often to read it, and
what the line does when the value changes: start or stop, count a product,
take a picture, reset the counters, set the batch, confirm a PLC action or
raise an alarm.
"""
from __future__ import annotations

import asyncio

import pytest

from app.events.alarm_events import ALARM_CATALOG, AlarmCode, AlarmSeverity, alarm_line, alarm_manager
from app.hardware.plc import factory as plc_factory
from app.services import plc_input_service
from app.services.plc_dispatcher_service import PLCDispatcherService, _CardState, _normalize_card
from app.services.plc_input_service import (
    COUNTER_MAX_STEP,
    PLCActor,
    PLCInputService,
    SignalFilter,
    counter_delta,
    edge_matches,
    truthy,
)
from app.services.line_config import (
    MAX_PLC_INPUTS,
    PLC_INPUT_ACTIONS,
    PRIMARY_LINE_ID,
    SCHEMA_VERSION,
    endpoints_used_by_line,
    line_warnings,
    normalize_plc_input,
    normalize_plc_inputs,
    product_sensors,
)
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_joined_cameras import LIST, _run, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import FakeDriver, plc  # noqa: F401  (a fixture)

API = "/api/v1/lines"
FAILED = AlarmCode.PLC_INPUT_FAILED


def _signal(action="start_line", **extra):
    return {"id": f"plcin_{action}", "name": action.replace("_", " "), "plc_endpoint_id": "ep1", "address": "10001",
            "action": action, **extra}


def _reader(camera_id="qr", **extra):
    return {"camera_id": camera_id, "role": "qr", "product_list_id": LIST, **extra}


def _vision(camera_id="vis", **extra):
    return {"camera_id": camera_id, "role": "vision", "model_id": "model-test", **extra}


def _save(**line):
    return SettingsPersistenceService.save_line({"enabled": True, **line})


# ── One card ──────────────────────────────────────────────────────────────────

def test_a_signal_card_gets_its_defaults():
    card = normalize_plc_input({"name": " Start ", "plc_endpoint_id": "ep1", "address": "10001", "action": "start_line"})
    assert card["id"].startswith("plcin_") and card["name"] == "Start"
    assert (card["enabled"], card["data_type"], card["poll_ms"], card["edge"], card["debounce_ms"]) == (True, "auto", 100, "rising", 0)
    assert "camera_id" not in card and "alarm_text" not in card and "card_id" not in card


def test_a_signal_card_keeps_what_was_set():
    card = normalize_plc_input({"id": "plcin_a", "name": "Sensor", "enabled": False, "plc_endpoint_id": "ep1",
                                "address": " 40011 ", "data_type": "COUNTER", "poll_ms": "250", "edge": "falling",
                                "debounce_ms": 30, "action": "product_sensor", "card_id": "x", "camera_id": "cam"})
    assert card == {"id": "plcin_a", "name": "Sensor", "enabled": False, "plc_endpoint_id": "ep1", "address": "40011",
                    "data_type": "counter", "poll_ms": 250, "edge": "falling", "debounce_ms": 30, "action": "product_sensor"}


def test_an_alarm_card_has_a_text_and_a_severity():
    card = normalize_plc_input(_signal("alarm", name="Guard door"))
    assert (card["alarm_text"], card["alarm_severity"]) == ("Guard door", "warning")
    card = normalize_plc_input(_signal("alarm", alarm_text=" Guard door is open ", alarm_severity="CRITICAL"))
    assert (card["alarm_text"], card["alarm_severity"]) == ("Guard door is open", "critical")


def test_every_action_is_accepted():
    for action in PLC_INPUT_ACTIONS:
        assert normalize_plc_input(_signal(action, camera_id="qr"))["action"] == action


@pytest.mark.parametrize("bad, word", [
    ({"action": "explode"}, "action"),
    ({}, "action"),
    ({"action": "start_line", "poll_ms": 10}, "between 50 and 10000"),
    ({"action": "start_line", "debounce_ms": 6000}, "between 0 and 5000"),
    ({"action": "start_line", "edge": "sideways"}, "edge"),
    ({"action": "start_line", "data_type": "word64"}, "data type"),
    ({"action": "start_line", "data_type": "counter"}, "product sensor"),
    ({"action": "start_line", "data_type": "string"}, "batch"),
    ({"action": "alarm", "alarm_severity": "loud"}, "severity"),
    ({"action": "alarm", "alarm_text": "x" * 201}, "200"),
    ({"action": "capture"}, "camera"),
    ({"action": "start_line", "address": "x" * 129}, "address"),
    ({"action": "start_line", "id": "no spaces"}, "id"),
    ({"action": "start_line", "enabled": "yes"}, "true or false"),
])
def test_signal_card_validation(bad, word):
    with pytest.raises(ValueError, match=word):
        normalize_plc_input({"address": "C1", "plc_endpoint_id": "ep1", **bad})


def test_two_signal_cards_cannot_share_an_id():
    with pytest.raises(ValueError, match="same id"):
        normalize_plc_inputs([_signal(), _signal()])


def test_at_most_32_signal_cards():
    assert MAX_PLC_INPUTS == 32
    cards = [_signal(id=f"plcin_{n}") for n in range(33)]
    assert len(normalize_plc_inputs(cards[:32])) == 32
    with pytest.raises(ValueError, match="at most 32"):
        normalize_plc_inputs(cards)
    with pytest.raises(ValueError, match="list"):
        normalize_plc_inputs({"id": "plcin_1"})


# ── Cards on a line ───────────────────────────────────────────────────────────

def test_a_line_keeps_its_signal_cards_when_a_client_leaves_them_out(lines):  # noqa: F811
    line = _save(name="Filling", plc_inputs=[_signal()])
    assert [card["id"] for card in line["plc_inputs"]] == ["plcin_start_line"]
    # A start or stop, or a client written before signals, sends the line without them.
    stopped = SettingsPersistenceService.save_line({"id": line["id"], "enabled": False})
    assert stopped["enabled"] is False and stopped["plc_inputs"] == line["plc_inputs"]
    # An empty list is a client that deleted them.
    assert SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": []})["plc_inputs"] == []


def test_line_1_keeps_its_signal_cards_in_its_own_entry(lines):  # noqa: F811
    saved = SettingsPersistenceService.save_line({"id": PRIMARY_LINE_ID, "plc_inputs": [_signal("reset_counters")]})
    state = SettingsPersistenceService._state
    assert state["lines"][0]["id"] == PRIMARY_LINE_ID and state["lines"][0]["plc_inputs"] == saved["plc_inputs"]
    assert "plc_inputs" not in state          # only the version 1 keys are at the top level
    renamed = SettingsPersistenceService.save_line({"id": PRIMARY_LINE_ID, "name": "Line one"})
    assert renamed["plc_inputs"] == saved["plc_inputs"]


def test_version_8_settings_load_without_signal_cards(lines):  # noqa: F811
    state = SettingsPersistenceService._state
    assert state["schema_version"] == SCHEMA_VERSION == 8
    assert all("plc_inputs" not in line for line in state["lines"])
    assert SettingsPersistenceService.get_line(PRIMARY_LINE_ID).get("plc_inputs", []) == []
    # Saving such a line does not add the key: its entry keeps the shape it had.
    SettingsPersistenceService.save_line({"id": PRIMARY_LINE_ID, "min_fps": 5})
    assert "plc_inputs" not in SettingsPersistenceService._state["lines"][0]


def test_a_bad_signal_card_stops_the_save_and_names_the_card(lines):  # noqa: F811
    with pytest.raises(ValueError, match="Signal 'Fast'.*between 50 and 10000"):
        _save(name="Filling", plc_inputs=[_signal(name="Fast", poll_ms=1)])
    assert all(line["name"] != "Filling" for line in SettingsPersistenceService.get_lines())


def test_a_product_sensor_is_refused_on_a_line_with_a_vision_camera(lines):  # noqa: F811
    with pytest.raises(ValueError, match="vision camera"):
        _save(name="Vision", cameras=[_vision()], plc_inputs=[_signal("product_sensor")])
    # Switched off or not: the counting camera counts the products there.
    with pytest.raises(ValueError, match="vision camera"):
        _save(name="Vision", cameras=[_vision()], plc_inputs=[_signal("product_sensor", enabled=False)])
    line = _save(name="Sensor only", plc_inputs=[_signal("product_sensor", data_type="counter")])
    assert [card["id"] for card in product_sensors(line)] == ["plcin_product_sensor"]
    # Adding a vision camera to that line is refused the same way.
    with pytest.raises(ValueError, match="vision camera"):
        SettingsPersistenceService.save_line({"id": line["id"], "cameras": [_vision()]})


def test_a_capture_signal_needs_a_reader_set_to_capture_on_a_signal(lines):  # noqa: F811
    capture = _signal("capture", camera_id="qr")
    with pytest.raises(ValueError, match="not on this line"):
        _save(name="No camera", plc_inputs=[capture])
    with pytest.raises(ValueError, match="When a PLC signal asks"):
        _save(name="Continuous", cameras=[_reader()], plc_inputs=[capture])
    with pytest.raises(ValueError, match="Camera 1"):
        _save(name="Vision", cameras=[_vision("qr")], plc_inputs=[capture])
    line = _save(name="On a signal", cameras=[_reader(qr_trigger="signal", qr_trigger_delay_ms=250)], plc_inputs=[capture])
    assert line["cameras"][0]["qr_trigger"] == "signal" and line["cameras"][0]["qr_trigger_delay_ms"] == 250
    assert line["plc_inputs"][0]["camera_id"] == "qr"
    # Taking the reader off "signal" while a card still names it is refused too.
    with pytest.raises(ValueError, match="When a PLC signal asks"):
        SettingsPersistenceService.save_line({"id": line["id"], "cameras": [_reader()]})


def test_a_signal_reader_needs_no_vision_camera(lines):  # noqa: F811
    line = _save(name="Reader", cameras=[_reader(qr_trigger="signal")])
    assert line["cameras"][0]["qr_trigger"] == "signal"
    with pytest.raises(ValueError, match="needs a vision camera"):
        _save(name="Wire line", cameras=[_reader("qr2", qr_trigger="line1")])


def test_a_signal_reader_that_no_signal_triggers_is_warned_about(lines):  # noqa: F811
    line = _save(name="Reader", cameras=[_reader(qr_trigger="signal")])
    assert any("Camera 1" in warning and "PLC signal" in warning for warning in line_warnings(line))
    line = SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": [_signal("capture", camera_id="qr")]})
    assert not any("PLC signal" in warning for warning in line_warnings(line))
    # A card that is switched off takes no picture either.
    line = SettingsPersistenceService.save_line(
        {"id": line["id"], "plc_inputs": [_signal("capture", camera_id="qr", enabled=False)]})
    assert any("Camera 1" in warning and "PLC signal" in warning for warning in line_warnings(line))


def test_sync_works_with_a_product_sensor_and_a_code_reader(lines):  # noqa: F811
    # Without a product source Sync has nothing to pair codes with.
    with pytest.raises(ValueError, match="Sync needs"):
        _save(name="No source", cameras=[_reader("qr0")], sync={"enabled": True})
    line = _save(name="Sensor line", cameras=[_reader("qr1")], sync={"enabled": True},
                 plc_inputs=[_signal("product_sensor")])
    assert line["sync"]["enabled"] is True
    # A reader whose codes decide good or reject needs each product paired with its code...
    checking = _reader("qr2", qr_action="accept_listed", qr_no_read="reject")
    checked = _save(name="Checked", cameras=[checking], plc_inputs=[_signal("product_sensor", id="plcin_s2")])
    assert checked["sync"]["enabled"] is True and checked["cameras"][0]["qr_no_read"] == "reject"
    # ...and stays that way while its sensor card is switched off.
    off = SettingsPersistenceService.save_line(
        {"id": checked["id"], "plc_inputs": [_signal("product_sensor", id="plcin_s2", enabled=False)]})
    assert off["sync"]["enabled"] is True and off["cameras"][0]["qr_no_read"] == "reject"
    # A line with only that reader counts one product per code read, as before.
    alone = _save(name="Alone", cameras=[{**checking, "camera_id": "qr3"}])
    assert alone["sync"]["enabled"] is False and alone["cameras"][0]["qr_no_read"] == "ignore"


def test_a_channel_a_signal_reads_counts_as_used(lines):  # noqa: F811
    line = _save(name="Filling", plc_inputs=[_signal(plc_endpoint_id="plc-7")])
    assert endpoints_used_by_line(line) == {"plc-7": "PLC signal 'start line'"}
    assert SettingsPersistenceService.lines_using_endpoint("plc-7") == ["Filling"]


# ── API ───────────────────────────────────────────────────────────────────────

def test_a_cloned_line_has_no_signal_cards(client, admin_headers):
    created = client.post(API, json={"name": "Signals source", "plc_inputs": [_signal()]}, headers=admin_headers)
    assert created.status_code == 201, created.text
    source = created.json()
    copy_id = None
    try:
        assert [card["id"] for card in source["plc_inputs"]] == ["plcin_start_line"]
        copy = client.post(f"{API}/{source['id']}/clone", json={"name": "Signals copy"}, headers=admin_headers)
        assert copy.status_code == 201, copy.text
        copy_id = copy.json()["id"]
        # Two lines started and stopped by one bit is never what a copy is for.
        assert copy.json().get("plc_inputs", []) == []
    finally:
        for line_id in (copy_id, source["id"]):
            if line_id:
                client.delete(f"{API}/{line_id}", headers=admin_headers)


# ── Edges, debounce and the PLC counter ───────────────────────────────────────

def test_a_rising_edge_fires_once():
    f = SignalFilter(0.0)
    assert f.update(False, 0.0) is None                 # the starting point
    assert f.update(True, 0.1) == (False, True)
    assert f.update(True, 0.2) is None
    assert edge_matches("rising", False, True) and not edge_matches("rising", True, False)
    assert edge_matches("falling", True, False) and not edge_matches("falling", False, True)
    assert edge_matches("change", 3, 4) and not edge_matches("change", 3, 3)
    # A number is on when it is not 0.
    assert edge_matches("rising", 0, 7) and not edge_matches("rising", 3, 4)


def test_debounce_ignores_a_value_that_does_not_last():
    f = SignalFilter(0.2)
    f.update(False, 0.0)
    assert f.update(True, 0.1) is None                  # first seen
    assert f.update(False, 0.15) is None                # gone again
    assert f.update(True, 0.2) is None
    assert f.update(True, 0.45) == (False, True)        # still there 0.2 s later
    assert f.value is True


def test_what_counts_as_on():
    assert [truthy(v) for v in (True, 1, 2.5, "LOT-7", "1")] == [True] * 5
    assert [truthy(v) for v in (False, 0, 0.0, None, "", " 0 ", "false", "OFF")] == [False] * 8


@pytest.mark.parametrize("previous, current, delta", [(10, 13, 3), (65530, 4, 10), (7, 7, 0), (500, 0, 0), (0, 1001, 0)])
def test_the_counter_increase_wraps_and_ignores_a_reset(previous, current, delta):
    assert COUNTER_MAX_STEP == 1000
    assert counter_delta(previous, current) == delta


def test_the_new_alarms_are_in_the_catalog():
    catalog = {entry["code"]: entry for entry in ALARM_CATALOG}
    assert (catalog["plc.input_failed"]["scope"], catalog["plc.input_failed"]["severity"]) == ("line", "warning")
    assert catalog["plc.input_alarm"]["scope"] == "line"
    assert (catalog["plc.action_not_confirmed"]["scope"], catalog["plc.action_not_confirmed"]["severity"]) == ("line", "critical")
    assert alarm_line("plc.input_failed", {"line_id": "line-9"}) == "line-9"
    assert PLCActor("plc:Line PLC").role == "PLC" and PLCActor("plc:Line PLC").clearance_level == 2


# ── Polling ───────────────────────────────────────────────────────────────────

class ReadingDriver(FakeDriver):
    """A fake PLC that can be read, by address. Every read, write and connect is noted in calls."""

    def __init__(self, endpoint):
        super().__init__(endpoint)
        self.values = {}
        self.calls = []
        self.connects = 0
        self.unreadable = set()
        self.drops_on_read = False
        self.write_seconds = 0.0

    async def connect(self):
        self.connects += 1
        return await super().connect()

    async def _read(self, address, data_type):
        self.calls.append(("read", address, data_type))
        if not self.reachable or self.drops_on_read:
            self.is_connected = False
            return False, None, "link down"
        if address in self.unreadable:
            return False, None, f"{address} cannot be read"
        return True, self.values.get(address, False), f"read {address}"

    async def _write(self, op, address, value):
        self.calls.append((op, address))
        await asyncio.sleep(self.write_seconds)
        return await super()._write(op, address, value)


@pytest.fixture
def signals(lines, plc, monkeypatch):  # noqa: F811
    """A line manager, one PLC channel (ep1) with a fake driver that can be read, and a clean service."""
    endpoint, _ = plc
    driver = ReadingDriver(endpoint)
    monkeypatch.setattr(plc_factory, "_driver_pool", {"ep1": driver})
    PLCInputService.reset()
    alarm_manager.reset()
    yield driver
    PLCInputService.reset()
    alarm_manager.reset()


@pytest.fixture
def acts(monkeypatch):
    """What the service would have done: (card id, old, new, count) of every action."""
    seen = []

    async def record(cls, card, old, new, count=1):
        seen.append((card["id"], old, new, count))

    monkeypatch.setattr(PLCInputService, "_act", classmethod(record))
    return seen


def _line_with(*cards, name="Signals", **line):
    saved = _save(name=name, plc_inputs=list(cards), **line)
    PLCInputService.reload()
    return saved


async def _pass(now=None):
    await PLCInputService.poll_once("ep1", now)
    await PLCInputService.drain()


def _status(line, card_id):
    return next(row for row in PLCInputService.get_status(line["id"]) if row["id"] == card_id)


def test_the_first_read_is_the_starting_point_and_runs_no_action(signals, acts):
    _line_with(_signal("start_line"))
    signals.values["10001"] = True          # the start bit is already on when the server starts

    async def steps():
        await _pass()
        await _pass()

    _run(steps)
    assert acts == []
    assert [call[0] for call in signals.calls] == ["read", "read"]


def test_an_edge_runs_the_cards_action_once(signals, acts):
    _line_with(_signal("start_line"))

    async def steps():
        for value in (False, True, True):
            signals.values["10001"] = value
            await _pass()

    _run(steps)
    assert acts == [("plcin_start_line", False, True, 1)]


def test_a_falling_and_a_change_card(signals, acts):
    _line_with(_signal("stop_line", address="C1", edge="falling"),
               _signal("reset_counters", address="R5", edge="change", data_type="uint16"))

    async def steps():
        for coil, register in ((True, 3), (False, 4), (True, 4)):
            signals.values.update({"C1": coil, "R5": register})
            await _pass()

    _run(steps)
    assert acts == [("plcin_stop_line", True, False, 1), ("plcin_reset_counters", 3, 4, 1)]
    assert ("read", "R5", "uint16") in signals.calls and ("read", "C1", "auto") in signals.calls


def test_run_level_acts_on_every_change(signals, acts):
    _line_with(_signal("run_level"))

    async def steps():
        for value in (False, True, False):
            signals.values["10001"] = value
            await _pass()

    _run(steps)
    assert acts == [("plcin_run_level", False, True, 1), ("plcin_run_level", True, False, 1)]


def test_an_alarm_card_follows_the_level_from_the_first_read(signals, acts):
    _line_with(_signal("alarm"))
    signals.values["10001"] = True

    async def steps():
        await _pass()                       # already on: the alarm is raised at once
        await _pass()
        signals.values["10001"] = False
        await _pass()

    _run(steps)
    assert acts == [("plcin_alarm", None, True, 1), ("plcin_alarm", True, False, 1)]


def test_a_debounced_card_waits_for_the_value_to_last(signals, acts):
    _line_with(_signal("start_line", debounce_ms=200))

    async def steps():
        for now, value in ((10.0, False), (10.1, True), (10.15, False), (10.2, True), (10.3, True), (10.45, True)):
            signals.values["10001"] = value
            await _pass(now)

    _run(steps)
    assert acts == [("plcin_start_line", False, True, 1)]


def test_a_counter_card_reports_the_increase(signals, acts):
    _line_with(_signal("product_sensor", address="R10", data_type="counter"))

    async def steps():
        for value in (10, 13, 13, 65535, 2):
            signals.values["R10"] = value
            await _pass()

    _run(steps)
    # The first read is the starting point; 13 -> 65535 is more than a counter moves between two reads.
    assert acts == [("plcin_product_sensor", 10, 13, 3), ("plcin_product_sensor", 65535, 2, 3)]
    assert ("read", "R10", "uint16") in signals.calls


def test_a_counter_reset_in_the_plc_counts_nothing(signals, acts):
    _line_with(_signal("product_sensor", address="R10", data_type="counter"))

    async def steps():
        for value in (10, 13, 13, 0, 2):
            signals.values["R10"] = value
            await _pass()

    _run(steps)
    assert [(old, new, count) for _, old, new, count in acts] == [(10, 13, 3), (0, 2, 2)]


def test_status_shows_the_value_the_change_time_and_the_read_count(signals, acts):
    line = _line_with(_signal("start_line"), _signal("stop_line", enabled=False))
    assert _status(line, "plcin_start_line")["read_count"] == 0     # nothing read yet
    seen = {}

    async def steps():
        await _pass()
        seen["first"] = dict(_status(line, "plcin_start_line"))
        signals.values["10001"] = True
        await _pass()
        seen["second"] = dict(_status(line, "plcin_start_line"))

    _run(steps)
    first, second = seen["first"], seen["second"]
    assert {key: first[key] for key in ("id", "name", "enabled", "action", "value", "changed_at", "read_count", "error")} == {
        "id": "plcin_start_line", "name": "start line", "enabled": True, "action": "start_line",
        "value": False, "changed_at": None, "read_count": 1, "error": None}
    assert first["last_read_at"] is not None
    assert (second["value"], second["read_count"]) == (True, 2) and second["changed_at"] is not None
    off = _status(line, "plcin_stop_line")
    assert (off["enabled"], off["value"], off["read_count"]) == (False, None, 0)
    assert PLCInputService.get_status("no-such-line") == []


def test_a_read_error_raises_plc_input_failed_and_the_next_good_read_clears_it(signals, acts):
    line = _line_with(_signal("start_line"))
    signals.unreadable.add("10001")

    async def one_pass():
        await _pass()

    _run(one_pass)
    alarm = alarm_manager.get(FAILED, "plc_input:plcin_start_line")
    assert alarm is not None and alarm.severity == AlarmSeverity.WARNING
    assert alarm.details["line_id"] == line["id"] and alarm.details["input_id"] == "plcin_start_line"
    assert "start line" in alarm.message and "10001 cannot be read" in alarm.message
    assert "cannot be read" in _status(line, "plcin_start_line")["error"]
    assert acts == []

    signals.unreadable.clear()
    _run(one_pass)
    assert alarm_manager.get(FAILED, "plc_input:plcin_start_line") is None
    assert _status(line, "plcin_start_line")["error"] is None


def test_a_plc_that_is_down_is_retried_at_the_reconnect_interval_not_every_poll(signals, acts):
    signals._ep["reconnect_interval"] = 5
    line = _line_with(_signal("start_line"), _signal("stop_line", address="C2"))
    signals.reachable = False

    async def steps():
        for n in range(20):
            await _pass(100.0 + n * 0.05)
        assert signals.connects == 1
        await _pass(105.0)
        assert signals.connects == 2
        signals.reachable = True
        await _pass(110.5)

    _run(steps)
    assert signals.connects == 3
    # A PLC that cannot be reached has its own alarm; its signals raise none of their own.
    assert alarm_manager.get(FAILED, "plc_input:plcin_start_line") is None
    assert [row["error"] for row in PLCInputService.get_status(line["id"])] == [None, None]
    assert [call[0] for call in signals.calls] == ["read", "read"]      # both cards, once the PLC was back


def test_signals_of_an_unreachable_plc_say_so(signals, acts):
    line = _line_with(_signal("start_line"))
    signals.reachable = False

    async def steps():
        await _pass(100.0)

    _run(steps)
    assert _status(line, "plcin_start_line")["error"] == "PLC not connected"
    assert signals.calls == []


def test_a_read_that_keeps_dropping_the_link_raises_the_alarm_the_second_time(signals, acts):
    _line_with(_signal("start_line"))
    signals.drops_on_read = True

    async def steps():
        await _pass(100.0)
        # One lost link is the PLC connection's own business...
        assert alarm_manager.get(FAILED, "plc_input:plcin_start_line") is None
        await _pass(100.05)                 # ...and is not retried before the reconnect interval
        assert signals.connects == 1
        await _pass(101.5)

    _run(steps)
    # ...but a signal whose read ends the connection every time is a fault of its own.
    assert signals.connects == 2
    assert alarm_manager.get(FAILED, "plc_input:plcin_start_line") is not None


def test_reads_never_come_between_a_cards_write_and_its_strobe(signals, acts):
    _line_with(_signal("start_line"))
    signals.write_seconds = 0.03
    card = _normalize_card({"id": "w", "name": "w", "enabled": True, "plc_endpoint_id": "ep1", "operation": "write",
                            "target_address": "40001", "write_value": 7, "strobe_address": "M0.1", "strobe_pulse_ms": 50})

    async def steps():
        await signals.connect()
        signals.calls.clear()
        write = asyncio.create_task(PLCDispatcherService._dispatch(card, _CardState(card_id="w"), {"event_id": "e1"}))
        await asyncio.sleep(0.01)           # the write is on its way, the strobe is still to come
        await _pass()
        await write

    _run(steps)
    assert signals.calls == [("WRITE", "40001"), ("PULSE", "M0.1"), ("read", "10001", "auto")]


def test_a_disabled_card_is_not_read(signals, acts):
    _line_with(_signal("start_line", enabled=False))
    signals.values["10001"] = True

    async def steps():
        await _pass()

    _run(steps)
    assert signals.calls == [] and signals.connects == 0


def test_a_card_without_a_channel_or_an_address_says_so(signals, acts):
    line = _line_with(_signal("start_line", plc_endpoint_id=""), _signal("stop_line", address=""),
                      _signal("reset_counters", plc_endpoint_id="gone"))

    async def steps():
        await _pass()
        await PLCInputService.poll_once("gone")

    _run(steps)
    errors = {row["id"]: row["error"] for row in PLCInputService.get_status(line["id"])}
    assert "channel" in errors["plcin_start_line"] and "address" in errors["plcin_stop_line"]
    assert "no longer exists" in errors["plcin_reset_counters"]
    assert signals.calls == []


def test_removing_a_signal_clears_its_alarms(signals, acts):
    line = _line_with(_signal("alarm"))
    source = "plc_input:plcin_alarm"
    details = {"line_id": line["id"], "input_id": "plcin_alarm"}

    def raise_both():
        async def steps():
            await _pass()
        _run(steps)
        alarm_manager.raise_alarm(FAILED, source, "could not be read", AlarmSeverity.WARNING, details)
        alarm_manager.raise_alarm(AlarmCode.PLC_INPUT_ALARM, source, "Guard door", AlarmSeverity.CRITICAL, details)

    raise_both()
    SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": []})
    PLCInputService.reload()
    assert alarm_manager.active("plc_input:") == []

    SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": [_signal("alarm")]})
    PLCInputService.reload()
    raise_both()
    SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": [_signal("alarm", enabled=False)]})
    PLCInputService.reload()
    assert alarm_manager.active("plc_input:") == []


def test_a_changed_card_starts_from_a_new_first_read(signals, acts):
    line = _line_with(_signal("start_line"))

    async def steps():
        await _pass()
        signals.values["C7"] = True
        SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": [_signal("start_line", address="C7")]})
        PLCInputService.reload()
        await _pass()                       # C7 is on, but it is the first read of the new address

    _run(steps)
    assert acts == []
    assert _status(line, "plcin_start_line")["read_count"] == 1


def test_an_action_that_fails_does_not_stop_the_poll(signals, monkeypatch):
    async def broken(cls, card, old, new, count=1):
        raise RuntimeError("the line is on fire")

    monkeypatch.setattr(PLCInputService, "_act", classmethod(broken))
    line = _line_with(_signal("start_line"))

    async def steps():
        for value in (False, True, False, True):
            signals.values["10001"] = value
            await _pass()

    _run(steps)
    assert _status(line, "plcin_start_line")["read_count"] == 4


def test_reload_starts_no_task_before_start(signals, acts):
    async def steps():
        _line_with(_signal("start_line"))
        assert PLCInputService._tasks == {} and PLCInputService.is_running() is False

    _run(steps)


def test_one_task_per_channel_at_the_smallest_poll(signals, acts):
    async def steps():
        _line_with(_signal("start_line", poll_ms=200), _signal("stop_line", address="C2", poll_ms=500))
        PLCInputService.start()
        try:
            assert list(PLCInputService._tasks) == ["ep1"] and PLCInputService.is_running()
            assert PLCInputService.poll_interval("ep1") == 0.2
            signals.values["10001"] = False
            await asyncio.sleep(0.05)
            signals.values["10001"] = True
            await asyncio.sleep(0.45)       # the task polls by itself
            await PLCInputService.drain()
            assert acts == [("plcin_start_line", False, True, 1)]
        finally:
            await PLCInputService.stop()
        assert PLCInputService._tasks == {} and PLCInputService.is_running() is False

    _run(steps)


def test_a_line_without_signal_cards_is_not_polled(signals, acts, monkeypatch):
    asked = []
    monkeypatch.setattr(plc_factory.PLCDriverFactory, "get_driver",
                        classmethod(lambda cls, endpoint, fresh=False: asked.append(endpoint) or signals))

    async def steps():
        _save(name="No signals")
        PLCInputService.start()
        try:
            await asyncio.sleep(0.05)
            assert PLCInputService._tasks == {}
        finally:
            await PLCInputService.stop()

    _run(steps)
    assert asked == [] and signals.calls == []


def test_a_task_ends_when_its_channel_has_no_cards_left(signals, acts):
    async def steps():
        line = _line_with(_signal("start_line", poll_ms=50))
        PLCInputService.start()
        try:
            assert list(PLCInputService._tasks) == ["ep1"]
            SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": []})
            PLCInputService.reload()
            assert PLCInputService._tasks == {}
        finally:
            await PLCInputService.stop()

    _run(steps)


def test_read_now_reads_a_card_that_is_not_saved(signals):
    signals.values["C9"] = True
    signals.unreadable.add("C8")
    seen = {}

    async def steps():
        seen["good"] = await PLCInputService.read_now(_signal("start_line", address="C9"))
        seen["bad"] = await PLCInputService.read_now(_signal("start_line", address="C8"))
        seen["missing"] = await PLCInputService.read_now(_signal("start_line", plc_endpoint_id="nope"))
        seen["unset"] = await PLCInputService.read_now(_signal("start_line", address=""))

    _run(steps)
    good, bad, missing, unset = seen["good"], seen["bad"], seen["missing"], seen["unset"]
    assert (good["ok"], good["value"]) == (True, True) and good["message"]
    assert (bad["ok"], bad["value"]) == (False, None) and "cannot be read" in bad["message"]
    assert missing["ok"] is False and "channel" in missing["message"]
    assert unset["ok"] is False and "address" in unset["message"]
    assert plc_input_service.NOT_CONNECTED == "PLC not connected"


# ── When the service starts, and when it reads its cards again ────────────────

def test_the_signal_poller_starts_with_the_saved_settings(lines, monkeypatch):  # noqa: F811
    from app.services.plc_failsafe_service import PLCFailsafeService

    started = []
    monkeypatch.setattr(PLCFailsafeService, "start", classmethod(lambda cls: started.append("failsafe")))
    monkeypatch.setattr(PLCInputService, "start", classmethod(lambda cls: started.append("signals")))

    async def steps():
        await SettingsPersistenceService.restore_on_startup()

    _run(steps)
    assert started == ["failsafe", "signals"]


def test_saving_or_deleting_a_plc_channel_reads_the_cards_again(lines, monkeypatch):  # noqa: F811
    reloads = []
    monkeypatch.setattr(PLCInputService, "reload", classmethod(lambda cls: reloads.append(1)))
    channel = {"id": "plc-9", "name": "Filler PLC", "protocol": "plc", "plc_sub_protocol": "modbus_tcp",
               "host": "127.0.0.1", "port": 1502, "enabled": True}

    async def steps():
        await SettingsPersistenceService.add_or_update_endpoint(dict(channel))
        assert len(reloads) == 1
        SettingsPersistenceService.delete_endpoint("plc-9")
        assert len(reloads) == 2
        # A channel that is not a PLC has no signals to read.
        await SettingsPersistenceService.add_or_update_endpoint(
            {"id": "tcp-9", "name": "MES", "protocol": "tcp", "host": "127.0.0.1", "port": 9109, "enabled": True})

    _run(steps)
    assert len(reloads) == 2


def test_saving_a_line_through_the_api_gives_the_service_its_cards(client, admin_headers):
    created = client.post(API, json={"name": "Signals API", "plc_inputs": [_signal()]}, headers=admin_headers)
    assert created.status_code == 201, created.text
    line_id = created.json()["id"]
    try:
        assert [row["id"] for row in PLCInputService.get_status(line_id)] == ["plcin_start_line"]
        # Tests never start the poller: the cards are known, nothing reads them.
        assert PLCInputService.is_running() is False and PLCInputService._tasks == {}
        changed = client.put(f"{API}/{line_id}", json={"plc_inputs": []}, headers=admin_headers)
        assert changed.status_code == 200, changed.text
        assert PLCInputService.get_status(line_id) == []
    finally:
        client.delete(f"{API}/{line_id}", headers=admin_headers)
    PLCInputService.reload()
    assert PLCInputService.get_status(line_id) == []
