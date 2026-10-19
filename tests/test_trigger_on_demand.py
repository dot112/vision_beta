"""System trigger: the model runs only on the triggered picture, the Picture delay, and "no picture".

A vision camera with Count mode "One picture per trigger" and its picture taken by the system
gets no frame to its model until a trigger comes. On a trigger the server waits the card's
Picture delay, then takes the first frame that arrives after that moment (waiting up to
TRIGGER_TIMEOUT_S, counted after the delay) and runs the model on it. A trigger whose picture
never comes still gets one answer while the line runs: a product, rejected with the reason
"no picture", and the alarm camera.picture_missing until the camera's next picture.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import app.services.trigger_inspection as trigger_inspection
from app.engines.inference_engine import STATUS_STOPPED, STATUS_WAITING_TRIGGER
from app.events.alarm_events import AlarmCode, alarm_manager
from app.schemas.vision import BoundingBox, DetectionItem, DetectionResponse
from app.services.plc_input_service import PLCInputService
from app.services.settings_persistence_service import SettingsPersistenceService
from app.services.trigger_inspection import TRIGGER_TIMEOUT_S, no_picture_product, render_trigger_capture, trigger_camera
from app.services.vision_service import CameraStreamPipeline, ContinuousVisionRunner, inspection_status
from app.state.application_state import app_state
from tests.conftest import FakeInferenceEngine
from tests.test_camera_trigger_modes import DENT, _picture, rig  # noqa: F401  (a fixture)
from tests.test_ip_camera_http import _Still, _wait_for
from tests.test_joined_cameras import _line, _run, _totals, lines  # noqa: F401  (a fixture)
from tests.test_trigger_inspection import _det, _trigger_cam
from tests.test_video_overlay import _RecordingEngine

_real_sleep = asyncio.sleep
# The wall clock of the on_demand fixture at its clock's 0 (in the past, so no product time is held back to now).
WALL0 = 1_700_000_000.0


class Clock:
    """Time for the trigger code: each of its sleeps moves the clock on instead of waiting.

    ``at(t, fn)`` runs ``fn`` once when the clock reaches ``t`` (to stop a line meanwhile).
    """

    def __init__(self):
        self.now = 0.0
        self._due = []

    def __call__(self):
        return self.now

    def at(self, t, fn):
        self._due.append((t, fn))

    async def sleep(self, seconds):
        self.now += seconds
        for item in [d for d in self._due if d[0] <= self.now + 1e-9]:
            self._due.remove(item)
            item[1]()
        await _real_sleep(0)


class StreamCamera:
    """An IP or USB camera: frame n (1, 2, ...) arrives at ``times[n - 1]`` on the clock; its pixels hold n."""

    def __init__(self, clock, times, connected=True):
        self.clock, self.times = clock, list(times)
        self.is_connected = connected
        self.name = "Stream"
        self._mats = {}

    def get_latest_frame_id(self):
        return sum(1 for t in self.times if t <= self.clock.now + 1e-9)

    def get_latest_raw_mat(self, copy=True):
        fid = self.get_latest_frame_id()
        if fid == 0 or not self.is_connected:
            return False, None, 0
        mat = self._mats.setdefault(fid, np.full((120, 160, 3), fid, np.uint8))
        return True, (mat.copy() if copy else mat), fid

    def grab_raw_frame(self):
        return self.get_latest_raw_mat(copy=True)


class FrameEngine:
    """The model: it finds ``names`` in every picture and notes which frame it got (its pixel value).

    ``delay``: real seconds each picture takes; ``gate``: cleared, the model does not finish.
    """

    is_loaded = True
    task = "detect"

    def __init__(self):
        self.seen = []
        self.names = ["bottle"]
        self.delay = 0.0
        self.gate = threading.Event()
        self.gate.set()

    def predict_mat(self, mat, conf_threshold=None, nms_threshold=None):
        self.seen.append(int(mat[0, 0, 0]))
        self.gate.wait(30)
        time.sleep(self.delay)
        found = [_det(name) for name in self.names]
        return DetectionResponse(model_name="fake", total_detections=len(found), detections=found,
                                 inference_time_ms=2.0, image_width=mat.shape[1], image_height=mat.shape[0])


@pytest.fixture
def on_demand(lines, monkeypatch):  # noqa: F811
    """Virtual time for the trigger, the cameras' real inference workers with FrameEngine, and drawn pictures noted."""
    clock = Clock()
    engine = FrameEngine()
    rendered = []

    def render(camera_id, frame, detections):
        rendered.append((camera_id, frame, list(detections)))
        return b"\xff\xd8drawn"

    monkeypatch.setattr(trigger_inspection, "_now", clock)
    monkeypatch.setattr(trigger_inspection, "_sleep", clock.sleep)
    monkeypatch.setattr(trigger_inspection, "_wall", lambda: WALL0 + clock.now)
    monkeypatch.setattr(trigger_inspection, "render_trigger_capture", render)
    monkeypatch.setattr(lines, "engine_for_camera", lambda camera_id: (engine, "fake", None))
    alarm_manager.reset()
    made = []

    def camera(camera_id="vis", times=(), connected=True):
        cam = StreamCamera(clock, times, connected)
        monkeypatch.setitem(app_state.cameras, camera_id, cam)
        made.append(camera_id)
        return cam

    yield SimpleNamespace(clock=clock, engine=engine, rendered=rendered, camera=camera)
    engine.gate.set()
    for camera_id in {"vis", *made}:
        CameraStreamPipeline.remove_camera(camera_id)
    alarm_manager.reset()


