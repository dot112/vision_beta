"""Triggered industrial cameras on a line: what each picture becomes.

The camera's trigger decides when a picture is taken; the line decides what a
picture means. Hardware trigger pushes every picture to the uses that count
every picture; software trigger takes a picture for whoever asks.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import numpy as np
import pytest

import app.services.trigger_inspection as trigger_inspection
import app.services.vision_service as vision_service
from app.events.alarm_events import AlarmCode, alarm_manager
from app.hardware.camera.gige_camera import GigECamera
from app.schemas.qr import BarcodeDecodeResponse, BarcodeItem
from app.schemas.vision import DetectionResponse
from app.services.line_config import line_warnings
from app.services.plc_input_service import PLCInputService
from app.services.qr_service import QRReaderPipeline
from app.services.settings_persistence_service import SettingsPersistenceService
from app.services.trigger_inspection import trigger_camera
from app.services.vision_service import CameraStreamPipeline, ContinuousVisionRunner
from app.state.application_state import app_state
from tests.test_industrial_camera import FakeBackend, _wait_until
from tests.test_joined_cameras import _line, _run, _totals, lines  # noqa: F401  (a fixture)
from tests.test_plc_inputs import _signal
from tests.test_trigger_inspection import _det, _trigger_cam

DENT, BOTTLE, EMPTY = 10, 20, 30


def _picture(value):
    return np.full((4, 4), value, np.uint8)


class PictureEngine:
    """A model that finds what a picture's first pixel says: 10 a dent, 20 a bottle, else nothing."""

    is_loaded = True
    task = "detect"

    def __init__(self):
        self.seen = []
        self.delay = 0.0
        self.gate = threading.Event()
        self.gate.set()

    def predict_mat(self, mat, conf_threshold=None, nms_threshold=None):
        value = int(mat[0, 0, 0])
        self.seen.append(value)
        self.gate.wait(5)
        time.sleep(self.delay)
        found = [_det(name) for name in {DENT: ["dent"], BOTTLE: ["bottle"]}.get(value, [])]
        return DetectionResponse(model_name="fake", total_detections=len(found), detections=found,
                                 inference_time_ms=1.0, image_width=mat.shape[1], image_height=mat.shape[0])


@pytest.fixture(autouse=True)
def _no_alarms():
    alarm_manager.reset()
    yield
    alarm_manager.reset()


