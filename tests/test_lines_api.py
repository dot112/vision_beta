"""Version 2 API: /lines, /products, and line_id on the existing endpoints."""
from __future__ import annotations

import pytest

from app.security.api_key_scopes import required_api_key_scopes
from app.services.line_config import PRIMARY_LINE_ID


@pytest.fixture
def new_line(client, admin_headers):
    created = []

    def make(name, **extra):
        res = client.post("/api/v1/lines", json={"name": name, **extra}, headers=admin_headers)
        assert res.status_code == 201, res.text
        created.append(res.json()["id"])
        return res.json()

    yield make
    for line_id in created:
        client.delete(f"/api/v1/lines/{line_id}", headers=admin_headers)


def test_existing_endpoints_answer_for_line1_when_no_line_is_named(client, admin_headers):
    for path in ("/api/v1/counting/stats", "/api/v1/counting/tracks", "/api/v1/plc/actions"):
        plain = client.get(path, headers=admin_headers)
        named = client.get(f"{path}?line_id={PRIMARY_LINE_ID}", headers=admin_headers)
        assert plain.status_code == 200, (path, plain.text)
        assert plain.json() == named.json(), path
    stats = client.get("/api/v1/counting/stats", headers=admin_headers).json()
    for key in ("total_inspected", "good_count", "rejected_count", "defect_ppm", "counts_by_class", "config"):
        assert key in stats
    alarms = client.get("/api/v1/alarms", headers=admin_headers).json()
    assert set(alarms) == {"alarms", "counts"}
    health = client.get("/api/v1/telemetry/health", headers=admin_headers).json()
    assert {"database", "cameras", "inference", "plc", "mqtt", "lines"} <= set(health["components"])
    assert client.get("/api/v1/counting/stats?line_id=nope", headers=admin_headers).status_code == 404
    assert client.get("/api/v1/plc/actions?line_id=nope", headers=admin_headers).status_code == 404


def test_line_crud_start_stop_and_clone(client, admin_headers, new_line):
    line = new_line("Packing 2", cameras=[{"camera_id": "cam-api-1"}, {"camera_id": "cam-api-2", "role": "qr"}],
                    sync={"enabled": True, "window_ms": 300})
    assert line["enabled"] is False and line["status"]["state"] == "stopped"
    assert line["sync"] == {"enabled": True, "window_ms": 300}

    listed = client.get("/api/v1/lines", headers=admin_headers).json()
    assert [ln["id"] for ln in listed["lines"]][0] == PRIMARY_LINE_ID
    assert line["id"] in [ln["id"] for ln in listed["lines"]]
    overview = client.get("/api/v1/lines/overview", headers=admin_headers).json()["lines"]
    assert any(row["id"] == line["id"] and row["has_qr"] for row in overview)

    # Sync needs a vision camera and a QR reader.
    bad = client.put(f"/api/v1/lines/{line['id']}", json={"cameras": [{"camera_id": "cam-api-1"}]}, headers=admin_headers)
    assert bad.status_code == 422
    renamed = client.put(f"/api/v1/lines/{line['id']}", json={"name": "Packing 2B"}, headers=admin_headers)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Packing 2B"

    # A camera belongs to one line only.
    clash = client.post("/api/v1/lines", json={"name": "Other", "cameras": [{"camera_id": "cam-api-1"}]}, headers=admin_headers)
    assert clash.status_code == 422

    card = {"id": "gate", "name": "Gate", "plc_endpoint_id": "", "trigger": "cross_line", "condition": "reject",
            "operation": "pulse", "target_address": "0", "camera_id": "cam-api-1"}
    res = client.post(f"/api/v1/plc/actions?line_id={line['id']}", json=card, headers=admin_headers)
    assert res.status_code == 200, res.text
    assert "gate" not in [c["id"] for c in client.get("/api/v1/plc/actions", headers=admin_headers).json()]
    assert [c["id"] for c in client.get(f"/api/v1/plc/actions?line_id={line['id']}", headers=admin_headers).json()] == ["gate"]
    wrong_cam = client.post(f"/api/v1/plc/actions?line_id={line['id']}", json={**card, "camera_id": "elsewhere"}, headers=admin_headers)
    assert wrong_cam.status_code == 422

    copy = client.post(f"/api/v1/lines/{line['id']}/clone", json={"name": "Packing 3"}, headers=admin_headers)
    assert copy.status_code == 201, copy.text
    copied = copy.json()
    try:
        assert copied["cameras"] == [] and copied["sync"]["enabled"] is False
        assert len(copied["plc_actions"]) == 1 and copied["plc_actions"][0]["id"] != "gate"
        assert "camera_id" not in copied["plc_actions"][0]
    finally:
        client.delete(f"/api/v1/lines/{copied['id']}", headers=admin_headers)

    started = client.post(f"/api/v1/lines/{line['id']}/start", headers=admin_headers)
    assert started.status_code == 200 and started.json()["enabled"] is True
    assert started.json()["camera_errors"]  # the cameras do not exist in this test database
    stopped = client.post(f"/api/v1/lines/{line['id']}/stop", headers=admin_headers)
    assert stopped.json()["status"]["state"] == "stopped"

    recent = client.get(f"/api/v1/lines/{line['id']}/qr/recent", headers=admin_headers).json()
    assert recent["reads"] == [] and recent["stats"]["codes_read"] == 0

    assert client.delete(f"/api/v1/lines/{PRIMARY_LINE_ID}", headers=admin_headers).status_code == 409
    assert client.get("/api/v1/lines/missing", headers=admin_headers).status_code == 404


