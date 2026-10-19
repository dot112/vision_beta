"""Camera trigger with a trigger notice: the camera's pictures are paired with the notices by time.

A camera on Camera trigger (its own input takes each picture) whose card names a trigger notice
is told by the "Take a picture" input with that number that a picture is coming. A notice and a
picture at most "Wait for the picture" (W) apart, in either order, are one product: made from
that picture, with the notice's context (trigger number, product sequence, handshake request).
A notice whose picture does not come within W is a "no picture" reject with its context. A
picture no notice claims is not inspected; it counts as late or extra, and three in a row raise
camera.picture_unpaired. Expiry runs on the line's timers, not on the camera runner.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

import app.services.line_service as line_service
import app.services.trigger_inspection as trigger_inspection
from app.events.alarm_events import ALARM_CATALOG, AlarmCode, alarm_manager
from app.services.plc_input_service import PLCInputService
from app.services.settings_persistence_service import SettingsPersistenceService
from app.services.vision_service import CameraStreamPipeline, ContinuousVisionRunner
from app.state.application_state import app_state
from tests.test_handshake import ACK_INPUT, HANDSHAKE
from tests.test_joined_cameras import _run, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import signals  # noqa: F401  (a fixture)
from tests.test_product_sequence import CAPTURE_INPUT, SEQ, SEQUENCE_INPUT
from tests.test_trigger_inspection import _det, _trigger_cam

# The wall clock at the test clock's 0 (in the past, so no product time is held back to now).
WALL0 = 1_700_000_000.0
WAIT_MS = 500
W = WAIT_MS / 1000.0
DENT, BOTTLE = 10, 20
NAMES = {DENT: "dent", BOTTLE: "bottle"}


class _Timer:
    def __init__(self, due, fn):
        self.due, self.fn, self.cancelled = due, fn, False

    def cancel(self):
        self.cancelled = True


class Clock:
    """The pairing clock. The line's timers run on it: ``advance`` fires each one that comes due, in time order."""

    def __init__(self):
        self.now = 0.0
        self.timers = []

    def __call__(self):
        return self.now

    def wall(self):
        return WALL0 + self.now

    def schedule(self, delay, fn):
        timer = _Timer(self.now + delay, fn)
        self.timers.append(timer)
        return timer

    def advance(self, seconds):
        end = self.now + seconds
        while True:
            due = [t for t in self.timers if t.due <= end + 1e-9]
            if not due:
                break
            timer = min(due, key=lambda t: t.due)
            self.timers.remove(timer)
            if not timer.cancelled:
                self.now = max(self.now, timer.due)
                timer.fn()
        self.now = end


class HardwareCamera:
    """An industrial camera on Hardware trigger: each picture its input takes waits in its queue for the runner."""

    trigger_mode = "hardware"

    def __init__(self, clock):
        self.clock = clock
        self.is_connected = True
        self.name = "Top"
        self.dropped_frames = 0
        self._latest_frame_id = 0
        self._queue = []

    def take(self, value, ago=0.0):
        """The camera's input fires: a picture whose pixels hold ``value`` arrives (``ago`` s before now)."""
        self._latest_frame_id += 1
        self._queue.append((np.full((4, 4, 3), value, np.uint8), self._latest_frame_id, WALL0 + self.clock.now - ago))

    @property
    def pending_triggered(self):
        return len(self._queue)

    def next_triggered_frame(self):
        return self._queue.pop(0) if self._queue else None

    def clear_triggered_frames(self):
        waiting = len(self._queue)
        self._queue.clear()
        return waiting

    def get_latest_frame_id(self):
        return self._latest_frame_id


class Model:
    """The camera's inference worker. It takes a picture while idle; ``finish`` makes the model get through it
    (a dent in a DENT picture, a bottle in a BOTTLE one) and keeps the frame, as the real worker does."""

    def __init__(self):
        self.is_busy = False
        self.taken = []
        self.last_inferred_frame = None
        self._job = None

    def submit_frame_if_idle(self, mat, frame_id, conf_thresh=None, copy=True, arrived_at=None, on_result=None,
                             on_failure=None):
        if self.is_busy:
            return False
        self.is_busy = True
        self.taken.append(int(mat[0, 0, 0]))
        self._job = (mat, frame_id, arrived_at, on_result)
        self._on_failure = on_failure
        return True

    def fail(self, why="the model raised"):
        """The model could not get through the picture it holds (the real worker calls on_failure)."""
        assert self._job is not None, "the model holds no picture"
        frame_id, on_failure = self._job[1], self._on_failure
        self._job = None
        on_failure(frame_id, why)
        self.is_busy = False

    def finish(self):
        assert self._job is not None, "the model holds no picture"
        mat, frame_id, arrived, on_result = self._job
        self._job = None
        self.last_inferred_frame = mat
        if on_result is not None:
            on_result(frame_id, [_det(NAMES[int(mat[0, 0, 0])])], 2.0, arrived)
        self.is_busy = False


