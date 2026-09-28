"""HTTP tests for the main /api/v1 endpoints.

The app starts with its real lifespan (migrations and admin seed) against a
temporary database; inference, cameras and PLC outputs are faked in conftest.
"""
from __future__ import annotations

import io

import pytest

API = "/api/v1"


# ── Public and auth ───────────────────────────────────────────────────────────

def test_discovery_is_public(client):
    res = client.get(f"{API}/discovery")
    assert res.status_code == 200
    assert res.json()["status"] == "online"


def test_protected_routes_require_auth(client):
    for path in ("/rules", "/actions", "/flows", "/cameras", "/system/settings", "/counting/stats"):
        assert client.get(API + path).status_code == 401, path


def test_bad_token_is_rejected(client):
    res = client.get(f"{API}/rules", headers={"Authorization": "Bearer not-a-token"})
    assert res.status_code == 401


def test_wrong_password_is_rejected(client):
    res = client.post(f"{API}/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert res.status_code == 401


def test_me_returns_admin_profile(client, admin_headers):
    res = client.get(f"{API}/auth/me", headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["username"] == "admin"
    assert res.json()["clearance_level"] == 3


def test_operator_cannot_create_rules(client, admin_headers):
    created = client.post(f"{API}/auth/users", headers=admin_headers, json={
        "username": "op_tester", "password": "operator-password-123456",
        "full_name": "Line Operator", "role": "operator", "clearance_level": 1,
    })
    assert created.status_code == 201, created.text
    login = client.post(f"{API}/auth/login", json={"username": "op_tester", "password": "operator-password-123456"})
    assert login.status_code == 200, login.text
    op_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get(f"{API}/rules", headers=op_headers).status_code == 200
    res = client.post(f"{API}/rules", headers=op_headers, json={
        "name": "nope", "condition_field": "defect_count", "operator": "gt", "threshold_value": "0",
    })
    assert res.status_code == 403


def test_api_key_scopes_are_enforced(client, admin_headers):
    me = client.get(f"{API}/auth/me", headers=admin_headers).json()
    created = client.post(f"{API}/auth/api-keys", headers=admin_headers, json={
        "user_id": me["id"], "name": "monitor only", "scopes": ["monitor:read"], "expires_in_days": 1,
    })
    assert created.status_code == 201, created.text
    key_headers = {"X-API-Key": created.json()["api_key"]}

    assert client.get(f"{API}/cameras", headers=key_headers).status_code == 200
    assert client.get(f"{API}/rules", headers=key_headers).status_code == 403
    assert client.get(f"{API}/auth/api-keys", headers=key_headers).status_code == 403


# ── Rules and actions ─────────────────────────────────────────────────────────

def test_action_and_rule_crud_and_evaluate(client, admin_headers):
    action = client.post(f"{API}/actions", headers=admin_headers, json={
        "name": "log reject", "action_type": "log", "target": "reject-bin",
    })
    assert action.status_code == 201, action.text
    action_id = action.json()["id"]

    rule = client.post(f"{API}/rules", headers=admin_headers, json={
        "name": "any defect", "condition_field": "defect_count", "operator": "gt",
        "threshold_value": "0", "action_id": action_id,
    })
    assert rule.status_code == 201, rule.text
    rule_id = rule.json()["id"]

    assert any(r["id"] == rule_id for r in client.get(f"{API}/rules", headers=admin_headers).json())
    assert client.get(f"{API}/rules/{rule_id}", headers=admin_headers).json()["name"] == "any defect"

    results = client.post(f"{API}/rules/evaluate", headers=admin_headers, json={"defect_count": 2}).json()
    mine = next(r for r in results if r["rule_id"] == rule_id)
    assert mine["matched"] is True
    assert mine["action_triggered"].startswith("Executed log reject")

    results = client.post(f"{API}/rules/evaluate", headers=admin_headers, json={"defect_count": 0}).json()
    assert next(r for r in results if r["rule_id"] == rule_id)["matched"] is False

    updated = client.put(f"{API}/rules/{rule_id}", headers=admin_headers, json={"threshold_value": "5"})
    assert updated.status_code == 200
    assert updated.json()["threshold_value"] == "5"

    executed = client.post(f"{API}/actions/{action_id}/execute", headers=admin_headers, json={"source": "test"})
    assert executed.status_code == 200
    assert executed.json()["success"] is True

    assert client.delete(f"{API}/rules/{rule_id}", headers=admin_headers).status_code == 200
    assert client.get(f"{API}/rules/{rule_id}", headers=admin_headers).status_code == 404
    assert client.delete(f"{API}/actions/{action_id}", headers=admin_headers).status_code == 200
    assert client.get(f"{API}/actions/{action_id}", headers=admin_headers).status_code == 404


def test_rule_rejects_unknown_operator(client, admin_headers):
    res = client.post(f"{API}/rules", headers=admin_headers, json={
        "name": "bad", "condition_field": "defect_count", "operator": "between", "threshold_value": "0",
    })
    assert res.status_code == 422


def test_webhook_action_refuses_unconfigured_url(client, admin_headers):
    action = client.post(f"{API}/actions", headers=admin_headers, json={
        "name": "exfil", "action_type": "webhook", "target": "http://example.invalid/hook",
    }).json()
    res = client.post(f"{API}/actions/{action['id']}/execute", headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["success"] is False
    client.delete(f"{API}/actions/{action['id']}", headers=admin_headers)


# ── Flows ─────────────────────────────────────────────────────────────────────

def _flow_body(**overrides):
    body = {
        "name": "reject on defect",
        "is_active": False,
        "nodes": [
            {"id": "trg", "type": "event_wireline", "config": {}},
            {"id": "out", "type": "modbus_out", "config": {"address": 1, "mode": "on"}},
        ],
        "links": [{"from_node": "trg", "from_port": "out:0", "to_node": "out"}],
    }
    body.update(overrides)
    return body


def test_flow_crud_deploy_and_test_inject(client, admin_headers, monkeypatch):
    from app.services import modbus_service

    writes = []

    async def fake_write_coil(address, value):
        writes.append((address, value))
        return True, None

    monkeypatch.setattr(modbus_service.ModbusService, "write_coil", staticmethod(fake_write_coil))

    created = client.post(f"{API}/flows", headers=admin_headers, json=_flow_body())
    assert created.status_code == 200, created.text
    flow_id = created.json()["id"]
    assert created.json()["is_active"] is False

    listed = client.get(f"{API}/flows", headers=admin_headers).json()
    assert any(f["id"] == flow_id for f in listed)

    renamed = client.put(f"{API}/flows/{flow_id}", headers=admin_headers, json={"name": "renamed"})
    assert renamed.json()["name"] == "renamed"

    assert client.post(f"{API}/flows/{flow_id}/deploy", headers=admin_headers).json()["is_active"] is True
    assert client.post(f"{API}/flows/{flow_id}/deploy", headers=admin_headers).json()["is_active"] is False

    injected = client.post(f"{API}/flows/{flow_id}/test", headers=admin_headers, json={"event_type": "wireline_cross"})
    assert injected.status_code == 200
    assert injected.json()["status"] == "injected"
    assert writes == [], "a test injection must never drive a real PLC output"

    assert client.delete(f"{API}/flows/{flow_id}", headers=admin_headers).status_code == 200
    assert client.get(f"{API}/flows/{flow_id}", headers=admin_headers).status_code == 404


def test_missing_flow_returns_404(client, admin_headers):
    assert client.post(f"{API}/flows/nope/deploy", headers=admin_headers).status_code == 404


# ── Cameras and vision ────────────────────────────────────────────────────────

def test_camera_crud(client, admin_headers):
    created = client.post(f"{API}/cameras", headers=admin_headers, json={
        "name": "Line 1", "type": "ip", "source": "rtsp://192.0.2.10/stream1", "settings": {},
    })
    assert created.status_code == 201, created.text
    cam_id = created.json()["id"]

    assert client.get(f"{API}/cameras/{cam_id}", headers=admin_headers).json()["name"] == "Line 1"
    patched = client.patch(f"{API}/cameras/{cam_id}", headers=admin_headers, json={"name": "Line 1 top"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "Line 1 top"

    status = client.get(f"{API}/cameras/{cam_id}/status", headers=admin_headers)
    assert status.status_code == 200
    assert status.json()["is_streaming"] is False

    assert client.delete(f"{API}/cameras/{cam_id}", headers=admin_headers).status_code == 200
    assert client.get(f"{API}/cameras/{cam_id}", headers=admin_headers).status_code == 404


def _png_bytes() -> bytes:
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".png", np.zeros((48, 64, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


def test_detect_image_uses_active_model_and_logs_result(client, admin_headers, fake_engine):
    res = client.post(
        f"{API}/vision/detect",
        headers=admin_headers,
        files={"file": ("part.png", io.BytesIO(_png_bytes()), "image/png")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total_detections"] == 1
    assert body["detections"][0]["class_name"] == "bottle"
    assert fake_engine.calls == 1

    history = client.get(f"{API}/vision/detections", headers=admin_headers, params={"limit": 5}).json()
    assert history and history[0]["total_detections"] == 1


def test_detect_rejects_non_image_upload(client, admin_headers, fake_engine):
    res = client.post(
        f"{API}/vision/detect",
        headers=admin_headers,
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert res.status_code == 415
    assert fake_engine.calls == 0


def test_detect_live_camera_with_fake_camera(client, admin_headers, fake_engine, fake_camera):
    created = client.post(f"{API}/cameras", headers=admin_headers, json={
        "name": "Fake", "type": "usb", "source": "0", "settings": {},
    }).json()
    fake_camera(created["id"])

    res = client.post(f"{API}/vision/detect/camera/{created['id']}", headers=admin_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["camera_id"] == created["id"]
    assert body["total_detections"] == 1
    assert fake_engine.calls == 1
    client.delete(f"{API}/cameras/{created['id']}", headers=admin_headers)


def test_stream_for_disconnected_camera_is_404(client, admin_headers):
    assert client.get(f"{API}/vision/stream/camera/missing", headers=admin_headers).status_code == 404


# ── System, counting and PLC ──────────────────────────────────────────────────

def test_system_settings_round_trip(client, admin_headers):
    res = client.get(f"{API}/system/settings", headers=admin_headers)
    assert res.status_code == 200
    assert "camera_auto_connect" in res.json()


def test_counting_stats_and_reset(client, admin_headers):
    assert client.get(f"{API}/counting/stats", headers=admin_headers).status_code == 200
    reset = client.post(f"{API}/counting/reset", headers=admin_headers)
    assert reset.status_code == 200


def test_plc_action_cards_list(client, admin_headers):
    res = client.get(f"{API}/plc/actions", headers=admin_headers)
    assert res.status_code == 200


# ── Dashboards ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/dashboard", "/dashboard/pro"])
def test_dashboards_are_served_to_local_clients(client, path):
    res = client.get(path)
    assert res.status_code == 200
    assert 'id="loginScreen"' in res.text
