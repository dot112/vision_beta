"""A text reader on One picture per trigger is a station: each trigger's picture is read, and its
result is a product of its own (own) or joins the counting camera's product (join), with the reasons
text_wrong, no_text and reader_error. The same triggers as a vision camera: the API, a PLC "Take a
picture" input, the camera's own hardware trigger."""
from __future__ import annotations

import asyncio

import pytest

import app.services.line_service as line_module
from app.engines.ocr_engine import OcrLine
from app.engines.text_reading import TextField, TextResult, error_result, text_values
from app.events.alarm_events import alarm_manager
from app.services import trigger_inspection
from app.services.ocr_service import OcrReaderPipeline
from app.services.plc_input_service import PLCInputService
from app.services.trigger_inspection import trigger_camera, trigger_cameras
from app.services.vision_service import CameraStreamPipeline, ContinuousVisionRunner
from tests.test_joined_cameras import _joined, _line, _run, _totals, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import _line_with, _signal, _status, signals  # noqa: F401  (a fixture)
from tests.test_trigger_inspection import _trigger_cam, workers  # noqa: F401  (a fixture)
from tests.test_trigger_notice import Clock, HardwareCamera

BOX = [[10, 10], [100, 10], [100, 30], [10, 30]]
OCR = {"fields": [{"name": "Lot", "find": "all", "format": "LOT A99999"}]}


def _result(text, fits, reason):
    field = TextField("Lot", text, text, fits, 0.9 if text else None, BOX if text else None, [0, 0, 64, 48])
    return TextResult([field], [OcrLine(text, 0.9, BOX)] if text else [], 21.0,
                      "good" if reason is None else "reject", reason, None)


GOOD = _result("LOT A12345", True, None)
WRONG = _result("LOT A1234", False, "text_wrong")
NONE = _result(None, False, "no_text")
ERROR = error_result(OCR, "no model")


def _reader(camera_id="t1", **extra):
    return {"camera_id": camera_id, "role": "ocr", "count_mode": "trigger", "ocr": OCR, **extra}


class _TextWorker:
    """What the trigger paths use of a text reader's worker; ``process`` says what the picture read."""

    def __init__(self):
        self.is_busy = False
        self.taken = []
        self.last_inferred_frame = None
        self._job = None

    def submit_frame_if_idle(self, mat, frame_id, conf_thresh=None, copy=False, arrived_at=None, on_result=None,
                             on_failure=None):
        if self.is_busy:
            return False
        self.is_busy = True
        self.taken.append(frame_id)
        self._job = (mat, frame_id, arrived_at, on_result)
        return True

    def process(self, result):
        mat, frame_id, arrived_at, on_result = self._job
        self._job, self.last_inferred_frame, self.is_busy = None, mat, False
        if on_result is not None:
            on_result(frame_id, result, result.ms, arrived_at)

    def latest(self):
        return None

    def corner(self):
        return None

    def clear_corner(self):
        pass


@pytest.fixture
def text_workers(monkeypatch):
    made = {}
    monkeypatch.setattr(OcrReaderPipeline, "get_worker", classmethod(lambda cls, cid: made.setdefault(cid, _TextWorker())))
    monkeypatch.setattr(OcrReaderPipeline, "peek", classmethod(lambda cls, cid: made.get(cid)))
    return made


@pytest.fixture(autouse=True)
def _no_picture_alarms_left():
    yield
    for alarm in alarm_manager.active("camera:"):
        if alarm.code in ("camera.picture_missing", "camera.model_unavailable"):
            alarm_manager.clear_alarm(alarm.code, alarm.source, "the test is over")


async def _trigger_text(runtime, camera_id, result, timeout=1.0):
    task = asyncio.create_task(trigger_camera(runtime, camera_id, timeout=timeout))
    await asyncio.sleep(0.03)
    OcrReaderPipeline.get_worker(camera_id).process(result)
    return await task


