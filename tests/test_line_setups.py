"""Saved line setups: a line's setup, its connections and its cameras' image settings in a file on the server
(data/line-setups/<line id>/<save id>.json), listed per line, loaded back onto the stopped line, deleted."""
from __future__ import annotations

import json
import time

import pytest

from app.security.api_key_scopes import required_api_key_scopes
from app.security.config_lock import blocks_when_locked
from app.services.line_config import SCHEMA_VERSION
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_industrial_camera import camera_row  # noqa: F401  (a fixture industrial uses)
from tests.test_trigger_mode_from_card import _card, _mode, industrial, save_line  # noqa: F401  (fixtures)

API = "/api/v1"


@pytest.fixture
def new_line(client, admin_headers):
    created = []

    def make(name, **extra):
        res = client.post(f"{API}/lines", json={"name": name, **extra}, headers=admin_headers)
        assert res.status_code == 201, res.text
        created.append(res.json()["id"])
        return res.json()

    yield make
    for line_id in created:
        client.post(f"{API}/lines/{line_id}/stop", headers=admin_headers)
        client.delete(f"{API}/lines/{line_id}", headers=admin_headers)


@pytest.fixture
def endpoints(client, admin_headers):
    """Connections made by the test, removed afterwards (once no line uses them)."""
    made = []

    def make(body):
        res = client.post(f"{API}/system/endpoints", json=body, headers=admin_headers)
        assert res.status_code == 200, res.text
        made.append(res.json()["id"])
        return res.json()["id"]

    yield make
    for endpoint_id in made:
        client.delete(f"{API}/system/endpoints/{endpoint_id}", headers=admin_headers)


