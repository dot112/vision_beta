"""PLC signals (inputs): a PLC can send signals into the software, per line.

A signal card names a PLC channel and an address, how often to read it, and
what the line does when the value changes: start or stop, count a product,
take a picture, reset the counters, set the batch, confirm a PLC action or
raise an alarm.
"""
from __future__ import annotations

import pytest

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
from tests.test_joined_cameras import LIST, lines  # noqa: F401  (a fixture)

API = "/api/v1/lines"


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