def _counters(runtime, camera_id="vis"):
    return next(row["counters"] for row in runtime.trigger_view() if row["camera_id"] == camera_id)


def _missing_alarm(camera_id="vis"):
    return alarm_manager.get(AlarmCode.CAMERA_PICTURE_MISSING, f"camera:{camera_id}")


# ── The picture after the delay ───────────────────────────────────────────────

def test_stream_trigger_takes_the_first_frame_after_the_delay(lines, on_demand):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(picture_delay_ms=300)])
    on_demand.camera("vis", times=(0.100, 0.250, 0.320, 0.400))
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "vis", trigger_no=4)

    _run(steps)
    answer = seen["answer"]
    assert on_demand.engine.seen == [3]                  # the frame of 320 ms, not the one of 250 ms
    assert (answer["status"], answer["result"], answer["class_name"], answer["trigger_no"]) == ("inspected", "good", "bottle", 4)
    assert answer["waited_ms"] == pytest.approx(320, abs=15)
    assert _totals(runtime.counter) == (1, 1, 0)
    # The picture is the frame the model got through, with what it found in it.
    (camera_id, frame, detections), = on_demand.rendered
    assert camera_id == "vis" and int(frame[0, 0, 0]) == 3 and [d.class_name for d in detections] == ["bottle"]
    capture, jpeg = runtime.trigger_capture("vis")
    assert jpeg == b"\xff\xd8drawn" and capture["has_picture"] is True
    assert (capture["class_name"], capture["result"], capture["trigger_no"]) == ("bottle", "good", 4)
    counters = _counters(runtime)
    assert (counters["triggers"], counters["pictures"], counters["results"], counters["missing"]) == (1, 1, 1, 0)


def test_two_triggers_in_one_frame_interval_get_two_frames(lines, on_demand):  # noqa: F811   Review Focus 3
    runtime = _line(lines, [_trigger_cam()])
    on_demand.camera("vis", times=[0.033 * n for n in range(1, 60)])
    seen = {}

    async def steps():
        seen["answers"] = await asyncio.gather(trigger_camera(runtime, "vis"), trigger_camera(runtime, "vis"))

    _run(steps)
    assert [a["status"] for a in seen["answers"]] == ["inspected", "inspected"]
    assert len(on_demand.engine.seen) == 2 and on_demand.engine.seen[0] != on_demand.engine.seen[1]
    assert [int(frame[0, 0, 0]) for _, frame, _ in on_demand.rendered] == on_demand.engine.seen
    assert _totals(runtime.counter) == (2, 2, 0) and len(runtime.sent) == 2
    assert _counters(runtime)["triggers"] == 2 and _counters(runtime)["pictures"] == 2


