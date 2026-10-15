"""Product checks: a line rejects an item when its product's rules match, for example "Wrong bottle"
(the class the camera saw is not the product's expected class). Reason product_check, PLC code 7."""
from __future__ import annotations

import asyncio
import csv
import io
import re
from datetime import datetime, timezone

import pytest

from app.db.models.production_record import ProductRecord
from app.services.line_config import REJECT_REASON_CODES, normalize_product_checks, product_result
from app.services.product_service import product_catalog
from app.services.records_export import csv_header, csv_rows
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_production_records import LIST, _cross, _run, lines, recorder  # noqa: F401  (fixtures)

CHECK = {"name": "Wrong bottle", "only_when": {"match": "all", "rules": [
    {"left": "class", "op": "!=", "right": "product.expected_class", "right_kind": "value"}]}}
LANE3 = {"name": "Lane 3", "only_when": {"rules": [{"left": "product.lane", "op": "=", "right": "3"}]}}


@pytest.fixture
def apples(lines, monkeypatch):  # noqa: F811
    """LISTED in LIST is a product with lane 3 and expected class apple."""
    monkeypatch.setattr(product_catalog, "_lists", {LIST: {"LISTED": {"code": "LISTED", "name": "Apple juice",
                                                                      "data": {"lane": 3, "expected_class": "apple"}}}})
    monkeypatch.setattr(product_catalog, "_names", {LIST: "List 1"})
    return lines


def _check_line(manager, cameras, checks, name="Checks"):
    cameras = [cam if cam.get("role", "vision") != "vision" else {"model_id": "model-test", **cam} for cam in cameras]
    line = SettingsPersistenceService.save_line({"name": name, "cameras": cameras, "enabled": True,
                                                 "sync": {"window_ms": 80}, "product_checks": checks})
    manager.apply_state()
    runtime = manager.get(line["id"])
    runtime.sent = []
    for counter in [runtime.counter, *runtime.aux_counters.values()]:
        counter.dispatch_event = lambda payload, plc_event, is_defect: runtime.sent.append((payload, plc_event))
    runtime._publish = lambda topics, payload, plc_event: runtime.sent.append((payload, plc_event))
    return runtime


# ── The settings ──────────────────────────────────────────────────────────────

def test_product_checks_are_checked():
    check = normalize_product_checks([CHECK])[0]
    assert re.fullmatch(r"check_[0-9a-f]{8}", check["id"]) and check["enabled"] is True and check["name"] == "Wrong bottle"
    assert check["only_when"]["rules"][0]["right_kind"] == "value"
    assert normalize_product_checks([{**CHECK, "id": "c1", "enabled": False}])[0]["enabled"] is False
    for bad, word in (([{**CHECK, "name": ""}], "name"), ([{**CHECK, "only_when": {"rules": []}}], "at least one rule"),
                      ([{**CHECK, "name": "x" * 65}], "64"), ([CHECK] * 11, "10"),
                      ([{**CHECK, "id": "a"}, {**CHECK, "id": "a"}], "same id"), ("checks", "list"), (["x"], "object"),
                      ([{**CHECK, "only_when": {"rules": [{"left": "speed", "op": "="}]}}], "Wrong bottle.*speed")):
        with pytest.raises(ValueError, match=word):
            normalize_product_checks(bad)


def test_a_product_check_comes_last():
    read = {"camera_id": "qr", "known": True}
    assert product_result(False, read, {}, product_check="Wrong bottle") == (True, "product_check", "qr")
    assert product_result(True, read, {}, product_check="Wrong bottle")[1] == "vision_class"
    assert product_result(False, {"camera_id": "qr", "known": False}, {"qr": {"action": "accept_listed", "no_read": "ignore"}},
                          product_check="Wrong bottle")[1] == "code_not_in_list"
    assert product_result(False, read, {}) == (False, None, None)
    assert REJECT_REASON_CODES["product_check"] == 7