def _notice_cam(camera_id="cam", **extra):
    return _trigger_cam(camera_id, **{"picture_source": "camera", "notice_trigger": 1, "picture_wait_ms": WAIT_MS, **extra})


@pytest.fixture
def rig(lines, monkeypatch):  # noqa: F811
    """A notice camera "cam" on a fake clock: ``rig.line(...)`` saves the line, ``rig.pump()`` is one runner pass.

    ``rig.crossings`` are the products the line made (with their context), ``rig.drawn`` the pictures drawn."""
    clock = Clock()
    model = Model()
    drawn = []
    crossings = []
    emitted_at = []
    monkeypatch.setattr(line_service, "_now", clock)
    monkeypatch.setattr(line_service, "_wall", clock.wall)
    monkeypatch.setattr(CameraStreamPipeline, "get_worker", classmethod(lambda cls, camera_id: model))
    monkeypatch.setitem(CameraStreamPipeline._workers, "cam", model)
    monkeypatch.setattr(lines, "engine_for_camera", lambda camera_id: (SimpleNamespace(is_loaded=True), "fake", None))

    def render(camera_id, frame, detections):
        drawn.append((camera_id, None if frame is None else int(frame[0, 0, 0]), [d.class_name for d in detections]))
        return b"\xff\xd8drawn"

    monkeypatch.setattr(trigger_inspection, "render_trigger_capture", render)
    camera = HardwareCamera(clock)
    monkeypatch.setitem(app_state.cameras, "cam", camera)
    memory = ContinuousVisionRunner.new_memory()
    alarm_manager.reset()

    def line(cameras=None, **extra):
        cameras = [{"model_id": "model-test", **cam} for cam in (cameras or [_notice_cam()])]
        saved = SettingsPersistenceService.save_line({"name": "Notice line", "enabled": True, "cameras": cameras, **extra})
        lines.apply_state()
        PLCInputService.reload()
        runtime = lines.get(saved["id"])
        runtime.sent = []
        monkeypatch.setattr(runtime, "_schedule_timer", clock.schedule)
        for counter in [runtime.counter, *runtime.aux_counters.values()]:
            counter.dispatch_event = lambda payload, plc_event, is_defect: runtime.sent.append((payload, plc_event))

            def noted(crossing, emit=counter.emit_crossing):
                crossings.append(crossing)
                emitted_at.append(clock.now)
                emit(crossing)

            monkeypatch.setattr(counter, "emit_crossing", noted)
        return runtime

    def running(runtime, on):
        SettingsPersistenceService.save_line({"id": runtime.id, "enabled": on})
        lines.apply_state()
        assert lines.get(runtime.id) is runtime and runtime.enabled is on

    def resave(runtime, cameras, **extra):
        """Save the running line again with these cameras (a settings change while it runs)."""
        cameras = [{"model_id": "model-test", **cam} for cam in cameras]
        SettingsPersistenceService.save_line({"id": runtime.id, "cameras": cameras, **extra})
        lines.apply_state()
        assert lines.get(runtime.id) is runtime and runtime.enabled

    yield SimpleNamespace(clock=clock, model=model, camera=camera, drawn=drawn, crossings=crossings, line=line,
                          emitted_at=emitted_at, running=running, resave=resave,
                          pump=lambda: ContinuousVisionRunner.feed_once(memory))
    alarm_manager.reset()


def _counters(runtime, camera_id="cam"):
    return next(row["counters"] for row in runtime.trigger_view() if row["camera_id"] == camera_id)


def _context(crossing):
    return tuple(crossing.get(key) for key in ("trigger_no", "sequence", "request_id"))


def _unpaired_alarm(camera_id="cam"):
    return alarm_manager.get(AlarmCode.CAMERA_PICTURE_UNPAIRED, f"camera:{camera_id}")


