"""GET /api/v1/system/usage: the machine's usage for the dashboard's top bar."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from app.security.api_key_scopes import required_api_key_scopes
from app.services import system_usage

API = "/api/v1"
READING = {
    "taken_at": "2026-10-25T13:02:44+00:00",
    "cpu": {"percent": 23.5, "per_core": [20.0, 27.0], "cores_logical": 2, "cores_physical": 1, "freq_mhz": None,
            "freq_max_mhz": None, "load_avg": None, "temp_c": None},
    "memory": {"percent": 50.0, "used": 1, "total": 2, "available": 1, "swap_percent": 0.0, "swap_used": 0, "swap_total": 0},
    "disks": [], "gpus": [], "server": {"cpu_percent": 1.0, "memory_rss": 1, "memory_percent": 0.1},
}


@pytest.fixture(autouse=True)
def fixed_reading(monkeypatch):
    monkeypatch.setattr(system_usage.usage_sampler, "latest", lambda wait=3.0: READING)


def _level1(client, admin_headers):
    name = f"usage1-{uuid.uuid4().hex[:6]}"
    made = client.post(f"{API}/auth/users", headers=admin_headers, json={
        "username": name, "password": "secret-pw-123", "role": "operator", "clearance_level": 1})
    assert made.status_code in (200, 201), made.text
    login = client.post(f"{API}/auth/login", json={"username": name, "password": "secret-pw-123"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _key(client, admin_headers, scopes):
    me = client.get(f"{API}/auth/me", headers=admin_headers).json()
    created = client.post(f"{API}/auth/api-keys", headers=admin_headers, json={
        "user_id": me["id"], "name": f"usage {uuid.uuid4().hex[:4]}", "scopes": scopes, "expires_in_days": 1})
    assert created.status_code == 201, created.text
    return {"X-API-Key": created.json()["api_key"]}


def test_signed_out_is_401(client):
    assert client.get(f"{API}/system/usage").status_code == 401


def test_level1_gets_the_reading(client, admin_headers):
    res = client.get(f"{API}/system/usage", headers=_level1(client, admin_headers))
    assert res.status_code == 200, res.text
    assert set(res.json()) == {"taken_at", "cpu", "memory", "disks", "gpus", "server"}


def test_api_key_needs_monitor_read(client, admin_headers):
    assert client.get(f"{API}/system/usage", headers=_key(client, admin_headers, ["monitor:read"])).status_code == 200
    assert client.get(f"{API}/system/usage", headers=_key(client, admin_headers, ["configuration:read"])).status_code == 403


def test_no_reading_yet_is_503(client, admin_headers, monkeypatch):
    monkeypatch.setattr(system_usage.usage_sampler, "latest", lambda wait=3.0: {})
    res = client.get(f"{API}/system/usage", headers=admin_headers)
    assert res.status_code == 503 and res.json()["detail"] == "No usage reading yet"


def test_scope_rule():
    assert required_api_key_scopes("GET", "/api/v1/system/usage") == {"monitor:read"}


def test_requirements_list_nvidia_ml_py():
    requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^nvidia-ml-py>=12\.535\b", requirements, re.M)
