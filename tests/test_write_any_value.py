"""Write any value: an output action writes a product field, the code, the product name, an input's value
or a formula; with no value it skips the write or writes a default; text goes to OPC UA String tags only.
"""
from __future__ import annotations

import pytest

from app.services.plc_failsafe_service import validate_write_value
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_send_cards import lines  # noqa: F401  (a fixture)

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
