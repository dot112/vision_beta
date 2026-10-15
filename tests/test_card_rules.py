"""Only when: every output action and send card can have up to 5 rules, and acts only when they pass."""
from __future__ import annotations

import asyncio

import pytest

import app.services.line_service as line_module
from app.services import event_values
from app.services.card_triggers import card_fires, normalize_trigger
from app.services.line_config import normalize_send_card, timer_interval_ms  # noqa: F401
from app.services.plc_dispatcher_service import PLCDispatcherService, _normalize_card
from app.services.send_dispatcher_service import SendDispatcherService
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_send_cards import lines  # noqa: F401  (a fixture)

LANE3 = {"match": "all", "rules": [{"left": "product.lane", "op": "=", "right": "3", "right_kind": "constant"}]}
BAD = {"rules": [{"left": "speed", "op": "="}]}


def _product(lane):
    return {"code": "J-3", "name": "Grape", "list": "Juices", "list_id": "l", "fields": {"lane": lane}}


def test_a_card_acts_only_when_its_rules_pass():
    card = normalize_trigger({"id": "k", "trigger": "cross_line", "condition": "reject", "only_when": LANE3})
    event = {"event_type": "crossing", "line_id": "line-1", "result": "reject", "product": _product(3)}
    assert card_fires(card, event)
    assert not card_fires(card, {**event, "product": _product(2)})
    assert not card_fires(card, {k: v for k, v in event.items() if k != "product"})
    assert not card_fires(card, {**event, "result": "good"})          # the condition still comes first
    anyone = normalize_trigger({"trigger": "cross_line", "condition": "any", "only_when": {
        "match": "any", "rules": [LANE3["rules"][0], {"left": "result", "op": "=", "right": "good", "right_kind": "constant"}]}})
    assert card_fires(anyone, {**event, "product": _product(2), "result": "good"})


def test_a_card_without_rules_never_computes_values(monkeypatch):
    def boom(event):
        raise AssertionError("values computed for a card without rules")

    monkeypatch.setattr(event_values, "values_for", boom)
    assert card_fires(normalize_trigger({"trigger": "cross_line", "condition": "any"}), {"event_type": "crossing", "line_id": "line-1"})
    assert card_fires(normalize_trigger({"trigger": "cross_line", "condition": "any", "only_when": None}),
                      {"event_type": "crossing", "line_id": "line-1"})


def _plc_card(**extra):
    return {"id": "w1", "name": "Sort", "enabled": True, "plc_endpoint_id": "", "trigger": "cross_line",
            "condition": "any", "operation": "pulse", "target_address": "Q0.1", **extra}


def test_only_when_is_kept_and_checked(lines):  # noqa: F811
    with pytest.raises(ValueError, match="Send card 'Report'.*speed"):
        normalize_send_card({"name": "Report", "only_when": BAD})
    sent = normalize_send_card({"name": "Report", "only_when": {"rules": [{"left": "result", "op": "=", "right": " good "}]}})
    assert sent["only_when"] == {"match": "all", "rules": [{"left": "result", "op": "=", "right": "good", "right_kind": "constant"}]}
    assert "only_when" not in normalize_send_card({"name": "Report"})

    with pytest.raises(ValueError, match="Sort.*speed"):
        SettingsPersistenceService.save_line({"name": "Rules line", "plc_actions": [_plc_card(only_when=BAD)]})
    line = SettingsPersistenceService.save_line({"name": "Rules line", "plc_actions": [_plc_card(
        only_when={"rules": [{"left": "result", "op": "=", "right": " good "}]})]})
    saved = SettingsPersistenceService.get_line_plc_actions(line["id"])[0]
    assert saved["only_when"]["rules"][0]["right"] == "good"                     # saved as checked
    with pytest.raises(ValueError, match="Sort.*speed"):
        SettingsPersistenceService.replace_line_plc_actions(line["id"], [_plc_card(only_when=BAD)])
    # An older client that leaves only_when out keeps it; null clears it.
    SettingsPersistenceService.replace_line_plc_actions(line["id"], [_plc_card()])
    assert SettingsPersistenceService.get_line_plc_actions(line["id"])[0]["only_when"]["rules"][0]["left"] == "result"
    SettingsPersistenceService.replace_line_plc_actions(line["id"], [_plc_card(only_when=None)])
    assert not SettingsPersistenceService.get_line_plc_actions(line["id"])[0].get("only_when")

    report = {"id": "send_r", "name": "Report", "endpoint_id": "", "trigger": "cross_line", "only_when": LANE3}
    SettingsPersistenceService.replace_line_send_actions(line["id"], [report])
    SettingsPersistenceService.replace_line_send_actions(line["id"], [{k: v for k, v in report.items() if k != "only_when"}])
    assert SettingsPersistenceService.get_line_send_actions(line["id"])[0]["only_when"] == LANE3


def test_a_timer_card_sends_only_when_its_rules_pass(lines, monkeypatch):  # noqa: F811
    line = SettingsPersistenceService.save_line({"name": "Timer rules", "enabled": True})
    lines.apply_state()
    monkeypatch.setattr(SendDispatcherService, "_queue", classmethod(lambda cls, card, message: True))
    card = {**normalize_send_card({"id": "send_t", "name": "Tick", "trigger": "timer", "set_value": "1000",
                                   "only_when": {"rules": [{"left": "batch", "op": "=", "right": "B-7"}]}}),
            "line_id": line["id"]}
    assert SendDispatcherService.send_timer(card) is False
    line_module.line_manager.get(line["id"]).set_batch("B-7")
    assert SendDispatcherService.send_timer(card) is True


def test_a_card_whose_rules_fail_does_not_stop_the_others(plc, monkeypatch):  # noqa: F811
    def boom(event):
        raise RuntimeError("broken values")

    monkeypatch.setattr(event_values, "values_for", boom)
    cards = [_plc_card(id="a", name="A", plc_endpoint_id="ep1", only_when=LANE3),
             _plc_card(id="b", name="B", plc_endpoint_id="ep1")]
    monkeypatch.setattr(PLCDispatcherService, "_cards", [_normalize_card(c) for c in cards])

    async def run():
        await PLCDispatcherService.evaluate({"event_type": "crossing", "event_id": "e1", "line_id": "line-1", "result": "good"})

    asyncio.run(run())
    assert "a" not in PLCDispatcherService._states or not PLCDispatcherService._states["a"].last_fired_at
    assert PLCDispatcherService._states["b"].last_fired_at > 0
