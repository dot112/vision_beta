"""A text reader's own thread: the frame runner feeds a continuous reader, its reads are only shown
(the line's latest text reads), the corner picture, and the alarm when it cannot read."""
from __future__ import annotations

import time

import numpy as np
import pytest

import app.services.line_service as line_module
from app.engines.ocr_engine import OcrLine
from app.engines.text_reading import TextField, TextResult, error_result
from app.events.alarm_events import alarm_manager
from app.services import ocr_service
from app.services.ocr_service import OcrReaderPipeline, OcrReaderWorker, read_for_camera
from app.services.settings_persistence_service import SettingsPersistenceService
from app.services.vision_service import ContinuousVisionRunner
from tests.test_joined_cameras import _line, lines  # noqa: F401  (a fixture)
from tests.test_trigger_on_demand import Clock, StreamCamera, _Spy

BOX = [[10, 10], [100, 10], [100, 30], [10, 30]]
OCR = {"fields": [{"name": "Lot", "find": "all", "format": "LOT A99999"}]}
GOOD = TextResult([TextField("Lot", "LOT A12345", "LOT A12345", True, 0.9, BOX, [0, 0, 640, 480])],
                  [OcrLine("LOT A12345", 0.9, BOX)], 35.0, "good", None, None)


def _reader(camera_id="c1", **extra):
    return {"camera_id": camera_id, "role": "ocr", "ocr": OCR, **extra}