def test_trigger_cameras_include_a_triggered_text_reader_only(lines):  # noqa: F811
    runtime = _line(lines, [_reader("t1"), {**_reader("c1"), "count_mode": "track"}])
    assert trigger_cameras(runtime) == ["t1"] and runtime.infers_on_demand("t1") and not runtime.infers_on_demand("c1")
    assert [row["camera_id"] for row in runtime.trigger_view()] == ["t1"]


@pytest.mark.parametrize("result, totals, reason, cls, status", [
    (GOOD, (1, 1, 0), None, "text ok", "good"),
    (WRONG, (1, 0, 1), "text_wrong", "text wrong", "wrong"),
    (NONE, (1, 0, 1), "no_text", "no text", "none"),
    (ERROR, (1, 0, 1), "reader_error", "reader error", "none"),
])
def test_an_own_text_reader_makes_one_product_per_trigger(lines, text_workers, fake_camera, result, totals, reason, cls, status):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("t1")])
    fake_camera("t1")
    seen = {}

    async def steps():
        seen["answer"] = await _trigger_text(runtime, "t1", result)

    _run(steps)
    answer = seen["answer"]
    assert _totals(runtime.aux_counters["t1"]) == totals and _totals(runtime.counter) == (0, 0, 0)
    payload, event = runtime.sent[0]
    assert (payload["class_name"], payload["reject_reason"], payload["camera_id"]) == (cls, reason, "t1")
    assert (answer["status"], answer["class_name"], answer["reject_reason"]) == ("inspected", cls, reason)
    assert (answer["text"], answer["text_status"], answer["inference_time_ms"]) == (text_values(result), status, result.ms)
    assert event["counting_camera"] is False
    read = runtime.recent_text_reads()[0]
    assert (read["camera_id"], read["result"], read["reason"], read["track_id"]) == \
        ("t1", "good" if reason is None else "reject", reason, answer["track_id"])
    row = runtime.trigger_view()[0]
    assert row["capture"]["class_name"] == cls and row["counters"]["results"] == 1


def test_a_reader_that_does_not_finish_in_time_is_a_reader_error(lines, text_workers, fake_camera, monkeypatch):  # noqa: F811
    monkeypatch.setattr(trigger_inspection, "TRIGGER_INFERENCE_TIMEOUT_S", 0.05)
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("t1")])
    fake_camera("t1")
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "t1", timeout=1.0)     # the worker never answers

    _run(steps)
    answer = seen["answer"]
    assert (answer["status"], answer["reject_reason"], answer["class_name"]) == ("inspected", "reader_error", "reader error")
    assert _totals(runtime.aux_counters["t1"]) == (1, 0, 1)
    assert runtime.recent_text_reads()[0]["reason"] == "reader_error"


def test_a_disconnected_reader_is_a_no_picture_reject(lines, text_workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("t1")])
    fake_camera("t1").is_connected = False
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "t1", timeout=0.1)

    _run(steps)
    assert (seen["answer"]["status"], seen["answer"]["reject_reason"]) == ("no_picture", "no_picture")
    assert text_workers.get("t1") is None or text_workers["t1"].taken == []


def test_a_joined_text_reader_decides_the_counting_cameras_product(lines, workers, text_workers, fake_camera):  # noqa: F811
    # A window wide enough for the reader's result on a busy machine (80 ms failed under the full suite).
    runtime = _line(lines, [_trigger_cam(), {**_reader("t1"), **_joined("t1", 0, 500, "reject")}])
    fake_camera("vis")
    fake_camera("t1")

    async def steps():
        task = asyncio.create_task(trigger_camera(runtime, "vis", timeout=1.0))
        await asyncio.sleep(0.03)
        CameraStreamPipeline.get_worker("vis").process("bottle")
        await task
        await _trigger_text(runtime, "t1", WRONG)
        await asyncio.sleep(0.05)
        task = asyncio.create_task(trigger_camera(runtime, "vis", timeout=1.0))
        await asyncio.sleep(0.03)
        CameraStreamPipeline.get_worker("vis").process("bottle")
        await task
        await asyncio.sleep(1.0)                           # the text reader reads nothing of it

    _run(steps)
    assert len(runtime.sent) == 2
    bad, missing = runtime.sent[0][0], runtime.sent[1][0]
    assert (bad["result"], bad["reject_reason"], bad["reject_camera_id"]) == ("REJECTED", "text_wrong", "t1")
    assert (missing["result"], missing["reject_reason"], missing["reject_camera_id"]) == ("REJECTED", "station_no_result", "t1")
    assert _totals(runtime.counter) == (2, 0, 2) and _totals(runtime.aux_counters["t1"]) == (0, 0, 0)


