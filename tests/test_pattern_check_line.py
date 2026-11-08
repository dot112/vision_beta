"""A pattern check on One picture per trigger is a station: each trigger's picture is checked, and its result
is a product of its own (own) or joins the counting camera's product (join), with the reasons
pattern_not_found, pattern_mismatch, pattern_defect and pattern_error. The same triggers as a vision camera:
the API, a PLC "Take a picture" input, the camera's own hardware trigger."""
from __future__ import annotations

import asyncio

import pytest

import app.services.line_service as line_module
from app.engines.pattern_check import match_values
from app.events.alarm_events import alarm_manager
from app.services import trigger_inspection
from app.services.match_service import PatternCheckPipeline
from app.services.plc_input_service import PLCInputService
from app.services.trigger_inspection import trigger_camera, trigger_cameras
from app.services.vision_service import CameraStreamPipeline, ContinuousVisionRunner
from tests.pattern_helpers import DEFECT, ERROR, GOOD, MISMATCH, NOT_FOUND, checker, write_set_file
from tests.test_joined_cameras import _joined, _line, _run, _totals, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import _line_with, _signal, _status, signals  # noqa: F401  (a fixture)
from tests.test_trigger_inspection import _trigger_cam, workers  # noqa: F401  (a fixture)
from tests.test_trigger_notice import Clock, HardwareCamera


def _checker(camera_id="m1", **extra):
    return checker(camera_id, count_mode="trigger", **extra)


class _MatchWorker:
    """What the trigger paths use of a pattern check's worker; ``process`` says what the picture gave."""

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


@pytest.fixture
def match_workers(monkeypatch, lines):  # noqa: F811
    write_set_file()
    made = {}
    monkeypatch.setattr(PatternCheckPipeline, "get_worker", classmethod(lambda cls, cid: made.setdefault(cid, _MatchWorker())))
    monkeypatch.setattr(PatternCheckPipeline, "peek", classmethod(lambda cls, cid: made.get(cid)))
    return made


@pytest.fixture(autouse=True)
def _no_picture_alarms_left():
    yield
    for alarm in alarm_manager.active("camera:"):
        if alarm.code in ("camera.picture_missing", "camera.model_unavailable"):
            alarm_manager.clear_alarm(alarm.code, alarm.source, "the test is over")


async def _trigger_check(runtime, camera_id, result, timeout=1.0):
    task = asyncio.create_task(trigger_camera(runtime, camera_id, timeout=timeout))
    await asyncio.sleep(0.03)
    PatternCheckPipeline.get_worker(camera_id).process(result)
    return await task


def test_trigger_cameras_include_a_triggered_pattern_check_only(lines, match_workers):  # noqa: F811
    runtime = _line(lines, [_checker("m1"), checker("c1")])
    assert trigger_cameras(runtime) == ["m1"] and runtime.infers_on_demand("m1") and not runtime.infers_on_demand("c1")
    assert [row["camera_id"] for row in runtime.trigger_view()] == ["m1"]


@pytest.mark.parametrize("result, totals, reason, cls, status", [
    (GOOD, (1, 1, 0), None, "pattern ok", "good"),
    (NOT_FOUND, (1, 0, 1), "pattern_not_found", "pattern not found", "not_found"),
    (MISMATCH, (1, 0, 1), "pattern_mismatch", "pattern mismatch", "mismatch"),
    (DEFECT, (1, 0, 1), "pattern_defect", "pattern defect", "defect"),
    (ERROR, (1, 0, 1), "pattern_error", "pattern error", "error"),
])
def test_an_own_pattern_check_makes_one_product_per_trigger(lines, match_workers, fake_camera, result, totals, reason, cls, status):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, _checker("m1")])
    fake_camera("m1")
    seen = {}

    async def steps():
        seen["answer"] = await _trigger_check(runtime, "m1", result)

    _run(steps)
    answer = seen["answer"]
    assert _totals(runtime.aux_counters["m1"]) == totals and _totals(runtime.counter) == (0, 0, 0)
    payload, event = runtime.sent[0]
    assert (payload["class_name"], payload["reject_reason"], payload["camera_id"]) == (cls, reason, "m1")
    assert payload["match_status"] == status and payload["match"] == match_values(result)
    assert (answer["status"], answer["class_name"], answer["reject_reason"]) == ("inspected", cls, reason)
    assert (answer["match"], answer["match_status"], answer["inference_time_ms"]) == (match_values(result), status, result.ms)
    assert event["counting_camera"] is False
    check = runtime.recent_pattern_checks()[0]
    assert (check["camera_id"], check["result"], check["reason"], check["track_id"], check["status"]) == \
        ("m1", "good" if reason is None else "reject", reason, answer["track_id"], status)
    assert check["checks"] == [{"name": c.name, "score": c.score, "defect_px": c.defect_px, "fits": c.fits}
                               for c in result.checks]
    row = runtime.trigger_view()[0]
    assert row["capture"]["class_name"] == cls and row["counters"]["results"] == 1


