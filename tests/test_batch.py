"""The batch (work order) number of a line.

A line has one batch number at a time. It is saved with the line, set through
the API, the dashboard or a PLC signal, and every product, code read, message
and record carries it. A line without a batch sends what it always sent.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

import app.services.line_service as line_module
from app.routes.v1.lines import set_line_batch
from app.services.card_triggers import line_state_event
from app.services.counting_service import counting_service
from app.services.line_config import BATCH_MAX_LENGTH, PRIMARY_LINE_ID, clean_batch
from app.services.line_service import LineManager
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.plc_input_service import PLCActor, PLCInputService
from app.services.production_records_service import build_row
from app.services.send_dispatcher_service import (
    MESSAGE_FIELDS,
    SendDispatcherService,
    build_message,
    line_state_payload,
    render_template,
    sample_payload,
)
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_joined_cameras import LIST, _cross, _joined, _line, _run, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import (  # noqa: F401  (fixtures)
    WHO,
    _audit,
    _card,
    _line_with,
    _pass,
    _signal,
    _status,
    operator_headers,
    signals,
)

API = "/api/v1/lines"
SUPERVISOR = PLCActor("tester", role="SUPERVISOR")


@pytest.fixture(autouse=True)
def no_batch_left_behind():
    """Line 1's counter is shared by the whole test run: it ends each test without a batch."""
    yield
    counting_service.batch = ""


def _batch(line_id):
    return SettingsPersistenceService.get_line(line_id, with_logic=False).get("batch", "")


# ── The text of a batch number ────────────────────────────────────────────────

@pytest.mark.parametrize("value, text", [
    ("  LOT-7 ", "LOT-7"), (1001, "1001"), (1001.0, "1001"), (12.5, "12.5"), (None, ""), ("", ""),
    ("LOT-7\x00\x00\x00", "LOT-7"),     # a PLC string padded to its full length
    ("x" * 64, "x" * 64),
])
def test_a_batch_is_cleaned(value, text):
    assert BATCH_MAX_LENGTH == 64
    assert clean_batch(value) == text


@pytest.mark.parametrize("bad", ["x" * 65, "LOT\n7", "LOT\x007", "LOT\t7", True, float("nan"), ["LOT-7"]])
def test_a_batch_that_is_too_long_or_has_a_control_character_is_refused(bad):
    with pytest.raises(ValueError):
        clean_batch(bad)


# ── Saved with the line ───────────────────────────────────────────────────────

def test_the_batch_survives_a_restart(lines):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}])
    assert SettingsPersistenceService.set_line_batch(runtime.id, " LOT-7 ") == "LOT-7"
    # Only the batch endpoint, a PLC signal and the dashboard field set it: a line save does not.
    saved = SettingsPersistenceService.save_line({"id": runtime.id, "name": "Renamed", "batch": "X"})
    assert saved["batch"] == "LOT-7"

    SettingsPersistenceService.load()               # the server starts again
    assert _batch(runtime.id) == "LOT-7"
    manager = LineManager()
    manager.apply_state()
    again = manager.get(runtime.id)
    assert again.batch == "LOT-7" and again.counter.batch == "LOT-7"
    assert manager.summary(again)["batch"] == "LOT-7"

    assert SettingsPersistenceService.set_line_batch(runtime.id, "") == ""
    assert "batch" not in SettingsPersistenceService.get_line(runtime.id)
    with pytest.raises(KeyError):
        SettingsPersistenceService.set_line_batch("no-such-line", "LOT-7")
    with pytest.raises(ValueError):
        SettingsPersistenceService.set_line_batch(runtime.id, "x" * 65)