def test_a_line_keeps_its_checks_when_a_client_leaves_them_out(lines):  # noqa: F811
    line = SettingsPersistenceService.save_line({"name": "Kept checks", "product_checks": [CHECK]})
    assert [c["name"] for c in line["product_checks"]] == ["Wrong bottle"]
    again = SettingsPersistenceService.save_line({"id": line["id"], "name": "Kept checks", "enabled": False})
    assert [c["name"] for c in again["product_checks"]] == ["Wrong bottle"]
    assert again["product_checks"][0]["id"] == line["product_checks"][0]["id"]
    cleared = SettingsPersistenceService.save_line({"id": line["id"], "name": "Kept checks", "product_checks": []})
    assert cleared["product_checks"] == []


# ── On the line ───────────────────────────────────────────────────────────────

def test_a_wrong_bottle_is_rejected(apples, recorder):  # noqa: F811
    runtime = _check_line(apples, [{"camera_id": "top"}, {"camera_id": "qr", "role": "qr", "product_list_id": LIST}], [CHECK])
    assert runtime.sync_enabled                                   # checks need each product paired with its code

    async def steps():
        runtime.on_qr_read("qr", "LISTED", "QR_CODE")
        await asyncio.sleep(0.01)
        _cross(runtime.counter, "top", class_name="orange")
        await asyncio.sleep(0.12)
        runtime.on_qr_read("qr", "LISTED", "QR_CODE")
        await asyncio.sleep(0.01)
        _cross(runtime.counter, "top", class_name="Apple")
        await asyncio.sleep(0.12)

    _run(steps)
    (wrong, wrong_event), (right, right_event) = runtime.sent
    assert (wrong["result"], wrong["reject_reason"], wrong["product_check"]) == ("REJECTED", "product_check", "Wrong bottle")
    assert (wrong_event["result"], wrong_event["reject_reason"], wrong_event["product_check"]) == ("reject", "product_check", "Wrong bottle")
    assert right["result"] == "PASSED" and "product_check" not in right
    assert recorder._rows[0]["details"]["product_check"] == "Wrong bottle"
    assert runtime.recent_reads()[1]["reject_reason"] == "product_check"


def test_checks_need_a_known_code(apples, recorder):  # noqa: F811
    runtime = _check_line(apples, [{"camera_id": "top"}, {"camera_id": "qr", "role": "qr", "product_list_id": LIST}], [CHECK])

    async def steps():
        runtime.on_qr_read("qr", "NOPE", "QR_CODE")               # a code in no list: no product to check
        await asyncio.sleep(0.01)
        _cross(runtime.counter, "top", class_name="orange")
        await asyncio.sleep(0.12)
        _cross(runtime.counter, "top", class_name="orange")       # no code at all
        await asyncio.sleep(0.25)

    _run(steps)
    assert [payload["result"] for payload, _ in runtime.sent] == ["PASSED", "PASSED"]


def test_a_code_only_line_runs_checks_without_class(apples, recorder):  # noqa: F811
    reader = [{"camera_id": "qr", "role": "qr", "product_list_id": LIST, "qr_action": "accept_listed"}]
    lane = _check_line(apples, reader, [LANE3], name="Code only lane")
    classes = _check_line(apples, [{**reader[0], "camera_id": "qr2"}], [CHECK], name="Code only class")

    async def steps():
        lane.on_qr_read("qr", "LISTED", "QR_CODE")
        classes.on_qr_read("qr2", "LISTED", "QR_CODE")
        await asyncio.sleep(0.01)

    _run(steps)
    (payload, event), = lane.sent
    assert (payload["result"], payload["reject_reason"], payload["product_check"]) == ("REJECTED", "product_check", "Lane 3")
    assert event["event_type"] == "code_product" and event["reject_reason"] == "product_check"
    (payload, _), = classes.sent
    assert payload["result"] == "PASSED"                          # no class on a code-only line: the check is false


def test_the_export_has_a_product_check_column():
    def record(details):
        return ProductRecord(id=1, recorded_at=datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc), kind="product",
                             counted=True, details=details)

    header = next(csv.reader(io.StringIO(csv_header("UTC").lstrip("﻿"))))
    assert header.index("product_check") == header.index("product_data") + 1
    rows = list(csv.reader(io.StringIO(csv_rows([record({"product_check": "Wrong bottle"}), record(None)], timezone.utc))))
    column = header.index("product_check")
    assert rows[0][column] == "Wrong bottle" and rows[1][column] == ""