@pytest.fixture
def rig(lines, monkeypatch):  # noqa: F811
    """``rig.camera(id, **settings)`` connects a stand-in industrial camera; ``rig.pump(until)`` runs the frame runner."""

    class Rig:
        engine = PictureEngine()
        memory = ContinuousVisionRunner.new_memory()
        cameras = {}

        def camera(self, camera_id, **settings):
            backend = FakeBackend()
            cam = GigECamera(camera_id, camera_id, "A1", settings=settings, backend=backend)
            assert cam.connect(), cam.last_error
            monkeypatch.setitem(app_state.cameras, camera_id, cam)
            self.cameras[camera_id] = cam
            return cam, backend

        def pump(self, until, timeout=3.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ContinuousVisionRunner.feed_once(self.memory)
                if until():
                    return True
                time.sleep(0.004)
            return bool(until())

        def pump_for(self, seconds):
            self.pump(lambda: False, timeout=seconds)

    rig = Rig()
    monkeypatch.setattr(lines, "engine_for_camera", lambda camera_id: (rig.engine, "fake", None))
    yield rig
    rig.engine.gate.set()
    for camera_id, cam in rig.cameras.items():
        cam.disconnect()
        CameraStreamPipeline.remove_camera(camera_id)
        QRReaderPipeline.remove_camera(camera_id)


def _reads(rig, runtime, camera_id, codes_by_value):
    """Decode by the picture's first pixel, and collect the captures the line is given."""
    reader = QRReaderPipeline.get_worker(camera_id)

    def decode(mat, **kwargs):
        data = codes_by_value.get(int(mat[0, 0, 0]))
        codes = [BarcodeItem(code_type="QR_CODE", data=data)] if data else []
        return BarcodeDecodeResponse(total_found=len(codes), codes=codes, decode_time_ms=1.0,
                                     image_width=mat.shape[1], image_height=mat.shape[0])

    reader._engine.decode = decode
    captures = []
    runtime.on_qr_capture = lambda cid, record, jpeg: captures.append(record)
    return reader, captures


# ── Hardware trigger: every picture, in order ────────────────────────────────


def test_every_hardware_picture_goes_to_the_model_in_order(lines, rig):  # noqa: F811   Review Focus 1
    runtime = _line(lines, [_trigger_cam("cam")])
    cam, backend = rig.camera("cam", trigger_mode="hardware")
    rig.engine.delay = 0.05                     # three triggers closer together than one model run
    for value in (DENT, BOTTLE, DENT):
        backend.push(_picture(value))
    assert _wait_until(lambda: cam.get_latest_frame_id() == 3)
    assert rig.pump(lambda: runtime.counter.total_inspected == 3)
    assert rig.engine.seen == [DENT, BOTTLE, DENT]
    assert _totals(runtime.counter) == (3, 1, 2) and cam.pending_triggered == 0


def test_each_hardware_triggered_picture_is_one_product(lines, rig):  # noqa: F811
    runtime = _line(lines, [_trigger_cam("cam", no_product="reject")])
    assert not runtime.plc_inputs                # no signal card is needed
    cam, backend = rig.camera("cam", trigger_mode="hardware")
    for value in (DENT, BOTTLE, EMPTY):
        backend.push(_picture(value))
    assert rig.pump(lambda: runtime.counter.total_inspected == 3)
    payloads = [payload for payload, _ in runtime.sent]
    assert [p["reject_reason"] for p in payloads] == ["vision_class", None, "nothing_detected"]
    assert [p["track_id"] for p in payloads] == [1, 2, 3] and _totals(runtime.counter) == (3, 1, 2)


def test_a_hardware_product_has_the_time_its_picture_arrived(lines, rig):  # noqa: F811   D6
    runtime = _line(lines, [_trigger_cam("cam")])
    cam, backend = rig.camera("cam", trigger_mode="hardware")
    rig.engine.gate.clear()                     # the model is slow: the result comes much later than the picture
    backend.push(_picture(BOTTLE))
    assert _wait_until(lambda: cam.pending_triggered == 1)
    arrived = cam._queue[0][2]
    rig.pump_for(0.25)
    rig.engine.gate.set()
    assert rig.pump(lambda: runtime.counter.total_inspected == 1)
    payload = runtime.sent[0][0]
    assert payload["timestamp"] == datetime.fromtimestamp(arrived, timezone.utc).isoformat()
    assert time.time() - arrived >= 0.25


def test_a_free_running_camera_still_gets_only_its_newest_picture(lines, rig):  # noqa: F811
    _line(lines, [{"camera_id": "cam"}])
    cam, backend = rig.camera("cam")
    rig.engine.gate.clear()
    backend.push(_picture(1))
    assert rig.pump(lambda: rig.engine.seen == [1])
    backend.push(_picture(2))
    backend.push(_picture(3))
    assert _wait_until(lambda: cam.get_latest_frame_id() == 3)
    rig.engine.gate.set()
    assert rig.pump(lambda: len(rig.engine.seen) == 2)
    rig.pump_for(0.1)
    assert rig.engine.seen == [1, 3]            # the picture in between was skipped, as for any video


def test_a_hardware_camera_that_counts_moving_products_is_fed_like_a_stream(lines, rig):  # noqa: F811   D4, D11
    runtime = _line(lines, [{"camera_id": "cam"}])      # Count moving products
    cam, backend = rig.camera("cam", trigger_mode="hardware")
    rig.engine.gate.clear()
    backend.push(_picture(BOTTLE))
    assert rig.pump(lambda: rig.engine.seen == [BOTTLE])
    for _ in range(GigECamera.QUEUE_SIZE + 5):          # an encoder fires faster than the model runs
        backend.push(_picture(DENT))
    assert _wait_until(lambda: cam.get_latest_frame_id() == GigECamera.QUEUE_SIZE + 6)
    rig.engine.gate.set()
    assert rig.pump(lambda: len(rig.engine.seen) == 2)
    rig.pump_for(0.1)
    assert len(rig.engine.seen) == 2 and cam.pending_triggered == 0
    assert runtime.counter.total_inspected == 0         # a picture is not a product here
    assert alarm_manager.get(AlarmCode.CAMERA_TRIGGER_OVERRUN, "camera:cam") is None


def test_dropped_pictures_raise_the_overrun_alarm_and_it_clears(lines, rig, monkeypatch):  # noqa: F811   Review Focus 2
    monkeypatch.setattr(GigECamera, "QUEUE_SIZE", 2)
    monkeypatch.setattr(vision_service, "OVERRUN_CLEAR_S", 0.1)
    runtime = _line(lines, [_trigger_cam("cam")])
    cam, backend = rig.camera("cam", trigger_mode="hardware")
    rig.engine.gate.clear()
    backend.push(_picture(BOTTLE))
    assert rig.pump(lambda: rig.engine.seen == [BOTTLE])        # the model is busy with the first
    for _ in range(3):
        backend.push(_picture(BOTTLE))                          # three more: the queue holds two
    assert _wait_until(lambda: cam.dropped_frames == 1)
    rig.pump_for(0.05)
    alarm = alarm_manager.get(AlarmCode.CAMERA_TRIGGER_OVERRUN, "camera:cam")
    assert alarm is not None and alarm.details["line_id"] == runtime.id and alarm.details["dropped"] == 1
    assert "cam" in alarm.message
    rig.engine.gate.set()
    assert rig.pump(lambda: runtime.counter.total_inspected == 3)      # 4 pictures, 1 lost
    assert rig.pump(lambda: alarm_manager.get(AlarmCode.CAMERA_TRIGGER_OVERRUN, "camera:cam") is None)


def test_a_stopped_line_makes_no_product_and_keeps_no_old_pictures(lines, rig):  # noqa: F811
    runtime = _line(lines, [_trigger_cam("cam")])
    cam, backend = rig.camera("cam", trigger_mode="hardware")
    SettingsPersistenceService.save_line({"id": runtime.id, "enabled": False})
    lines.apply_state()
    runtime = lines.get(runtime.id)
    assert not runtime.enabled
    backend.push(_picture(BOTTLE))
    backend.push(_picture(BOTTLE))
    assert _wait_until(lambda: cam.get_latest_frame_id() == 2)
    rig.pump_for(0.1)
    assert cam.pending_triggered == 0 and rig.engine.seen == []
    SettingsPersistenceService.save_line({"id": runtime.id, "enabled": True})
    lines.apply_state()
    rig.pump_for(0.1)
    assert lines.get(runtime.id).counter.total_inspected == 0      # the pictures taken while stopped are gone
    assert alarm_manager.get(AlarmCode.CAMERA_TRIGGER_OVERRUN, "camera:cam") is None


def test_a_camera_without_a_model_makes_no_products(lines, rig):  # noqa: F811
    runtime = _line(lines, [_trigger_cam("cam", no_product="reject")])
    cam, backend = rig.camera("cam", trigger_mode="hardware")
    rig.engine.is_loaded = False
    try:
        backend.push(_picture(EMPTY))
        assert rig.pump(lambda: cam.pending_triggered == 0 and cam.get_latest_frame_id() == 1)
        rig.pump_for(0.1)
        assert runtime.counter.total_inspected == 0     # not "nothing detected": nothing looked
    finally:
        rig.engine.is_loaded = True


# ── Hardware trigger and the code reader ─────────────────────────────────────


def test_every_hardware_picture_of_a_code_reader_is_read_once(lines, rig):  # noqa: F811   D4
    runtime = _line(lines, [{"camera_id": "cam", "role": "qr", "qr_trigger": "signal"}])
    cam, backend = rig.camera("cam", trigger_mode="hardware")
    reader, captures = _reads(rig, runtime, "cam", {1: "AAA", 2: "BBB"})
    for value in (1, 1, 2, 9):                  # the same code on two products in a row is two reads
        backend.push(_picture(value))
    assert rig.pump(lambda: len(captures) == 4)
    assert [[c["code"] for c in record["codes"]] for record in captures] == [["AAA"], ["AAA"], ["BBB"], []]
    assert [record["status"] for record in captures] == ["read", "read", "read", "no_read"]
    assert all(record["frames"] == 1 for record in captures) and cam.pending_triggered == 0


def test_one_hardware_picture_serves_the_model_and_the_code_reader(lines, rig):  # noqa: F811   D11
    runtime = _line(lines, [_trigger_cam("cam", read_codes=True, qr_trigger="signal")])
    cam, backend = rig.camera("cam", trigger_mode="hardware")
    reader, captures = _reads(rig, runtime, "cam", {BOTTLE: "AAA"})
    backend.push(_picture(BOTTLE))
    backend.push(_picture(BOTTLE))
    assert rig.pump(lambda: len(captures) == 2 and runtime.counter.total_inspected == 2)
    assert rig.engine.seen == [BOTTLE, BOTTLE] and cam.pending_triggered == 0


def test_a_code_reader_that_reads_continuously_takes_a_hardware_camera_as_a_stream(lines, rig):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "cam", "role": "qr"}])     # Read codes: continuously
    cam, backend = rig.camera("cam", trigger_mode="hardware")
    reader, captures = _reads(rig, runtime, "cam", {1: "AAA"})
    seen = []
    runtime.show_read = lambda cid, data, fmt: seen.append(data)          # read continuously: only shown
    backend.push(_picture(1))
    assert rig.pump(lambda: seen == ["AAA"])
    backend.push(_picture(1))
    assert _wait_until(lambda: cam.get_latest_frame_id() == 2)
    rig.pump_for(0.15)
    assert seen == ["AAA"] and captures == [] and cam.pending_triggered == 0     # the same code still in view: one read


