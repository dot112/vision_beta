"""Each carton carries the product ID and the batch of its code (a list in split or GS1 format)."""
from __future__ import annotations

import asyncio

import pytest

from app.services import event_values
from app.services.formula import PLAIN_NAMES
from app.services.line_config import REASON_NOT_LISTED, REASON_PRODUCT_CHECK, REJECT_REASON_CODES
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.send_dispatcher_service import render_template
from tests.test_production_records import LIST, _cross, _line, _run, lines, recorder  # noqa: F401  (fixtures)

SPLIT = {"mode": "split", "separator": ";", "product_part": 1, "batch_part": 2}
READER = {"camera_id": "qr", "role": "qr", "product_list_id": LIST, "qr_action": "accept_listed",
          "qr_trigger": "signal", "qr_no_read": "reject"}
VISION = {"camera_id": "top"}


@pytest.fixture
def cartons(lines, monkeypatch):  # noqa: F811
    """The list holds P-100 and is in split format."""
    from app.services.product_service import product_catalog

    monkeypatch.setattr(product_catalog, "_lists", {LIST: {"P-100": {"code": "P-100", "name": "Juice", "data": {"lane": 2}}}})
    monkeypatch.setattr(product_catalog, "_formats", {LIST: dict(SPLIT)})
    return lines


def _watch(runtime):
    """The products' messages and PLC events, and the code reads' messages."""
    seen = {"products": [], "reads": []}
    runtime.counter.dispatch_event = lambda payload, plc_event, is_defect: seen["products"].append((payload, plc_event))
    runtime._publish = lambda topics, payload, plc_event: seen["reads"].append((payload, plc_event))
    return seen


def _carton(runtime, code, is_defect=False):
    async def steps():
        if code is not None:
            runtime.on_qr_read("qr", code, "QR_CODE")
            await asyncio.sleep(0.01)
        _cross(runtime.counter, "top", is_defect=is_defect)
        await asyncio.sleep(0.15)
    return steps


def test_a_paired_carton_carries_its_batch(cartons, recorder):  # noqa: F811
    runtime = _line(cartons, [VISION, READER])
    runtime.set_batch("WO-1")
    seen = _watch(runtime)
    _run(_carton(runtime, "P-100;B-2026-07"))
    (payload, plc_event), = seen["products"]
    assert (payload["batch"], plc_event["batch"]) == ("B-2026-07", "B-2026-07")
    assert (payload["product_id"], plc_event["product_id"]) == ("P-100", "P-100")
    assert payload["qr_code"] == plc_event["qr_code"] == "P-100;B-2026-07"      # the whole code stays
    assert payload["result"] == "PASSED" and "code_problem" not in payload
    row, = recorder._rows
    assert (row["batch"], row["code"], row["product_name"]) == ("B-2026-07", "P-100;B-2026-07", "Juice")
    assert row["details"]["product_id"] == "P-100"
    assert runtime.batch == "WO-1"


def test_a_carton_without_a_batch_keeps_the_lines(cartons, recorder):  # noqa: F811
    runtime = _line(cartons, [VISION, READER])
    runtime.set_batch("WO-1")
    seen = _watch(runtime)
    _run(_carton(runtime, "P-100;"))
    (payload, plc_event), = seen["products"]
    assert (payload["batch"], plc_event["batch"], payload["result"]) == ("WO-1", "WO-1", "PASSED")
    assert payload["code_problem"] == "no batch part"
    assert recorder._rows[0]["details"]["code_problem"] == "no batch part" and runtime.batch == "WO-1"


def test_an_unknown_product_id_is_rejected(cartons, recorder):  # noqa: F811
    runtime = _line(cartons, [VISION, READER])
    seen = _watch(runtime)
    _run(_carton(runtime, "P-999;B-2026-07"))                                    # BOX-006
    (payload, plc_event), = seen["products"]
    assert (payload["result"], payload["reject_reason"], plc_event["result"]) == ("REJECTED", REASON_NOT_LISTED, "reject")
    assert REJECT_REASON_CODES[plc_event["reject_reason"]] == 2
    assert (payload["product_id"], payload["batch"]) == ("P-999", "B-2026-07")
    assert recorder._rows[0]["details"]["product_id"] == "P-999"


def test_a_code_that_does_not_fit_is_rejected_with_its_problem(cartons, recorder):  # noqa: F811
    runtime = _line(cartons, [VISION, READER])
    seen = _watch(runtime)
    _run(_carton(runtime, "P-100"))
    (payload, _), = seen["products"]
    assert (payload["result"], payload["reject_reason"], payload["code_problem"]) == ("REJECTED", REASON_NOT_LISTED, "no separator")
    assert "product_id" not in payload
    assert recorder._rows[0]["details"]["code_problem"] == "no separator"


