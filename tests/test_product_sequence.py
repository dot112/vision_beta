"""Each carton keeps the product sequence the PLC held when it asked for the carton's pictures.

The line names one of its inputs as the sequence input. A "Take a picture" input reads it
once when it fires, and the value travels with the request into the product it makes: its
message, PLC event, value names, product checks and record.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import app.services.line_service as line_module
from app.services import event_values
from app.services.formula import PLAIN_NAMES
from app.services.line_config import TEMPLATE_FIELDS, normalize_line
from app.services.plc_input_service import PLCInputService
from app.services.production_records_service import build_row
from app.services.qr_service import QRReaderPipeline
from app.services.records_export import _FIELDS
from app.services.send_dispatcher_service import MESSAGE_FIELDS, render_template
from app.services.settings_persistence_service import SettingsPersistenceService
from app.services.vision_service import CameraStreamPipeline
from tests.test_joined_cameras import LIST, _cross, _run, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import signals  # noqa: F401  (a fixture)
from tests.test_trigger_inspection import _det, workers  # noqa: F401  (a fixture)

CAP, SEQ = "DB1.CaptureRequest", "DB1.ProductSequence"
SPLIT = {"mode": "split", "separator": ";", "product_part": 1, "batch_part": 2}
GOOD_QR = "P-100;B-2026-07"
CAPTURE_INPUT = {"id": "in_capture", "name": "CaptureRequest", "plc_endpoint_id": "ep1", "address": CAP,
                 "action": "capture", "trigger_no": 1, "data_type": "bool"}
SEQUENCE_INPUT = {"id": "in_seq", "name": "ProductSequence", "plc_endpoint_id": "ep1", "address": SEQ,
                  "action": "none", "data_type": "uint16", "value_name": "seq"}
VISION = {"camera_id": "cam2", "role": "vision", "model_id": "model-test", "counting": True, "count_mode": "trigger",
          "expected_classes": ["carton"], "defect_classes": ["damage", "wrong_label"], "triggers": [1]}
READER = {"camera_id": "cam1", "role": "qr", "product_list_id": LIST, "qr_action": "accept_listed",
          "qr_no_read": "reject", "qr_trigger": "signal", "triggers": [1]}


# ── Stand-ins for the two cameras ─────────────────────────────────────────────

class _Reader:
    """Camera 1: each picture asked for is "taken" after ``delay`` s and shows ``code`` (None: no code)."""

    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.requests = []
        self.code = GOOD_QR
        self.delay = 0.01

    def request_capture(self, **request):
        self.requests.append(request)
        code = self.code
        loop = asyncio.get_running_loop()
        loop.call_later(self.delay, self._deliver, request, code)

    def _deliver(self, request, code):
        runtime = line_module.line_manager.route(self.camera_id)[0]
        record = {"timestamp": "2026-10-17T10:00:00+00:00", "t": time.monotonic(), "camera_id": self.camera_id,
                  "track_id": None, "test": False, "status": "read" if code else "no_read",
                  "codes": [{"code": code, "format": "QR_CODE", "known": True}] if code else []}
        for key in ("trigger_no", "sequence", "request_id"):
            if request.get(key) is not None:
                record[key] = request[key]
        runtime.on_qr_capture(self.camera_id, record, None)


class _Vision:
    """Camera 2's model: it gets through every picture it is given at once and finds ``names`` in it."""

    def __init__(self):
        self.names = ["carton"]
        self.is_busy = False

    def submit_frame_if_idle(self, mat, frame_id, conf_thresh=None, copy=True, arrived_at=None, on_result=None,
                             on_failure=None):
        if on_result is not None:
            on_result(frame_id, [_det(name) for name in self.names], 3.0, arrived_at)
        return True

    def get_latest_inference(self):
        return [_det(name) for name in self.names], 3.0