# ── Pairing ───────────────────────────────────────────────────────────────────

def test_notice_then_picture_makes_one_product_with_the_notice_context(rig):
    runtime = rig.line()
    assert runtime.has_notice("cam")
    runtime.trigger_notice("cam", 1, {"sequence": 42, "request_id": 7})
    rig.clock.advance(0.2)
    rig.camera.take(BOTTLE)
    rig.pump()
    assert rig.model.taken == [BOTTLE]
    rig.model.finish()

    crossing, = rig.crossings
    assert _context(crossing) == (1, 42, 7)
    assert (crossing["class_name"], crossing["is_defect"]) == ("bottle", False)
    assert crossing["time"] == pytest.approx(WALL0 + 0.2, abs=1e-3)          # the time the picture arrived
    payload = runtime.sent[0][0]
    assert payload["trigger_no"] == 1 and runtime.counter.total_inspected == 1
    # The picture with what the model found in it is the camera's latest capture.
    assert rig.drawn == [("cam", BOTTLE, ["bottle"])]
    capture, jpeg = runtime.trigger_capture("cam")
    assert jpeg == b"\xff\xd8drawn" and (capture["class_name"], capture["result"], capture["trigger_no"]) == ("bottle", "good", 1)
    assert _counters(runtime) == {"triggers": 1, "pictures": 1, "results": 1, "missing": 0, "late": 0, "extra": 0}
    rig.clock.advance(5.0)                       # the notice's timer finds nothing left to answer
    assert len(rig.crossings) == 1 and _counters(runtime)["missing"] == 0


def test_a_paired_picture_the_model_cannot_inspect_still_gets_its_notice_a_no_picture_answer(rig):
    runtime = rig.line()
    runtime.trigger_notice("cam", 1, {"sequence": 42, "request_id": 7})
    rig.clock.advance(0.2)
    rig.camera.take(BOTTLE)
    rig.pump()
    rig.model.fail("out of memory")

    crossing, = rig.crossings
    assert (crossing["class_name"], crossing["reject_reason"]) == ("no picture", "no_picture")
    assert _context(crossing) == (1, 42, 7)
    assert _counters(runtime) == {"triggers": 1, "pictures": 1, "results": 0, "missing": 1, "late": 0, "extra": 0}
    assert alarm_manager.get(AlarmCode.CAMERA_PICTURE_MISSING, "camera:cam") is not None
    rig.clock.advance(5.0)                       # nothing is answered twice
    assert len(rig.crossings) == 1


def test_picture_then_notice_pairs_too(rig):
    runtime = rig.line()
    rig.camera.take(DENT)
    rig.clock.advance(0.1)
    rig.pump()                                   # the runner hands it over 0.1 s after it arrived
    assert rig.model.taken == []                 # no notice yet: not inspected
    rig.clock.advance(0.3)
    runtime.trigger_notice("cam", 1, {"sequence": 5})
    assert rig.model.taken == [DENT]
    rig.model.finish()

    crossing, = rig.crossings
    assert _context(crossing) == (1, 5, None)
    assert (crossing["class_name"], crossing["is_defect"]) == ("dent", True)
    assert crossing["time"] == pytest.approx(WALL0, abs=1e-3)               # the picture's time, not the notice's
    rig.clock.advance(5.0)
    assert len(rig.crossings) == 1
    assert _counters(runtime) == {"triggers": 1, "pictures": 1, "results": 1, "missing": 0, "late": 0, "extra": 0}


