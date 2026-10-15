"""Write any value: an output action writes a product field, the code, the product name, an input's value
or a formula; with no value it skips the write or writes a default; text goes to OPC UA String tags only.
"""
from __future__ import annotations

import asyncio
import types

import pytest

import app.services.line_service as line_module
from app.events.alarm_events import alarm_manager
from app.services.plc_dispatcher_service import PLCDispatcherService, _CardState
from app.services.plc_failsafe_service import validate_write_value
from app.services.plc_input_service import PLCInputService
from app.services.product_service import product_catalog
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_plc_failsafe import _card, plc  # noqa: F401  (a fixture)
from tests.test_send_cards import lines  # noqa: F401  (a fixture)

WIDGET = {"code": "J-1", "name": "Widget", "list": "Juices", "list_id": "list-j",
          "fields": {"lane": 3, "recipe": "R-12", "fragile": True}}
EVENT = {"event_id": "e1", "line_id": "line-1", "result": "good", "good_count": 7, "reject_count": 1,
         "qr_code": "J-1", "qr_status": "known", "product": WIDGET}

W = {"id": "w1", "name": "Sort", "operation": "WRITE"}


def _write_card(**extra):
    return {"id": "w1", "name": "Sort", "enabled": True, "plc_endpoint_id": "", "trigger": "cross_line",
            "condition": "any", "operation": "WRITE", "target_address": "DB1.Lane", **extra}


# ── Save-time checks ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("extra, word", [
    ({"value_source": "product_field"}, "product field"), ({"value_source": "product_field", "value_field": "1x"}, "product field"),
    ({"value_source": "input", "value_input": ""}, "input"), ({"value_source": "formula", "formula": "1 +"}, "formula"),
    ({"value_source": "formula"}, "formula"),
    ({"value_source": "code", "if_missing": "explode"}, "no value"), ({"value_source": "code", "if_missing": "default"}, "default value"),
    ({"value_source": "code", "if_missing": "default", "default_value": float("inf")}, "default value"),
    ({"value_source": "code", "if_missing": "default", "default_value": True}, "default value"),
    ({"value_source": "code", "if_missing": "default", "default_value": "x" * 257}, "default value"),
])
def test_bad_write_settings_are_refused(extra, word):
    with pytest.raises(ValueError, match=word):
        validate_write_value({**W, **extra})


def test_good_write_settings_and_only_a_write_card_writes_a_value():
    for extra in ({"value_source": "product_field", "value_field": "lane"}, {"value_source": "code"},
                  {"value_source": "product_name"}, {"value_source": "input", "value_input": "speed"},
                  {"value_source": "formula", "formula": 'if(result == "good", product.lane, 9)'},
                  {"value_source": "code", "if_missing": "default", "default_value": 0}):
        validate_write_value({**W, **extra})
    with pytest.raises(ValueError, match="Write"):
        validate_write_value({**W, "operation": "PULSE", "value_source": "formula", "formula": "1"})


def test_a_text_default_needs_an_opc_ua_controller():
    card = {**W, "value_source": "code", "if_missing": "default", "default_value": "NONE", "plc_endpoint_id": "ep1"}
    with pytest.raises(ValueError, match="OPC UA"):
        validate_write_value(card, [{"id": "ep1", "plc_sub_protocol": "modbus_tcp"}])
    validate_write_value(card, [{"id": "ep1", "plc_sub_protocol": "opc_ua"}])
    validate_write_value(card)                                         # the controller is not known: accepted


def test_the_new_settings_survive_an_older_client(lines):  # noqa: F811
    card = _write_card(value_source="formula", formula="product.lane * 10", if_missing="default", default_value=0)
    line = SettingsPersistenceService.save_line({"name": "Sort line", "plc_actions": [card]})
    older = {k: v for k, v in card.items() if k not in ("value_source", "formula", "if_missing", "default_value")}
    SettingsPersistenceService.replace_line_plc_actions(line["id"], [older])
    saved = SettingsPersistenceService.get_line_plc_actions(line["id"])[0]
    assert (saved["value_source"], saved["formula"], saved["if_missing"], saved["default_value"]) == (
        "formula", "product.lane * 10", "default", 0)