def test_setting_the_batch_is_audited_once_per_change(lines):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}])
    assert set_line_batch(runtime.id, "LOT-7", SUPERVISOR) == "LOT-7"
    assert set_line_batch(runtime.id, "LOT-7", SUPERVISOR) == "LOT-7"       # no change, nothing to write down
    assert set_line_batch(runtime.id, "", SUPERVISOR) == ""
    assert _audit({"id": runtime.id}) == [("tester", "SUPERVISOR", "SET_BATCH")] * 2
    details = [entry["details"] for entry in SettingsPersistenceService.get_audit_logs(line_id=runtime.id)]
    assert "LOT-7" in details[1] and "cleared" in details[0]
    assert runtime.batch == "" and runtime.counter.batch == ""


# ── On every product, code read, message and record ───────────────────────────

def test_a_product_carries_the_batch(lines):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}])
    set_line_batch(runtime.id, "LOT-7", SUPERVISOR)
    _cross(runtime.counter, "vis")
    payload, plc_event = runtime.sent[-1]
    assert payload["batch"] == "LOT-7" and plc_event["batch"] == "LOT-7"
    assert build_row(payload, "product", True)["batch"] == "LOT-7"


def test_a_code_read_and_a_missing_code_carry_the_batch(lines):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "qr", "role": "qr", "product_list_id": LIST}])
    set_line_batch(runtime.id, "LOT-7", SUPERVISOR)
    published = []
    runtime._publish = lambda topics, payload, plc_event: published.append((payload, plc_event))

    async def steps():
        runtime.on_qr_read("qr", "LISTED", "QRCODE")
        await asyncio.sleep(0.02)
        runtime._emit_no_read("qr", {"class_name": None})

    _run(steps)
    assert [payload["event"] for payload, _ in published] == ["QR_CODE_READ", "QR_CODE_NO_READ"]
    assert all(payload["batch"] == "LOT-7" and plc_event["batch"] == "LOT-7" for payload, plc_event in published)
    assert build_row(published[0][0], "code", False)["batch"] == "LOT-7"


def test_a_line_without_a_batch_sends_its_messages_as_before(lines):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, {"camera_id": "qr", "role": "qr", "product_list_id": LIST}])
    published = []
    runtime._publish = lambda topics, payload, plc_event: published.append((payload, plc_event))

    async def steps():
        _cross(runtime.counter, "vis")
        runtime.on_qr_read("qr", "LISTED", "QRCODE")
        await asyncio.sleep(0.02)

    _run(steps)
    for payload, plc_event in runtime.sent + published:
        assert "batch" not in payload and "batch" not in plc_event
    assert build_row(runtime.sent[0][0], "product", True)["batch"] is None
    assert "batch" not in SendDispatcherService._with_totals(line_state_payload(line_state_event(runtime.id, "x", True)), runtime.id)
    assert "batch" not in lines.summary(runtime) or lines.summary(runtime)["batch"] is None


def test_an_own_station_and_a_joined_product_carry_the_batch(lines):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}])
    set_line_batch(runtime.id, "LOT-7", SUPERVISOR)
    # Cameras added after the batch was set get it too.
    SettingsPersistenceService.save_line({"id": runtime.id, "cameras": [
        {"camera_id": "vis", "model_id": "model-test"},
        {"camera_id": "own", "model_id": "model-test", "station": "own"},
        {"model_id": "model-test", **_joined("join", 0, 80)},
    ]})
    lines.apply_state()
    runtime.sent = []
    for counter in [runtime.counter, *runtime.aux_counters.values()]:
        counter.dispatch_event = lambda payload, plc_event, is_defect: runtime.sent.append((payload, plc_event))

    async def steps():
        _cross(runtime.aux_counters["own"], "own")
        _cross(runtime.counter, "vis")
        _cross(runtime.aux_counters["join"], "join")
        await asyncio.sleep(0.2)

    _run(steps)
    assert len(runtime.sent) == 2
    own, product = runtime.sent
    assert own[0]["camera_id"] == "own" and own[0]["batch"] == "LOT-7" and own[1]["batch"] == "LOT-7"
    assert "stations" in product[0] and product[0]["batch"] == "LOT-7" and product[1]["batch"] == "LOT-7"