@pytest.mark.parametrize("arrives, status", [(6.5, "inspected"), (7.2, "no_picture")])
def test_delay_does_not_eat_the_frame_timeout(lines, on_demand, arrives, status):  # noqa: F811   Review Focus 2 / 5
    assert TRIGGER_TIMEOUT_S == 2.0
    runtime = _line(lines, [_trigger_cam(picture_delay_ms=5000)])
    on_demand.camera("vis", times=(arrives,))
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "vis")

    _run(steps)
    # The 2 s for the frame start after the 5 s delay: 6.5 s is in time, 7.2 s is not.
    assert seen["answer"]["status"] == status


# ── No picture ────────────────────────────────────────────────────────────────

def test_no_frame_in_two_seconds_is_a_no_picture_reject(lines, on_demand):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(no_product="ignore")])
    on_demand.camera("vis", times=(0.0,))                # only a frame from before the trigger
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "vis", trigger_no=2, with_detections=True)

    _run(steps)
    answer = seen["answer"]
    assert (answer["status"], answer["result"], answer["class_name"], answer["reject_reason"]) == (
        "no_picture", "reject", "no picture", "no_picture")
    assert answer["trigger_no"] == 2 and answer["detection_items"] == [] and answer["track_id"] == 1
    assert on_demand.engine.seen == [] and on_demand.rendered == []
    # A product like any other reject: counted, recorded and sent.
    assert _totals(runtime.counter) == (1, 0, 1)
    payload, event = runtime.sent[0]
    assert (payload["result"], payload["reject_reason"], payload["class_name"]) == ("REJECTED", "no_picture", "no picture")
    assert (event["result"], event["reject_reason"], payload["trigger_no"]) == ("reject", "no_picture", 2)
    alarm = _missing_alarm()
    assert alarm is not None and alarm.severity.value == "warning"
    assert alarm.details["line_id"] == runtime.id and "no new picture" in alarm.details["why"]
    capture, jpeg = runtime.trigger_capture("vis")
    assert capture["has_picture"] is False and jpeg is None
    assert (capture["class_name"], capture["result"], capture["reason"]) == ("no picture", "reject", "no_picture")
    counters = _counters(runtime)
    assert (counters["triggers"], counters["pictures"], counters["results"], counters["missing"]) == (1, 0, 0, 1)


@pytest.mark.parametrize("attached", [True, False])
def test_disconnected_camera_is_a_no_picture_reject_not_an_error(lines, on_demand, attached):  # noqa: F811
    runtime = _line(lines, [_trigger_cam()])
    if attached:
        on_demand.camera("vis", times=(0.01, 0.02), connected=False)
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "vis")

    _run(steps)
    assert (seen["answer"]["status"], seen["answer"]["result"]) == ("no_picture", "reject")
    assert _totals(runtime.counter) == (1, 0, 1)
    assert "not connected" in _missing_alarm().details["why"]


@pytest.mark.parametrize("stop_at, delay_ms, times", [
    (0.1, 300, (0.2, 0.35)),        # during the Picture delay
    (0.5, 0, ()),                   # while waiting for a frame that never comes
    (0.5, 0, (0.6,)),               # while waiting for a frame that comes after the stop
])
def test_line_stopped_while_waiting_makes_no_product(lines, on_demand, stop_at, delay_ms, times):  # noqa: F811   Review Focus 2
    runtime = _line(lines, [_trigger_cam(picture_delay_ms=delay_ms, no_product="reject")])
    on_demand.camera("vis", times=times)
    on_demand.clock.at(stop_at, lambda: setattr(runtime, "enabled", False))
    seen = {}

    async def steps():
        with pytest.raises(ValueError, match="stopped"):
            await trigger_camera(runtime, "vis")
        seen["done"] = True

    _run(steps)
    assert seen["done"] and runtime.sent == [] and _totals(runtime.counter) == (0, 0, 0)
    assert _missing_alarm() is None and runtime.trigger_capture("vis") == (None, None)