def test_an_input_value_must_exist_on_the_line(lines):  # noqa: F811
    card = _write_card(value_source="input", value_input="speed")
    with pytest.raises(ValueError, match="Sort.*speed"):
        SettingsPersistenceService.save_line({"name": "Speed line", "plc_actions": [card]})
    speed = {"id": "in_speed", "name": "Belt speed", "action": "start_line", "plc_endpoint_id": "", "address": "",
             "value_name": "speed"}
    line = SettingsPersistenceService.save_line({"name": "Speed line", "plc_inputs": [speed], "plc_actions": [card]})
    assert SettingsPersistenceService.get_line_plc_actions(line["id"])[0]["value_input"] == "speed"
    with pytest.raises(ValueError, match="Sort.*speed"):
        SettingsPersistenceService.save_line({"id": line["id"], "name": "Speed line", "plc_inputs": []})
    with pytest.raises(ValueError, match="Sort.*speed"):
        SettingsPersistenceService.save_line({"id": line["id"], "name": "Speed line", "plc_inputs": [{**speed, "value_name": "rate"}]})
    with pytest.raises(ValueError, match="Sort.*other"):
        SettingsPersistenceService.replace_line_plc_actions(line["id"], [{**card, "value_input": "other"}])


# ── The formula check ─────────────────────────────────────────────────────────

def test_the_formula_check(client, admin_headers):
    made = client.post("/api/v1/products/lists", json={"name": "Formula check list",
                                                       "fields": [{"key": "lane", "type": "number"}]}, headers=admin_headers)
    assert made.status_code == 201, made.text
    list_id = made.json()["id"]
    line = client.post("/api/v1/lines", json={"name": "Formula check", "cameras": [
        {"camera_id": "formula-qr", "role": "qr", "product_list_id": list_id}]}, headers=admin_headers)
    assert line.status_code in (200, 201), line.text
    line_id = line.json()["id"]
    url = f"/api/v1/lines/{line_id}/formula/check"
    try:
        ok = client.post(url, json={"formula": "product.lane + product.brix"}, headers=admin_headers).json()
        assert ok["ok"] is True and ok["names"] == ["product.brix", "product.lane"]
        assert len(ok["warnings"]) == 1 and "brix" in ok["warnings"][0]
        assert client.post(url, json={"formula": "product.name + text(good_count)"}, headers=admin_headers).json()["warnings"] == []
        bad = client.post(url, json={"formula": "1 +"}, headers=admin_headers).json()
        assert bad["ok"] is False and isinstance(bad["position"], int) and "position" in bad["error"]
        assert "speed" in " ".join(client.post(url, json={"formula": "input.speed"}, headers=admin_headers).json()["warnings"])
        assert client.post(url, json={"formula": 5}, headers=admin_headers).json()["ok"] is False
        assert client.post("/api/v1/lines/no-such-line/formula/check", json={"formula": "1"}, headers=admin_headers).status_code == 404
    finally:
        client.delete(f"/api/v1/lines/{line_id}", headers=admin_headers)
        client.delete(f"/api/v1/products/lists/{list_id}", headers=admin_headers)


# ── Writing the value ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("extra, want", [
    ({"value_source": "product_field", "value_field": "lane"}, 3.0),
    ({"value_source": "product_field", "value_field": "fragile"}, 1.0),
    ({"value_source": "formula", "formula": 'if(result == "good", product.lane, 9)'}, 3.0),
    ({"value_source": "formula", "formula": "good_count / 2"}, 3.5),
    ({"value_source": "product_field", "value_field": "recipe"}, "R-12"),
    ({"value_source": "code"}, "J-1"),
    ({"value_source": "product_name"}, "Widget"),
    ({"value_source": "formula", "formula": 'product.recipe + "/" + code'}, "R-12/J-1"),
])
def test_each_new_source_writes_its_value(extra, want):
    value = PLCDispatcherService.write_value(_card("w1", "DB1.X", operation="WRITE", **extra), EVENT)
    assert value == want and type(value) is type(want)


def _dispatch(card, event):
    state = _CardState(card["id"])
    asyncio.run(PLCDispatcherService._dispatch(card, state, event))
    return state


def test_a_missing_value_skips_the_write_without_an_alarm(plc):  # noqa: F811
    _, driver = plc
    alarm_manager.reset()
    for extra, name in (({"value_source": "product_field", "value_field": "lane"}, "product.lane"),
                        ({"value_source": "code"}, "the code"), ({"value_source": "product_name"}, "the product name"),
                        ({"value_source": "formula", "formula": "product.lane * 2"}, "the formula")):
        state = _dispatch(_card("w1", "DB1.Lane", operation="WRITE", **extra), {**EVENT, "product": None, "qr_code": None})
        assert state.status == "skipped" and state.last_result["skipped"] is True
        assert state.last_result["message"] == f"Skipped: no value for {name}"
    assert driver.ops == [] and alarm_manager.active() == []


def test_if_missing_default_writes_the_default(plc):  # noqa: F811
    _, driver = plc
    card = _card("w1", "DB1.Lane", operation="WRITE", value_source="product_field", value_field="lane",
                 if_missing="default", default_value=0)
    assert _dispatch(card, {**EVENT, "product": None}).status == "sent"
    assert driver.ops == [("WRITE", "DB1.Lane", 0.0)]


