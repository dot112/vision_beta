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


def test_line_crud_start_stop_and_clone(client, admin_headers, new_line, vision_model):
    line = new_line("Packing 2", cameras=[{"camera_id": "cam-api-1", "model_id": vision_model}, {"camera_id": "cam-api-2", "role": "qr"}],
                    sync={"enabled": True, "window_ms": 300})
    assert line["enabled"] is False and line["status"]["state"] == "stopped"
    assert line["sync"] == {"enabled": True, "window_ms": 300}

    listed = client.get("/api/v1/lines", headers=admin_headers).json()
    assert [ln["id"] for ln in listed["lines"]][0] == PRIMARY_LINE_ID
    assert line["id"] in [ln["id"] for ln in listed["lines"]]
    overview = client.get("/api/v1/lines/overview", headers=admin_headers).json()["lines"]
    assert any(row["id"] == line["id"] and row["has_qr"] for row in overview)

    # Sync needs a vision camera and a QR reader. (A camera sent without its model keeps the one it has.)
    bad = client.put(f"/api/v1/lines/{line['id']}", json={"cameras": [{"camera_id": "cam-api-1"}]}, headers=admin_headers)
    assert bad.status_code == 422 and "Sync needs" in bad.json()["detail"]
    renamed = client.put(f"/api/v1/lines/{line['id']}", json={"name": "Packing 2B"}, headers=admin_headers)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Packing 2B"

    # A camera belongs to one line only.
    clash = client.post("/api/v1/lines", json={"name": "Other", "cameras": [{"camera_id": "cam-api-1", "model_id": vision_model}]},
                        headers=admin_headers)
    assert clash.status_code == 422 and "already belongs" in clash.json()["detail"]

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


def test_yield_target_is_saved_reported_and_cloned(client, admin_headers, new_line):
    line = new_line("Filling 4", yield_target=98)
    assert line["yield_target"] == 98.0 and line["status"]["yield_target"] == 98.0
    changed = client.put(f"/api/v1/lines/{line['id']}", json={"yield_target": 0}, headers=admin_headers)
    assert changed.status_code == 200 and changed.json()["status"]["yield_target"] == 0.0
    assert client.put(f"/api/v1/lines/{line['id']}", json={"yield_target": 120}, headers=admin_headers).status_code == 422
    client.put(f"/api/v1/lines/{line['id']}", json={"yield_target": 95.5}, headers=admin_headers)
    copy = client.post(f"/api/v1/lines/{line['id']}/clone", json={"name": "Filling 5"}, headers=admin_headers)
    try:
        assert copy.status_code == 201 and copy.json()["yield_target"] == 95.5
    finally:
        client.delete(f"/api/v1/lines/{copy.json()['id']}", headers=admin_headers)


def test_start_and_stop_send_a_line_state_event_only_on_a_change(client, admin_headers, new_line, monkeypatch):
    from app.services.plc_dispatcher_service import PLCDispatcherService

    sent = []

    async def capture(event):
        sent.append(event)

    monkeypatch.setattr(PLCDispatcherService, "evaluate", capture)
    line = new_line("Capping 6")
    base = f"/api/v1/lines/{line['id']}"
    assert client.post(f"{base}/start", headers=admin_headers).status_code == 200
    assert client.post(f"{base}/start", headers=admin_headers).status_code == 200  # already running
    assert client.post(f"{base}/stop", headers=admin_headers).status_code == 200
    assert client.put(base, json={"enabled": True}, headers=admin_headers).status_code == 200
    assert [(e["event_type"], e["line_id"], e["line_running"]) for e in sent] == [
        ("line_state", line["id"], True), ("line_state", line["id"], False), ("line_state", line["id"], True),
    ]
    client.post(f"{base}/stop", headers=admin_headers)


def test_channel_used_by_a_line_cannot_be_deleted(client, admin_headers, new_line):
    ep = client.post("/api/v1/system/endpoints", json={"name": "MES hook", "protocol": "webhook", "url": "https://mes.example/hook"},
                     headers=admin_headers)
    assert ep.status_code == 200, ep.text
    ep_id = ep.json()["id"]
    line = new_line("Hooked", send_actions=[{"id": "send_hook", "name": "Results to MES", "endpoint_id": ep_id}])
    listed = client.get("/api/v1/system/endpoints", headers=admin_headers).json()
    assert next(e for e in listed if e["id"] == ep_id)["used_by"] == ["Hooked"]

    refused = client.delete(f"/api/v1/system/endpoints/{ep_id}", headers=admin_headers)
    assert refused.status_code == 409 and "Hooked" in refused.json()["detail"]
    client.put(f"/api/v1/lines/{line['id']}", json={"send_actions": []}, headers=admin_headers)
    assert client.delete(f"/api/v1/system/endpoints/{ep_id}", headers=admin_headers).status_code == 200