def _save(client, headers, line_id, name="Product A"):
    res = client.post(f"{API}/lines/{line_id}/setups", json={"name": name}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def _files(data_dir, line_id):
    return sorted((data_dir / "line-setups" / line_id).glob("setup-*.json"))


def _audit(client, headers, action):
    return [e for e in client.get(f"{API}/system/audit-logs?limit=50", headers=headers).json() if e.get("action") == action]


def test_a_save_holds_the_line_its_connections_and_camera_settings(client, admin_headers, data_dir, new_line, endpoints):
    hook = endpoints({"name": "MES hook", "protocol": "webhook", "url": "https://mes.example/hook",
                      "headers": "Authorization: Bearer s3cret"})
    cam = endpoints({"name": "Belt cam", "protocol": "ipcam", "camera_kind": "ip", "source": "http://10.0.0.5/video"})
    roi = {"roi_enabled": True, "roi_x": 0.1, "roi_y": 0.1, "roi_w": 0.5, "roi_h": 0.5}
    assert client.patch(f"{API}/cameras/{cam}", json={"settings": roi}, headers=admin_headers).status_code == 200
    line = new_line("Packing", cameras=[{"camera_id": cam, "role": "qr", "qr_trigger": "signal"}],
                    send_actions=[{"id": "send_mes", "name": "To MES", "endpoint_id": hook}])

    res = client.post(f"{API}/lines/{line['id']}/setups", json={"name": "  Product A  "}, headers=admin_headers)
    assert res.status_code == 201, res.text
    assert res.json()["name"] == "Product A" and res.json()["line_name"] == "Packing"
    [path] = _files(data_dir, line["id"])
    save = json.loads(path.read_text(encoding="utf-8"))
    assert path.stem == save["id"] == res.json()["id"]
    assert (save["kind"], save["format"], save["schema_version"]) == ("line-setup", 1, SCHEMA_VERSION)
    assert save["line_id"] == line["id"] and save["saved_by"] == "admin"
    assert save["line"]["send_actions"][0]["endpoint_id"] == hook
    for live in ("enabled", "batch", "counts_reset_at", "status", "warnings"):
        assert live not in save["line"]
    assert {e["id"] for e in save["endpoints"]} == {hook, cam}
    assert next(e for e in save["endpoints"] if e["id"] == hook)["headers"] == "Authorization: Bearer s3cret"
    [camera] = save["cameras"]
    assert camera["id"] == cam and camera["name"] == "Belt cam" and camera["settings"]["roi_w"] == 0.5
    assert _audit(client, admin_headers, "SAVE_LINE_SETUP")


def test_the_list_is_this_lines_newest_first_without_contents(client, admin_headers, data_dir, new_line):
    a = new_line("Line A")
    b = new_line("Line B")
    first = _save(client, admin_headers, a["id"], "First")
    time.sleep(0.01)
    second = _save(client, admin_headers, a["id"], "Second")
    _save(client, admin_headers, b["id"], "Other")
    listed = client.get(f"{API}/lines/{a['id']}/setups", headers=admin_headers).json()
    assert [s["id"] for s in listed] == [second["id"], first["id"]]
    assert all(set(s) == {"id", "name", "line_name", "saved_at", "saved_by"} for s in listed)
    assert client.get(f"{API}/lines/line-none/setups", headers=admin_headers).status_code == 404


def test_a_name_must_be_1_to_64_characters(client, admin_headers, data_dir, new_line):
    line = new_line("Named")
    for bad in ("", "   ", "x" * 65):
        assert client.post(f"{API}/lines/{line['id']}/setups", json={"name": bad}, headers=admin_headers).status_code == 422
    assert _save(client, admin_headers, line["id"], "x" * 64)["name"] == "x" * 64


def test_a_bad_setup_id_is_refused(client, admin_headers, data_dir, new_line):
    line = new_line("Guarded")
    _save(client, admin_headers, line["id"])
    (data_dir / "system_state.json").write_text("{}", encoding="utf-8")
    for bad in ("..%2F..%2Fsystem_state", "setup-XYZ", "setup-1a2b3c4d"):
        assert client.delete(f"{API}/lines/{line['id']}/setups/{bad}", headers=admin_headers).status_code == 404
    assert (data_dir / "system_state.json").exists() and len(_files(data_dir, line["id"])) == 1


def test_delete_removes_the_save(client, admin_headers, data_dir, new_line):
    line = new_line("Tidy")
    saved = _save(client, admin_headers, line["id"])
    res = client.delete(f"{API}/lines/{line['id']}/setups/{saved['id']}", headers=admin_headers)
    assert res.status_code == 200 and res.json()["id"] == saved["id"]
    assert _files(data_dir, line["id"]) == []
    assert client.get(f"{API}/lines/{line['id']}/setups", headers=admin_headers).json() == []
    assert _audit(client, admin_headers, "DELETE_LINE_SETUP")


def test_api_keys_and_the_lock():
    assert required_api_key_scopes("GET", "/api/v1/lines/line-2/setups") == {"monitor:read"}
    assert required_api_key_scopes("POST", "/api/v1/lines/line-2/setups") is None
    assert required_api_key_scopes("POST", "/api/v1/lines/line-2/setups/setup-1a2b3c4d/load") is None
    assert required_api_key_scopes("DELETE", "/api/v1/lines/line-2/setups/setup-1a2b3c4d") is None
    assert not blocks_when_locked("POST", "/api/v1/lines/{line_id}/setups")
    assert blocks_when_locked("POST", "/api/v1/lines/{line_id}/setups/{setup_id}/load")
    assert blocks_when_locked("DELETE", "/api/v1/lines/{line_id}/setups/{setup_id}")


# ── Load ──────────────────────────────────────────────────────────────────────


def _packing(client, headers, endpoints, new_line, name, source, camera_on_send=False):
    """A line with an IP camera reading codes and a send card to a webhook. Gives (hook, cam, line)."""
    hook = endpoints({"name": f"{name} hook", "protocol": "webhook", "url": "https://mes.example/hook",
                      "headers": "Authorization: Bearer s3cret"})
    cam = endpoints({"name": "Belt cam", "protocol": "ipcam", "camera_kind": "ip", "source": source})
    roi = {"roi_enabled": True, "roi_x": 0.1, "roi_y": 0.1, "roi_w": 0.5, "roi_h": 0.5}
    assert client.patch(f"{API}/cameras/{cam}", json={"settings": roi}, headers=headers).status_code == 200
    card = {"id": "send_mes", "name": "To MES", "endpoint_id": hook, **({"camera_id": cam} if camera_on_send else {})}
    line = new_line(name, cameras=[{"camera_id": cam, "role": "qr", "qr_trigger": "signal", "qr_hold_ms": 2000}],
                    send_actions=[card])
    return hook, cam, line


def _load(client, headers, line_id, setup_id):
    return client.post(f"{API}/lines/{line_id}/setups/{setup_id}/load", headers=headers)


def _endpoint(endpoint_id):
    return next((e for e in SettingsPersistenceService.get_endpoints() if e.get("id") == endpoint_id), None)


def _roi_w(client, headers, cam):
    return client.get(f"{API}/cameras/{cam}", headers=headers).json()["settings"].get("roi_w")


def test_load_puts_the_setup_back(client, admin_headers, data_dir, endpoints, new_line):
    hook, cam, line = _packing(client, admin_headers, endpoints, new_line, "Packing", "http://10.0.1.1/video")
    base = f"{API}/lines/{line['id']}"
    saved = _save(client, admin_headers, line["id"])
    changed = client.put(base, json={"name": "Packing 2", "yield_target": 80, "send_actions": [],
                                     "cameras": [{"camera_id": cam, "role": "qr", "qr_trigger": "signal",
                                                  "qr_hold_ms": 500}]}, headers=admin_headers)
    assert changed.status_code == 200, changed.text
    assert client.put(f"{base}/batch", json={"batch": "WO-1"}, headers=admin_headers).status_code == 200
    assert client.patch(f"{API}/cameras/{cam}", json={"settings": {"roi_w": 0.3}}, headers=admin_headers).status_code == 200

    res = _load(client, admin_headers, line["id"], saved["id"])
    assert res.status_code == 200, res.text
    assert res.json()["loaded"] is True and res.json()["name"] == "Product A" and res.json()["warnings"] == []
    after = client.get(base, headers=admin_headers).json()
    assert after["name"] == "Packing" and after["yield_target"] == line["yield_target"]
    assert [c["id"] for c in after["send_actions"]] == ["send_mes"] and after["send_actions"][0]["endpoint_id"] == hook
    assert after["cameras"][0]["qr_hold_ms"] == 2000
    assert after["batch"] == "WO-1" and after["enabled"] is False
    assert _roi_w(client, admin_headers, cam) == 0.5
    assert res.json()["line"]["name"] == "Packing"


def test_load_is_refused_on_a_running_line(client, admin_headers, data_dir, new_line):
    line = new_line("Runner")
    saved = _save(client, admin_headers, line["id"])
    assert client.post(f"{API}/lines/{line['id']}/start", headers=admin_headers).status_code == 200
    res = _load(client, admin_headers, line["id"], saved["id"])
    assert res.status_code == 409 and res.json()["detail"] == "Stop the line to load a setup"
    assert client.get(f"{API}/lines/{line['id']}", headers=admin_headers).json()["enabled"] is True
    assert not _audit(client, admin_headers, "LOAD_LINE_SETUP") or all(
        "'Runner'" not in e.get("details", "") for e in _audit(client, admin_headers, "LOAD_LINE_SETUP"))


def test_load_refuses_a_newer_save(client, admin_headers, data_dir, new_line):
    line = new_line("Newer")
    saved = _save(client, admin_headers, line["id"])
    [path] = _files(data_dir, line["id"])
    save = json.loads(path.read_text(encoding="utf-8"))
    save["schema_version"] = SCHEMA_VERSION + 1
    save["line"]["name"] = "From the future"
    path.write_text(json.dumps(save), encoding="utf-8")
    res = _load(client, admin_headers, line["id"], saved["id"])
    assert res.status_code == 409 and res.json()["detail"] == "This setup was saved by a newer version of the server"
    assert client.get(f"{API}/lines/{line['id']}", headers=admin_headers).json()["name"] == "Newer"


def test_load_of_another_lines_save_is_404(client, admin_headers, data_dir, new_line):
    a = new_line("Own A")
    b = new_line("Own B")
    saved = _save(client, admin_headers, a["id"])
    assert _load(client, admin_headers, b["id"], saved["id"]).status_code == 404
    assert _load(client, admin_headers, a["id"], "setup-00000000").status_code == 404
    assert _load(client, admin_headers, "line-none", saved["id"]).status_code == 404
    assert client.get(f"{API}/lines/{b['id']}", headers=admin_headers).json()["name"] == "Own B"


def test_a_deleted_connection_is_added_back(client, admin_headers, data_dir, endpoints, new_line):
    hook, cam, line = _packing(client, admin_headers, endpoints, new_line, "Rebuilt", "http://10.0.1.2/video")
    saved = _save(client, admin_headers, line["id"])
    base = f"{API}/lines/{line['id']}"
    assert client.put(base, json={"cameras": [], "send_actions": []}, headers=admin_headers).status_code == 200
    for endpoint_id in (hook, cam):
        assert client.delete(f"{API}/system/endpoints/{endpoint_id}", headers=admin_headers).status_code == 200
    assert _endpoint(hook) is None and _endpoint(cam) is None

    res = _load(client, admin_headers, line["id"], saved["id"])
    assert res.status_code == 200, res.text
    assert res.json()["warnings"] == []
    assert _endpoint(hook)["headers"] == "Authorization: Bearer s3cret" and _endpoint(cam)["name"] == "Belt cam"
    after = client.get(base, headers=admin_headers).json()
    assert [c["camera_id"] for c in after["cameras"]] == [cam] and after["send_actions"][0]["endpoint_id"] == hook
    assert _roi_w(client, admin_headers, cam) == 0.5


def test_an_existing_connection_is_kept(client, admin_headers, data_dir, endpoints, new_line):
    hook, cam, line = _packing(client, admin_headers, endpoints, new_line, "Kept", "http://10.0.1.3/video")
    saved = _save(client, admin_headers, line["id"])
    changed = client.put(f"{API}/system/endpoints/{hook}", json={"name": "Kept hook", "protocol": "webhook",
                                                                  "url": "https://mes.example/other"},
                         headers=admin_headers)
    assert changed.status_code == 200, changed.text
    assert _load(client, admin_headers, line["id"], saved["id"]).status_code == 200
    assert _endpoint(hook)["url"] == "https://mes.example/other"


def test_a_camera_on_another_line_is_left_out(client, admin_headers, data_dir, endpoints, new_line):
    hook, cam, line = _packing(client, admin_headers, endpoints, new_line, "Lender", "http://10.0.1.4/video",
                               camera_on_send=True)
    saved = _save(client, admin_headers, line["id"])
    base = f"{API}/lines/{line['id']}"
    moved = client.put(base, json={"cameras": [], "send_actions": [{"id": "send_mes", "name": "To MES",
                                                                   "endpoint_id": hook}]}, headers=admin_headers)
    assert moved.status_code == 200, moved.text
    other = new_line("Borrower", cameras=[{"camera_id": cam, "role": "qr", "qr_trigger": "signal"}])

    res = _load(client, admin_headers, line["id"], saved["id"])
    assert res.status_code == 200, res.text
    assert res.json()["warnings"] == ["Camera Belt cam is on Borrower now: left out"]
    assert [c["camera_id"] for c in client.get(f"{API}/lines/{other['id']}", headers=admin_headers).json()["cameras"]] == [cam]
    after = client.get(base, headers=admin_headers).json()
    assert after["cameras"] == [] and not after["send_actions"][0].get("camera_id")


def test_a_trigger_the_card_controls_is_kept(client, admin_headers, data_dir, industrial, save_line):
    row, _driver = industrial("off")
    made = save_line(_card(row["id"], picture_source="system"))
    assert made.status_code == 201, made.text
    line_id = made.json()["id"]
    assert _mode(client, admin_headers, row["id"]) == "software"
    saved = _save(client, admin_headers, line_id)
    [path] = _files(data_dir, line_id)
    save = json.loads(path.read_text(encoding="utf-8"))
    save["cameras"][0]["settings"]["trigger_mode"] = "hardware"
    path.write_text(json.dumps(save), encoding="utf-8")
    # The card changed since: the camera is on Hardware now, and the load sets Software back.
    assert save_line(_card(row["id"], picture_source="camera"), line_id=line_id).status_code == 200
    assert _mode(client, admin_headers, row["id"]) == "hardware"

    res = _load(client, admin_headers, line_id, saved["id"])
    assert res.status_code == 200, res.text
    assert _mode(client, admin_headers, row["id"]) == "software"


def test_load_is_in_the_audit_log(client, admin_headers, data_dir, new_line):
    line = new_line("Ledger")
    saved = _save(client, admin_headers, line["id"])
    assert _load(client, admin_headers, line["id"], saved["id"]).status_code == 200
    details = [e.get("details") for e in _audit(client, admin_headers, "LOAD_LINE_SETUP")]
    assert "Loaded setup 'Product A' onto line 'Ledger'" in details