def test_an_unread_input_is_missing(plc, monkeypatch):  # noqa: F811
    _, driver = plc
    card = _card("w1", "DB1.Speed", operation="WRITE", value_source="input", value_input="speed")
    monkeypatch.setattr(PLCInputService, "values", classmethod(lambda cls, line_id: {}))
    assert _dispatch(card, EVENT).last_result["message"] == "Skipped: no value for input.speed"
    monkeypatch.setattr(PLCInputService, "values", classmethod(lambda cls, line_id: {"speed": 12.5}))
    assert _dispatch(card, EVENT).status == "sent" and driver.ops == [("WRITE", "DB1.Speed", 12.5)]


def test_text_goes_only_to_a_controller_that_writes_text(plc):  # noqa: F811
    _, driver = plc
    card = _card("w1", "DB1.Recipe", operation="WRITE", value_source="code", on_failure="retry", retry_attempts=2)
    state = _dispatch(card, EVENT)
    assert driver.ops == [] and state.status == "failed" and "writes numbers only" in state.last_result["message"]
    assert state.last_result["attempts"] == 1
    driver.writes_text = True
    assert _dispatch(card, EVENT).status == "sent"
    assert driver.ops == [("WRITE", "DB1.Recipe", "J-1")]


def test_the_existing_sources_fail_as_before(plc):  # noqa: F811
    _, driver = plc
    state = _dispatch(_card("w1", "DB1.Batch", operation="WRITE", value_source="batch"), {**EVENT, "batch": "A-1"})
    assert state.status == "failed" and "not a number" in state.last_result["message"] and driver.ops == []


def test_the_test_button_can_use_a_code(plc, monkeypatch):  # noqa: F811
    _, driver = plc
    runtime = types.SimpleNamespace(cameras=[{"camera_id": "vis", "role": "vision"},
                                             {"camera_id": "qr", "role": "qr", "product_list_id": "list-j"}],
                                    name="Juice", batch="", counter=types.SimpleNamespace(good_count=0, rejected_count=0))
    monkeypatch.setattr(line_module.line_manager, "get", lambda line_id: runtime)
    monkeypatch.setattr(product_catalog, "_lists", {"list-j": {"J-1": {"code": "J-1", "name": "Widget", "data": {"lane": 3}}}})
    monkeypatch.setattr(product_catalog, "_names", {"list-j": "Juices"})
    card = _card("w1", "DB1.Lane", operation="WRITE", value_source="product_field", value_field="lane")
    result = asyncio.run(PLCDispatcherService.dispatch_manual(card, code="J-1"))
    assert result["status"] == "sent" and driver.ops == [("WRITE", "DB1.Lane", 3.0)]
    result = asyncio.run(PLCDispatcherService.dispatch_manual(card, code="NOPE"))
    assert result["status"] == "skipped"
    result = asyncio.run(PLCDispatcherService.dispatch_manual(card))
    assert result["status"] == "skipped"


def test_the_test_route_hands_the_card_its_line_and_code(client, admin_headers, monkeypatch):
    seen = {}

    async def fake(cls, card, code=None):
        seen.update(card=card, code=code)
        return {"success": True, "message": "ok", "status": "sent"}

    monkeypatch.setattr(PLCDispatcherService, "dispatch_manual", classmethod(fake))
    line = client.post("/api/v1/lines", json={"name": "Test route line"}, headers=admin_headers)
    assert line.status_code in (200, 201), line.text
    line_id = line.json()["id"]
    card = {"id": "route_card", "name": "Sort", "operation": "WRITE", "value_source": "code", "plc_endpoint_id": "ep-x",
            "target_address": "DB1.Code"}
    try:
        res = client.post(f"/api/v1/plc/actions/route_card/test?line_id={line_id}",
                          json={"confirm": True, "card": card, "code": "J-1"}, headers=admin_headers)
        assert res.status_code == 200, res.text
        assert seen["card"]["line_id"] == line_id and seen["code"] == "J-1"
    finally:
        client.delete(f"/api/v1/lines/{line_id}", headers=admin_headers)


def test_a_formula_that_cannot_be_worked_out_skips(plc, monkeypatch):  # noqa: F811
    _, driver = plc
    monkeypatch.setattr(PLCInputService, "values", classmethod(lambda cls, line_id: {"speed": float("inf")}))
    state = _dispatch(_card("w1", "DB1.Speed", operation="WRITE", value_source="formula", formula="round(input.speed)"), EVENT)
    assert state.status == "skipped" and driver.ops == []
