"""Trigger inspection: a vision camera that takes one picture per trigger.

For indexing machines, rotary tables and parts that stand still. A trigger
(a PLC signal, the API) waits for the camera's next picture, and what the
model found in it is one product: counted, recorded and sent like a product
that crossed the count lines.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

import app.services.line_service as line_module
from app.events.alarm_events import alarm_manager
from app.schemas.counting import CountingConfig
from app.schemas.vision import BoundingBox, DetectionItem
from app.security.api_key_scopes import required_api_key_scopes
from app.services.card_triggers import card_fires, normalize_trigger
from app.services.line_config import (
    CAMERA_KEPT_KEYS,
    REASON_NOTHING_DETECTED,
    REJECT_REASON_CODES,
    REJECT_REASONS,
    normalize_camera,
)
from app.services.plc_input_service import PLCInputService
from app.services.production_records_service import build_row
from app.services.settings_persistence_service import SettingsPersistenceService, counting_config_from_dict
from app.services.trigger_inspection import (
    NOTHING_CLASS,
    TRIGGER_TIMEOUT_S,
    inspection_verdict,
    trigger_camera,
    trigger_cameras,
)
from app.services.vision_service import CameraStreamPipeline
from tests.conftest import FakeInferenceEngine
from tests.test_joined_cameras import LIST, _cross, _joined, _line, _run, _totals, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import _card, _line_with, _pass, _signal, _status, operator_headers, signals  # noqa: F401

API = "/api/v1"


def _det(name, conf=0.9):
    return DetectionItem(class_id=0, class_name=name, confidence=conf,
                         bbox=BoundingBox(x1=10, y1=20, x2=50, y2=80, width=40, height=60))


def _config(expected, defects, no_product="ignore", **extra):
    return CountingConfig(expected_classes=expected, defect_classes=defects, count_mode="trigger",
                          no_product=no_product, **extra)


def _trigger_cam(camera_id="vis", **extra):
    return {"camera_id": camera_id, "count_mode": "trigger", "expected_classes": ["bottle"],
            "defect_classes": ["dent"], **extra}


@pytest.fixture(autouse=True)
def _no_picture_alarms_left():
    """A trigger without a picture raises camera.picture_missing; it does not outlive its test."""
    yield
    for alarm in alarm_manager.active("camera:"):
        if alarm.code == "camera.picture_missing":
            alarm_manager.clear_alarm(alarm.code, alarm.source, "the test is over")


# ── What one picture says ─────────────────────────────────────────────────────

@pytest.mark.parametrize("names, expected, defects, no_product, verdict", [
    (["bottle"], ["bottle"], ["dent"], "ignore", ("bottle", False, None)),
    (["bottle", "dent"], ["bottle"], ["dent"], "ignore", ("dent", True, None)),
    (["cap"], [], ["dent"], "ignore", ("cap", False, None)),               # empty product list: any detection is a product
    (["scratch"], ["bottle"], [], "ignore", ("scratch", True, None)),      # the name rule, on by default
    ([], ["bottle"], ["dent"], "ignore", None),
    (["cap"], ["bottle"], ["dent"], "ignore", None),                       # neither a product nor a defect
    ([], ["bottle"], ["dent"], "reject", (NOTHING_CLASS, True, "nothing_detected")),
    (["cap"], ["bottle"], ["dent"], "reject", (NOTHING_CLASS, True, "nothing_detected")),
    ([], ["bottle"], ["dent"], "good", ("product", False, None)),
])
def test_what_one_picture_says(names, expected, defects, no_product, verdict):
    found = inspection_verdict([_det(name) for name in names], _config(expected, defects, no_product))
    if verdict is None:
        assert found is None
    else:
        assert (found["class_name"], found["is_defect"], found["reject_reason"]) == verdict


def test_the_verdict_takes_the_surest_detection_and_its_box():
    found = inspection_verdict([_det("bottle", 0.6), _det("bottle", 0.95)], _config(["bottle"], ["dent"]))
    assert found["confidence"] == 0.95 and found["bbox"]["x2"] == 50
    assert NOTHING_CLASS == "nothing detected" and TRIGGER_TIMEOUT_S == 2.0
    # With the name rule switched off, a class called "scratch" is only a defect when it is ticked.
    relaxed = _config([], [], name_based_defects=False)
    assert inspection_verdict([_det("scratch")], relaxed)["is_defect"] is False


def test_the_new_reason_has_a_number():
    assert REASON_NOTHING_DETECTED == "nothing_detected" and REASON_NOTHING_DETECTED in REJECT_REASONS
    assert REJECT_REASON_CODES["nothing_detected"] == 6


# ── The camera settings ───────────────────────────────────────────────────────

def test_a_camera_in_track_mode_saves_as_before():
    camera = normalize_camera({"camera_id": "vis", "model_id": "m", "count_mode": "track", "no_product": "reject"}, 0)
    assert "count_mode" not in camera and "no_product" not in camera
    assert "count_mode" not in normalize_camera({"camera_id": "vis", "model_id": "m"}, 0)
    config = counting_config_from_dict({}, camera)
    assert (config.count_mode, config.no_product) == ("track", "ignore")


def test_a_camera_in_trigger_mode_keeps_its_two_settings():
    camera = normalize_camera({"camera_id": "vis", "model_id": "m", "count_mode": "TRIGGER"}, 0)
    assert (camera["count_mode"], camera["no_product"]) == ("trigger", "ignore")
    camera = normalize_camera({"camera_id": "vis", "model_id": "m", "count_mode": "trigger", "no_product": "reject"}, 0)
    config = counting_config_from_dict({}, camera)
    assert (config.count_mode, config.no_product) == ("trigger", "reject")
    # A code reader has no count mode.
    assert "count_mode" not in normalize_camera({"camera_id": "qr", "role": "qr", "count_mode": "trigger"}, 0)


@pytest.mark.parametrize("bad, word", [
    ({"count_mode": "sometimes"}, "count mode"),
    ({"count_mode": "trigger", "no_product": "maybe"}, "nothing is found"),
])
def test_count_mode_validation(bad, word):
    with pytest.raises(ValueError, match=word):
        normalize_camera({"camera_id": "vis", "model_id": "m", **bad}, 0)


def test_count_mode_is_kept_when_a_client_leaves_it_out(lines):  # noqa: F811
    assert "count_mode" in CAMERA_KEPT_KEYS and "no_product" in CAMERA_KEPT_KEYS
    runtime = _line(lines, [_trigger_cam(no_product="reject")])
    older = SettingsPersistenceService.save_line({"id": runtime.id, "cameras": [{"camera_id": "vis", "model_id": "model-test"}]})
    assert (older["cameras"][0]["count_mode"], older["cameras"][0]["no_product"]) == ("trigger", "reject")
    back = SettingsPersistenceService.save_line(
        {"id": runtime.id, "cameras": [{"camera_id": "vis", "model_id": "model-test", "count_mode": "track"}]})
    assert "count_mode" not in back["cameras"][0] and "no_product" not in back["cameras"][0]


def test_a_reader_cannot_wait_for_a_wire_line_of_a_camera_that_does_not_track(lines):  # noqa: F811
    reader = {"camera_id": "qr", "role": "qr", "product_list_id": LIST, "qr_trigger": "line1"}
    with pytest.raises(ValueError, match="One picture per trigger.*Read codes to 'When an input asks'"):
        SettingsPersistenceService.save_line({"name": "Indexing", "cameras": [
            {"model_id": "model-test", **_trigger_cam()}, reader]})


def test_a_capture_signal_can_name_a_vision_camera_in_trigger_mode_only(lines):  # noqa: F811
    capture = _signal("capture", camera_id="vis")
    with pytest.raises(ValueError, match="Camera 1"):
        SettingsPersistenceService.save_line({"name": "Tracking", "cameras": [
            {"camera_id": "vis", "model_id": "model-test"}], "plc_inputs": [capture]})
    line = SettingsPersistenceService.save_line({"name": "Indexing", "cameras": [
        {"model_id": "model-test", **_trigger_cam()}], "plc_inputs": [capture]})
    assert line["cameras"][0]["triggers"] == [1] and "camera_id" not in line["plc_inputs"][0]
    # Back to tracking unticks the trigger on the camera; the input stays.
    line = SettingsPersistenceService.save_line({"id": line["id"], "cameras": [
        {"camera_id": "vis", "model_id": "model-test", "count_mode": "track"}]})
    assert "triggers" not in line["cameras"][0] and line["plc_inputs"][0]["trigger_no"] == 1


# ── The tracker in trigger mode ───────────────────────────────────────────────

def test_a_camera_in_trigger_mode_counts_nothing_from_its_tracker(lines):  # noqa: F811
    tracking = _line(lines, [{"camera_id": "vis"}], name="Tracking")
    _cross(tracking.counter, "vis")
    assert _totals(tracking.counter) == (1, 1, 0)

    indexing = _line(lines, [_trigger_cam("vis2")], name="Indexing")
    _cross(indexing.counter, "vis2")
    assert _totals(indexing.counter) == (0, 0, 0) and indexing.sent == []


def test_the_overlay_of_a_camera_in_trigger_mode_has_no_count_lines(lines, monkeypatch):  # noqa: F811
    import numpy as np

    from app.engines import inference_engine

    drawn = []
    monkeypatch.setattr(inference_engine, "_draw_count_lines", lambda *args, **kwargs: drawn.append(args))
    _line(lines, [{"camera_id": "vis"}], name="Tracking")
    _line(lines, [_trigger_cam("vis2")], name="Indexing")
    engine = inference_engine.InferenceEngine(device="cpu")
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    engine.draw_annotations_mat(frame.copy(), [], 1.0, camera_id="vis")
    assert len(drawn) == 1
    engine.draw_annotations_mat(frame.copy(), [], 1.0, camera_id="vis2")
    assert len(drawn) == 1


# ── The picture counter a trigger waits on ────────────────────────────────────

def test_the_inference_worker_counts_every_picture_it_gets_through(lines, monkeypatch):  # noqa: F811
    import time

    import numpy as np

    from app.services.vision_service import _CameraInferenceWorker

    engine = FakeInferenceEngine()
    monkeypatch.setattr(line_module.line_manager, "engine_for_camera", lambda camera_id: (engine, "fake-model", None))
    worker = _CameraInferenceWorker("cam-seq")
    frame = np.zeros((48, 64, 3), dtype=np.uint8)

    def process(frame_id):
        assert worker.submit_frame_if_idle(frame, frame_id)
        deadline = time.time() + 2
        while worker.is_busy and time.time() < deadline:
            time.sleep(0.005)

    try:
        assert worker.result_seq == 0
        process(7)
        assert worker.result_seq == 1 and len(worker.get_latest_inference()[0]) == 1
        process(1)                          # the camera reconnected: its frame ids start again
        assert worker.result_seq == 2
        engine.is_loaded = False            # a picture nothing is detected in is still one more picture
        process(2)
        assert worker.result_seq == 3 and worker.get_latest_inference()[0] == []
    finally:
        worker.stop()


# ── A trigger ─────────────────────────────────────────────────────────────────

class _Worker:
    """What trigger_camera uses of a camera's inference worker: it takes a picture when it is idle,
    and ``process`` says what the model found in it."""

    def __init__(self):
        self.result_seq = 0
        self.is_busy = False
        self.detections = []
        self.inference_ms = 4.2
        self.submitted = []
        self._waiting = None

    def get_latest_inference(self):
        return list(self.detections), self.inference_ms

    def submit_frame_if_idle(self, mat, frame_id, conf_thresh=None, copy=True, arrived_at=None, on_result=None):
        if self.is_busy:
            return False
        self.is_busy = True
        self.submitted.append(frame_id)
        self._waiting = (frame_id, arrived_at, on_result)
        return True

    def process(self, *names, conf=0.9):
        """The model got through one more picture and found these classes in it."""
        self.detections = [_det(name, conf) for name in names]
        self.result_seq += 1
        self.is_busy = False
        waiting, self._waiting = self._waiting, None
        if waiting is not None and waiting[2] is not None:
            waiting[2](waiting[0], list(self.detections), self.inference_ms, waiting[1])


@pytest.fixture
def workers(lines, monkeypatch):  # noqa: F811
    """Fake inference workers by camera id, and a loaded model for every camera."""
    made = {}
    monkeypatch.setattr(CameraStreamPipeline, "get_worker",
                        classmethod(lambda cls, camera_id: made.setdefault(camera_id, _Worker())))
    engine = FakeInferenceEngine()
    monkeypatch.setattr(lines, "engine_for_camera", lambda camera_id: (engine, "fake-model", None))
    return made


async def _trigger(runtime, made, camera_id, *names, timeout=1.0):
    """Trigger a camera; its next picture shows ``names``."""
    task = asyncio.create_task(trigger_camera(runtime, camera_id, timeout=timeout))
    await asyncio.sleep(0.03)
    CameraStreamPipeline.get_worker(camera_id).process(*names)
    return await task


def test_a_trigger_counts_one_good_product(lines, workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [_trigger_cam()])
    fake_camera("vis")
    assert trigger_cameras(runtime) == ["vis"]
    seen = {}

    async def steps():
        seen["first"] = await _trigger(runtime, workers, "vis", "bottle")
        seen["second"] = await _trigger(runtime, workers, "vis", "bottle")

    _run(steps)
    first = seen["first"]
    assert (first["camera_id"], first["status"], first["result"], first["class_name"]) == ("vis", "inspected", "good", "bottle")
    assert (first["reject_reason"], first["track_id"], first["detections"], first["inference_time_ms"]) == (None, 1, 1, 4.2)
    assert first["waited_ms"] >= 20
    assert _totals(runtime.counter) == (2, 2, 0)
    payloads, events = zip(*runtime.sent)
    assert [payload["track_id"] for payload in payloads] == [1, 2] and seen["second"]["track_id"] == 2
    assert payloads[0]["class_name"] == "bottle" and payloads[0]["camera_id"] == "vis" and payloads[0]["bbox"]["x1"] == 10
    assert (events[0]["event_type"], events[0]["result"], events[0]["counting_camera"]) == ("crossing", "good", True)
    assert len({event["event_id"] for event in events}) == 2
    row = build_row(payloads[0], "product", True)
    assert (row["result"], row["class_name"], row["track_id"], row["camera_id"]) == ("good", "bottle", 1, "vis")
    assert card_fires(normalize_trigger({"line_id": runtime.id, "trigger": "cross_line", "condition": "good"}), events[0])


def test_a_trigger_counts_one_reject(lines, workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [_trigger_cam()])
    fake_camera("vis")
    seen = {}

    async def steps():
        seen["result"] = await _trigger(runtime, workers, "vis", "bottle", "dent")

    _run(steps)
    assert (seen["result"]["result"], seen["result"]["class_name"], seen["result"]["reject_reason"]) == ("reject", "dent", "vision_class")
    assert _totals(runtime.counter) == (1, 0, 1)
    payload, event = runtime.sent[0]
    assert (payload["result"], payload["reject_reason"], payload["class_name"]) == ("REJECTED", "vision_class", "dent")
    assert build_row(payload, "product", True)["reject_reason"] == "vision_class"
    assert card_fires(normalize_trigger({"line_id": runtime.id, "trigger": "cross_line", "condition": "reject"}), event)


@pytest.mark.parametrize("no_product, status, totals", [
    ("ignore", "nothing", (0, 0, 0)),
    ("reject", "inspected", (1, 0, 1)),
    ("good", "inspected", (1, 1, 0)),
])
def test_nothing_found(lines, workers, fake_camera, no_product, status, totals):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(no_product=no_product)])
    fake_camera("vis")
    seen = {}

    async def steps():
        seen["result"] = await _trigger(runtime, workers, "vis")

    _run(steps)
    result = seen["result"]
    assert result["status"] == status and result["detections"] == 0
    assert _totals(runtime.counter) == totals
    if no_product == "ignore":
        assert result["result"] is None and runtime.sent == []
    elif no_product == "reject":
        payload, event = runtime.sent[0]
        assert (result["result"], result["reject_reason"]) == ("reject", "nothing_detected")
        assert (payload["reject_reason"], payload["class_name"]) == ("nothing_detected", NOTHING_CLASS)
        assert event["reject_reason"] == "nothing_detected" and event["result"] == "reject"
        assert build_row(payload, "product", True)["reject_reason"] == "nothing_detected"
    else:
        payload, event = runtime.sent[0]
        assert (result["result"], payload["class_name"], payload["reject_reason"]) == ("good", "product", None)


def test_a_picture_the_model_does_not_get_through_in_time_is_a_no_picture_reject(lines, workers, fake_camera, monkeypatch):  # noqa: F811
    from app.services import trigger_inspection

    monkeypatch.setattr(trigger_inspection, "TRIGGER_INFERENCE_TIMEOUT_S", 0.1)
    runtime = _line(lines, [_trigger_cam(no_product="ignore")])
    fake_camera("vis")
    seen = {}

    async def steps():
        seen["result"] = await trigger_camera(runtime, "vis", timeout=0.1)

    _run(steps)
    assert (seen["result"]["status"], seen["result"]["result"], seen["result"]["reject_reason"]) == (
        "no_picture", "reject", "no_picture")
    assert _totals(runtime.counter) == (1, 0, 1)
    assert runtime.sent[0][0]["reject_reason"] == "no_picture"


def test_a_trigger_while_the_worker_is_busy_skips_the_frame_in_flight(lines, workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [_trigger_cam()])
    fake_camera("vis")
    seen = {}

    async def steps():
        worker = CameraStreamPipeline.get_worker("vis")
        worker.is_busy = True               # this picture was taken before the trigger
        task = asyncio.create_task(trigger_camera(runtime, "vis", timeout=1.0))
        await asyncio.sleep(0.03)
        worker.process("dent")              # the part was not in place yet
        await asyncio.sleep(0.05)
        assert not task.done()
        worker.process("bottle")
        seen["result"] = await task

    _run(steps)
    assert (seen["result"]["result"], seen["result"]["class_name"]) == ("good", "bottle")
    assert _totals(runtime.counter) == (1, 1, 0)


def test_two_triggers_at_once_take_two_pictures(lines, workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [_trigger_cam()])
    fake_camera("vis")
    seen = {}

    async def steps():
        worker = CameraStreamPipeline.get_worker("vis")
        first = asyncio.create_task(trigger_camera(runtime, "vis", timeout=1.0))
        second = asyncio.create_task(trigger_camera(runtime, "vis", timeout=1.0))
        await asyncio.sleep(0.03)
        worker.process("bottle")
        await asyncio.sleep(0.05)
        assert first.done() and not second.done()
        worker.process("dent")
        seen["results"] = [await first, await second]

    _run(steps)
    assert [result["result"] for result in seen["results"]] == ["good", "reject"]
    assert _totals(runtime.counter) == (2, 1, 1)


def test_a_trigger_on_a_stopped_line_or_a_camera_that_cannot_take_it_is_refused(lines, workers, fake_camera, monkeypatch):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(), {"camera_id": "tracking", "station": "own"}])
    fake_camera("vis")
    fake_camera("tracking")
    seen = []

    async def refused(camera_id, word):
        with pytest.raises(ValueError, match=word):
            await trigger_camera(runtime, camera_id, timeout=0.1)
        seen.append(word)

    async def steps():
        await refused("tracking", "One picture per trigger")
        await refused("no-such-camera", "One picture per trigger")
        runtime.enabled = False
        await refused("vis", "stopped")
        runtime.enabled = True
        # A camera whose model is not loaded has nothing to judge a picture with.
        unloaded = FakeInferenceEngine()
        unloaded.is_loaded = False
        monkeypatch.setattr(lines, "engine_for_camera", lambda camera_id: (unloaded, "No model", None))
        await refused("vis", "model")

    _run(steps)
    assert len(seen) == 4
    assert _totals(runtime.counter) == (0, 0, 0) and runtime.sent == []


def test_a_trigger_on_a_disconnected_camera_is_a_no_picture_reject(lines, workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [_trigger_cam()])
    seen = {}

    async def steps():
        seen["absent"] = await trigger_camera(runtime, "vis", timeout=0.1)
        fake_camera("vis").is_connected = False
        seen["dropped"] = await trigger_camera(runtime, "vis", timeout=0.1)

    _run(steps)
    assert [(a["status"], a["result"]) for a in (seen["absent"], seen["dropped"])] == [("no_picture", "reject")] * 2
    assert _totals(runtime.counter) == (2, 0, 2)
    assert "vis" not in workers                     # nothing went to the model


def test_a_triggered_counting_camera_waits_for_its_code(lines, workers, fake_camera):  # noqa: F811
    reader = {"camera_id": "qr", "role": "qr", "product_list_id": LIST, "qr_action": "accept_listed", "qr_trigger": "signal", "qr_no_read": "reject"}
    runtime = _line(lines, [_trigger_cam(), reader])
    fake_camera("vis")
    assert runtime.sync_enabled

    async def steps():
        await _trigger(runtime, workers, "vis", "bottle")
        assert runtime.sent == []           # held until its code comes
        runtime.on_qr_read("qr", "LISTED", "QRCODE")
        await asyncio.sleep(0.05)
        await _trigger(runtime, workers, "vis", "bottle")
        await asyncio.sleep(0.2)            # no code follows this one

    _run(steps)
    assert len(runtime.sent) == 2
    assert (runtime.sent[0][0]["result"], runtime.sent[0][0]["qr_code"]) == ("PASSED", "LISTED")
    assert (runtime.sent[1][0]["result"], runtime.sent[1][0]["reject_reason"]) == ("REJECTED", "no_code")
    assert _totals(runtime.counter) == (2, 1, 1)


def test_a_joined_camera_in_trigger_mode_joins_the_product(lines, workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(), {**_trigger_cam("side", no_product="reject"), **_joined("side", 0, 80, "reject")}])
    fake_camera("vis")
    fake_camera("side")

    async def steps():
        await _trigger(runtime, workers, "vis", "bottle")
        await _trigger(runtime, workers, "side", "bottle")
        await asyncio.sleep(0.05)
        await _trigger(runtime, workers, "vis", "bottle")
        await _trigger(runtime, workers, "side")            # the side camera finds nothing
        await asyncio.sleep(0.2)

    _run(steps)
    assert len(runtime.sent) == 2
    good, bad = runtime.sent[0][0], runtime.sent[1][0]
    assert (good["result"], good["stations"][0]["result"]) == ("PASSED", "good")
    assert (bad["result"], bad["reject_reason"], bad["reject_camera_id"]) == ("REJECTED", "nothing_detected", "side")
    assert _totals(runtime.counter) == (2, 1, 1)
    assert _totals(runtime.aux_counters["side"]) == (0, 0, 0)       # it counts nothing on its own


def test_an_own_station_in_trigger_mode_counts_on_its_own_counter(lines, workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, {**_trigger_cam("station"), "station": "own"}])
    fake_camera("station")
    assert trigger_cameras(runtime) == ["station"]

    async def steps():
        await _trigger(runtime, workers, "station", "dent")

    _run(steps)
    assert _totals(runtime.counter) == (0, 0, 0)
    assert _totals(runtime.aux_counters["station"]) == (1, 0, 1)
    payload, event = runtime.sent[0]
    assert payload["camera_id"] == "station" and event["counting_camera"] is False


# ── From a PLC signal ─────────────────────────────────────────────────────────

def test_a_capture_signal_triggers_a_vision_camera_in_trigger_mode(signals, workers, fake_camera):  # noqa: F811
    line = _line_with(_signal("capture", camera_id="vis"), cameras=[{"model_id": "model-test", **_trigger_cam()}])
    runtime = line_module.line_manager.get(line["id"])
    runtime.sent = []
    runtime.counter.dispatch_event = lambda payload, plc_event, is_defect: runtime.sent.append((payload, plc_event))
    fake_camera("vis")

    async def steps():
        signals.values["10001"] = False
        await PLCInputService.poll_once("ep1")
        signals.values["10001"] = True
        await PLCInputService.poll_once("ep1")             # the edge: the action waits for the next picture
        await asyncio.sleep(0.03)
        CameraStreamPipeline.get_worker("vis").process("bottle")
        await PLCInputService.drain()

    _run(steps)
    assert _totals(runtime.counter) == (1, 1, 0)
    assert runtime.sent[0][0]["class_name"] == "bottle"
    assert _status(line, "plcin_capture")["error"] is None


def test_a_capture_signal_whose_picture_does_not_come_makes_a_no_picture_reject(signals, workers, fake_camera, monkeypatch):  # noqa: F811
    from app.services import trigger_inspection

    monkeypatch.setattr(trigger_inspection, "TRIGGER_TIMEOUT_S", 0.05)
    line = _line_with(_signal("capture", camera_id="vis"), cameras=[{"model_id": "model-test", **_trigger_cam()}])
    runtime = line_module.line_manager.get(line["id"])
    fake_camera("vis").is_connected = False

    async def steps():
        await PLCInputService._act(_card(line, "plcin_capture"), False, True)

    _run(steps)                                         # an answer, not an error on the card
    assert _totals(runtime.counter) == (1, 0, 1)


# ── The endpoints ─────────────────────────────────────────────────────────────

class _AutoWorker(_Worker):
    """A worker whose model gets through every picture at once and finds ``names`` in it."""

    def __init__(self, *names):
        super().__init__()
        self.detections = [_det(name) for name in names]

    def submit_frame_if_idle(self, mat, frame_id, conf_thresh=None, copy=True, arrived_at=None, on_result=None):
        self.submitted.append(frame_id)
        if on_result is not None:
            on_result(frame_id, list(self.detections), self.inference_ms, arrived_at)
        return True


@pytest.fixture
def trigger_line(client, admin_headers, vision_model, fake_engine, fake_camera, monkeypatch):
    """A running line through the API: 'cam-trig' in trigger mode, 'cam-track' tracking as an own station."""
    suffix = uuid.uuid4().hex[:6]
    trig, track = f"cam-trig-{suffix}", f"cam-track-{suffix}"
    made = {trig: _AutoWorker("bottle", "dent"), track: _AutoWorker("bottle")}
    monkeypatch.setattr(CameraStreamPipeline, "get_worker",
                        classmethod(lambda cls, camera_id: made.setdefault(camera_id, _AutoWorker())))
    fake_camera(trig)
    fake_camera(track)
    created = client.post(f"{API}/lines", headers=admin_headers, json={"name": f"Indexing {suffix}", "enabled": True, "cameras": [
        {"model_id": vision_model, **_trigger_cam(trig)},
        {"camera_id": track, "model_id": vision_model, "station": "own", "expected_classes": ["bottle"]},
    ]})
    assert created.status_code == 201, created.text
    line = created.json()
    started = client.post(f"{API}/lines/{line['id']}/start", headers=admin_headers)
    assert started.status_code == 200, started.text
    yield {"id": line["id"], "trig": trig, "track": track}
    client.post(f"{API}/lines/{line['id']}/stop", headers=admin_headers)
    client.delete(f"{API}/lines/{line['id']}", headers=admin_headers)


def _line_total(client, admin_headers, line_id):
    return client.get(f"{API}/lines/{line_id}", headers=admin_headers).json()["status"]["total_inspected"]


def test_the_line_trigger_endpoint(client, admin_headers, operator_headers, trigger_line):  # noqa: F811
    url = f"{API}/lines/{trigger_line['id']}/trigger"
    assert required_api_key_scopes("POST", "/api/v1/lines/line-1/trigger") == {"inspection:trigger"}
    assert client.post(url, headers=operator_headers).status_code == 403

    every = client.post(url, headers=admin_headers)
    assert every.status_code == 200, every.text
    assert every.json()["line_id"] == trigger_line["id"]
    assert [(r["camera_id"], r["status"], r["result"], r["class_name"]) for r in every.json()["results"]] == [
        (trigger_line["trig"], "inspected", "reject", "dent")]
    one = client.post(url, headers=admin_headers, json={"camera_id": trigger_line["trig"]})
    assert one.status_code == 200 and one.json()["results"][0]["track_id"] == 2
    assert _line_total(client, admin_headers, trigger_line["id"]) == 2

    tracking = client.post(url, headers=admin_headers, json={"camera_id": trigger_line["track"]})
    assert tracking.status_code == 409 and "One picture per trigger" in tracking.json()["detail"]
    assert client.post(f"{API}/lines/no-such-line/trigger", headers=admin_headers).status_code == 404
    client.post(f"{API}/lines/{trigger_line['id']}/stop", headers=admin_headers)
    stopped = client.post(url, headers=admin_headers)
    assert stopped.status_code == 409 and "stopped" in stopped.json()["detail"]
    assert _line_total(client, admin_headers, trigger_line["id"]) == 2


def test_the_line_trigger_endpoint_answers_no_picture_for_a_disconnected_camera(client, admin_headers, trigger_line):
    from app.state.application_state import app_state

    app_state.cameras[trigger_line["trig"]].is_connected = False
    res = client.post(f"{API}/lines/{trigger_line['id']}/trigger", headers=admin_headers)
    assert res.status_code == 200, res.text
    assert [(r["status"], r["result"], r["reject_reason"]) for r in res.json()["results"]] == [
        ("no_picture", "reject", "no_picture")]
    assert _line_total(client, admin_headers, trigger_line["id"]) == 1
    status = client.get(f"{API}/lines/{trigger_line['id']}", headers=admin_headers).json()["status"]
    row, = status["trigger_cameras"]
    assert row["capture"]["has_picture"] is False and row["counters"]["missing"] == 1
    assert alarm_manager.clear_source(f"camera:{trigger_line['trig']}") == 1      # camera.picture_missing


def test_the_line_trigger_without_a_camera_skips_cameras_that_take_their_own_picture(
        client, admin_headers, vision_model, fake_engine, fake_camera, monkeypatch):
    suffix = uuid.uuid4().hex[:6]
    system, own = f"cam-sys-{suffix}", f"cam-own-{suffix}"
    made = {system: _AutoWorker("bottle"), own: _AutoWorker("bottle")}
    monkeypatch.setattr(CameraStreamPipeline, "get_worker",
                        classmethod(lambda cls, camera_id: made.setdefault(camera_id, _AutoWorker())))
    fake_camera(system)
    fake_camera(own)
    created = client.post(f"{API}/lines", headers=admin_headers, json={"name": f"Mixed {suffix}", "enabled": True, "cameras": [
        {"model_id": vision_model, **_trigger_cam(system, picture_source="system")},
        {"model_id": vision_model, **_trigger_cam(own, picture_source="camera", notice_trigger=1)},
    ]})
    assert created.status_code == 201, created.text
    line_id = created.json()["id"]
    try:
        assert client.post(f"{API}/lines/{line_id}/start", headers=admin_headers).status_code == 200
        res = client.post(f"{API}/lines/{line_id}/trigger", headers=admin_headers, json={})
        assert res.status_code == 200, res.text
        assert [(r["camera_id"], r["status"]) for r in res.json()["results"]] == [(system, "inspected")]
        assert _line_total(client, admin_headers, line_id) == 1
    finally:
        client.post(f"{API}/lines/{line_id}/stop", headers=admin_headers)
        client.delete(f"{API}/lines/{line_id}", headers=admin_headers)


def test_a_line_whose_cameras_all_take_their_own_picture_answers_409_to_a_trigger(
        client, admin_headers, vision_model, fake_engine, fake_camera, monkeypatch):
    suffix = uuid.uuid4().hex[:6]
    own = f"cam-own-{suffix}"
    monkeypatch.setattr(CameraStreamPipeline, "get_worker", classmethod(lambda cls, camera_id: _AutoWorker("bottle")))
    fake_camera(own)
    created = client.post(f"{API}/lines", headers=admin_headers, json={"name": f"Own {suffix}", "enabled": True, "cameras": [
        {"model_id": vision_model, **_trigger_cam(own, picture_source="camera", notice_trigger=1)},
    ]})
    assert created.status_code == 201, created.text
    line_id = created.json()["id"]
    try:
        assert client.post(f"{API}/lines/{line_id}/start", headers=admin_headers).status_code == 200
        res = client.post(f"{API}/lines/{line_id}/trigger", headers=admin_headers, json={})
        assert res.status_code == 409 and "the server takes" in res.json()["detail"]
    finally:
        client.post(f"{API}/lines/{line_id}/stop", headers=admin_headers)
        client.delete(f"{API}/lines/{line_id}", headers=admin_headers)


def test_a_line_without_a_trigger_camera_answers_409(client, admin_headers):
    created = client.post(f"{API}/lines", headers=admin_headers, json={"name": f"Plain {uuid.uuid4().hex[:6]}"})
    line_id = created.json()["id"]
    try:
        res = client.post(f"{API}/lines/{line_id}/trigger", headers=admin_headers)
        assert res.status_code == 409 and "One picture per trigger" in res.json()["detail"]
    finally:
        client.delete(f"{API}/lines/{line_id}", headers=admin_headers)


OLD_KEYS = {"camera_id", "camera_name", "model_name", "total_detections", "detections", "inference_time_ms",
            "acquisition_time_ms", "total_latency_ms", "passed"}


def test_control_trigger_still_answers_as_before(client, admin_headers, trigger_line):
    res = client.post(f"{API}/control/trigger/{trigger_line['track']}", headers=admin_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"status", "camera_id", "inspection_result"} and body["status"] == "triggered"
    assert set(body["inspection_result"]) == OLD_KEYS and body["inspection_result"]["total_detections"] == 1
    assert _line_total(client, admin_headers, trigger_line["id"]) == 0        # it counts nothing, as before


def test_control_trigger_counts_for_a_camera_in_trigger_mode(client, admin_headers, trigger_line):
    res = client.post(f"{API}/control/trigger/{trigger_line['trig']}", headers=admin_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"status", "camera_id", "inspection_result", "product"} and body["status"] == "triggered"
    assert set(body["inspection_result"]) == OLD_KEYS
    assert (body["inspection_result"]["total_detections"], body["inspection_result"]["passed"]) == (2, False)
    assert [d["class_name"] for d in body["inspection_result"]["detections"]] == ["bottle", "dent"]
    assert (body["product"]["status"], body["product"]["result"], body["product"]["track_id"]) == ("inspected", "reject", 1)
    assert "detection_items" not in body["product"]
    assert _line_total(client, admin_headers, trigger_line["id"]) == 1
    client.post(f"{API}/lines/{trigger_line['id']}/stop", headers=admin_headers)
    assert client.post(f"{API}/control/trigger/{trigger_line['trig']}", headers=admin_headers).status_code == 409