def test_channel_used_by_a_line_cannot_be_deleted(client, admin_headers, new_line):
    ep = client.post("/api/v1/system/endpoints", json={"name": "MES hook", "protocol": "webhook", "url": "https://mes.example/hook"},
                     headers=admin_headers)
    assert ep.status_code == 200, ep.text
    ep_id = ep.json()["id"]
    line = new_line("Hooked", action_trigger={"send_webhook": True, "webhook_endpoint_id": ep_id})
    listed = client.get("/api/v1/system/endpoints", headers=admin_headers).json()
    assert next(e for e in listed if e["id"] == ep_id)["used_by"] == ["Hooked"]

    refused = client.delete(f"/api/v1/system/endpoints/{ep_id}", headers=admin_headers)
    assert refused.status_code == 409 and "Hooked" in refused.json()["detail"]
    client.put(f"/api/v1/lines/{line['id']}", json={"action_trigger": {"webhook_endpoint_id": None}}, headers=admin_headers)
    assert client.delete(f"/api/v1/system/endpoints/{ep_id}", headers=admin_headers).status_code == 200


def test_products_crud_import_export_and_catalog(client, admin_headers):
    from app.services.product_service import product_catalog

    created = client.post("/api/v1/products", json={"code": " SKU-100 ", "name": "Widget"}, headers=admin_headers)
    assert created.status_code == 201, created.text
    product = created.json()
    assert product["code"] == "SKU-100"
    assert product_catalog.lookup("SKU-100")["name"] == "Widget"
    assert client.post("/api/v1/products", json={"code": "SKU-100", "name": "Dup"}, headers=admin_headers).status_code == 422

    changed = client.put(f"/api/v1/products/{product['id']}", json={"name": "Widget XL"}, headers=admin_headers)
    assert changed.json()["name"] == "Widget XL"
    assert client.get("/api/v1/products?search=xl", headers=admin_headers).json()["total"] == 1

    csv_text = "code,name,description\nSKU-200,Gadget,\n-99,Formula-looking,\n"
    imported = client.post("/api/v1/products/import", files={"file": ("p.csv", csv_text, "text/csv")}, headers=admin_headers)
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"added": 2, "updated": 0, "removed": 0, "total": 3}

    exported = client.get("/api/v1/products/export", headers=admin_headers)
    assert exported.headers["content-type"].startswith("text/csv")
    assert "'-99" in exported.text  # a cell a spreadsheet would run as a formula is escaped

    # Re-importing the export changes nothing; replace removes codes missing from the file.
    again = client.post("/api/v1/products/import?replace=true", files={"file": ("p.csv", exported.text, "text/csv")}, headers=admin_headers)
    assert again.json() == {"added": 0, "updated": 0, "removed": 0, "total": 3}
    only_one = client.post("/api/v1/products/import?replace=true", files={"file": ("p.csv", "code,name\nSKU-200,Gadget\n", "text/csv")},
                           headers=admin_headers)
    assert only_one.json()["removed"] == 2 and product_catalog.lookup("SKU-100") is None

    bad = client.post("/api/v1/products/import", files={"file": ("p.csv", "sku,label\n1,2\n", "text/csv")}, headers=admin_headers)
    assert bad.status_code == 422

    remaining = client.get("/api/v1/products", headers=admin_headers).json()["products"]
    for p in remaining:
        assert client.delete(f"/api/v1/products/{p['id']}", headers=admin_headers).status_code == 200
    assert len(product_catalog) == 0


def test_mobile_scan_reports_known_codes(client, admin_headers):
    res = client.post("/api/v1/qr/mobile-scan", json={"code": "NOT-LISTED", "format": "QR"}, headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["known"] is False and res.json()["code"] == "NOT-LISTED"


def test_api_key_scopes_for_lines_and_products():
    assert required_api_key_scopes("GET", "/api/v1/lines") == {"monitor:read"}
    assert required_api_key_scopes("GET", "/api/v1/lines/line-2/qr/recent") == {"monitor:read"}
    assert required_api_key_scopes("PUT", "/api/v1/lines/line-2") == {"configuration:write"}
    assert required_api_key_scopes("POST", "/api/v1/lines/line-2/clone") == {"configuration:write", "plc:configure"}
    assert required_api_key_scopes("GET", "/api/v1/products/export") == {"configuration:read"}
    assert required_api_key_scopes("POST", "/api/v1/products/import") == {"configuration:write"}


@pytest.mark.parametrize("path", ["/dashboard", "/dashboard/pro"])
def test_dashboards_load_the_production_lines_script(client, path):
    page = client.get(path)
    assert page.status_code == 200
    assert '<script src="/assets/production_lines.js' in page.text
    script = client.get("/assets/production_lines.js")
    assert script.status_code == 200 and "window.ProductionLines" in script.text
