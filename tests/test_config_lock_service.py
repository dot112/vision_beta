"""The configuration lock's state: who locks and unlocks, the lock code, the wrong-code wait."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from app.services.config_lock_service import ConfigLockService, LockError
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService

OPERATOR = SimpleNamespace(username="op", role="operator", clearance_level=1)
SUPERVISOR = SimpleNamespace(username="sup", role="supervisor", clearance_level=2)
ADMIN = SimpleNamespace(username="boss", role="admin", clearance_level=3)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ConfigLockService, "clock", lambda: now[0])

    def reset():
        SettingsPersistenceService.get_state()
        SettingsPersistenceService._state["config_lock"] = copy.deepcopy(DEFAULT_STATE["config_lock"])
        SettingsPersistenceService.save()
        ConfigLockService.reset_attempts()

    reset()
    yield now
    reset()


def _lock_actions():
    return [e["action"] for e in reversed(SettingsPersistenceService.get_audit_logs(limit=500))
            if e.get("category") == "Configuration lock"]


def _error(call, *args):
    with pytest.raises(LockError) as caught:
        call(*args)
    return caught.value


def test_starts_unlocked_without_a_code():
    assert ConfigLockService.status() == {"locked": False, "changed_by": None, "changed_at": None,
                                          "code_set": False, "retry_after": 0}
    assert ConfigLockService.is_locked() is False


def test_level2_locks_admin_unlocks_without_code():
    before = _lock_actions()
    status = ConfigLockService.lock(SUPERVISOR)
    assert status["locked"] is True and status["changed_by"] == "sup" and status["changed_at"]
    assert ConfigLockService.is_locked() is True
    assert ConfigLockService.unlock(ADMIN, None)["locked"] is False
    assert ConfigLockService.status()["changed_by"] == "boss"
    assert _lock_actions()[len(before):] == ["CONFIG_LOCKED", "CONFIG_UNLOCKED"]


def test_asking_for_the_state_it_is_in_writes_nothing():
    ConfigLockService.lock(ADMIN)
    before = _lock_actions()
    assert ConfigLockService.lock(SUPERVISOR)["changed_by"] == "boss"
    ConfigLockService.unlock(ADMIN, None)
    assert ConfigLockService.unlock(ADMIN, None)["locked"] is False
    assert _lock_actions()[len(before):] == ["CONFIG_UNLOCKED"]


def test_level1_cannot_lock_or_unlock():
    assert _error(ConfigLockService.lock, OPERATOR).status_code == 403
    ConfigLockService.lock(ADMIN)
    assert _error(ConfigLockService.unlock, OPERATOR, "anything").status_code == 403
    assert ConfigLockService.is_locked() is True


def test_level2_unlocks_only_with_the_right_code():
    ConfigLockService.set_code(ADMIN, "abc123")
    ConfigLockService.lock(ADMIN)
    wrong = _error(ConfigLockService.unlock, SUPERVISOR, "abc124")
    assert (wrong.status_code, wrong.detail) == (403, "Wrong lock code.")
    assert _error(ConfigLockService.unlock, SUPERVISOR, None).status_code == 403
    assert ConfigLockService.is_locked() is True
    assert ConfigLockService.unlock(SUPERVISOR, "abc123")["locked"] is False
    assert _lock_actions()[-1] == "CONFIG_UNLOCKED"


def test_level2_without_a_code_set_is_told_only_an_admin_can_unlock():
    ConfigLockService.lock(ADMIN)
    for _ in range(6):
        error = _error(ConfigLockService.unlock, SUPERVISOR, "whatever")
        assert (error.status_code, error.detail) == (403, "No lock code is set: only an Admin can unlock.")
    assert ConfigLockService.status()["retry_after"] == 0


def test_five_wrong_codes_wait_five_minutes(fresh):
    ConfigLockService.set_code(ADMIN, "abc123")
    ConfigLockService.lock(ADMIN)
    before = len(_lock_actions())
    for _ in range(5):
        assert _error(ConfigLockService.unlock, SUPERVISOR, "nope00").status_code == 403
    waiting = _error(ConfigLockService.unlock, SUPERVISOR, "abc123")
    assert waiting.status_code == 429 and waiting.retry_after == 300
    assert waiting.detail == "Too many wrong codes. Try again in 300 s."
    assert ConfigLockService.status()["retry_after"] == 300
    fresh[0] += 301
    assert ConfigLockService.status()["retry_after"] == 0
    assert ConfigLockService.unlock(SUPERVISOR, "abc123")["locked"] is False
    actions = _lock_actions()[before:]
    assert actions.count("UNLOCK_BLOCKED") == 1 and actions.count("UNLOCK_WRONG_CODE") == 5


def test_a_right_code_clears_the_wrong_count():
    ConfigLockService.set_code(ADMIN, "abc123")
    ConfigLockService.lock(ADMIN)
    for _ in range(4):
        _error(ConfigLockService.unlock, SUPERVISOR, "nope00")
    ConfigLockService.unlock(SUPERVISOR, "abc123")
    ConfigLockService.lock(ADMIN)
    for _ in range(4):
        _error(ConfigLockService.unlock, SUPERVISOR, "nope00")
    assert ConfigLockService.unlock(SUPERVISOR, "abc123")["locked"] is False


def test_admin_unlock_clears_the_wrong_count():
    ConfigLockService.set_code(ADMIN, "abc123")
    ConfigLockService.lock(ADMIN)
    for _ in range(5):
        _error(ConfigLockService.unlock, SUPERVISOR, "nope00")
    assert ConfigLockService.unlock(ADMIN, None)["retry_after"] == 0
    ConfigLockService.lock(ADMIN)
    assert ConfigLockService.unlock(SUPERVISOR, "abc123")["locked"] is False


def test_code_is_trimmed_and_checked():
    assert ConfigLockService.set_code(ADMIN, "  abc123  ")["code_set"] is True
    ConfigLockService.lock(ADMIN)
    assert ConfigLockService.unlock(SUPERVISOR, " abc123 ")["locked"] is False
    assert _error(ConfigLockService.set_code, ADMIN, "  ab   ").status_code == 422
    assert _error(ConfigLockService.set_code, ADMIN, "x" * 129).status_code == 422
    assert ConfigLockService.set_code(ADMIN, "x" * 128)["code_set"] is True
    assert _error(ConfigLockService.set_code, SUPERVISOR, "abcdef").status_code == 403


def test_code_set_and_removed_are_audited():
    before = _lock_actions()
    ConfigLockService.set_code(ADMIN, "abc123")
    status = ConfigLockService.remove_code(ADMIN)
    assert status["code_set"] is False
    assert _lock_actions()[len(before):] == ["LOCK_CODE_SET", "LOCK_CODE_REMOVED"]
    assert _error(ConfigLockService.remove_code, SUPERVISOR).status_code == 403
    ConfigLockService.lock(ADMIN)
    assert _error(ConfigLockService.unlock, SUPERVISOR, "abc123").detail == "No lock code is set: only an Admin can unlock."


def test_code_hash_never_leaves():
    ConfigLockService.set_code(ADMIN, "abc123")
    assert SettingsPersistenceService._state["config_lock"]["code_hash"]
    shown = json.dumps(SettingsPersistenceService.redact_secrets(SettingsPersistenceService.get_state()))
    assert "code_hash" not in shown and "abc123" not in shown
    assert "code_hash" not in ConfigLockService.status()


def test_settings_update_cannot_touch_the_lock():
    ConfigLockService.lock(ADMIN)
    SettingsPersistenceService.update_settings({"config_lock": {"locked": False}})
    assert ConfigLockService.is_locked() is True


def test_lock_survives_reload():
    ConfigLockService.set_code(ADMIN, "abc123")
    ConfigLockService.lock(ADMIN)
    _error(ConfigLockService.unlock, SUPERVISOR, "nope00")
    SettingsPersistenceService.load()
    ConfigLockService.reset_attempts()  # a restart starts the count again
    assert ConfigLockService.is_locked() is True
    assert ConfigLockService.status()["retry_after"] == 0
    assert ConfigLockService.unlock(SUPERVISOR, "abc123")["locked"] is False


def test_a_settings_file_without_the_lock_loads_unlocked():
    del SettingsPersistenceService._state["config_lock"]
    SettingsPersistenceService.save()
    SettingsPersistenceService.load()
    assert ConfigLockService.status() == {"locked": False, "changed_by": None, "changed_at": None,
                                          "code_set": False, "retry_after": 0}