def test_a_line_stopped_while_the_model_runs_makes_no_product(lines, on_demand, monkeypatch):  # noqa: F811
    runtime = _line(lines, [_trigger_cam()])
    on_demand.camera("vis", times=(0.01,))
    predict = on_demand.engine.predict_mat

    def stop_then_predict(mat, **kwargs):
        runtime.enabled = False
        return predict(mat, **kwargs)

    monkeypatch.setattr(on_demand.engine, "predict_mat", stop_then_predict)

    async def steps():
        with pytest.raises(ValueError, match="stopped"):
            await trigger_camera(runtime, "vis")

    _run(steps)
    assert runtime.sent == [] and _totals(runtime.counter) == (0, 0, 0)


def test_a_frame_after_the_camera_reconnected_is_new_though_its_number_is_lower(lines, on_demand):  # noqa: F811
    runtime = _line(lines, [_trigger_cam()])
    cam = on_demand.camera("vis", times=[0.001 * n for n in range(1, 8)])     # frames 1-7 before the trigger
    on_demand.clock.now = 0.05
    # It reconnects at 0.1 s and numbers its frames from 1 again.
    on_demand.clock.at(0.1, lambda: setattr(cam, "times", [0.12]))
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "vis")

    _run(steps)
    assert seen["answer"]["status"] == "inspected" and on_demand.engine.seen == [1]


def test_an_industrial_camera_is_not_fired_when_the_line_stops_during_the_delay(lines, rig):  # noqa: F811
    runtime = _line(lines, [_trigger_cam("cam", picture_delay_ms=200)])
    cam, backend = rig.camera("cam", trigger_mode="software")
    backend.on_trigger = lambda: backend.push(_picture(DENT))

    async def steps():
        task = asyncio.ensure_future(trigger_camera(runtime, "cam"))
        await asyncio.sleep(0.05)
        runtime.enabled = False
        with pytest.raises(ValueError, match="stopped"):
            await task

    _run(steps)
    assert ("execute", "TriggerSoftware") not in backend.calls and runtime.sent == []


def test_a_line_stopped_while_the_camera_takes_its_picture_makes_no_no_picture_product(lines, rig, monkeypatch):  # noqa: F811
    monkeypatch.setattr(trigger_inspection, "TRIGGER_TIMEOUT_S", 0.1)
    runtime = _line(lines, [_trigger_cam("cam")])
    cam, backend = rig.camera("cam", trigger_mode="software")
    backend.on_trigger = lambda: setattr(runtime, "enabled", False)       # stopped; no picture comes

    async def steps():
        with pytest.raises(ValueError, match="stopped"):
            await trigger_camera(runtime, "cam")

    _run(steps)
    assert runtime.sent == [] and _totals(runtime.counter) == (0, 0, 0)
    assert alarm_manager.get(AlarmCode.CAMERA_PICTURE_MISSING, "camera:cam") is None


def test_next_picture_clears_the_missing_alarm(lines, on_demand):  # noqa: F811
    runtime = _line(lines, [_trigger_cam()])
    on_demand.camera("vis", times=(3.0,))               # nothing for the first trigger, a frame for the second
    seen = {}

    async def steps():
        seen["first"] = await trigger_camera(runtime, "vis")
        seen["alarm"] = _missing_alarm()
        seen["second"] = await trigger_camera(runtime, "vis")

    _run(steps)
    assert seen["first"]["status"] == "no_picture" and seen["alarm"] is not None
    assert seen["second"]["status"] == "inspected" and _missing_alarm() is None
    counters = _counters(runtime)
    assert (counters["triggers"], counters["pictures"], counters["results"], counters["missing"]) == (2, 1, 1, 1)
    assert runtime.trigger_capture("vis")[0]["has_picture"] is True