def test_the_six_cartons(cartons, recorder):  # noqa: F811
    runtime = _line(cartons, [VISION, READER])
    seen = _watch(runtime)

    async def steps():
        for code, defect in (("P-100;B-2026-07", False),     # BOX-001: pass
                             ("P-100;B-2026-07", True),      # BOX-002: damaged packaging
                             (None, False),                  # BOX-003: no QR
                             ("P-100;B-2026-07", True),      # BOX-004: wrong product label (seen by Camera 2)
                             ("P-100;B-2026-07", False),     # BOX-005: pass
                             ("P-999;B-2026-07", False)):    # BOX-006: unknown product ID
            await _carton(runtime, code, defect)()

    _run(steps)
    results = [payload["result"] for payload, _ in seen["products"]]
    assert results == ["PASSED", "REJECTED", "REJECTED", "REJECTED", "PASSED", "REJECTED"]
    counter = runtime.counter
    assert (counter.total_inspected, counter.good_count, counter.rejected_count) == (6, 2, 4)
    assert len([r for r in recorder._rows if r["kind"] == "product"]) == 6


def test_values_and_messages_name_the_product_id():
    assert "product_id" in PLAIN_NAMES
    assert event_values.values_for({"product_id": "P-100", "batch": "B-7"})["product_id"] == "P-100"
    assert "product_id" not in event_values.values_for({"qr_code": "P-100"})
    assert render_template("{product_id} / {batch}", {"product_id": "P-100", "batch": "B-7"}) == "P-100 / B-7"


def test_product_checks_see_the_cartons_batch(cartons, recorder):  # noqa: F811
    from app.services.settings_persistence_service import SettingsPersistenceService

    runtime = _line(cartons, [VISION, READER])
    SettingsPersistenceService.save_line({"id": runtime.id, "name": runtime.name, "product_checks": [
        {"name": "Old batch", "only_when": {"rules": [{"left": "batch", "op": "=", "right": "B-OLD"}]}}]})
    cartons.apply_state()
    seen = _watch(runtime)

    async def steps():
        await _carton(runtime, "P-100;B-OLD")()
        await _carton(runtime, "P-100;B-NEW")()

    _run(steps)
    assert [(p["result"], p["reject_reason"]) for p, _ in seen["products"]] == [
        ("REJECTED", REASON_PRODUCT_CHECK), ("PASSED", None)]


def test_a_code_only_line_carries_the_batch(cartons):
    runtime = _line(cartons, [READER])
    runtime.set_batch("WO-1")
    seen = _watch(runtime)

    async def steps():
        runtime.on_qr_read("qr", "P-100;B-7", "QR_CODE")
        await asyncio.sleep(0.05)

    _run(steps)
    (payload, plc_event), = seen["reads"]
    assert (payload["result"], payload["batch"], plc_event["batch"], payload["product_id"]) == ("PASSED", "B-7", "B-7", "P-100")


def test_test_with_a_code_takes_the_whole_code(cartons, monkeypatch):
    runtime = _line(cartons, [VISION, READER])
    events = []

    async def capture(cls, card, state, event=None):
        events.append(event)

    monkeypatch.setattr(PLCDispatcherService, "_dispatch", classmethod(capture))
    card = {"id": "w1", "line_id": runtime.id, "name": "Lane", "trigger": "cross_line", "operation": "write"}
    asyncio.run(PLCDispatcherService.dispatch_manual(card, code="P-100;B-2026-07"))
    event, = events
    assert event["product"]["code"] == "P-100" and event["product_id"] == "P-100" and event["batch"] == "B-2026-07"
    assert event["qr_code"] == "P-100;B-2026-07" and event["qr_status"] == "known"


def test_shown_only_reads_show_the_product_id(cartons):
    runtime = _line(cartons, [VISION, {**READER, "qr_action": "report", "qr_trigger": "continuous"}])
    seen = _watch(runtime)
    runtime.show_read("qr", "P-100;B-4", "QR_CODE")
    read, = runtime.recent_reads()
    assert (read["product_id"], read["batch"], read["status"], read["shown_only"]) == ("P-100", "B-4", "known", True)
    assert seen == {"products": [], "reads": []}


def test_a_whole_code_list_sends_the_messages_it_sent(cartons):
    from app.services.product_service import product_catalog

    product_catalog._formats = {}                                    # the list reads whole codes, as before
    product_catalog._lists = {LIST: {"P-100": {"code": "P-100", "name": "Juice"}}}
    runtime = _line(cartons, [READER])
    seen = _watch(runtime)

    async def steps():
        runtime.on_qr_read("qr", "P-100", "QR_CODE")
        await asyncio.sleep(0.05)

    _run(steps)
    (payload, plc_event), = seen["reads"]
    assert "product_id" not in payload and "code_problem" not in payload      # no new message keys
    assert plc_event["product_id"] == "P-100"                                 # rules and writes still have it
    assert render_template("{product_id}", {"code": "P-100"}) == "P-100"
    assert render_template("[{product_id}]", {"code": "P-100", "code_problem": "no separator"}) == "[]"


# ── The code's batch sets the line's batch ────────────────────────────────────

from app.services.line_config import line_warnings, normalize_camera  # noqa: E402
from app.services.settings_persistence_service import SettingsPersistenceService  # noqa: E402

SETS = {**READER, "code_sets_batch": True}


def _batch_audit(runtime):
    logs = SettingsPersistenceService.get_audit_logs(line_id=runtime.id)
    return [(entry["username"], entry["action"]) for entry in reversed(logs) if entry["action"] == "SET_BATCH"]