def _wait(test, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if test():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def worker(monkeypatch):
    made = []

    def make(camera_id="c1", results=(GOOD,), on_read=None):
        queue = list(results)

        def read(cid, mat):
            if on_read is not None:
                on_read()
            return queue.pop(0) if queue else GOOD

        monkeypatch.setattr(ocr_service, "read_for_camera", read)
        made.append(OcrReaderWorker(camera_id))
        return made[-1]

    yield make
    for w in made:
        w.stop()


@pytest.fixture(autouse=True)
def _no_reader_alarms_left():
    yield
    for alarm in alarm_manager.active("camera:"):
        if alarm.code == "camera.model_unavailable":
            alarm_manager.clear_alarm(alarm.code, alarm.source, "the test is over")


def test_a_continuous_reader_is_fed_frames(lines, monkeypatch):  # noqa: F811
    _line(lines, [_reader("c1"), _reader("t1", count_mode="trigger")])
    readers = {}
    monkeypatch.setattr(OcrReaderPipeline, "get_worker", classmethod(lambda cls, cid: readers.setdefault(cid, _Spy())))
    clock = Clock()
    memory = ContinuousVisionRunner.new_memory()
    for camera_id in ("c1", "t1"):
        ContinuousVisionRunner._feed_camera(camera_id, StreamCamera(clock, (0.0,)), lines, memory)
    assert readers["c1"].frames == [1]
    assert "t1" not in readers or readers["t1"].frames == []           # its pictures come from triggers


def test_its_reads_are_only_shown(lines, worker, fake_camera):  # noqa: F811
    runtime = _line(lines, [_reader("c1")])
    fake_camera("c1")
    w = worker()
    assert w.submit_frame_if_idle(np.zeros((480, 640, 3), np.uint8), 1)
    assert _wait(lambda: runtime.recent_text_reads())
    read = runtime.recent_text_reads()[0]
    assert read["fields"] == [{"name": "Lot", "text": "LOT A12345", "fits": True}]
    assert (read["camera_id"], read["status"], read["shown_only"], read["result"]) == ("c1", "good", True, None)
    assert runtime.sent == [] and runtime.counter.total_inspected == 0
    assert w.latest() is GOOD and _wait(lambda: not w.is_busy)


def test_the_same_text_within_the_hold_time_is_listed_once(lines):  # noqa: F811
    runtime = _line(lines, [_reader("c1", qr_hold_ms=1000)])
    assert runtime.show_text_read("c1", GOOD, now=10.0) is True
    assert runtime.show_text_read("c1", GOOD, now=10.5) is False
    assert runtime.show_text_read("c1", GOOD, now=11.6) is True
    nothing = error_result(OCR, "no model")
    assert runtime.show_text_read("c1", nothing, now=20.0) is False     # nothing read: not listed
    assert len(runtime.recent_text_reads()) == 2


def test_a_triggered_read_is_remembered_with_its_product(lines):  # noqa: F811
    runtime = _line(lines, [_reader("t1", count_mode="trigger")])
    runtime.remember_text_read("t1", GOOD, {"result": "good", "reject_reason": None, "track_id": 7})
    read = runtime.recent_text_reads()[0]
    assert (read["result"], read["reason"], read["track_id"], read["shown_only"]) == ("good", None, 7, False)
    assert len(runtime.recent_text_reads(limit=1)) == 1


def test_the_worker_keeps_the_corner_picture_of_its_first_field(lines, worker, fake_camera):  # noqa: F811
    _line(lines, [_reader("c1")])
    fake_camera("c1")
    w = worker()
    w.submit_frame_if_idle(np.full((200, 300, 3), 90, np.uint8), 1)
    assert _wait(lambda: w.corner() is not None)
    corner = w.corner()
    assert (corner.code, corner.known) == ("LOT A12345", True) and corner.image.ndim == 3
    w.clear_corner()
    assert w.corner() is None


def test_a_read_for_a_camera_no_line_has_is_not_shown(lines, worker, fake_camera):  # noqa: F811
    runtime = _line(lines, [_reader("c1")])
    fake_camera("c1")

    def leave():
        SettingsPersistenceService.save_line({"id": runtime.id, "name": runtime.name, "cameras": []})
        line_module.line_manager.apply_state()

    w = worker(on_read=leave)
    w.submit_frame_if_idle(np.zeros((48, 64, 3), np.uint8), 1)
    assert _wait(lambda: w.latest() is not None and not w.is_busy)
    assert runtime.recent_text_reads() == []


def test_a_result_goes_to_its_callback_instead(lines, worker, fake_camera):  # noqa: F811
    runtime = _line(lines, [_reader("c1")])
    fake_camera("c1")
    got = []
    w = worker()
    w.submit_frame_if_idle(np.zeros((48, 64, 3), np.uint8), 5, arrived_at=12.5,
                           on_result=lambda fid, result, ms, arrived: got.append((fid, result, ms, arrived)))
    assert _wait(lambda: got)
    assert got == [(5, GOOD, 35.0, 12.5)] and runtime.recent_text_reads() == []
    assert w.last_inferred_frame is not None


def test_a_reader_that_cannot_read_raises_an_alarm_and_clears_it(lines, worker, fake_camera):  # noqa: F811
    _line(lines, [_reader("c1")])
    fake_camera("c1")
    w = worker(results=[error_result(OCR, "no model"), GOOD])
    w.submit_frame_if_idle(np.zeros((48, 64, 3), np.uint8), 1)
    raised = lambda: [a for a in alarm_manager.active("camera:c1") if a.code == "camera.model_unavailable"]  # noqa: E731
    assert _wait(raised)
    assert "(text reader)" in raised()[0].message and "no model" in raised()[0].message
    assert _wait(lambda: not w.is_busy)
    w.submit_frame_if_idle(np.zeros((48, 64, 3), np.uint8), 2)
    assert _wait(lambda: not raised())


def test_read_for_camera_uses_the_cameras_settings(lines, monkeypatch):  # noqa: F811
    _line(lines, [_reader("c1"), {"camera_id": "vis"}])

    class Engine:
        def read(self, img, *, upside_down=False, allowed=None):
            return [OcrLine("LOT A12345", 0.9, BOX)]

    asked = []
    monkeypatch.setattr(ocr_service, "engine_for", lambda model: asked.append(model) or Engine())
    result = read_for_camera("c1", np.zeros((480, 640, 3), np.uint8))
    assert (result.result, result.fields[0].text, asked) == ("good", "LOT A12345", ["small"])
    result = read_for_camera("vis", np.zeros((48, 64, 3), np.uint8))
    assert (result.reason, result.error) == ("reader_error", "the camera is not a text reader on a line")


def test_the_recent_route(client, admin_headers):
    runtime = line_module.line_manager.get("line-1")
    runtime.remember_text_read("c9", GOOD, {"result": "good", "reject_reason": None, "track_id": 3})
    answer = client.get("/api/v1/lines/line-1/ocr/recent?limit=5", headers=admin_headers)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["line_id"] == "line-1" and body["reads"][0]["camera_id"] == "c9"
    assert client.get("/api/v1/lines/nope/ocr/recent", headers=admin_headers).status_code == 404