def test_nothing_found_is_a_picture_too(lines, on_demand):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(no_product="ignore")])
    on_demand.camera("vis", times=(2.5, 2.6))           # after the first trigger gave up, in time for the second
    on_demand.engine.names = []
    seen = {}

    async def steps():
        seen["missing"] = await trigger_camera(runtime, "vis")
        seen["nothing"] = await trigger_camera(runtime, "vis")

    _run(steps)
    assert (seen["missing"]["status"], seen["nothing"]["status"]) == ("no_picture", "nothing")
    assert _missing_alarm() is None
    capture, jpeg = runtime.trigger_capture("vis")
    assert capture["has_picture"] is True and capture["result"] is None and jpeg is not None
    counters = _counters(runtime)
    assert (counters["pictures"], counters["results"], counters["missing"]) == (1, 1, 1)


def test_an_industrial_camera_on_system_trigger_is_fired_after_the_delay(lines, rig):  # noqa: F811
    runtime = _line(lines, [_trigger_cam("cam", picture_delay_ms=150)])
    cam, backend = rig.camera("cam", trigger_mode="software")
    fired = []

    def on_trigger():
        fired.append(time.monotonic())
        backend.push(_picture(DENT))

    backend.on_trigger = on_trigger
    seen = {}

    async def steps():
        started = time.monotonic()
        seen["answer"] = await trigger_camera(runtime, "cam")
        seen["after"] = fired[0] - started

    _run(steps)
    assert (seen["answer"]["status"], seen["answer"]["class_name"]) == ("inspected", "dent")
    assert seen["after"] >= 0.14


def test_a_camera_whose_card_says_it_takes_its_own_pictures_is_not_triggered(lines, on_demand):  # noqa: F811
    # Its Trigger may not be set to Hardware yet: the card decides, and nothing is made.
    runtime = _line(lines, [_trigger_cam(picture_source="camera")])
    on_demand.camera("vis", times=(0.01,))

    async def steps():
        with pytest.raises(ValueError, match="Camera trigger"):
            await trigger_camera(runtime, "vis")

    _run(steps)
    assert on_demand.engine.seen == [] and runtime.sent == [] and _counters(runtime)["triggers"] == 0


def test_capture_input_answers_no_picture_instead_of_raising(lines, on_demand):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(no_product="reject")])
    on_demand.camera("vis")                              # no frame comes
    hooked = []
    runtime.counter.product_hook = lambda crossing, payload, plc_event: hooked.append(crossing)
    seen = {}

    async def steps():
        seen["answer"] = await PLCInputService._capture_camera(runtime, "vis", 3, {"sequence": 42, "request_id": 7})

    _run(steps)
    answer = seen["answer"]
    assert (answer["status"], answer["result"], answer["reject_reason"], answer["trigger_no"]) == (
        "no_picture", "reject", "no_picture", 3)
    crossing, = hooked
    assert (crossing["sequence"], crossing["request_id"], crossing["trigger_no"]) == (42, 7, 3)
    assert crossing["reject_reason"] == "no_picture" and crossing["is_defect"] is True


# ── The model only on the triggered picture ──────────────────────────────────

class _Spy:
    is_busy = False

    def __init__(self):
        self.frames = []

    def submit_frame_if_idle(self, mat, fid, conf_thresh=None, copy=True, **kwargs):
        self.frames.append(fid)
        return True


def test_runner_does_not_feed_on_demand_cameras_to_the_model(lines, monkeypatch):  # noqa: F811
    from app.services.qr_service import QRReaderPipeline

    _line(lines, [{**_trigger_cam("vis"), "read_codes": True, "qr_trigger": "continuous"},
                  {"camera_id": "track", "expected_classes": ["bottle"], "station": "own"}])
    models, readers = {}, {}
    monkeypatch.setattr(CameraStreamPipeline, "get_worker", classmethod(lambda cls, cid: models.setdefault(cid, _Spy())))
    monkeypatch.setattr(QRReaderPipeline, "get_worker", classmethod(lambda cls, cid: readers.setdefault(cid, _Spy())))
    clock = Clock()
    cameras = {"vis": StreamCamera(clock, (0.0,)), "track": StreamCamera(clock, (0.0,))}
    memory = ContinuousVisionRunner.new_memory()
    for camera_id, cam in cameras.items():
        ContinuousVisionRunner._feed_camera(camera_id, cam, lines, memory)
    assert "vis" not in models or models["vis"].frames == []        # its model waits for a trigger
    assert readers["vis"].frames == [1]                              # its code reader is still fed
    assert models["track"].frames == [1]                             # a camera that tracks is fed as before