def test_a_signal_card_for_a_hardware_camera_is_not_obeyed(lines, rig):  # noqa: F811   D16
    line = SettingsPersistenceService.save_line({"name": "Reader", "enabled": True, "cameras": [
        {"camera_id": "cam", "role": "qr", "qr_trigger": "signal"}], "plc_inputs": [_signal("capture", camera_id="cam")]})
    lines.apply_state()
    runtime = lines.get(line["id"])
    rig.camera("cam", trigger_mode="hardware")
    reader, captures = _reads(rig, runtime, "cam", {1: "AAA"})
    card = {**runtime.plc_inputs[0], "line_id": runtime.id, "line_name": runtime.name}

    async def steps():
        with pytest.raises(ValueError, match="its own input"):
            await PLCInputService._capture(card)

    _run(steps)
    rig.pump_for(0.1)
    assert captures == []


# ── Software trigger ─────────────────────────────────────────────────────────


def test_a_software_trigger_takes_the_picture_it_asked_for(lines, rig):  # noqa: F811
    runtime = _line(lines, [_trigger_cam("cam")])
    cam, backend = rig.camera("cam", trigger_mode="software")
    backend.on_trigger = lambda: backend.push(_picture(DENT))
    rig.pump_for(0.05)
    assert rig.engine.seen == []                # nothing is inspected until something asks
    seen = {}

    async def steps():
        seen["answer"] = await trigger_camera(runtime, "cam", with_detections=True)

    _run(steps)
    answer = seen["answer"]
    assert (answer["status"], answer["result"], answer["class_name"], answer["track_id"]) == ("inspected", "reject", "dent", 1)
    assert answer["detections"] == 1 and len(answer["detection_items"]) == 1
    assert backend.calls.count(("execute", "TriggerSoftware")) == 1
    assert rig.engine.seen == [DENT] and _totals(runtime.counter) == (1, 0, 1)
    rig.pump_for(0.05)
    assert rig.engine.seen == [DENT]            # the runner does not inspect it a second time