def test_a_check_that_does_not_finish_in_time_is_a_pattern_error(lines, match_workers, fake_camera, monkeypatch):  # noqa: F811
    monkeypatch.setattr(trigger_inspection, "TRIGGER_INFERENCE_TIMEOUT_S", 0.05)
    runtime = _line(lines, [{"camera_id": "vis"}, _checker("m1")])
    fake_camera("m1")
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "m1", timeout=1.0)     # the worker never answers

    _run(steps)
    answer = seen["answer"]
    assert (answer["status"], answer["reject_reason"], answer["class_name"]) == ("inspected", "pattern_error", "pattern error")
    assert _totals(runtime.aux_counters["m1"]) == (1, 0, 1)
    assert runtime.recent_pattern_checks()[0]["reason"] == "pattern_error"


def test_a_disconnected_camera_is_a_no_picture_reject(lines, match_workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, _checker("m1")])
    fake_camera("m1").is_connected = False
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "m1", timeout=0.1)

    _run(steps)
    assert (seen["answer"]["status"], seen["answer"]["reject_reason"]) == ("no_picture", "no_picture")
    assert match_workers.get("m1") is None or match_workers["m1"].taken == []


def test_a_joined_pattern_check_decides_the_counting_cameras_product(lines, workers, match_workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(), {**_checker("m1"), **_joined("m1", 0, 500, "reject")}])
    fake_camera("vis")
    fake_camera("m1")

    async def steps():
        task = asyncio.create_task(trigger_camera(runtime, "vis", timeout=1.0))
        await asyncio.sleep(0.03)
        CameraStreamPipeline.get_worker("vis").process("bottle")
        await task
        await _trigger_check(runtime, "m1", DEFECT)
        await asyncio.sleep(0.05)
        task = asyncio.create_task(trigger_camera(runtime, "vis", timeout=1.0))
        await asyncio.sleep(0.03)
        CameraStreamPipeline.get_worker("vis").process("bottle")
        await task
        await asyncio.sleep(1.0)                           # the pattern check sees nothing of it

    _run(steps)
    assert len(runtime.sent) == 2
    bad, missing = runtime.sent[0][0], runtime.sent[1][0]
    assert (bad["result"], bad["reject_reason"], bad["reject_camera_id"]) == ("REJECTED", "pattern_defect", "m1")
    assert (missing["result"], missing["reject_reason"], missing["reject_camera_id"]) == ("REJECTED", "station_no_result", "m1")
    assert _totals(runtime.counter) == (2, 0, 2) and _totals(runtime.aux_counters["m1"]) == (0, 0, 0)


def test_a_capture_input_triggers_a_pattern_check(signals, match_workers, fake_camera):  # noqa: F811
    line = _line_with(_signal("capture", camera_id="m1"), cameras=[_checker("m1")])
    runtime = line_module.line_manager.get(line["id"])
    runtime.sent = []
    for counter in [runtime.counter, *runtime.aux_counters.values()]:
        counter.dispatch_event = lambda payload, plc_event, is_defect: runtime.sent.append((payload, plc_event))
    fake_camera("m1")

    async def steps():
        signals.values["10001"] = False
        await PLCInputService.poll_once("ep1")
        signals.values["10001"] = True
        await PLCInputService.poll_once("ep1")             # the edge: the action waits for the next picture
        await asyncio.sleep(0.03)
        PatternCheckPipeline.get_worker("m1").process(GOOD)
        await PLCInputService.drain()

    _run(steps)
    assert runtime.sent[0][0]["class_name"] == "pattern ok"
    assert _status(line, "plcin_capture")["error"] is None