def test_annotated_video_of_an_on_demand_camera_has_no_boxes(lines):  # noqa: F811
    runtime = _line(lines, [_trigger_cam("c"), {"camera_id": "t", "expected_classes": ["bottle"], "station": "own"}])
    engine = FakeInferenceEngine()
    assert STATUS_WAITING_TRIGGER == "WAITING FOR TRIGGER"
    assert inspection_status(lines.route("c"), engine, "c") == STATUS_WAITING_TRIGGER
    assert inspection_status(lines.route("t"), engine, "t") is None
    runtime.enabled = False
    assert inspection_status(lines.route("c"), engine, "c") == STATUS_STOPPED


def test_the_video_of_an_on_demand_camera_is_drawn_without_the_last_results(monkeypatch):
    from app.services.line_service import line_manager

    engine = _RecordingEngine()
    runtime = SimpleNamespace(enabled=True, reads_codes=lambda camera_id: False, infers_on_demand=lambda camera_id: True)
    camera_id = "cam-video-on-demand"
    monkeypatch.setattr(line_manager, "engine_for_camera", lambda cid: (engine, "fake", None))
    monkeypatch.setattr(line_manager, "route", lambda cid: (runtime, "vision"))
    monkeypatch.setitem(app_state.cameras, camera_id, _Still(camera_id, 90))
    worker = CameraStreamPipeline.get_worker(camera_id)
    with worker._lock:
        worker._latest_detections, worker._latest_inference_ms = [_det("bottle")], 40.0
    publisher = CameraStreamPipeline.get_publisher(camera_id)
    publisher.add_subscriber()
    try:
        assert _wait_for(lambda: engine.calls)
        detections, kwargs = engine.calls[-1]
        assert kwargs.get("status") == STATUS_WAITING_TRIGGER
        assert (detections, kwargs["latency_ms"]) == ([], 0.0)
    finally:
        publisher.remove_subscriber()
        CameraStreamPipeline.remove_camera(camera_id)


def test_on_demand_cameras_are_not_inferring_and_not_in_fps(lines, monkeypatch):  # noqa: F811
    on_demand = _line(lines, [_trigger_cam("c")], name="Indexing")
    tracking = _line(lines, [{"camera_id": "t", "expected_classes": ["bottle"]}], name="Conveyor")
    hardware = _line(lines, [_trigger_cam("h", picture_source="camera")], name="Robot")
    monkeypatch.setitem(lines._engines, "model-test", {"engine": FakeInferenceEngine(), "name": "fake"})
    now = time.monotonic()
    for runtime, camera_id in ((on_demand, "c"), (tracking, "t"), (hardware, "h")):
        for n in range(20):
            runtime.record_frame(camera_id, now - 2 + n * 0.1)
    assert on_demand.infers_on_demand("c") is True
    assert not tracking.infers_on_demand("t") and not hardware.infers_on_demand("h")
    assert lines.inferring("c") is None
    assert lines.inferring("t") is True and lines.inferring("h") is True
    assert on_demand.processed_fps() == 0.0
    assert tracking.processed_fps() > 5 and hardware.processed_fps() > 5


# ── The picture of a trigger shows every box ──────────────────────────────────

