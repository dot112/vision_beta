"""Vision pipeline and API plumbing: output must stay the same while doing less work."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np
import pytest

from app.schemas.vision import BoundingBox, DetectionItem


def _det(x1, y1, x2, y2, class_name="bottle", class_id=0, polygon=None):
    return DetectionItem(
        class_id=class_id,
        class_name=class_name,
        confidence=0.9,
        bbox=BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2, width=x2 - x1, height=y2 - y1),
        polygon=polygon,
    )


# ── Inference engine ──────────────────────────────────────────────────────────

class _CapturingSession:
    def __init__(self):
        self.inputs = None

    def run(self, output_names, feed):
        self.inputs = feed
        return [np.zeros((1, 84, 10), dtype=np.float32)]  # no box above threshold


def test_preprocessing_matches_reference_tensor():
    from app.engines.inference_engine import InferenceEngine

    engine = InferenceEngine(device="cpu")
    session = _CapturingSession()
    engine.ort_session = session
    engine.input_name = "images"
    engine.is_loaded = True

    img = np.random.default_rng(0).integers(0, 256, (720, 1280, 3), dtype=np.uint8)
    engine.predict_mat(img)

    blob = session.inputs["images"]
    resized = cv2.resize(img, engine.input_size)
    expected = np.expand_dims(resized[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) * (1.0 / 255.0), 0)
    assert blob.dtype == np.float32
    assert blob.flags["C_CONTIGUOUS"]
    assert np.array_equal(blob, expected)


def test_blend_filled_rect_matches_full_frame_blend():
    from app.engines.inference_engine import _blend_filled_rect

    rng = np.random.default_rng(1)
    for pt1, pt2 in [((10, 0), (30, 480)), ((-5, 460), (700, 480)), ((620, 0), (640, 480)), ((0, 0), (0, 0)), ((50, 50), (40, 45))]:
        img = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
        expected = img.copy()
        overlay = expected.copy()
        cv2.rectangle(overlay, pt1, pt2, (255, 230, 0), -1)
        cv2.addWeighted(overlay, 0.30, expected, 0.70, 0, expected)

        _blend_filled_rect(img, pt1, pt2, (255, 230, 0), 0.30, 0.70)
        assert np.array_equal(img, expected), (pt1, pt2)


def test_segment_outline_uses_display_coordinates_and_leaves_detections_alone():
    from app.engines.inference_engine import InferenceEngine

    engine = InferenceEngine(device="cpu")
    engine.task = "segment"
    # A 1280x720 source frame shown at half size: the object sits at 200..300 x 150..250
    # in the display image and at 400..600 x 300..500 in source coordinates.
    display = np.zeros((360, 640, 3), dtype=np.uint8)
    display[150:250, 200:300] = 255
    det = _det(400, 300, 600, 500)

    outline = engine._trace_box_outline(display, det.bbox, 0.5, 0.5)
    xs, ys = outline[:, 0], outline[:, 1]
    assert (xs.min(), ys.min(), xs.max(), ys.max()) == (200, 150, 300, 250)

    engine.draw_annotations_mat(display.copy(), [det], draw_wirelines=False, scale_x=0.5, scale_y=0.5, camera_rotation=90)
    assert det.polygon is None
    assert det.mask_area is None


def test_stream_overlay_draws_the_cameras_own_tracks(monkeypatch):
    from app.engines.inference_engine import InferenceEngine
    from app.engines.tracker import WirelineTracker
    from app.schemas.counting import CountingConfig
    from app.services.counting_service import counting_service

    tracker = WirelineTracker(track_high_thresh=0.5, min_hits=1)
    tracker.update([_det(280, 200, 360, 280)], 640, 480, 0.35, 0.65, "horizontal", "forward", ["bottle"], [], camera_rotation=90)
    assert tracker.objects
    monkeypatch.setitem(counting_service._trackers, "cam-A", tracker)
    monkeypatch.setattr(counting_service, "config", CountingConfig(expected_classes=["bottle"], defect_classes=[]))

    engine = InferenceEngine(device="cpu")
    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    with_tracks = engine.draw_annotations_mat(blank.copy(), [], draw_wirelines=False, camera_rotation=90, camera_id="cam-A")
    other_camera = engine.draw_annotations_mat(blank.copy(), [], draw_wirelines=False, camera_rotation=90, camera_id="cam-B")

    # The track's box is drawn for its own camera only.
    assert with_tracks[200:282, 278:362].any()
    assert not other_camera[200:282, 278:362].any()
    assert counting_service.get_tracker("cam-A") is tracker
    assert counting_service.get_tracker("cam-B") is counting_service.tracker
    assert counting_service.get_tracker(None) is counting_service.tracker


# ── Several models on one GPU ─────────────────────────────────────────────────

class _GpuSession:
    """An ONNX Runtime session that records how many sessions run at the same moment."""

    provider = "DmlExecutionProvider"
    running = 0
    most_at_once = 0
    freed_while_running = 0
    _count = threading.Lock()

    def __init__(self, path, opts=None, providers=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def get_providers(self):
        return [self.provider, "CPUExecutionProvider"]

    def get_inputs(self):
        class _Input:
            name = "images"
        return [_Input()]

    def get_modelmeta(self):
        class _Meta:
            custom_metadata_map = {}
        return _Meta()

    def run(self, output_names, feed):
        cls = _GpuSession
        with cls._count:
            cls.running += 1
            cls.most_at_once = max(cls.most_at_once, cls.running)
        self.entered.set()
        time.sleep(0.01)
        self.release.wait(5)
        with cls._count:
            cls.running -= 1
        return [np.zeros((1, 84, 10), dtype=np.float32)]

    def __del__(self):
        if _GpuSession.running:
            _GpuSession.freed_while_running += 1


def _gpu_engines(monkeypatch, tmp_path, provider, count=2):
    from app.engines import inference_engine

    pytest.importorskip("onnxruntime")
    monkeypatch.setattr(_GpuSession, "provider", provider)
    monkeypatch.setattr(_GpuSession, "running", 0)
    monkeypatch.setattr(_GpuSession, "most_at_once", 0)
    monkeypatch.setattr(_GpuSession, "freed_while_running", 0)
    monkeypatch.setattr(inference_engine.ort, "InferenceSession", _GpuSession)
    monkeypatch.setattr(inference_engine.ort, "get_available_providers", lambda: [provider, "CPUExecutionProvider"])
    engines = []
    for i in range(count):
        path = tmp_path / f"model{i}.onnx"
        path.write_bytes(b"stands in for a model")
        engine = inference_engine.InferenceEngine(model_path=str(path), device="dml" if "Dml" in provider else "cpu")
        assert engine.is_loaded and isinstance(engine.ort_session, _GpuSession)
        engines.append(engine)
    return engines


def _predict_on_threads(engines, frames=20):
    img = np.zeros((48, 64, 3), dtype=np.uint8)
    failures = []

    def work(engine):
        try:
            for _ in range(frames):
                engine.predict_mat(img)
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=work, args=(engine,)) for engine in engines]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not failures, failures


def test_directml_models_never_run_at_the_same_time(monkeypatch, tmp_path):
    # Two DirectML sessions running at once on one GPU end the server process.
    engines = _gpu_engines(monkeypatch, tmp_path, "DmlExecutionProvider")
    _predict_on_threads(engines)
    assert _GpuSession.most_at_once == 1


def test_cpu_models_still_run_side_by_side(monkeypatch, tmp_path):
    engines = _gpu_engines(monkeypatch, tmp_path, "CPUExecutionProvider")
    _predict_on_threads(engines)
    assert _GpuSession.most_at_once == 2


def test_a_directml_model_is_freed_only_while_no_other_one_runs(monkeypatch, tmp_path):
    # Freeing a DirectML session while another one runs ends the server process too.
    running, dropped = _gpu_engines(monkeypatch, tmp_path, "DmlExecutionProvider")
    running.ort_session.release.clear()
    img = np.zeros((48, 64, 3), dtype=np.uint8)
    worker = threading.Thread(target=running.predict_mat, args=(img,))
    worker.start()
    assert running.ort_session.entered.wait(5)

    closer = threading.Thread(target=dropped.close)
    closer.start()
    closer.join(0.3)
    assert closer.is_alive(), "close() did not wait for the model that was running"
    assert dropped.ort_session is not None

    running.ort_session.release.set()
    worker.join(5)
    closer.join(5)
    assert not closer.is_alive()
    assert dropped.ort_session is None and not dropped.is_loaded
    assert _GpuSession.freed_while_running == 0
    with pytest.raises(RuntimeError):
        dropped.predict_mat(img)


# ── Inference worker and continuous runner ────────────────────────────────────

class _BlockingEngine:
    is_loaded = True
    task = "detect"

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.seen = []

    def predict_mat(self, mat, conf_threshold=None, nms_threshold=None):
        from app.schemas.vision import DetectionResponse

        self.seen.append(mat)
        self.started.set()
        self.release.wait(timeout=5)
        h, w = mat.shape[:2]
        return DetectionResponse(model_name="fake", total_detections=0, detections=[], inference_time_ms=1.0, image_width=w, image_height=h)


def test_worker_takes_one_frame_at_a_time_and_can_skip_the_copy(monkeypatch):
    from app.services import counting_service as counting_module
    from app.services import vision_service

    engine = _BlockingEngine()
    from app.services.line_service import line_manager
    monkeypatch.setattr(line_manager, "engine_for_camera", lambda camera_id: (engine, "fake", None))
    monkeypatch.setattr(counting_module.counting_service, "process_frame", lambda *a, **k: [])

    worker = vision_service._CameraInferenceWorker("cam-worker-test")
    try:
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        assert worker.submit_frame_if_idle(frame, 1, copy=False) is True
        assert engine.started.wait(timeout=2)
        assert worker.is_busy
        assert worker.submit_frame_if_idle(frame, 2) is False  # busy: dropped, not queued
        engine.release.set()

        deadline = time.time() + 2
        while worker.is_busy and time.time() < deadline:
            time.sleep(0.01)
        assert not worker.is_busy
        assert engine.seen[0] is frame  # handed over without a copy

        copied_source = np.ones((48, 64, 3), dtype=np.uint8)
        assert worker.submit_frame_if_idle(copied_source, 3) is True
        deadline = time.time() + 2
        while len(engine.seen) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert engine.seen[1] is not copied_source
        assert np.array_equal(engine.seen[1], copied_source)
    finally:
        engine.release.set()
        worker.stop()


class _ZeroCopyCamera:
    """Driver with the USB/IP camera frame API; records how frames were requested."""

    def __init__(self):
        self.is_connected = True
        self.settings = {}
        self._frame = np.zeros((48, 64, 3), dtype=np.uint8)
        self._fid = 0
        self.copy_requests = []
        self.grab_calls = 0

    def get_latest_frame_id(self):
        self._fid += 1
        return self._fid

    def get_latest_raw_mat(self, copy=True):
        self.copy_requests.append(copy)
        return True, self._frame, self._fid

    def grab_raw_frame(self):
        self.grab_calls += 1
        return True, self._frame.copy(), self._fid


def test_continuous_runner_feeds_frames_without_copying(monkeypatch):
    from app.services import counting_service as counting_module
    from app.services import vision_service
    from app.state.application_state import app_state

    engine = _BlockingEngine()
    engine.release.set()
    from app.services.line_service import line_manager
    monkeypatch.setattr(line_manager, "engine_for_camera", lambda camera_id: (engine, "fake", None))
    monkeypatch.setattr(counting_module.counting_service, "process_frame", lambda *a, **k: [])
    camera = _ZeroCopyCamera()
    monkeypatch.setitem(app_state.cameras, "cam-runner-test", camera)

    before = app_state.processed_frames
    vision_service.ContinuousVisionRunner.start()
    try:
        deadline = time.time() + 3
        while app_state.processed_frames < before + 3 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        vision_service.ContinuousVisionRunner.stop()
        vision_service.CameraStreamPipeline.remove_camera("cam-runner-test")

    assert app_state.processed_frames >= before + 3
    assert camera.copy_requests and not any(camera.copy_requests)
    assert camera.grab_calls == 0
    assert all(seen is camera._frame for seen in engine.seen)


# ── Outbound event delivery ───────────────────────────────────────────────────

def test_slow_destination_does_not_hold_up_others_and_order_is_kept():
    from app.services.counting_service import _TelemetryDispatcher

    dispatcher = _TelemetryDispatcher()
    done = []
    try:
        async def slow():
            await asyncio.sleep(0.5)
            done.append("slow")

        def ordered(i):
            async def run():
                done.append(("fast", i))
            return run

        dispatcher.submit(slow, key=("webhook", "http://down.example"))
        for i in range(3):
            dispatcher.submit(ordered(i), key=("mqtt", "line/1"))

        deadline = time.time() + 2
        while len(done) < 4 and time.time() < deadline:
            time.sleep(0.01)
        assert done == [("fast", 0), ("fast", 1), ("fast", 2), "slow"]
    finally:
        dispatcher.stop()


def test_dispatcher_drops_when_full():
    from app.services.counting_service import _TelemetryDispatcher

    dispatcher = _TelemetryDispatcher()
    dispatcher._MAX_PENDING = 2
    gate = threading.Event()
    try:
        async def wait_for_gate():
            while not gate.is_set():
                await asyncio.sleep(0.01)

        for _ in range(5):
            dispatcher.submit(wait_for_gate, key="lane")
        assert dispatcher._dropped == 3
    finally:
        gate.set()
        dispatcher.stop()
    assert dispatcher._pending == 0


# ── MQTT client ───────────────────────────────────────────────────────────────

def test_mqtt_connect_does_not_block_the_event_loop(monkeypatch):
    from app.hardware.mqtt.client import MQTTClient

    client = MQTTClient(host="192.0.2.1")
    opens = []

    def slow_open():
        opens.append(threading.current_thread().name)
        time.sleep(0.4)  # stands in for paho's blocking socket connect
        client._client = object()
        client.is_connected = True

    monkeypatch.setattr(client, "_open_blocking", slow_open)

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                await asyncio.sleep(0.01)
                ticks += 1

        tick_task = asyncio.create_task(ticker())
        results = await asyncio.gather(client.connect(), client.connect())
        stop = True
        await tick_task
        return results, ticks

    results, ticks = asyncio.run(scenario())
    assert results == [True, True]
    assert len(opens) == 1  # the second caller waited for the first attempt
    assert opens[0] != threading.main_thread().name
    assert ticks >= 10  # the loop kept running during the 0.4 s connect


# ── HTTP middleware ───────────────────────────────────────────────────────────

def test_middleware_headers_on_plain_and_streaming_responses():
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient

    from app.middleware.security_headers import SecurityHeadersMiddleware
    from app.middleware.timing import TimingMiddleware

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TimingMiddleware)

    @app.get("/plain")
    async def plain():
        return {"ok": True}

    @app.get("/framed")
    async def framed():
        from fastapi import Response
        return Response("x", headers={"X-Frame-Options": "SAMEORIGIN"})

    @app.get("/stream")
    async def stream():
        async def body():
            for i in range(3):
                yield f"--frame\r\n{i}\r\n".encode()
        return StreamingResponse(body(), media_type="multipart/x-mixed-replace; boundary=frame")

    with TestClient(app) as client:
        for path in ("/plain", "/stream"):
            res = client.get(path)
            assert res.status_code == 200
            assert res.headers["X-Content-Type-Options"] == "nosniff"
            assert res.headers["X-Frame-Options"] == "DENY"
            assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
            assert res.headers["X-Process-Time"].endswith("ms")
        assert client.get("/plain").json() == {"ok": True}
        assert client.get("/stream").content == b"--frame\r\n0\r\n--frame\r\n1\r\n--frame\r\n2\r\n"
        # A header the route set itself is kept, as before.
        assert client.get("/framed").headers["X-Frame-Options"] == "SAMEORIGIN"