def test_a_hardware_triggered_check_checks_every_picture(lines, match_workers, monkeypatch):  # noqa: F811
    from app.state.application_state import app_state

    runtime = _line(lines, [_checker("m1", picture_source="camera")])
    cam = HardwareCamera(Clock())
    monkeypatch.setitem(app_state.cameras, "m1", cam)
    cam.take(50)
    memory = ContinuousVisionRunner.new_memory()
    ContinuousVisionRunner._feed_camera("m1", cam, lines, memory)
    assert match_workers["m1"].taken == [1]

    async def steps():
        match_workers["m1"].process(DEFECT)

    _run(steps)
    assert _totals(runtime.aux_counters["m1"]) == (1, 0, 1)
    assert runtime.sent[0][0]["reject_reason"] == "pattern_defect"
    capture = runtime.trigger_view()[0]["capture"]
    assert (capture["reason"], capture["has_picture"]) == ("pattern_defect", True)


def test_a_result_after_the_line_stopped_makes_nothing(lines, match_workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, _checker("m1")])
    fake_camera("m1")

    async def steps():
        task = asyncio.create_task(trigger_camera(runtime, "m1", timeout=1.0))
        await asyncio.sleep(0.03)
        runtime.enabled = False
        PatternCheckPipeline.get_worker("m1").process(GOOD)
        with pytest.raises(ValueError):
            await task

    _run(steps)
    assert _totals(runtime.aux_counters["m1"]) == (0, 0, 0) and runtime.recent_pattern_checks() == []


def test_the_status_row_of_a_pattern_check(lines, match_workers):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, _checker("m1"), {**_checker("m2"), **_joined("m2", 300, 500, "ignore")},
                            checker("c1")])
    rows = {row["camera_id"]: row for row in lines.summary(runtime)["cameras"]}
    assert (rows["m1"]["role"], rows["m1"]["count_mode"], rows["m1"]["station"]) == ("match", "trigger", "own")
    assert rows["m1"]["counts"] == {"total_inspected": 0, "good_count": 0, "rejected_count": 0}
    assert (rows["m2"]["station"], rows["m2"]["join_offset_ms"], rows["m2"]["join_missing"]) == ("join", 300, "ignore")
    assert "station_stats" in rows["m2"]
    assert rows["c1"].get("count_mode") is None and "station" not in rows["c1"]


def test_the_recent_checks_are_kept_and_cleared(lines, match_workers):  # noqa: F811
    runtime = _line(lines, [_checker("m1")])
    for track in range(60):
        runtime.remember_pattern_check("m1", GOOD, {"result": "good", "reject_reason": None, "track_id": track})
    assert len(runtime.recent_pattern_checks()) == 50 and runtime.recent_pattern_checks(limit=1)[0]["track_id"] == 59
    runtime.reset()
    assert runtime.recent_pattern_checks() == []


def test_the_recent_route(client, admin_headers):
    runtime = line_module.line_manager.get("line-1")
    runtime.remember_pattern_check("m9", GOOD, {"result": "good", "reject_reason": None, "track_id": 3})
    answer = client.get("/api/v1/lines/line-1/match/recent?limit=5", headers=admin_headers)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["line_id"] == "line-1" and body["checks"][0]["camera_id"] == "m9" and body["checks"][0]["score"] == 98
    assert client.get("/api/v1/lines/no-such-line/match/recent", headers=admin_headers).status_code == 404


def test_a_check_that_raises_is_a_pattern_error_at_once(lines, fake_camera, monkeypatch):  # noqa: F811
    """Review I2: with the real checker thread, a check that raises answers the trigger with pattern_error, not a 10 s
    wait for a model (nor a lost product on the hardware paths, which take the same on_result)."""
    from app.services import match_service

    write_set_file()

    def boom(camera_id, mat):
        raise RuntimeError("boom")

    monkeypatch.setattr(match_service, "check_for_camera", boom)
    runtime = _line(lines, [{"camera_id": "vis"}, _checker("m1")])
    fake_camera("m1")
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "m1", timeout=1.0)

    try:
        _run(steps)
    finally:
        PatternCheckPipeline.remove_camera("m1")
    answer = seen["answer"]
    assert (answer["status"], answer["reject_reason"], answer["class_name"]) == ("inspected", "pattern_error", "pattern error")
    assert answer["waited_ms"] < 3000 and _totals(runtime.aux_counters["m1"]) == (1, 0, 1)
    assert runtime.recent_pattern_checks()[0]["error"] == "RuntimeError: boom"