def test_a_trigger_picture_shows_a_box_at_the_edge_and_no_exit_band(lines):  # noqa: F811   Ruling 8
    _line(lines, [_trigger_cam("c")])
    frame = np.zeros((120, 160, 3), np.uint8)
    # Centred 112 px down: in the 20 px band along the bottom, where a tracking camera's products leave.
    edge = DetectionItem(class_id=0, class_name="bottle", confidence=0.9,
                         bbox=BoundingBox(x1=60, y1=104, x2=100, y2=120, width=40, height=16))
    picture = cv2.imdecode(np.frombuffer(render_trigger_capture("c", frame, [edge]), np.uint8), cv2.IMREAD_COLOR)
    assert picture[104:118, 58:64].max() > 120, "the box at the edge was not drawn"
    assert picture[102:120, 0:40].max() < 30, "an exit band was drawn"


# ── The model's own time (fix round 1) ────────────────────────────────────────

def test_a_slow_first_inference_is_inspected_not_no_picture(lines, on_demand):  # noqa: F811
    # A model's first run on a GPU can take seconds: longer than the 2 s for the frame, within the 10 s for the model.
    assert (TRIGGER_TIMEOUT_S, trigger_inspection.TRIGGER_INFERENCE_TIMEOUT_S) == (2.0, 10.0)
    runtime = _line(lines, [_trigger_cam()])
    on_demand.camera("vis", times=(0.01,))
    on_demand.engine.delay = 2.3
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "vis")

    _run(steps)
    assert (seen["answer"]["status"], seen["answer"]["class_name"]) == ("inspected", "bottle")
    counters = _counters(runtime)
    assert (counters["pictures"], counters["results"], counters["missing"]) == (1, 1, 0)


def test_a_model_that_does_not_finish_in_time_is_a_no_picture_reject(lines, on_demand, monkeypatch):  # noqa: F811
    monkeypatch.setattr(trigger_inspection, "TRIGGER_INFERENCE_TIMEOUT_S", 0.3)
    runtime = _line(lines, [_trigger_cam()])
    on_demand.camera("vis", times=(0.01,))
    on_demand.engine.gate.clear()                        # the model never finishes
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "vis")

    _run(steps)
    assert (seen["answer"]["status"], seen["answer"]["reject_reason"]) == ("no_picture", "no_picture")
    assert _missing_alarm().details["why"] == "the model did not finish in time"
    counters = _counters(runtime)
    # A frame was taken (a picture), and the trigger got no picture through (missing), and no result.
    assert (counters["triggers"], counters["pictures"], counters["results"], counters["missing"]) == (1, 1, 0, 1)
    assert _totals(runtime.counter) == (1, 0, 1)


@pytest.mark.parametrize("finishes, status, counted", [(True, "inspected", (1, 1, 0)), (False, "no_picture", (1, 0, 1))])
def test_an_industrial_cameras_picture_has_the_models_own_time(lines, rig, monkeypatch, finishes, status, counted):  # noqa: F811
    # Scaled down: 0.2 s for the picture; the model takes 0.4 s (longer than the picture's time, so it has its
    # own), with 2 s to do it (a wide margin for a cold, slow machine), or never finishes (1 s is enough to say so).
    monkeypatch.setattr(trigger_inspection, "TRIGGER_TIMEOUT_S", 0.2)
    monkeypatch.setattr(trigger_inspection, "TRIGGER_INFERENCE_TIMEOUT_S", 2.0 if finishes else 1.0)
    runtime = _line(lines, [_trigger_cam("cam")])
    cam, backend = rig.camera("cam", trigger_mode="software")
    backend.on_trigger = lambda: backend.push(_picture(DENT))
    rig.engine.delay = 0.4
    if not finishes:
        rig.engine.gate.clear()
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "cam")

    _run(steps)
    rig.engine.gate.set()
    assert seen["answer"]["status"] == status
    counters = _counters(runtime, "cam")
    assert (counters["pictures"], counters["results"], counters["missing"]) == counted


def test_a_no_picture_product_has_the_time_of_the_trigger_and_its_delay(lines, on_demand):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(picture_delay_ms=300)])
    on_demand.camera("vis")                              # no frame comes
    on_demand.clock.now = 5.0                            # the trigger comes at 5 s
    hooked = []
    runtime.counter.product_hook = lambda crossing, payload, plc_event: hooked.append(crossing)

    async def steps():
        await trigger_camera(runtime, "vis")

    _run(steps)
    crossing, = hooked
    assert on_demand.clock.now >= 7.3 - 1e-6             # it was made when the 2 s for the frame were over ...
    assert crossing["time"] == pytest.approx(WALL0 + 5.3, abs=1e-3)  # ... and has the trigger's time plus its delay