@pytest.fixture
def carton(signals, workers, fake_camera, monkeypatch):  # noqa: F811
    """A carton line on the fake PLC: Camera 1 reads codes on trigger 1, Camera 2 inspects on trigger 1."""
    from app.services.product_service import product_catalog

    monkeypatch.setattr(product_catalog, "_lists", {LIST: {"P-100": {"code": "P-100", "name": "Juice carton"}}})
    monkeypatch.setattr(product_catalog, "_formats", {LIST: dict(SPLIT)})
    readers = {}
    monkeypatch.setattr(QRReaderPipeline, "get_worker",
                        classmethod(lambda cls, camera_id: readers.setdefault(camera_id, _Reader(camera_id))))
    vision = _Vision()
    monkeypatch.setattr(CameraStreamPipeline, "get_worker", classmethod(lambda cls, camera_id: vision))
    fake_camera("cam2")
    fake_camera("cam1")
    signals.values.update({CAP: False, SEQ: 0})
    return {"plc": signals, "readers": readers, "vision": vision}


def _save(cameras=(VISION, READER), inputs=(CAPTURE_INPUT, SEQUENCE_INPUT), **extra):
    line = SettingsPersistenceService.save_line({"name": "Carton line", "enabled": True, "cameras": list(cameras),
                                                 "plc_inputs": list(inputs), "sync": {"window_ms": 300}, **extra})
    line_module.line_manager.apply_state()
    PLCInputService.reload()
    runtime = line_module.line_manager.get(line["id"])
    runtime.sent = []
    runtime.counter.dispatch_event = lambda payload, plc_event, is_defect: runtime.sent.append((payload, plc_event))
    runtime._publish = lambda topics, payload, plc_event: runtime.sent.append((payload, plc_event))
    return runtime


async def _poll():
    await PLCInputService.poll_once("ep1")
    await PLCInputService.drain()


async def _inspect(env, sequence, code=GOOD_QR, names=("carton",), settle=0.08):
    """The PLC holds ``sequence`` and raises CaptureRequest; the cameras see ``code`` and ``names``."""
    plc = env["plc"]
    env["vision"].names = list(names)
    reader = env["readers"].setdefault("cam1", _Reader("cam1"))
    reader.code = code
    plc.values[SEQ] = sequence
    plc.values[CAP] = True
    await _poll()
    await asyncio.sleep(settle)
    plc.values[CAP] = False
    await _poll()


# ── The setting ───────────────────────────────────────────────────────────────

def test_the_sequence_input_must_be_an_input_of_the_line(lines):  # noqa: F811
    with pytest.raises(ValueError, match="sequence"):
        normalize_line({"name": "L", "plc_inputs": [CAPTURE_INPUT], "sequence_input_id": "in_seq"})
    line = normalize_line({"name": "L", "plc_inputs": [CAPTURE_INPUT, SEQUENCE_INPUT], "sequence_input_id": "in_seq"})
    assert line["sequence_input_id"] == "in_seq"
    assert "sequence_input_id" not in normalize_line({"name": "L"})
    saved = SettingsPersistenceService.save_line({"name": "Seq", "plc_inputs": [CAPTURE_INPUT, SEQUENCE_INPUT],
                                                  "sequence_input_id": "in_seq"})
    # A client that leaves it out keeps it; null clears it.
    kept = SettingsPersistenceService.save_line({"id": saved["id"], "name": "Seq renamed"})
    assert kept["sequence_input_id"] == "in_seq"
    cleared = SettingsPersistenceService.save_line({"id": saved["id"], "name": "Seq", "sequence_input_id": None})
    assert "sequence_input_id" not in cleared
    # The input cannot be removed while it is the sequence input.
    SettingsPersistenceService.save_line({"id": saved["id"], "name": "Seq", "sequence_input_id": "in_seq"})
    with pytest.raises(ValueError, match="sequence"):
        SettingsPersistenceService.save_line({"id": saved["id"], "name": "Seq", "plc_inputs": [CAPTURE_INPUT]})


# ── The sequence travels with the capture request ─────────────────────────────

def test_a_capture_reads_the_sequence_once_and_both_cameras_carry_it(carton):
    runtime = _save(sequence_input_id="in_seq")

    async def steps():
        await _poll()                        # the starting point
        await _inspect(carton, 7)

    _run(steps)
    request, = carton["readers"]["cam1"].requests
    assert (request["sequence"], request["trigger_no"]) == (7, 1)
    (payload, plc_event), = runtime.sent
    assert (payload["sequence"], plc_event["sequence"]) == (7, 7)
    assert (payload["result"], payload["qr_code"]) == ("PASSED", GOOD_QR)
    # Read once more, fresh, when the capture fired: three polls and the capture's own read.
    reads = [call for call in carton["plc"].calls if call[:2] == ("read", SEQ)]
    assert len(reads) == 4