def test_a_picture_nobody_asked_a_software_camera_for_is_not_inspected(lines, rig):  # noqa: F811   D12
    runtime = _line(lines, [_trigger_cam("cam", no_product="reject")])
    cam, backend = rig.camera("cam", trigger_mode="software")
    backend.push(_picture(DENT))                # a late or stray picture: no trigger is waiting for it
    assert _wait_until(lambda: cam.get_latest_frame_id() == 1)
    rig.pump_for(0.1)
    assert rig.engine.seen == [] and runtime.counter.total_inspected == 0


def test_an_unanswered_software_trigger_does_not_shift_the_next_one(lines, rig, monkeypatch):  # noqa: F811   Review Focus 4
    monkeypatch.setattr(trigger_inspection, "TRIGGER_TIMEOUT_S", 0.1)
    runtime = _line(lines, [_trigger_cam("cam")])
    cam, backend = rig.camera("cam", trigger_mode="software")
    seen = {}

    async def steps():
        seen["first"] = await trigger_camera(runtime, "cam")            # the camera does not answer
        backend.push(_picture(DENT))                                    # ... its picture comes late
        assert _wait_until(lambda: cam.pending_triggered == 1)
        backend.on_trigger = lambda: backend.push(_picture(BOTTLE))
        seen["second"] = await trigger_camera(runtime, "cam")

    _run(steps)
    assert seen["first"]["status"] == "no_frame" and seen["first"]["result"] is None
    assert (seen["second"]["status"], seen["second"]["result"]) == ("inspected", "good")
    assert rig.engine.seen == [BOTTLE] and _totals(runtime.counter) == (1, 1, 0)


def test_the_server_cannot_trigger_a_hardware_triggered_camera(lines, rig):  # noqa: F811   D8
    runtime = _line(lines, [_trigger_cam("cam")])
    rig.camera("cam", trigger_mode="hardware")

    async def steps():
        with pytest.raises(ValueError, match="triggered by its own input"):
            await trigger_camera(runtime, "cam")

    _run(steps)
    assert runtime.counter.total_inspected == 0


