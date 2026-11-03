"""A pattern check's own thread: the frame runner feeds a continuous pattern check, its results are only
kept (for the video), a trigger's result goes to its callback, the alarm when it cannot check, and
where a trigger's picture goes by the camera's job."""
from __future__ import annotations

import logging
import time

import numpy as np
import pytest

from app.events.alarm_events import alarm_manager
from app.services import match_service
from app.services.match_service import PatternCheckPipeline, PatternCheckWorker, check_for_camera
from app.services.ocr_service import OcrReaderPipeline
from app.services.vision_service import CameraStreamPipeline, ContinuousVisionRunner, picture_worker
from app.utils.logger import LogThrottle
from tests.pattern_helpers import ERROR, GOOD, checker, write_set_file
from tests.test_joined_cameras import _line, lines  # noqa: F401  (a fixture)
from tests.test_trigger_on_demand import Clock, StreamCamera, _Spy


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

    def make(camera_id="m1", results=(GOOD,), on_check=None):
        queue = list(results)

        def check(cid, mat):
            if on_check is not None:
                on_check()
            item = queue.pop(0) if queue else GOOD
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(match_service, "check_for_camera", check)
        made.append(PatternCheckWorker(camera_id))
        return made[-1]

    yield make
    for w in made:
        w.stop()


@pytest.fixture(autouse=True)
def _no_alarms_left():
    yield
    for alarm in alarm_manager.active("camera:"):
        if alarm.code == "camera.model_unavailable":
            alarm_manager.clear_alarm(alarm.code, alarm.source, "the test is over")


def test_a_continuous_check_is_fed_frames(lines, monkeypatch):  # noqa: F811
    write_set_file()
    _line(lines, [checker("m1"), checker("t1", count_mode="trigger")])
    spies = {}
    monkeypatch.setattr(PatternCheckPipeline, "get_worker", classmethod(lambda cls, cid: spies.setdefault(cid, _Spy())))
    clock = Clock()
    memory = ContinuousVisionRunner.new_memory()
    for camera_id in ("m1", "t1"):
        ContinuousVisionRunner._feed_camera(camera_id, StreamCamera(clock, (0.0,)), lines, memory)
    assert spies["m1"].frames == [1]
    assert "t1" not in spies or spies["t1"].frames == []               # its pictures come from triggers


def test_its_results_are_only_kept(lines, worker, fake_camera):  # noqa: F811
    write_set_file()
    runtime = _line(lines, [checker("m1")])
    fake_camera("m1")
    w = worker()
    assert w.submit_frame_if_idle(np.zeros((480, 640, 3), np.uint8), 1)
    assert _wait(lambda: w.latest() is GOOD and not w.is_busy)
    assert runtime.sent == [] and runtime.counter.total_inspected == 0
    assert w.last_inferred_frame is not None


def test_a_result_goes_to_its_callback_instead(lines, worker, fake_camera):  # noqa: F811
    write_set_file()
    _line(lines, [checker("m1")])
    fake_camera("m1")
    got = []
    w = worker()
    w.submit_frame_if_idle(np.zeros((48, 64, 3), np.uint8), 5, arrived_at=12.5,
                           on_result=lambda fid, result, ms, arrived: got.append((fid, result, ms, arrived)))
    assert _wait(lambda: got)
    assert got == [(5, GOOD, 38.0, 12.5)]


def test_check_for_camera_without_a_set_is_an_error(lines):  # noqa: F811
    folder = write_set_file()
    _line(lines, [checker("m1"), {"camera_id": "vis"}])
    for path in folder.iterdir():
        path.unlink()
    folder.rmdir()
    result = check_for_camera("m1", np.zeros((48, 64, 3), np.uint8))
    assert result.reason == "pattern_error" and "missing" in result.error
    result = check_for_camera("vis", np.zeros((48, 64, 3), np.uint8))
    assert (result.reason, result.error) == ("pattern_error", "the camera is not a pattern check on a line")


def test_check_for_camera_uses_the_saved_settings(lines, monkeypatch):  # noqa: F811
    write_set_file()
    _line(lines, [checker("m1", match={**checker()["match"], "angle": 7})])
    seen = []
    monkeypatch.setattr(match_service, "load_set", lambda set_id: seen.append(set_id) or "the set")
    monkeypatch.setattr(match_service, "check_picture", lambda ref, match, mat: seen.append((ref, match["angle"])) or GOOD)
    assert check_for_camera("m1", np.zeros((48, 64, 3), np.uint8)) is GOOD
    assert seen == ["ref-0a1b2c3d", ("the set", 7)]


def test_an_error_raises_the_alarm_and_a_good_check_clears_it(lines, worker, fake_camera):  # noqa: F811
    write_set_file()
    _line(lines, [checker("m1")])
    fake_camera("m1")
    w = worker(results=[ERROR, GOOD])
    w.submit_frame_if_idle(np.zeros((48, 64, 3), np.uint8), 1)
    raised = lambda: [a for a in alarm_manager.active("camera:m1") if a.code == "camera.model_unavailable"]  # noqa: E731
    assert _wait(raised)
    assert "(pattern check)" in raised()[0].message and "missing" in raised()[0].message
    assert _wait(lambda: not w.is_busy)
    w.submit_frame_if_idle(np.zeros((48, 64, 3), np.uint8), 2)
    assert _wait(lambda: not raised())


def test_a_check_that_raises_is_logged_once_a_minute(lines, worker, fake_camera, caplog, monkeypatch):  # noqa: F811
    monkeypatch.setattr(match_service, "_error_log", LogThrottle(60.0))     # nothing logged by earlier tests
    write_set_file()
    _line(lines, [checker("m1")])
    fake_camera("m1")
    failures = []
    w = worker(results=[RuntimeError("boom")] * 3)
    with caplog.at_level(logging.WARNING):
        for frame in range(3):
            assert _wait(lambda: not w.is_busy)
            w.submit_frame_if_idle(np.zeros((48, 64, 3), np.uint8), frame,
                                   on_failure=lambda fid, why: failures.append((fid, why)))
            assert _wait(lambda: len(failures) == frame + 1)
    assert [why for _, why in failures] == ["RuntimeError: boom"] * 3
    assert len([r for r in caplog.records if "Pattern check failed" in r.getMessage()]) == 1


def test_picture_worker_by_role(monkeypatch):
    made = []
    monkeypatch.setattr(PatternCheckPipeline, "get_worker", classmethod(lambda cls, cid: made.append(("match", cid)) or "pattern"))
    monkeypatch.setattr(OcrReaderPipeline, "get_worker", classmethod(lambda cls, cid: made.append(("ocr", cid)) or "text"))
    monkeypatch.setattr(CameraStreamPipeline, "get_worker", classmethod(lambda cls, cid: made.append(("vision", cid)) or "model"))
    assert [picture_worker("a", "match"), picture_worker("b", "ocr"), picture_worker("c", "vision")] == ["pattern", "text", "model"]
    assert made == [("match", "a"), ("ocr", "b"), ("vision", "c")]