def test_a_capture_input_triggers_a_text_reader(signals, text_workers, fake_camera):  # noqa: F811
    line = _line_with(_signal("capture", camera_id="t1"), cameras=[_reader("t1")])
    runtime = line_module.line_manager.get(line["id"])
    runtime.sent = []
    for counter in [runtime.counter, *runtime.aux_counters.values()]:
        counter.dispatch_event = lambda payload, plc_event, is_defect: runtime.sent.append((payload, plc_event))
    fake_camera("t1")

    async def steps():
        signals.values["10001"] = False
        await PLCInputService.poll_once("ep1")
        signals.values["10001"] = True
        await PLCInputService.poll_once("ep1")             # the edge: the action waits for the next picture
        await asyncio.sleep(0.03)
        OcrReaderPipeline.get_worker("t1").process(GOOD)
        await PLCInputService.drain()

    _run(steps)
    assert runtime.sent[0][0]["class_name"] == "text ok"
    assert _status(line, "plcin_capture")["error"] is None


def test_a_hardware_triggered_reader_reads_every_picture(lines, text_workers, monkeypatch):  # noqa: F811
    from app.state.application_state import app_state

    runtime = _line(lines, [_reader("t1", picture_source="camera")])
    cam = HardwareCamera(Clock())
    monkeypatch.setitem(app_state.cameras, "t1", cam)
    cam.take(50)
    memory = ContinuousVisionRunner.new_memory()
    ContinuousVisionRunner._feed_camera("t1", cam, lines, memory)
    assert text_workers["t1"].taken == [1]

    async def steps():
        text_workers["t1"].process(WRONG)

    _run(steps)
    assert _totals(runtime.aux_counters["t1"]) == (1, 0, 1)
    assert runtime.sent[0][0]["reject_reason"] == "text_wrong"
    capture = runtime.trigger_view()[0]["capture"]
    assert (capture["reason"], capture["has_picture"]) == ("text_wrong", True)


def test_a_result_after_the_line_stopped_makes_nothing(lines, text_workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("t1")])
    fake_camera("t1")

    async def steps():
        task = asyncio.create_task(trigger_camera(runtime, "t1", timeout=1.0))
        await asyncio.sleep(0.03)
        runtime.enabled = False
        OcrReaderPipeline.get_worker("t1").process(GOOD)
        with pytest.raises(ValueError):
            await task

    _run(steps)
    assert _totals(runtime.aux_counters["t1"]) == (0, 0, 0) and runtime.recent_text_reads() == []


def test_the_status_row_of_a_text_reader(lines):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("t1"), {**_reader("t2"), **_joined("t2", 300, 500, "ignore")},
                            {**_reader("c1"), "count_mode": "track"}])
    rows = {row["camera_id"]: row for row in lines.summary(runtime)["cameras"]}
    assert (rows["t1"]["role"], rows["t1"]["count_mode"], rows["t1"]["station"]) == ("ocr", "trigger", "own")
    assert rows["t1"]["counts"] == {"total_inspected": 0, "good_count": 0, "rejected_count": 0}
    assert (rows["t2"]["station"], rows["t2"]["join_offset_ms"], rows["t2"]["join_missing"]) == ("join", 300, "ignore")
    assert "station_stats" in rows["t2"]
    assert rows["c1"].get("count_mode") is None and "station" not in rows["c1"]
