"""Input changed: a PLC input's accepted change fires the output actions and send cards that name it."""
from __future__ import annotations

import pytest

from app.services.card_triggers import card_fires, input_event, normalize_trigger
from app.services.event_values import values_for
from app.services.line_config import normalize_plc_input, normalize_send_card
from app.services.plc_input_service import PLCInputService
from app.services.send_dispatcher_service import input_payload, render_template
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_joined_cameras import _run, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import _line_with, _pass, _signal, acts, signals  # noqa: F401  (fixtures)


def _input_card(cond, input_id="lane3"):
    return normalize_trigger({"id": f"c-{cond}", "trigger": "input", "input_id": input_id, "condition": cond})


@pytest.mark.parametrize("previous, value, fired", [
    (False, True, {"on", "change"}), (True, False, {"off", "change"}), (0, 5, {"on", "change"}),
    (5, 7, {"change"}), ("", "FULL", {"on", "change"}),
])
def test_an_input_change_fires_its_input_cards(previous, value, fired):
    event = {"event_type": "input", "line_id": "line-1", "input_id": "lane3", "value": value, "previous": previous}
    assert {c for c in ("on", "off", "change") if card_fires(_input_card(c), event)} == fired
    assert not card_fires(_input_card("change", "other"), event)
    assert not card_fires(normalize_trigger({"trigger": "cross_line", "condition": "any"}), event)
    assert not card_fires(normalize_trigger({"trigger": "line_state", "condition": "toggled"}), event)
    assert not card_fires(_input_card("change"), {"event_type": "crossing", "line_id": "line-1", "result": "good"})
    assert card_fires({**_input_card("change"), "camera_id": "cam-1"}, event) is ("change" in fired)   # line-wide


def test_input_events_follow_accepted_changes(signals, acts, monkeypatch):  # noqa: F811
    published = []
    monkeypatch.setattr(PLCInputService, "_publish_input",
                        classmethod(lambda cls, card, old, new: published.append((card["id"], old, new))))
    _line_with(_signal("none", value_name="lane3"),
               _signal("start_line", address="10002"),
               _signal("product_sensor", address="10003", data_type="counter"),
               {**_signal("stop_line", address="10004"), "id": "plcin_slow", "debounce_ms": 5000})

    async def steps():
        for lane3, start, counter in ((False, False, 0), (True, True, 1), (True, True, 2), (False, True, 3)):
            signals.values.update({"10001": lane3, "10002": start, "10003": counter, "10004": lane3})
            await _pass()

    _run(steps)
    assert published == [("plcin_none", False, True), ("plcin_start_line", False, True), ("plcin_none", True, False)]
    assert ("plcin_start_line", False, True, 1) in acts                 # its own action still runs
    assert not any(card_id == "plcin_none" for card_id, *_ in acts)     # "none" only fires cards


def test_the_input_message_and_its_placeholders(lines):  # noqa: F811
    line = SettingsPersistenceService.save_line({"name": "Inputs line", "enabled": True})
    lines.apply_state()
    event = input_event({"id": "in_lane3", "name": "Lane 3 full", "line_id": line["id"]}, False, True)
    assert event["event_type"] == "input" and event["input_id"] == "in_lane3" and event["line_id"] == line["id"]
    payload = input_payload(event)
    assert (payload["event"], payload["input_name"], payload["value"], payload["previous"], payload["line_name"]) == (
        "INPUT_CHANGED", "Lane 3 full", True, False, "Inputs line")
    assert render_template("{input_name}: {input.value} (was {input.previous})", payload) == "Lane 3 full: True (was False)"
    values = values_for(event)
    assert values["input.value"] is True and values["input.previous"] is False


def test_cards_must_name_an_input_of_their_line(lines):  # noqa: F811
    send = {"id": "send_lane3", "name": "Lane 3 message", "endpoint_id": "", "trigger": "input", "input_id": "in_lane3",
            "condition": "on"}
    action = {"id": "act_lane3", "name": "Lane 3 lamp", "enabled": True, "plc_endpoint_id": "", "trigger": "input",
              "input_id": "in_lane3", "condition": "on", "operation": "pulse", "target_address": "Q0.3"}
    with pytest.raises(ValueError, match="Lane 3 lamp.*in_lane3"):
        SettingsPersistenceService.save_line({"name": "Lane line", "plc_actions": [action]})
    with pytest.raises(ValueError, match="Lane 3 message.*in_lane3"):
        SettingsPersistenceService.save_line({"name": "Lane line", "send_actions": [send]})
    lane3 = {"id": "in_lane3", "name": "Lane 3 full", "action": "none", "plc_endpoint_id": "", "address": ""}
    line = SettingsPersistenceService.save_line({"name": "Lane line", "plc_inputs": [lane3], "plc_actions": [action],
                                                 "send_actions": [send]})
    assert SettingsPersistenceService.get_line_plc_actions(line["id"])[0]["input_id"] == "in_lane3"
    assert SettingsPersistenceService.get_line_send_actions(line["id"])[0]["input_id"] == "in_lane3"
    with pytest.raises(ValueError, match="in_lane3"):
        SettingsPersistenceService.save_line({"id": line["id"], "name": "Lane line", "plc_inputs": []})
    with pytest.raises(ValueError, match="Lane 3 message.*other"):
        SettingsPersistenceService.replace_line_send_actions(line["id"], [{**send, "input_id": "other"}])
    # An older client that leaves input_id out keeps it.
    SettingsPersistenceService.replace_line_send_actions(line["id"], [{k: v for k, v in send.items() if k != "input_id"}])
    assert SettingsPersistenceService.get_line_send_actions(line["id"])[0]["input_id"] == "in_lane3"


def test_the_new_input_settings_are_checked():
    assert normalize_plc_input({"action": "none", "data_type": "string"})["action"] == "none"
    with pytest.raises(ValueError, match="string"):
        normalize_plc_input({"action": "start_line", "data_type": "string"})
    with pytest.raises(ValueError, match="input"):
        normalize_send_card({"trigger": "input", "condition": "on"})
    with pytest.raises(ValueError, match="on"):
        normalize_send_card({"trigger": "input", "input_id": "lane3", "condition": "rising"})
    card = normalize_send_card({"trigger": "input", "input_id": "lane3"})
    assert (card["trigger"], card["input_id"], card["condition"]) == ("input", "lane3", "on")
    assert "input_id" not in normalize_send_card({"trigger": "cross_line", "input_id": "lane3"})
