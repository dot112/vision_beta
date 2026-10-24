"""The configuration lock over HTTP: the lock routes, and the server refusing locked changes."""
from __future__ import annotations

import copy
import json
import uuid

import pytest

from app.security.config_lock import LOCKED_MESSAGE
from app.services.config_lock_service import ConfigLockService
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService

API = "/api/v1"


def _reset_lock():
    SettingsPersistenceService.get_state()
    SettingsPersistenceService._state["config_lock"] = copy.deepcopy(DEFAULT_STATE["config_lock"])
    SettingsPersistenceService.save()
    ConfigLockService.reset_attempts()


@pytest.fixture(autouse=True)
def unlocked():
    _reset_lock()
    yield
    _reset_lock()


def _user_headers(client, admin_headers, level):
    name = f"lock{level}-{uuid.uuid4().hex[:6]}"
    role = {1: "operator", 2: "supervisor"}[level]
    made = client.post(f"{API}/auth/users", headers=admin_headers, json={
        "username": name, "password": "secret-pw-123", "role": role, "clearance_level": level})
    assert made.status_code in (200, 201), made.text
    login = client.post(f"{API}/auth/login", json={"username": name, "password": "secret-pw-123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture
def supervisor(client, admin_headers):
    return _user_headers(client, admin_headers, 2)


@pytest.fixture
def operator(client, admin_headers):
    return _user_headers(client, admin_headers, 1)


@pytest.fixture
def config_key(client, admin_headers):
    me = client.get(f"{API}/auth/me", headers=admin_headers).json()
    created = client.post(f"{API}/auth/api-keys", headers=admin_headers, json={
        "user_id": me["id"], "name": f"lock {uuid.uuid4().hex[:4]}",
        "scopes": ["monitor:read", "configuration:read", "configuration:write", "camera:control"], "expires_in_days": 1})
    assert created.status_code == 201, created.text
    return {"X-API-Key": created.json()["api_key"]}


# Locked changes, sent with bodies the routes refuse: unlocked they answer 4xx from the
# route itself (not 423) and change nothing.
LOCKED_SAMPLES = [
    ("PUT", "/lines/line-1", {"name": ""}),
    ("POST", "/cameras", {}),
    ("POST", "/products/lists", {}),
    ("POST", "/system/settings", []),
    ("POST", "/cameras/no-such-camera/connect", None),
]


def _send(client, method, path, headers, body=None):
    return client.request(method, API + path, headers=headers, json=body)


def test_locked_changes_answer_423_for_everyone(client, admin_headers, supervisor, config_key):
    client.post(f"{API}/system/lock", headers=admin_headers)
    for headers in (admin_headers, supervisor, config_key):
        for method, path, body in LOCKED_SAMPLES:
            res = _send(client, method, path, headers, body)
            assert res.status_code == 423, (method, path, res.status_code, res.text)
            assert res.json()["detail"] == LOCKED_MESSAGE
    client.post(f"{API}/system/unlock", headers=admin_headers)
    for method, path, body in LOCKED_SAMPLES:
        assert _send(client, method, path, admin_headers, body).status_code != 423, (method, path)


def test_running_the_line_works_while_locked(client, admin_headers, supervisor):
    line = client.post(f"{API}/lines", headers=admin_headers, json={"name": f"Lock run {uuid.uuid4().hex[:4]}"}).json()
    base = f"{API}/lines/{line['id']}"
    try:
        assert client.post(f"{API}/system/lock", headers=supervisor).json()["locked"] is True
        assert client.post(f"{base}/start", headers=supervisor).status_code == 200
        assert client.put(f"{base}/batch", headers=supervisor, json={"batch": "B-LOCK-1"}).status_code == 200
        assert client.post(f"{base}/stop", headers=supervisor).status_code == 200
        assert client.post(f"{API}/counting/reset?line_id={line['id']}", headers=supervisor).status_code == 200
        assert client.get(f"{API}/lines", headers=supervisor).status_code == 200
        assert client.delete(base, headers=admin_headers).status_code == 423
    finally:
        client.post(f"{API}/system/unlock", headers=admin_headers)
        client.delete(base, headers=admin_headers)


def test_unsigned_request_is_401_not_423(client, admin_headers):
    client.post(f"{API}/system/lock", headers=admin_headers)
    assert client.put(f"{API}/lines/line-1", json={}).status_code == 401


def test_lock_routes_by_level(client, admin_headers, supervisor, operator):
    assert client.post(f"{API}/system/lock", headers=operator).status_code == 403
    assert client.get(f"{API}/system/lock", headers=operator).json()["locked"] is False
    assert client.post(f"{API}/system/lock", headers=supervisor).json()["locked"] is True
    assert client.post(f"{API}/system/unlock", headers=operator, json={"code": "lock-123"}).status_code == 403
    assert client.put(f"{API}/system/lock/code", headers=supervisor, json={"code": "lock-123"}).status_code == 403
    assert client.put(f"{API}/system/lock/code", headers=admin_headers, json={"code": "lock-123"}).json()["code_set"] is True
    assert client.put(f"{API}/system/lock/code", headers=admin_headers, json={"code": "abc"}).status_code == 422
    wrong = client.post(f"{API}/system/unlock", headers=supervisor, json={"code": "lock-124"})
    assert (wrong.status_code, wrong.json()["detail"]) == (403, "Wrong lock code.")
    assert client.post(f"{API}/system/unlock", headers=supervisor, json={"code": "lock-123"}).json()["locked"] is False
    assert client.delete(f"{API}/system/lock/code", headers=supervisor).status_code == 403
    assert client.delete(f"{API}/system/lock/code", headers=admin_headers).json()["code_set"] is False


def test_level2_unlock_without_a_code_set(client, admin_headers, supervisor):
    client.post(f"{API}/system/lock", headers=admin_headers)
    for _ in range(6):
        res = client.post(f"{API}/system/unlock", headers=supervisor, json={"code": "whatever"})
        assert (res.status_code, res.json()["detail"]) == (403, "No lock code is set: only an Admin can unlock.")
    assert client.get(f"{API}/system/lock", headers=supervisor).json()["retry_after"] == 0


def test_admin_unlocks_without_a_code(client, admin_headers):
    client.put(f"{API}/system/lock/code", headers=admin_headers, json={"code": "lock-123"})
    client.post(f"{API}/system/lock", headers=admin_headers)
    assert client.post(f"{API}/system/unlock", headers=admin_headers).json()["locked"] is False


def test_too_many_wrong_codes_is_429_with_retry_after(client, admin_headers, supervisor):
    client.put(f"{API}/system/lock/code", headers=admin_headers, json={"code": "lock-123"})
    client.post(f"{API}/system/lock", headers=admin_headers)
    for _ in range(5):
        assert client.post(f"{API}/system/unlock", headers=supervisor, json={"code": "nope-00"}).status_code == 403
    waiting = client.post(f"{API}/system/unlock", headers=supervisor, json={"code": "lock-123"})
    assert waiting.status_code == 429 and 295 <= int(waiting.headers["Retry-After"]) <= 300
    assert client.get(f"{API}/system/lock", headers=supervisor).json()["locked"] is True


def test_api_keys_read_but_cannot_lock(client, config_key):
    assert client.get(f"{API}/system/lock", headers=config_key).status_code == 200
    assert client.post(f"{API}/system/lock", headers=config_key).status_code == 403
    assert client.post(f"{API}/system/unlock", headers=config_key, json={}).status_code == 403
    assert client.put(f"{API}/system/lock/code", headers=config_key, json={"code": "lock-123"}).status_code == 403


def test_poll_changes_carries_the_lock(client, admin_headers):
    client.put(f"{API}/system/lock/code", headers=admin_headers, json={"code": "lock-123"})
    client.post(f"{API}/system/lock", headers=admin_headers)
    first = client.get(f"{API}/system/poll-changes?since_version=0", headers=admin_headers).json()
    assert first["config_lock"]["locked"] is True and first["config_lock"]["changed_by"] == "admin"
    quiet = client.get(f"{API}/system/poll-changes?since_version={first['version']}", headers=admin_headers).json()
    assert quiet["has_changes"] is False and quiet["config_lock"]["locked"] is True
    for path in ("/system/settings", "/system/lock", f"/system/poll-changes?since_version=0"):
        text = json.dumps(client.get(API + path, headers=admin_headers).json())
        assert "code_hash" not in text and "lock-123" not in text, path