def test_notice_without_picture_is_a_no_picture_reject_with_its_context(rig, signals):  # noqa: F811
    runtime = rig.line(plc_inputs=[CAPTURE_INPUT, SEQUENCE_INPUT, ACK_INPUT], sequence_input_id="in_seq",
                       handshake=HANDSHAKE)
    card = {**runtime.plc_inputs[0], "line_id": runtime.id, "line_name": runtime.name}
    signals.values[SEQ] = 42
    seen = {}

    async def steps():
        seen["answers"] = await PLCInputService._capture(card)
        seen["waiting"] = runtime.handshake.view()
        rig.clock.advance(W - 0.05)
        seen["before"] = list(rig.crossings)
        rig.clock.advance(0.1)

    _run(steps)
    assert seen["answers"] == [{"camera_id": "cam", "status": "expecting", "trigger_no": 1}]
    request_id = seen["waiting"]["request_id"]
    assert seen["waiting"]["state"] == "waiting_result" and request_id is not None
    assert seen["before"] == []                  # the notice waits W for its picture
    crossing, = rig.crossings
    assert _context(crossing) == (1, 42, request_id)
    assert (crossing["class_name"], crossing["reject_reason"]) == ("no picture", "no_picture")
    assert crossing["time"] == pytest.approx(WALL0, abs=1e-3)               # the notice's time
    # The handshake got its one result: a reject.
    view = runtime.handshake.view()
    assert (view["state"], view["last_result"], view["request_id"]) == ("result_ready", "reject", request_id)
    alarm = alarm_manager.get(AlarmCode.CAMERA_PICTURE_MISSING, "camera:cam")
    assert alarm is not None and alarm.details["why"] == f"no picture within {WAIT_MS} ms of the notice"
    capture, jpeg = runtime.trigger_capture("cam")
    assert jpeg is None and capture["has_picture"] is False and capture["reason"] == "no_picture"
    assert _counters(runtime) == {"triggers": 1, "pictures": 0, "results": 0, "missing": 1, "late": 0, "extra": 0}


def test_camera_disconnected_with_notices_waiting_still_answers(rig):  # Review Focus 1
    runtime = rig.line()
    runtime.trigger_notice("cam", 1, {"sequence": 1})
    rig.clock.advance(0.1)
    runtime.trigger_notice("cam", 1, {"sequence": 2})
    rig.camera.is_connected = False              # the driver is gone: the runner does not feed the camera now
    for _ in range(3):
        rig.pump()
    rig.clock.advance(1.0)                       # the line's own timers still run

    assert [(_context(c), c["reject_reason"]) for c in rig.crossings] == [((1, 1, None), "no_picture"),
                                                                          ((1, 2, None), "no_picture")]
    assert [c["time"] for c in rig.crossings] == pytest.approx([WALL0, WALL0 + 0.1], abs=1e-3)
    assert runtime.counter.total_inspected == 2 and runtime.counter.rejected_count == 2
    assert _counters(runtime) == {"triggers": 2, "pictures": 0, "results": 0, "missing": 2, "late": 0, "extra": 0}


def test_notice_while_the_previous_picture_is_in_the_model(rig):  # Review Focus 4
    runtime = rig.line()
    runtime.trigger_notice("cam", 1, {"sequence": 1})
    rig.camera.take(BOTTLE)
    rig.pump()
    assert rig.model.taken == [BOTTLE] and rig.model.is_busy
    rig.clock.advance(0.2)
    runtime.trigger_notice("cam", 1, {"sequence": 2})
    rig.camera.take(DENT)
    rig.pump()
    assert rig.model.taken == [BOTTLE]           # the model is still busy with the first picture
    rig.clock.advance(3 * W)                     # longer than the wait: the second notice was paired already
    assert rig.crossings == [] and _counters(runtime)["missing"] == 0

    rig.model.finish()
    rig.pump()                                   # the runner hands the waiting pair to the model
    assert rig.model.taken == [BOTTLE, DENT]
    rig.model.finish()
    assert [(_context(c), c["class_name"]) for c in rig.crossings] == [((1, 1, None), "bottle"), ((1, 2, None), "dent")]
    assert [c["time"] for c in rig.crossings] == pytest.approx([WALL0, WALL0 + 0.2], abs=1e-3)
    assert _counters(runtime) == {"triggers": 2, "pictures": 2, "results": 2, "missing": 0, "late": 0, "extra": 0}


def test_a_paired_picture_waiting_for_the_model_is_inspected_though_the_camera_disconnects(rig):
    runtime = rig.line()
    for sequence, value in ((1, BOTTLE), (2, DENT)):
        runtime.trigger_notice("cam", 1, {"sequence": sequence})
        rig.camera.take(value)
        rig.pump()
    assert rig.model.taken == [BOTTLE]
    rig.camera.is_connected = False              # the runner stops feeding the camera
    rig.model.finish()
    rig.pump()
    rig.clock.advance(0.1)                       # a line timer hands the waiting pair to the model
    assert rig.model.taken == [BOTTLE, DENT]
    rig.model.finish()
    assert [c.get("sequence") for c in rig.crossings] == [1, 2]
    assert _counters(runtime)["results"] == 2


