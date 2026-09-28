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
