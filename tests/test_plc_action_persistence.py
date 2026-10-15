"""Regression coverage for persisted PLC action cards.

No test in this module contacts a PLC; the endpoint is just configuration data.
"""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import app.services.settings_persistence_service as persistence_module
from app.routes.v1.plc import save_plc_actions_batch
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService


@pytest.fixture
def isolated_persistence(monkeypatch, tmp_path):
    state_file = tmp_path / "system_state.json"
    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(state_file))
    monkeypatch.setattr(SettingsPersistenceService, "_state", copy.deepcopy(DEFAULT_STATE))
    monkeypatch.setattr(SettingsPersistenceService, "_recent_changes", [])
    monkeypatch.setattr(PLCDispatcherService, "_cards", [])
    monkeypatch.setattr(PLCDispatcherService, "_states", {})
    return state_file


def _card() -> dict:
    return {
        "id": "tag-1-toggle",
        "name": "Toggle Tag_1 on rejected crossing",
        "enabled": True,
        "plc_endpoint_id": "plc-opcua",
        "trigger": "cross_line",
        "condition": "reject",
        "target_type": "opcua_node_bool",
        "target_address": 'ns=3;s="Tag_1"',
        "operation": "toggle",
    }


def test_replace_plc_actions_is_persisted_and_detached(isolated_persistence):
    card = _card()
    saved = SettingsPersistenceService.replace_plc_actions([card])
    SettingsPersistenceService.save()

    # Mutating either caller-owned object must not alter the saved configuration.
    card["target_address"] = "not-the-saved-node"
    saved[0]["operation"] = "set"

    written = json.loads(isolated_persistence.read_text(encoding="utf-8"))
    assert written["plc_actions"][0]["target_address"] == 'ns=3;s="Tag_1"'
    assert written["plc_actions"][0]["operation"] == "toggle"


def test_batch_save_survives_service_reload_and_hot_loads_dispatcher(isolated_persistence):
    user = SimpleNamespace(username="tester", role="supervisor", clearance_level=2)
    result = asyncio.run(save_plc_actions_batch([_card()], user=user))

    assert result == {"saved": 1}
    assert [card["id"] for card in PLCDispatcherService._cards] == ["tag-1-toggle"]

    # Simulate the next FastAPI process reading the on-disk state.
    SettingsPersistenceService._state = {}
    PLCDispatcherService._cards = []
    SettingsPersistenceService.load()
    PLCDispatcherService.load_cards()

    assert SettingsPersistenceService.get_plc_actions()[0]["target_address"] == 'ns=3;s="Tag_1"'
    assert [card["id"] for card in PLCDispatcherService._cards] == ["tag-1-toggle"]


def test_empty_canonical_list_does_not_resurrect_legacy_cards(isolated_persistence):
    SettingsPersistenceService._state["plc_actions"] = []
    SettingsPersistenceService._state["action_trigger"]["plc_actions"] = [_card()]

    assert SettingsPersistenceService.get_plc_actions() == []


# The dashboard puts a card's id into its pages; an id that closes a quote
# there could run as script in another user's browser (security audit 2026-10-14).
SCRIPT_IN_ID = "x');window.__auditXss=1;//"


@pytest.mark.parametrize("bad_id", [SCRIPT_IN_ID, "it's", 'say"hi', 'a"onmouseover="x', "a<b>", "a/b", "a b", "-flag", "x" * 65, ["list"]])
def test_an_output_action_id_outside_letters_digits_and_dot_colon_dash_underscore_is_refused(isolated_persistence, bad_id):
    SettingsPersistenceService.replace_plc_actions([_card()])

    with pytest.raises(ValueError, match="id"):
        SettingsPersistenceService.replace_plc_actions([_card(), {**_card(), "id": bad_id}])

    assert [card["id"] for card in SettingsPersistenceService.get_plc_actions()] == ["tag-1-toggle"]


def test_an_other_lines_output_action_id_is_checked_too(isolated_persistence):
    line = SettingsPersistenceService.save_line({"name": "Line 2", "cameras": []})

    with pytest.raises(ValueError, match="id"):
        SettingsPersistenceService.replace_line_plc_actions(line["id"], [{**_card(), "id": SCRIPT_IN_ID}])

    assert SettingsPersistenceService.get_line_plc_actions(line["id"]) == []


def test_the_batch_route_answers_422_for_a_card_id_with_a_quote(isolated_persistence):
    from fastapi import HTTPException

    user = SimpleNamespace(username="tester", role="supervisor", clearance_level=2)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(save_plc_actions_batch([{**_card(), "id": SCRIPT_IN_ID}], user=user))

    assert refused.value.status_code == 422
    assert PLCDispatcherService._cards == []


@pytest.mark.parametrize("good_id", ["plc_act_k3j9x0a", "tag-1-toggle", "Line2.reject:A", "7"])
def test_ids_the_dashboard_and_older_versions_make_are_still_saved(isolated_persistence, good_id):
    saved = SettingsPersistenceService.replace_plc_actions([{**_card(), "id": good_id}])

    assert [card["id"] for card in saved] == [good_id]