def _reads(runtime, *codes, camera_id="qr"):
    async def steps():
        for code in codes:
            runtime.on_qr_read(camera_id, code, "QR_CODE")
            await asyncio.sleep(0.03)
    _run(steps)


def test_a_listed_carton_sets_the_lines_batch(cartons):
    runtime = _line(cartons, [VISION, SETS])
    _watch(runtime)
    _reads(runtime, "P-100;B-2")
    assert runtime.batch == "B-2" and SettingsPersistenceService.get_line(runtime.id)["batch"] == "B-2"
    assert _batch_audit(runtime) == [("camera:qr", "SET_BATCH")]


def test_the_same_batch_writes_no_audit(cartons):
    runtime = _line(cartons, [VISION, SETS])
    _watch(runtime)
    _reads(runtime, "P-100;B-2", "P-100;B-2", "P-100;")
    assert runtime.batch == "B-2" and len(_batch_audit(runtime)) == 1


def test_unknown_or_shown_reads_never_set_it(cartons):
    runtime = _line(cartons, [VISION, SETS])
    runtime.set_batch("WO-1")
    _watch(runtime)
    _reads(runtime, "P-999;B-3")
    runtime.show_read("qr", "P-100;B-4", "QR_CODE")
    assert runtime.batch == "WO-1" and _batch_audit(runtime) == []
    off = _line(cartons, [{"camera_id": "top2"}, {**READER, "camera_id": "qr2"}], name="Off")
    _watch(off)
    _reads(off, "P-100;B-5", camera_id="qr2")
    assert off.batch == ""


def test_the_option_is_off_by_default_and_kept(cartons):
    assert "code_sets_batch" not in normalize_camera(READER, 0)
    assert normalize_camera(SETS, 0)["code_sets_batch"] is True
    assert "code_sets_batch" not in normalize_camera({**VISION, "code_sets_batch": True}, 0)      # reads no codes
    with pytest.raises(ValueError, match="true or false"):
        normalize_camera({**READER, "code_sets_batch": "maybe"}, 0)
    line = SettingsPersistenceService.save_line({"name": "Kept", "cameras": [VISION | {"model_id": "m"}, SETS]})
    again = SettingsPersistenceService.save_line({"id": line["id"], "name": "Kept", "cameras": [VISION | {"model_id": "m"}, READER]})
    assert again["cameras"][1]["code_sets_batch"] is True
    off = SettingsPersistenceService.save_line({"id": line["id"], "name": "Kept",
                                                "cameras": [VISION | {"model_id": "m"}, {**READER, "code_sets_batch": False}]})
    assert "code_sets_batch" not in off["cameras"][1]


def test_warning_when_the_list_has_no_batch_part():
    line = {"cameras": [VISION, SETS]}
    text = "Camera 2 sets the line's batch from its codes, but the codes of its product list have no batch part."
    assert any(text in w for w in line_warnings(line, list_formats={}))                          # whole codes
    assert any(text in w for w in line_warnings(line, list_formats={LIST: {**SPLIT, "batch_part": None}}))
    assert not any(text in w for w in line_warnings(line, list_formats={LIST: SPLIT}))
    assert not any(text in w for w in line_warnings(line, list_formats={LIST: {"mode": "gs1"}}))
    assert not any(text in w for w in line_warnings(line))                                       # formats not known


def test_test_with_a_code_says_what_the_code_holds(cartons, monkeypatch):
    runtime = _line(cartons, [VISION, READER])

    async def quiet(cls, card, state, event=None):
        return None

    monkeypatch.setattr(PLCDispatcherService, "_dispatch", classmethod(quiet))
    card = {"id": "w2", "line_id": runtime.id, "name": "Lane", "trigger": "cross_line", "operation": "write"}
    result = asyncio.run(PLCDispatcherService.dispatch_manual(card, code="P-100;B-2026-07"))
    assert (result["product_id"], result["batch"]) == ("P-100", "B-2026-07")
    result = asyncio.run(PLCDispatcherService.dispatch_manual(card, code="P-100"))
    assert (result["product_id"], result["batch"]) == (None, None)
    result = asyncio.run(PLCDispatcherService.dispatch_manual(card))
    assert "product_id" not in result and "batch" not in result


def test_the_test_route_answers_the_product_id_and_batch(client, admin_headers, monkeypatch):
    async def fake(cls, card, code=None):
        return {"success": True, "message": "ok", "status": "sent", "product_id": "P-100", "batch": "B-2026-07"}

    monkeypatch.setattr(PLCDispatcherService, "dispatch_manual", classmethod(fake))
    card = {"id": "route_card2", "name": "Sort", "operation": "WRITE", "value_source": "code", "plc_endpoint_id": "ep-x",
            "target_address": "DB1.Code"}
    res = client.post("/api/v1/plc/actions/route_card2/test", json={"confirm": True, "card": card, "code": "P-100;B-2026-07"},
                      headers=admin_headers)
    assert res.status_code == 200, res.text
    assert (res.json()["product_id"], res.json()["batch"]) == ("P-100", "B-2026-07")