def test_the_batch_placeholder_and_field():
    payload = {"event": "WIRELINE_OBJECT_CROSSED", "result": "PASSED", "batch": "LOT-7"}
    assert MESSAGE_FIELDS["batch"] == ("batch",)
    assert render_template("{batch};{result}", payload) == "LOT-7;PASSED"
    assert build_message({"fields": ["batch"]}, payload) == {"batch": "LOT-7"}
    assert render_template("[{batch}]", {"result": "PASSED"}) == "[]"


def test_a_line_start_message_carries_the_batch(lines):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}])
    set_line_batch(runtime.id, "LOT-7", SUPERVISOR)
    event = line_state_event(runtime.id, runtime.name, True)
    message = SendDispatcherService._with_totals(line_state_payload(event), runtime.id)
    assert message["batch"] == "LOT-7" and message["event"] == "LINE_STARTED"


# ── As a value a PLC card writes ──────────────────────────────────────────────

def test_a_plc_card_writes_the_batch(lines):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}])
    card = {"id": "w", "value_source": "batch", "operation": "WRITE", "line_id": runtime.id}
    with pytest.raises(ValueError, match="no batch"):
        PLCDispatcherService.write_value(card, line_state_event(runtime.id, runtime.name, True))
    set_line_batch(runtime.id, "1001", SUPERVISOR)
    # A product's event carries its batch; a line start or an alarm reads the line's batch now.
    assert PLCDispatcherService.write_value(card, {"batch": "2002"}) == 2002.0
    assert PLCDispatcherService.write_value(card, line_state_event(runtime.id, runtime.name, True)) == 1001.0
    set_line_batch(runtime.id, "LOT-7", SUPERVISOR)
    with pytest.raises(ValueError, match="LOT-7"):
        PLCDispatcherService.write_value(card, line_state_event(runtime.id, runtime.name, True))


def test_the_examples_have_a_batch(plc):  # noqa: F811
    _, driver = plc
    card = {"id": "w", "name": "Batch to PLC", "enabled": True, "plc_endpoint_id": "ep1", "operation": "WRITE",
            "target_address": "40001", "value_source": "batch"}
    seen = {}

    async def steps():
        seen["result"] = await PLCDispatcherService.dispatch_manual(card)

    _run(steps)
    assert seen["result"]["success"] is True, seen["result"]
    assert driver.ops == [("WRITE", "40001", 1.0)]
    for trigger in ("cross_line", "reject_counter", "qr_read", "qr_no_read", "line_state", "alarm"):
        assert sample_payload({"trigger": trigger})["batch"] == "B-1001", trigger


# ── The endpoint ──────────────────────────────────────────────────────────────

