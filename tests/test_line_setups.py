"""Saved line setups: a line's setup, its connections and its cameras' image settings in a file on the server
(data/line-setups/<line id>/<save id>.json), listed per line, loaded back onto the stopped line, deleted."""
from __future__ import annotations

import json
import time

import pytest

from app.security.api_key_scopes import required_api_key_scopes
from app.security.config_lock import blocks_when_locked
from app.services.line_config import SCHEMA_VERSION

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