def test_the_sequence_stays_with_its_carton_when_the_plc_moves_on(carton):
    runtime = _save(sequence_input_id="in_seq")
    carton["readers"]["cam1"] = reader = _Reader("cam1")
    reader.delay = 0.15                     # the code comes late

    async def steps():
        await _poll()
        carton["plc"].values.update({SEQ: 7, CAP: True})
        await _poll()
        carton["plc"].values.update({SEQ: 8, CAP: False})      # the PLC already holds the next number
        await _poll()
        await asyncio.sleep(0.3)

    _run(steps)
    (payload, plc_event), = runtime.sent
    assert (payload["sequence"], plc_event["sequence"]) == (7, 7)


def test_the_capture_reads_the_value_the_plc_holds_now_not_the_last_poll(carton):
    runtime = _save(sequence_input_id="in_seq")

    async def steps():
        carton["plc"].values[SEQ] = 1
        await _poll()                                   # the poll saw 1
        carton["plc"].values[SEQ] = 2                   # the PLC moved on in the same scan as its request
        card = next(c for c in PLCInputService._all if c["line_id"] == runtime.id and c["id"] == "in_capture")
        await PLCInputService._capture(card)
        await asyncio.sleep(0.08)

    _run(steps)
    assert carton["readers"]["cam1"].requests[0]["sequence"] == 2
    assert runtime.sent[0][0]["sequence"] == 2


def test_a_moving_product_takes_the_sequence_of_its_code(carton):
    from tests.test_joined_cameras import _cross

    runtime = _save(cameras=[{"camera_id": "top", "model_id": "model-test"}, READER], sequence_input_id="in_seq")
    carton["readers"]["cam1"] = _Reader("cam1")

    async def steps():
        await _poll()
        carton["plc"].values.update({SEQ: 3, CAP: True})
        await _poll()
        await asyncio.sleep(0.03)                       # the code of carton 3 is read
        carton["plc"].values.update({SEQ: 4, CAP: False})
        await _poll()
        _cross(runtime.counter, "top")                  # carton 3 crosses the count line
        await asyncio.sleep(0.05)

    _run(steps)
    (payload, plc_event), = runtime.sent
    assert (payload["sequence"], plc_event["sequence"], payload["qr_code"]) == (3, 3, GOOD_QR)


def test_a_code_only_carton_carries_the_sequence_of_its_read(carton):
    runtime = _save(cameras=[{**READER, "qr_no_read": "ignore"}], sequence_input_id="in_seq")
    carton["readers"]["cam1"] = reader = _Reader("cam1")
    reader.delay = 0.1

    async def steps():
        await _poll()
        carton["plc"].values.update({SEQ: 3, CAP: True})
        await _poll()
        carton["plc"].values.update({SEQ: 4, CAP: False})
        await _poll()
        await asyncio.sleep(0.2)

    _run(steps)
    (payload, plc_event), = runtime.sent
    assert (payload["sequence"], plc_event["sequence"], payload["result"]) == (3, 3, "PASSED")
    read, = runtime.recent_reads()
    assert read["sequence"] == 3


def test_a_product_without_a_trigger_takes_the_value_of_that_moment(signals, lines):  # noqa: F811
    runtime = _save(cameras=[{"camera_id": "top", "model_id": "model-test"}],
                    inputs=[SEQUENCE_INPUT], sequence_input_id="in_seq")
    signals.values[SEQ] = 42

    async def steps():
        await _poll()
        _cross(runtime.counter, "top")

    _run(steps)
    (payload, plc_event), = runtime.sent
    assert (payload["sequence"], plc_event["sequence"]) == (42, 42)


def test_a_line_without_a_sequence_input_sends_what_it_sent(carton):
    runtime = _save()

    async def steps():
        await _poll()
        await _inspect(carton, 7)

    _run(steps)
    (payload, plc_event), = runtime.sent
    assert "sequence" not in payload and "sequence" not in plc_event
    assert "sequence" not in carton["readers"]["cam1"].requests[0]