def test_no_picture_product_takes_the_time_it_is_given(lines):  # noqa: F811
    runtime = _line(lines, [_trigger_cam()])
    hooked = []
    runtime.counter.product_hook = lambda crossing, payload, plc_event: hooked.append(crossing)
    at = time.time() - 4.0

    async def steps():
        no_picture_product(runtime, "vis", "no picture within 1000 ms of the notice", trigger_no=5, at=at)
        no_picture_product(runtime, "vis", "the camera is not connected")

    _run(steps)
    first, second = hooked
    assert first["time"] == pytest.approx(at, abs=1e-3) and first["trigger_no"] == 5
    assert second["time"] == pytest.approx(time.time(), abs=1.0)        # without one: now
    alarm_manager.clear_source("camera:vis")


# ── A warm model for the first trigger (fix round 1) ─────────────────────────

class _WarmEngine(FakeInferenceEngine):
    """A loaded model of input size 320 x 256 that notes the size of each picture it is given."""

    input_size = (320, 256)

    def __init__(self, fail=False):
        super().__init__()
        self.shapes = []
        self.fail = fail

    def predict_mat(self, mat, conf_threshold=None, nms_threshold=None):
        self.shapes.append(mat.shape)
        if self.fail:
            raise RuntimeError("the GPU said no")
        return super().predict_mat(mat, conf_threshold, nms_threshold)


@pytest.fixture
def models(lines, monkeypatch):  # noqa: F811
    """Each model loads as its own _WarmEngine ("m-broken" as one whose run fails)."""
    made = {}

    async def load(model_id):
        made[model_id] = _WarmEngine(fail=model_id == "m-broken")
        return {"engine": made[model_id], "name": model_id}, None

    monkeypatch.setattr(lines, "_load_engine", load)
    return made


async def _settle(seconds=0.3):
    await asyncio.sleep(seconds)                        # the warm-up runs off the event loop


def _cards(model_id="m-trigger"):
    return [{**_trigger_cam("c"), "model_id": model_id},
            {"camera_id": "t", "model_id": "m-track", "expected_classes": ["bottle"], "station": "own"}]


def test_the_model_of_an_on_demand_camera_is_warmed_up_once_when_it_is_loaded(lines, models):  # noqa: F811
    _line(lines, _cards())

    async def steps():
        await lines.refresh_models()
        await _settle()
        await lines.refresh_models()                    # loaded already: not again
        await _settle()

    _run(steps)
    assert models["m-trigger"].shapes == [(256, 320, 3)]     # one dummy picture of the model's input size
    assert models["m-track"].shapes == []                   # a camera that infers every frame warms its own


def test_the_model_of_an_on_demand_camera_is_warmed_up_when_its_line_starts(lines, models):  # noqa: F811
    line = SettingsPersistenceService.save_line({"name": "Indexing", "cameras": _cards(), "enabled": False})
    lines.apply_state()

    async def steps():
        await lines.refresh_models()
        await _settle()
        assert models["m-trigger"].shapes == []         # loaded, but its line is stopped
        SettingsPersistenceService.save_line({"id": line["id"], "name": "Indexing", "cameras": _cards(), "enabled": True})
        lines.apply_state()
        await lines.refresh_models()
        await _settle()

    _run(steps)
    assert models["m-trigger"].shapes == [(256, 320, 3)]


def test_a_warm_up_that_fails_is_only_logged(lines, models, caplog):  # noqa: F811
    _line(lines, _cards("m-broken"))

    async def steps():
        assert await lines.refresh_models() == {}
        await _settle()

    _run(steps)
    assert models["m-broken"].shapes == [(256, 320, 3)]
    assert "could not be warmed up" in caplog.text and "the GPU said no" in caplog.text