def test_a_refused_software_trigger_is_an_error_not_a_product(lines, rig):  # noqa: F811
    runtime = _line(lines, [_trigger_cam("cam", no_product="reject")])
    cam, backend = rig.camera("cam", trigger_mode="software")
    backend.fail["execute"] = OSError("the camera is busy")

    async def steps():
        with pytest.raises(ValueError, match="busy"):
            await trigger_camera(runtime, "cam")

    _run(steps)
    assert runtime.counter.total_inspected == 0


def test_a_code_reader_triggers_a_software_camera_for_its_picture(lines, rig):  # noqa: F811   D12
    runtime = _line(lines, [{"camera_id": "cam", "role": "qr", "qr_trigger": "signal"}])
    cam, backend = rig.camera("cam", trigger_mode="software")
    reader, captures = _reads(rig, runtime, "cam", {1: "AAA"})
    backend.on_trigger = lambda: backend.push(_picture(1))
    reader.request_capture()
    assert _wait_until(lambda: len(captures) == 1)
    assert captures[0]["status"] == "read" and captures[0]["codes"][0]["code"] == "AAA"
    assert backend.calls.count(("execute", "TriggerSoftware")) == 1


def test_a_code_that_does_not_read_is_photographed_again(lines, rig):  # noqa: F811   D12
    runtime = _line(lines, [{"camera_id": "cam", "role": "qr", "qr_trigger": "signal"}])
    cam, backend = rig.camera("cam", trigger_mode="software")
    reader, captures = _reads(rig, runtime, "cam", {1: "AAA"})
    shots = iter((9, 9, 1))
    backend.on_trigger = lambda: backend.push(_picture(next(shots)))
    reader.request_capture()
    assert _wait_until(lambda: len(captures) == 1)
    assert captures[0]["status"] == "read" and captures[0]["frames"] == 3
    assert backend.calls.count(("execute", "TriggerSoftware")) == 3


def test_a_software_camera_that_does_not_answer_gives_a_capture_without_a_picture(lines, rig, monkeypatch):  # noqa: F811
    monkeypatch.setattr(GigECamera, "TRIGGER_WAIT_S", 0.05)
    runtime = _line(lines, [{"camera_id": "cam", "role": "qr", "qr_trigger": "signal"}])
    cam, backend = rig.camera("cam", trigger_mode="software")
    reader, captures = _reads(rig, runtime, "cam", {1: "AAA"})
    reader.request_capture()
    assert _wait_until(lambda: len(captures) == 1)
    assert captures[0]["status"] == "camera_offline" and captures[0]["codes"] == []


# ── Warnings for combinations that do nothing ────────────────────────────────


def _warned(cameras, modes, plc_inputs=()):
    return line_warnings({"cameras": cameras, "plc_inputs": list(plc_inputs)}, trigger_modes=modes)


def test_a_hardware_code_reader_needs_no_signal_card():
    reader = {"camera_id": "cam", "role": "qr", "qr_trigger": "signal"}
    assert any("an input asks" in w for w in _warned([reader], {}))                # a free running camera does
    assert _warned([reader], {"cam": "hardware"}) == []


def test_a_signal_card_for_a_hardware_camera_is_warned_about():
    reader = {"camera_id": "cam", "role": "qr", "qr_trigger": "signal"}
    card = {"action": "capture", "camera_id": "cam", "enabled": True, "name": "Read now"}
    warnings = _warned([reader], {"cam": "hardware"}, [card])
    assert len(warnings) == 1 and "Read now" in warnings[0] and "its own input" in warnings[0]


@pytest.mark.parametrize("camera, word", [
    ({"camera_id": "cam", "model_id": "m"}, "Count mode"),
    ({"camera_id": "cam", "role": "qr"}, "Read codes"),
])
def test_a_software_camera_nothing_triggers_is_warned_about(camera, word):
    warnings = _warned([camera], {"cam": "software"})
    assert len(warnings) == 1 and "Camera 1" in warnings[0] and word in warnings[0]
    assert _warned([camera], {}) == []
    on_its_input = _warned([camera], {"cam": "hardware"})
    # A code reader on hardware trigger that reads Continuously only shows its codes: said once.
    assert on_its_input == [] if word == "Count mode" else (len(on_its_input) == 1 and "only shows the codes" in on_its_input[0])


def test_the_one_picture_uses_of_a_software_camera_are_not_warned_about():
    card = {"action": "capture", "camera_id": "cam", "enabled": True, "name": "Take"}
    counting = {"camera_id": "cam", "model_id": "m", "count_mode": "trigger"}
    reader = {"camera_id": "cam", "role": "qr", "qr_trigger": "signal"}
    assert _warned([counting], {"cam": "software"}, [card]) == []
    assert _warned([reader], {"cam": "software"}, [card]) == []