def test_products_crud_import_export_and_catalog(client, admin_headers):
    """The calls a client written for one product list makes: they work on the oldest list."""
    from app.services.product_service import product_catalog

    for old in client.get("/api/v1/products/lists", headers=admin_headers).json()["lists"]:
        assert client.delete(f"/api/v1/products/lists/{old['id']}", headers=admin_headers).status_code == 200

    created = client.post("/api/v1/products", json={"code": " SKU-100 ", "name": "Widget"}, headers=admin_headers)
    assert created.status_code == 201, created.text
    product = created.json()
    assert product["code"] == "SKU-100"
    list_id = product["list_id"]
    assert product_catalog.lookup("SKU-100", list_id)["name"] == "Widget"
    assert product_catalog.lookup_any("SKU-100")["name"] == "Widget"
    assert client.post("/api/v1/products", json={"code": "SKU-100", "name": "Dup"}, headers=admin_headers).status_code == 422

    changed = client.put(f"/api/v1/products/{product['id']}", json={"name": "Widget XL"}, headers=admin_headers)
    assert changed.json()["name"] == "Widget XL"
    assert client.get("/api/v1/products?search=xl", headers=admin_headers).json()["total"] == 1

    # A file imports as a list of its own; the first list is left as it was.
    csv_text = "code,name,description\nSKU-200,Gadget,\n-99,Formula-looking,\n"
    imported = client.post("/api/v1/products/import", files={"file": ("p.csv", csv_text, "text/csv")}, headers=admin_headers)
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert (body["added"], body["total"], body["list"]["name"]) == (2, 2, "p")
    assert client.get("/api/v1/products", headers=admin_headers).json()["total"] == 1

    exported = client.get(f"/api/v1/products/export?list_id={body['list']['id']}", headers=admin_headers)
    assert exported.headers["content-type"].startswith("text/csv")
    assert "'-99" in exported.text  # a cell a spreadsheet would run as a formula is escaped

    # Putting the export back changes nothing; replacing removes codes missing from the file.
    replace = f"/api/v1/products/lists/{body['list']['id']}/replace"
    again = client.post(replace, files={"file": ("p.csv", exported.text, "text/csv")}, headers=admin_headers).json()
    assert (again["added"], again["updated"], again["removed"], again["total"]) == (0, 0, 0, 2)
    only_one = client.post(replace, files={"file": ("p.csv", "code,name\nSKU-200,Gadget\n", "text/csv")}, headers=admin_headers).json()
    assert only_one["removed"] == 1 and product_catalog.lookup("-99", body["list"]["id"]) is None

    bad = client.post("/api/v1/products/import", files={"file": ("p.csv", "sku,label\n1,2\n", "text/csv")}, headers=admin_headers)
    assert bad.status_code == 422

    for left in client.get("/api/v1/products/lists", headers=admin_headers).json()["lists"]:
        assert client.delete(f"/api/v1/products/lists/{left['id']}", headers=admin_headers).status_code == 200
    assert len(product_catalog) == 0 and product_catalog.list_ids() == []


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


def test_dashboard_loads_the_production_lines_script(client):
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert '<script src="/assets/production_lines.js' in page.text
    script = client.get("/assets/production_lines.js")
    assert script.status_code == 200 and "window.ProductionLines" in script.text
    # Revalidated on every load, so an updated server reaches open dashboards.
    assert script.headers["cache-control"] == "no-cache"


def test_dashboard_loads_its_stylesheet_font_and_logo(client):
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert '<link rel="stylesheet" href="/assets/dashboard.css' in page.text
    css = client.get("/assets/dashboard.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert css.headers["cache-control"] == "no-cache"
    font = client.get("/assets/fonts/inter-latin.woff2")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2"
    assert 'src="/assets/zajel_logo_small.png"' in page.text
    logo = client.get("/assets/zajel_logo_small.png")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"


def test_line_dashboard_has_the_alarms_panel(client):
    page = client.get("/dashboard").text
    assert 'id="alarmPanel"' in page and 'id="lineAlarmPill"' in page
    # It asks for this line's alarms plus the ones every line shares, and acknowledges through the API.
    assert "include_all_lines=true" in page
    assert "/acknowledge" in page