def test_a_picture_handed_over_late_does_not_pair_with_a_later_notice(rig):
    runtime = rig.line()                         # W = 0.5
    runtime.trigger_notice("cam", 1, {"sequence": 1})
    rig.camera.take(BOTTLE)                      # its picture, but the runner hands it over late
    rig.clock.advance(0.7)                       # the notice went missing at 0.5
    runtime.trigger_notice("cam", 1, {"sequence": 2})
    rig.pump()                                   # the picture, taken at 0, comes more than W before this notice
    assert rig.model.taken == []
    rig.clock.advance(1.0)
    assert [(c.get("sequence"), c["reject_reason"]) for c in rig.crossings] == [(1, "no_picture"), (2, "no_picture")]
    counters = _counters(runtime)
    assert (counters["missing"], counters["late"] + counters["extra"], counters["results"]) == (2, 1, 0)


def test_a_slow_sequence_read_still_pairs_the_picture_taken_at_the_edge(rig, signals, monkeypatch):  # noqa: F811
    runtime = rig.line([_notice_cam(picture_wait_ms=200)], plc_inputs=[CAPTURE_INPUT])
    card = {**runtime.plc_inputs[0], "line_id": runtime.id, "line_name": runtime.name}

    async def slow_read(cls, line):
        # The camera takes its picture as the input's edge is seen, and the runner hands it over at once;
        # the PLC answers the sequence read 0.5 s later (more than W = 0.2).
        rig.camera.take(BOTTLE)
        rig.clock.advance(0.01)
        rig.pump()
        rig.clock.advance(0.49)
        return 42

    monkeypatch.setattr(PLCInputService, "read_sequence", classmethod(slow_read))
    seen = {}

    async def steps():
        seen["answers"] = await PLCInputService._capture(card)

    _run(steps)
    assert seen["answers"] == [{"camera_id": "cam", "status": "expecting", "trigger_no": 1}]
    assert rig.model.taken == [BOTTLE]
    rig.model.finish()
    crossing, = rig.crossings
    assert _context(crossing) == (1, 42, None) and crossing["class_name"] == "bottle"
    assert crossing["time"] == pytest.approx(WALL0, abs=1e-3)
    # Nothing waits on: a later picture without a notice is counted as usual.
    rig.clock.advance(0.1)
    rig.camera.take(DENT)
    rig.pump()
    rig.clock.advance(0.5)
    assert _counters(runtime) == {"triggers": 1, "pictures": 2, "results": 1, "missing": 0, "late": 0, "extra": 1}


def test_a_slow_read_whose_picture_never_comes_answers_from_the_edge(rig, signals, monkeypatch):  # noqa: F811
    runtime = rig.line([_notice_cam(picture_wait_ms=200)], plc_inputs=[CAPTURE_INPUT])
    card = {**runtime.plc_inputs[0], "line_id": runtime.id, "line_name": runtime.name}

    async def slow_read(cls, line):
        rig.clock.advance(0.5)
        return 7

    monkeypatch.setattr(PLCInputService, "read_sequence", classmethod(slow_read))

    async def steps():
        await PLCInputService._capture(card)

    _run(steps)
    rig.clock.advance(0.5)
    crossing, = rig.crossings
    assert _context(crossing) == (1, 7, None) and crossing["reject_reason"] == "no_picture"
    assert crossing["time"] == pytest.approx(WALL0, abs=1e-3)              # the edge, not the end of the read


class _StillCamera:
    """A stream camera whose picture never changes: a System trigger on it waits its whole 2 s."""

    is_connected = True
    trigger_mode = "off"
    name = "Still"

    def get_latest_frame_id(self):
        return 1

    def get_latest_raw_mat(self, copy=True):
        return True, np.zeros((4, 4, 3), np.uint8), 1