def test_a_sequence_that_cannot_be_read_is_left_out(carton):
    runtime = _save(sequence_input_id="in_seq")

    async def steps():
        carton["plc"].values[SEQ] = 5
        await _poll()
        carton["plc"].unreadable.add(SEQ)       # an old value must not stand in for one nobody can see
        await _inspect(carton, 6)

    _run(steps)
    (payload, plc_event), = runtime.sent
    assert "sequence" not in payload and "sequence" not in plc_event and payload["result"] == "PASSED"


def test_the_reader_capture_record_carries_the_request(monkeypatch):
    from app.services import qr_service
    from app.state.application_state import app_state

    monkeypatch.setattr(app_state, "cameras", {})
    worker = qr_service._QRReaderWorker("cam-gone")
    try:
        record, _ = worker.capture({"sequence": 12, "request_id": 4, "trigger_no": 1})
        plain, _ = worker.capture({})
    finally:
        worker.stop()
    assert (record["sequence"], record["request_id"]) == (12, 4)
    assert "sequence" not in plain and "request_id" not in plain


def test_the_six_cartons_keep_their_sequences(carton, monkeypatch):
    from tests.test_production_records import _Recorder
    import app.services.production_records_service as records_module

    recorder = _Recorder()
    monkeypatch.setattr(records_module, "production_recorder", recorder)
    runtime = _save(sequence_input_id="in_seq")

    async def steps():
        await _poll()
        for sequence, code, names in ((1, GOOD_QR, ["carton"]),              # BOX-001 pass
                                      (2, GOOD_QR, ["carton", "damage"]),    # BOX-002 damaged
                                      (3, None, ["carton"]),                 # BOX-003 no QR
                                      (4, GOOD_QR, ["wrong_label"]),         # BOX-004 wrong label
                                      (5, GOOD_QR, ["carton"]),              # BOX-005 pass
                                      (6, "P-999;B-2026-07", ["carton"])):   # BOX-006 unknown product
            await _inspect(carton, sequence, code, names, settle=0.4 if code is None else 0.08)

    _run(steps)
    products = [(payload["sequence"], payload["result"]) for payload, _ in runtime.sent if "sequence" in payload]
    assert products == [(1, "PASSED"), (2, "REJECTED"), (3, "REJECTED"), (4, "REJECTED"), (5, "PASSED"), (6, "REJECTED")]
    counter = runtime.counter
    assert (counter.total_inspected, counter.good_count, counter.rejected_count) == (6, 2, 4)
    rows = [row for row in recorder._rows if row["kind"] == "product"]
    assert [row["details"]["sequence"] for row in rows] == [1, 2, 3, 4, 5, 6]


# ── Where the sequence shows ──────────────────────────────────────────────────

def test_the_value_name_the_message_field_and_the_placeholder():
    assert "sequence" in PLAIN_NAMES and "sequence" in TEMPLATE_FIELDS
    assert MESSAGE_FIELDS["sequence"] == ("sequence",)
    assert event_values.values_for({"sequence": 5})["sequence"] == 5
    assert "sequence" not in event_values.values_for({"result": "good"})
    assert render_template("BOX {sequence}", {"sequence": 5}) == "BOX 5"
    row = build_row({"event": "WIRELINE_OBJECT_CROSSED", "sequence": 5, "is_defect": False}, "product", True)
    assert row["details"]["sequence"] == 5
    headers = [name for name, _ in _FIELDS]
    assert headers.index("sequence") == headers.index("product_check") + 1
    script = (Path(__file__).resolve().parents[1] / "assets" / "card_values.js").read_text(encoding="utf-8")
    assert '{ name: "sequence"' in script


def test_product_checks_see_the_sequence(carton):
    runtime = _save(sequence_input_id="in_seq", product_checks=[
        {"name": "Carton 2", "only_when": {"rules": [{"left": "sequence", "op": "=", "right": "2"}]}}])

    async def steps():
        await _poll()
        await _inspect(carton, 1)
        await _inspect(carton, 2)

    _run(steps)
    assert [(p["sequence"], p["result"], p.get("product_check")) for p, _ in runtime.sent] == [
        (1, "PASSED", None), (2, "REJECTED", "Carton 2")]