def test_the_batch_endpoint(client, admin_headers, operator_headers):  # noqa: F811
    created = client.post(API, json={"name": f"Batch {uuid.uuid4().hex[:6]}"}, headers=admin_headers)
    assert created.status_code == 201, created.text
    line_id = created.json()["id"]
    url = f"{API}/{line_id}/batch"
    try:
        assert created.json()["status"]["batch"] is None
        saved = client.put(url, json={"batch": " WO-2026-118 "}, headers=admin_headers)
        assert saved.status_code == 200, saved.text
        assert saved.json() == {"line_id": line_id, "batch": "WO-2026-118"}
        assert client.put(url, json={"batch": "WO-9"}, headers=operator_headers).status_code == 403
        long = client.put(url, json={"batch": "x" * 65}, headers=admin_headers)
        assert long.status_code == 422 and "64" in long.json()["detail"]
        assert client.put(url, json={}, headers=admin_headers).status_code == 422
        assert client.put(f"{API}/no-such-line/batch", json={"batch": "WO-9"}, headers=admin_headers).status_code == 404

        line = client.get(f"{API}/{line_id}", headers=admin_headers).json()
        assert line["batch"] == "WO-2026-118" and line["status"]["batch"] == "WO-2026-118"
        overview = client.get(f"{API}/overview", headers=admin_headers).json()["lines"]
        assert next(row for row in overview if row["id"] == line_id)["batch"] == "WO-2026-118"
        # A start and a stop keep it.
        assert client.post(f"{API}/{line_id}/start", headers=admin_headers).json()["status"]["batch"] == "WO-2026-118"
        assert client.post(f"{API}/{line_id}/stop", headers=admin_headers).json()["status"]["batch"] == "WO-2026-118"
        logs = client.get(f"/api/v1/system/audit-logs?line_id={line_id}", headers=admin_headers).json()
        logs = logs if isinstance(logs, list) else logs.get("logs", [])
        assert [(entry["username"], entry["action"]) for entry in logs if entry["action"] == "SET_BATCH"] == [("admin", "SET_BATCH")]
        cleared = client.put(url, json={"batch": ""}, headers=admin_headers)
        assert cleared.status_code == 200 and cleared.json()["batch"] == ""
        assert "batch" not in client.get(f"{API}/{line_id}", headers=admin_headers).json()
    finally:
        client.delete(f"{API}/{line_id}", headers=admin_headers)


# ── From the PLC ──────────────────────────────────────────────────────────────

def test_a_set_batch_signal_sets_the_batch(signals):  # noqa: F811
    line = _line_with(_signal("set_batch", address="R7", data_type="uint16"))
    runtime = line_module.line_manager.get(line["id"])
    seen = []

    async def steps():
        for value in (1001, 1001, 1002):
            signals.values["R7"] = value
            await _pass()
            seen.append((_batch(line["id"]), runtime.batch))

    _run(steps)
    # The batch the PLC holds is the line's batch from the first read on.
    assert seen == [("1001", "1001"), ("1001", "1001"), ("1002", "1002")]
    assert _audit(line) == [(WHO, "PLC", "SET_BATCH"), (WHO, "PLC", "SET_BATCH")]


def test_a_set_batch_signal_reads_text(signals):  # noqa: F811
    line = _line_with(_signal("set_batch", address="ns=2;s=Batch", data_type="string"))
    signals.values["ns=2;s=Batch"] = "LOT-7"

    async def steps():
        await _pass()

    _run(steps)
    assert _batch(line["id"]) == "LOT-7"
    assert ("read", "ns=2;s=Batch", "string") in signals.calls


def test_a_batch_from_the_plc_is_cleaned_or_refused(signals):  # noqa: F811
    line = _line_with(_signal("set_batch", address="R7"))
    seen = {}

    async def steps():
        await PLCInputService._act(_card(line, "plcin_set_batch"), None, 1001.0)
        seen["real"] = _batch(line["id"])
        signals.values["R7"] = 1001
        await _pass()
        for name, value in (("long", "x" * 200), ("control", "LOT\n7"), ("bit", True)):
            signals.values["R7"] = value
            await _pass()
            seen[name] = (_batch(line["id"]), _status(line, "plcin_set_batch")["error"])
            await _pass()                           # the poll goes on, and still says what is wrong
            seen[name + " after"] = _status(line, "plcin_set_batch")["error"]
        signals.values["R7"] = 1003
        await _pass()
        seen["good again"] = (_batch(line["id"]), _status(line, "plcin_set_batch")["error"])

    _run(steps)
    assert seen["real"] == "1001"                   # a REAL that holds a whole number
    assert seen["long"][0] == "1001" and "64" in seen["long"][1] and seen["long after"] == seen["long"][1]
    assert seen["control"][0] == "1001" and "not allowed" in seen["control"][1]
    assert seen["bit"][0] == "1001" and seen["bit"][1]
    assert seen["good again"] == ("1003", None)
    assert _status(line, "plcin_set_batch")["read_count"] == 8
    assert line["id"] != PRIMARY_LINE_ID