def test_a_notice_is_answered_in_its_time_while_its_input_waits_for_another_camera(rig, signals, monkeypatch):  # noqa: F811
    # One input is the notice of "cam" and the System trigger of "sys"; the input waits up to 2 s for
    # the picture of "sys", but the notice's hold ends with the notice.
    monkeypatch.setitem(app_state.cameras, "sys", _StillCamera())

    async def sleep(seconds):
        rig.clock.advance(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(trigger_inspection, "_now", rig.clock)
    monkeypatch.setattr(trigger_inspection, "_wall", rig.clock.wall)
    monkeypatch.setattr(trigger_inspection, "_sleep", sleep)
    runtime = rig.line([_notice_cam(), _trigger_cam("sys", triggers=[1])], plc_inputs=[CAPTURE_INPUT])
    card = {**runtime.plc_inputs[0], "line_id": runtime.id, "line_name": runtime.name}
    seen = {}

    async def steps():
        seen["answers"] = await PLCInputService._capture(card)

    _run(steps)
    statuses = {answer["camera_id"]: answer["status"] for answer in seen["answers"]}
    assert statuses == {"cam": "expecting", "sys": "no_picture"}
    by_camera = {c["camera_id"]: at for c, at in zip(rig.crossings, rig.emitted_at)}
    assert by_camera["cam"] == pytest.approx(W + 0.01, abs=0.02)      # not when "sys" gave up (2 s)
    assert by_camera["sys"] == pytest.approx(2.0, abs=0.05)


# ── Wait for the picture changed while the line runs ─────────────────────────

def test_a_new_wait_answers_the_waiting_notices_and_keeps_the_pairs(rig):
    runtime = rig.line()                         # W = 0.5
    runtime.trigger_notice("cam", 1, {"sequence": 1})
    rig.camera.take(BOTTLE)
    rig.pump()                                   # pair 1 is in the model
    runtime.trigger_notice("cam", 1, {"sequence": 2})
    rig.camera.take(DENT)
    rig.pump()                                   # pair 2 waits for the model
    rig.camera.take(BOTTLE, ago=0.6)             # a picture no notice claims
    rig.pump()
    runtime.trigger_notice("cam", 1, {"sequence": 3, "request_id": 9})     # a notice whose picture has not come
    rig.clock.advance(0.1)

    rig.resave(runtime, [_notice_cam(picture_wait_ms=800)])
    # The waiting notice gets its answer at once, with its context and time; the waiting picture is dropped.
    assert [(_context(c), c["reject_reason"]) for c in rig.crossings] == [((1, 3, 9), "no_picture")]
    assert rig.crossings[0]["time"] == pytest.approx(WALL0, abs=1e-3)
    assert (_counters(runtime)["missing"], _counters(runtime)["extra"]) == (1, 1)
    # The pairs waiting for the model go on.
    rig.model.finish()
    rig.pump()
    rig.model.finish()
    assert [c.get("sequence") for c in rig.crossings] == [3, 1, 2]
    # And the new wait applies: a notice now waits 0.8 s for its picture.
    rig.clock.advance(2.0)
    runtime.trigger_notice("cam", 1, {"sequence": 4})
    rig.clock.advance(0.7)
    rig.camera.take(BOTTLE)
    rig.pump()
    rig.model.finish()
    assert [c.get("sequence") for c in rig.crossings] == [3, 1, 2, 4]
    assert _counters(runtime) == {"triggers": 4, "pictures": 4, "results": 3, "missing": 1, "late": 0, "extra": 1}


def test_a_save_that_keeps_the_wait_keeps_what_waits(rig):
    runtime = rig.line()
    runtime.trigger_notice("cam", 1, {"sequence": 1})       # waits for its picture
    rig.camera.take(DENT, ago=0.6)               # a picture no notice claims waits too
    rig.pump()
    rig.resave(runtime, [_notice_cam()], name="Renamed line")
    assert rig.crossings == [] and _counters(runtime)["extra"] == 0
    rig.camera.take(BOTTLE)
    rig.pump()                                   # the notice still takes its picture
    rig.model.finish()
    rig.clock.advance(1.0)
    assert [(c.get("sequence"), c["class_name"]) for c in rig.crossings] == [(1, "bottle")]
    assert _counters(runtime) == {"triggers": 1, "pictures": 2, "results": 1, "missing": 0, "late": 0, "extra": 1}


# ── Pictures without a notice ────────────────────────────────────────────────

def test_three_unpaired_pictures_raise_the_alarm_and_a_pair_clears_it(rig):
    runtime = rig.line()
    runtime.trigger_notice("cam", 1, {"sequence": 1})       # its picture never comes
    rig.clock.advance(W + 0.1)
    rig.camera.take(BOTTLE)                      # 0.1 s after that notice's window ended: late
    rig.pump()
    rig.clock.advance(W + 0.1)
    rig.camera.take(BOTTLE)                      # long after: extra
    rig.pump()
    rig.clock.advance(W + 0.1)
    assert _unpaired_alarm() is None             # two in a row
    assert (_counters(runtime)["late"], _counters(runtime)["extra"]) == (1, 1)

    rig.camera.take(DENT)
    rig.pump()
    rig.clock.advance(W + 0.1)
    alarm = _unpaired_alarm()
    assert alarm is not None and alarm.details["camera_id"] == "cam" and alarm.details["line_id"] == runtime.id
    assert alarm.severity.value == "warning"
    assert rig.model.taken == []                 # none of them was inspected
    assert [c["reject_reason"] for c in rig.crossings] == ["no_picture"]
    assert _counters(runtime) == {"triggers": 1, "pictures": 3, "results": 0, "missing": 1, "late": 1, "extra": 2}

    missing = (AlarmCode.CAMERA_PICTURE_MISSING, "camera:cam")
    assert alarm_manager.get(*missing) is not None          # pictures no notice claimed do not clear it
    runtime.trigger_notice("cam", 1, {"sequence": 2})
    rig.camera.take(BOTTLE)
    rig.pump()
    assert _unpaired_alarm() is None             # the next paired picture clears it
    rig.model.finish()
    assert [c.get("sequence") for c in rig.crossings] == [1, 2]
    assert alarm_manager.get(*missing) is None               # and its product the missing picture's
    for _ in range(2):                           # and the count in a row starts again
        rig.clock.advance(0.1)
        rig.camera.take(DENT)
        rig.pump()
    rig.clock.advance(W + 0.1)
    assert _counters(runtime)["extra"] == 4 and _unpaired_alarm() is None


def test_an_unpaired_picture_from_before_a_pair_does_not_count_in_a_row(rig):
    runtime = rig.line()
    rig.camera.take(BOTTLE, ago=0.6)             # no notice for this one (handed over late)
    rig.pump()
    rig.clock.advance(0.1)
    runtime.trigger_notice("cam", 1, {"sequence": 1})       # too long after that picture: it pairs with the next
    rig.camera.take(BOTTLE)
    rig.pump()
    rig.model.finish()
    for _ in range(2):                           # two more without a notice
        rig.clock.advance(0.1)
        rig.camera.take(DENT)
        rig.pump()
    rig.clock.advance(W + 0.1)                   # the first one goes out after the pair, but it was before it
    assert _counters(runtime)["extra"] == 3 and _unpaired_alarm() is None
    assert [c.get("sequence") for c in rig.crossings] == [1]


def test_the_unpaired_alarm_is_known():
    entry = next(e for e in ALARM_CATALOG if e["code"] == "camera.picture_unpaired")
    assert AlarmCode.CAMERA_PICTURE_UNPAIRED == "camera.picture_unpaired"
    assert (entry["severity"], entry["scope"], entry["group"]) == ("warning", "line", "Cameras")


def test_pictures_without_a_notice_do_not_pile_up(rig, monkeypatch):
    monkeypatch.setattr(line_service, "MAX_WAITING_PICTURES", 2)
    runtime = rig.line()
    for _ in range(5):                           # faster than any notice: only 2 wait, each holds a frame
        rig.camera.take(BOTTLE)
        rig.pump()
    assert _counters(runtime)["extra"] == 3      # the ones that found no room count at once
    rig.clock.advance(W + 0.1)
    assert _counters(runtime) == {"triggers": 0, "pictures": 5, "results": 0, "missing": 0, "late": 0, "extra": 5}


def test_a_pair_the_model_cannot_take_in_time_gets_a_no_picture_answer(rig, monkeypatch):
    monkeypatch.setattr(line_service, "MAX_QUEUED_PAIRS", 1)
    runtime = rig.line()
    for sequence in (1, 2, 3):                   # the model is busy with the first: one pair may wait for it
        runtime.trigger_notice("cam", 1, {"sequence": sequence})
        rig.camera.take(BOTTLE)
        rig.pump()
    assert [(c.get("sequence"), c["reject_reason"]) for c in rig.crossings] == [(2, "no_picture")]
    rig.model.finish()
    rig.pump()
    rig.model.finish()
    assert [c.get("sequence") for c in rig.crossings] == [2, 1, 3]
    assert _counters(runtime) == {"triggers": 3, "pictures": 3, "results": 2, "missing": 1, "late": 0, "extra": 0}


# ── The line stops ────────────────────────────────────────────────────────────

def test_line_stop_drops_waiting_notices_and_pictures(rig):
    runtime = rig.line()
    runtime.trigger_notice("cam", 1, {"sequence": 1})
    rig.camera.take(BOTTLE)
    rig.pump()                                   # pair 1 is in the model
    runtime.trigger_notice("cam", 1, {"sequence": 2})
    rig.camera.take(DENT)
    rig.pump()                                   # pair 2 waits for the model
    rig.camera.take(BOTTLE, ago=0.6)             # a picture no notice claims
    rig.pump()
    runtime.trigger_notice("cam", 1, {"sequence": 3})       # a notice whose picture has not come
    before = _counters(runtime)

    rig.running(runtime, False)
    rig.model.finish()                           # the model gets through pair 1 after the stop
    rig.pump()
    rig.clock.advance(3.0)
    assert rig.crossings == [] and runtime.sent == [] and rig.model.taken == [BOTTLE]
    assert _counters(runtime) == before          # nothing missing, late or extra: dropped

    rig.running(runtime, True)
    rig.pump()
    rig.clock.advance(3.0)
    assert rig.crossings == [] and rig.model.taken == [BOTTLE] and _counters(runtime) == before
    # The line works again.
    runtime.trigger_notice("cam", 1, {"sequence": 4})
    rig.camera.take(BOTTLE)
    rig.pump()
    rig.model.finish()
    assert [c.get("sequence") for c in rig.crossings] == [4]


# ── The "Take a picture" input ────────────────────────────────────────────────

def test_capture_input_on_a_notice_camera_is_obeyed(rig, signals):  # noqa: F811
    runtime = rig.line(plc_inputs=[CAPTURE_INPUT])
    card = {**runtime.plc_inputs[0], "line_id": runtime.id, "line_name": runtime.name}
    seen = {}

    async def steps():
        seen["answers"] = await PLCInputService._capture(card)

    _run(steps)                                  # not the "not obeyed" error of a hardware camera
    assert seen["answers"] == [{"camera_id": "cam", "status": "expecting", "trigger_no": 1}]
    assert _counters(runtime)["triggers"] == 1
    rig.camera.take(BOTTLE)
    rig.pump()
    rig.model.finish()
    crossing, = rig.crossings
    assert _context(crossing) == (1, None, None) and crossing["class_name"] == "bottle"


def test_a_notice_for_a_camera_without_a_model_is_refused(rig, lines, signals, monkeypatch):  # noqa: F811
    runtime = rig.line(plc_inputs=[CAPTURE_INPUT])
    monkeypatch.setattr(lines, "engine_for_camera", lambda camera_id: (SimpleNamespace(is_loaded=False), "fake", None))
    card = {**runtime.plc_inputs[0], "line_id": runtime.id, "line_name": runtime.name}

    async def steps():
        with pytest.raises(ValueError, match="no vision model"):
            await PLCInputService._capture(card)

    _run(steps)
    rig.clock.advance(1.0)
    assert rig.crossings == [] and _counters(runtime)["triggers"] == 0
    # The refused input holds nothing back: a picture without a notice is counted as usual.
    rig.camera.take(BOTTLE)
    rig.pump()
    rig.clock.advance(1.0)
    assert _counters(runtime)["extra"] == 1


# ── Without a notice ──────────────────────────────────────────────────────────

def test_hardware_camera_without_notice_still_counts_every_picture(rig):
    runtime = rig.line([_trigger_cam("cam", picture_source="camera")])
    assert not runtime.has_notice("cam")
    for value in (BOTTLE, DENT, BOTTLE):
        rig.camera.take(value)
    for _ in range(3):
        rig.pump()
        rig.pump()                               # the next picture waits while the model is busy
        rig.model.finish()
    assert rig.model.taken == [BOTTLE, DENT, BOTTLE]
    assert [c["class_name"] for c in rig.crossings] == ["bottle", "dent", "bottle"]
    assert all(_context(c) == (None, None, None) for c in rig.crossings)
    assert runtime.counter.total_inspected == 3
    assert [frame for _, frame, _ in rig.drawn] == [BOTTLE, DENT, BOTTLE]
    capture, jpeg = runtime.trigger_capture("cam")
    assert jpeg == b"\xff\xd8drawn" and capture["class_name"] == "bottle" and capture["trigger_no"] is None
    rig.clock.advance(5.0)
    assert len(rig.crossings) == 3
    assert _counters(runtime) == {"triggers": 0, "pictures": 3, "results": 3, "missing": 0, "late": 0, "extra": 0}
