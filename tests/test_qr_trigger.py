"""QR readers that take one picture per product, on a wire line crossing.

Covers the tracker reporting each wire line crossing, the counter handing it
to the line, the line asking its QR reader for a picture, the picture being
decoded and drawn, reads and no-reads reaching the line, Sync pairing a
picture with the product that triggered it, the settings, and the API.
"""
from __future__ import annotations

import asyncio
import threading
import time

import cv2
import numpy as np
import pytest

from app.engines.qr_engine import QREngine
from app.engines.tracker import WirelineTracker
from app.schemas.vision import BoundingBox, DetectionItem
from app.security.api_key_scopes import required_api_key_scopes
from app.services.counting_service import CountingService
from app.services.line_config import normalize_line
from app.services.line_service import LineRuntime, SyncPairer
from app.state.application_state import app_state


def _det(cx: float, cy: float, name: str = "bottle") -> DetectionItem:
    return DetectionItem(
        class_id=0, class_name=name, confidence=0.9,
        bbox=BoundingBox(x1=cx - 20, y1=cy - 30, x2=cx + 20, y2=cy + 30, width=40, height=60),
    )


def _qr_frame(*texts: str) -> np.ndarray:
    """A 960x540 picture with each text as a QR code, left to right."""
    img = np.full((540, 960, 3), 230, np.uint8)
    for i, text in enumerate(texts):
        code = cv2.resize(cv2.QRCodeEncoder.create().encode(text), None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
        x = 40 + i * 450
        img[60:60 + code.shape[0], x:x + code.shape[1]] = cv2.cvtColor(code, cv2.COLOR_GRAY2BGR)
    return img


class FrameCamera:
    """A connected camera whose frame id moves on every time it is asked."""

    def __init__(self, frame):
        self.name = "QR test camera"
        self.is_connected = True
        self.last_error = None
        self.settings = {}
        self._frame = frame
        self._fid = 0
        self._lock = threading.Lock()

    def get_latest_frame_id(self):
        with self._lock:
            self._fid += 1
            return self._fid

    def get_latest_raw_mat(self, copy=True):
        return True, self._frame.copy(), self._fid

    grab_raw_frame = lambda self: self.get_latest_raw_mat()  # noqa: E731

    def disconnect(self):
        self.is_connected = False


def _runtime(trigger: str = "line2", delay_ms: int = 0, sync: bool = False) -> LineRuntime:
    line = normalize_line({
        "id": "line-trig",
        "name": "Trigger line",
        "cameras": [
            {"camera_id": "cam-vision", "role": "vision", "counting": True},
            {"camera_id": "cam-qr", "role": "qr", "qr_trigger": trigger, "qr_trigger_delay_ms": delay_ms},
        ],
        "sync": {"enabled": sync, "window_ms": 500},
    })
    runtime = LineRuntime("line-trig", "Trigger line", CountingService(line_id="line-trig", line_name="Trigger line"))
    runtime.configure(line, {})
    return runtime


# ── Tracker and counter ───────────────────────────────────────────────────────

def test_tracker_reports_each_wire_line_as_it_is_crossed():
    tracker = WirelineTracker(track_high_thresh=0.5, min_hits=1, position_tolerance=300.0, max_speed_pixels=200.0)
    crossings, counted = [], []
    for f in range(25):
        counted.extend(tracker.update([_det(100, 50 + f * 20)], 640, 480, 0.35, 0.65, "horizontal", "forward", ["bottle"], []))
        crossings.extend(tracker.line_crossings)
    assert [line for _, _, line in crossings] == [1, 2]
    assert crossings[0][0] == crossings[1][0] and crossings[0][1] == "bottle"
    assert len(counted) == 1


def test_counter_hands_each_crossing_to_the_line():
    class FakeTracker:
        line_crossings = [(7, "bottle", 1)]

        def update(self, **kwargs):
            return []

    counter = CountingService(line_id="line-x", line_name="X")
    counter._trackers["cam-v"] = FakeTracker()
    seen = []
    counter.line_crossing_sink = lambda *args: seen.append(args)
    counter.process_frame([], 640, 480, camera_rotation=0, camera_flip_h=False, camera_flip_v=False, camera_id="cam-v")
    assert seen == [("cam-v", 1, 7, "bottle")]


# ── The line asks its QR reader for a picture ─────────────────────────────────

def test_line_asks_for_a_picture_only_on_the_chosen_wire_line(monkeypatch):
    from app.services import qr_service

    requests = []

    class FakeWorker:
        def request_capture(self, **kwargs):
            requests.append(kwargs)

    monkeypatch.setattr(qr_service.QRReaderPipeline, "get_worker", classmethod(lambda cls, cid: FakeWorker()))
    runtime = _runtime(trigger="line2", delay_ms=250)
    assert runtime.qr_triggered("cam-qr") and not runtime.qr_triggered("cam-vision")
    assert runtime.counter.line_crossing_sink is not None

    runtime._on_wire_crossing("cam-vision", 1, 5, "bottle")
    assert requests == []
    runtime._on_wire_crossing("some-other-camera", 2, 5, "bottle")
    assert requests == []
    runtime._on_wire_crossing("cam-vision", 2, 5, "bottle")
    assert requests == [{"track_id": 5, "class_name": "bottle", "wire_line": 2, "delay": 0.25}]

    continuous = normalize_line({
        "id": "line-trig", "name": "Trigger line",
        "cameras": [{"camera_id": "cam-vision", "role": "vision"}, {"camera_id": "cam-qr", "role": "qr"}],
    })
    runtime.configure(continuous, {})
    assert not runtime.qr_triggered("cam-qr") and runtime.counter.line_crossing_sink is None


# ── Taking, decoding and drawing the picture ──────────────────────────────────

def test_qr_engine_reads_codes_with_their_four_corners():
    result = QREngine().decode(_qr_frame("PROD-0042"))
    assert [c.data for c in result.codes] == ["PROD-0042"]
    code = result.codes[0]
    assert code.code_type == "QR_CODE" and len(code.polygon) == 4
    assert code.bbox.width > 50 and code.bbox.height > 50


def test_picture_is_decoded_drawn_and_reported_to_the_line(monkeypatch):
    from app.services import qr_service
    from app.services.product_service import product_catalog

    monkeypatch.setitem(app_state.cameras, "cam-qr", FrameCamera(_qr_frame("PROD-0042", "PROD-7")))
    monkeypatch.setattr(product_catalog, "lookup", lambda code, list_id=None: {"name": "Cola 0.5 l"} if code == "PROD-0042" else None)
    worker = qr_service._QRReaderWorker("cam-qr")
    try:
        record, jpeg = worker.capture({"track_id": 3, "class_name": "bottle", "wire_line": 1})
    finally:
        worker.stop()
    assert record["status"] == "read" and record["track_id"] == 3 and record["wire_line"] == 1
    codes = {c["code"]: c for c in record["codes"]}
    assert codes["PROD-0042"]["known"] is True and codes["PROD-0042"]["product_name"] == "Cola 0.5 l"
    assert codes["PROD-7"]["known"] is False
    assert jpeg[:2] == b"\xff\xd8"  # a JPEG
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape[1] == 960

    runtime = _runtime(trigger="line1")
    kept = runtime.on_qr_capture("cam-qr", record, jpeg)
    assert kept["id"] == 1
    info, image = runtime.capture_image()
    assert info["id"] == 1 and image == jpeg
    assert runtime.qr_stats["codes_read"] == 2


def test_camera_offline_is_a_picture_without_codes(monkeypatch):
    from app.services import qr_service

    monkeypatch.setattr(app_state, "cameras", {})
    worker = qr_service._QRReaderWorker("cam-gone")
    try:
        record, jpeg = worker.capture({"track_id": 1, "wire_line": 2})
    finally:
        worker.stop()
    assert record["status"] == "camera_offline" and record["codes"] == [] and jpeg is None


def test_worker_takes_the_picture_after_the_delay(monkeypatch):
    from app.services import qr_service
    from app.services.line_service import line_manager

    monkeypatch.setitem(app_state.cameras, "cam-qr", FrameCamera(_qr_frame("PROD-1")))
    runtime = _runtime(trigger="line1", delay_ms=200)
    monkeypatch.setattr(line_manager, "route", lambda cid: (runtime, "qr") if cid == "cam-qr" else None)
    worker = qr_service._QRReaderWorker("cam-qr")
    try:
        asked = time.monotonic()
        worker.request_capture(track_id=9, wire_line=1, delay=0.2)
        deadline = time.monotonic() + 5
        while runtime.last_capture is None and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()
    assert runtime.last_capture is not None, "no picture was taken"
    assert runtime.last_capture["track_id"] == 9
    assert runtime.last_capture["t"] - asked >= 0.19


def test_picture_without_a_code_is_a_no_read_when_sync_is_off(monkeypatch):
    from app.services.plc_dispatcher_service import PLCDispatcherService

    events = []

    async def fake_evaluate(cls, event):
        events.append(event)

    monkeypatch.setattr(PLCDispatcherService, "evaluate", classmethod(fake_evaluate))
    previous = CountingService._main_loop
    record = {"timestamp": "2026-01-01T00:00:00+00:00", "t": time.monotonic(), "track_id": 4, "class_name": "bottle",
              "wire_line": 1, "test": False, "status": "no_read", "codes": []}

    async def scenario(runtime):
        CountingService.set_event_loop(asyncio.get_running_loop())
        runtime.on_qr_capture("cam-qr", dict(record), b"jpeg")
        await asyncio.sleep(0.05)

    try:
        runtime = _runtime(trigger="line1", sync=False)
        asyncio.run(scenario(runtime))
        assert runtime.qr_stats["no_reads"] == 1
        assert runtime.recent_reads()[0]["status"] == "no_read"
        assert events and events[0]["event_type"] == "qr_read" and events[0]["qr_status"] == "no_read"

        synced = _runtime(trigger="line1", sync=True)
        events.clear()
        asyncio.run(scenario(synced))
        # With Sync on, the product's crossing reports the no-read instead.
        assert synced.qr_stats["no_reads"] == 0 and events == []
    finally:
        CountingService._main_loop = previous


def test_test_picture_sends_nothing():
    runtime = _runtime(trigger="line1")
    runtime.on_qr_capture("cam-qr", {"test": True, "codes": [{"code": "X", "format": "QR_CODE"}], "status": "read"}, b"j")
    assert runtime.capture_image()[0]["test"] is True
    assert runtime.qr_stats["codes_read"] == 0 and runtime.recent_reads() == []


# ── Sync ──────────────────────────────────────────────────────────────────────

def test_sync_pairs_a_picture_with_the_product_that_triggered_it():
    pairer = SyncPairer(0.5, crossing_key=lambda c: c["track"], read_key=lambda r: r.get("track"))
    assert pairer.add_crossing(0.00, {"track": 1}) == []
    assert pairer.add_crossing(0.10, {"track": 2}) == []
    out = pairer.add_read(0.15, {"code": "B", "track": 2})
    assert out == [("paired", {"track": 2}, {"code": "B", "track": 2})]
    # A read without a track pairs by time, with the oldest waiting product.
    out = pairer.add_read(0.20, {"code": "A"})
    assert out == [("paired", {"track": 1}, {"code": "A"})]


def test_sync_does_not_pair_a_picture_with_another_product():
    pairer = SyncPairer(0.5, crossing_key=lambda c: c["track"], read_key=lambda r: r.get("track"))
    pairer.add_crossing(0.0, {"track": 1})
    assert pairer.add_read(0.1, {"code": "B", "track": 2}) == []
    assert pairer.waiting == 2


# ── Settings ──────────────────────────────────────────────────────────────────

def test_capture_settings_are_validated():
    line = normalize_line({"name": "L", "cameras": [
        {"camera_id": "v", "role": "vision"}, {"camera_id": "q", "role": "qr"}]})
    qr = line["cameras"][1]
    assert qr["qr_trigger"] == "continuous" and qr["qr_trigger_delay_ms"] == 0
    assert "qr_trigger" not in line["cameras"][0]
    with pytest.raises(ValueError, match="capture must be"):
        normalize_line({"name": "L", "cameras": [{"camera_id": "v", "role": "vision"},
                                                 {"camera_id": "q", "role": "qr", "qr_trigger": "sometimes"}]})
    with pytest.raises(ValueError, match="needs a vision camera"):
        normalize_line({"name": "L", "cameras": [{"camera_id": "q", "role": "qr", "qr_trigger": "line1"}]})
    with pytest.raises(ValueError, match="capture delay"):
        normalize_line({"name": "L", "cameras": [{"camera_id": "v", "role": "vision"},
                                                 {"camera_id": "q", "role": "qr", "qr_trigger": "line1", "qr_trigger_delay_ms": 60000}]})


# ── API ───────────────────────────────────────────────────────────────────────

def test_capture_routes(client, admin_headers):
    from app.services.line_service import line_manager

    runtime = line_manager.get("line-1")
    saved = (runtime.last_capture, runtime._capture_jpeg)
    try:
        runtime.last_capture, runtime._capture_jpeg = None, None
        assert client.get("/api/v1/lines/line-1/qr/capture", headers=admin_headers).status_code == 404
        assert client.get("/api/v1/lines/line-1/qr/recent", headers=admin_headers).json()["last_capture"] is None
        # Line 1 has no QR reader in the tests.
        assert client.post("/api/v1/lines/line-1/qr/capture", headers=admin_headers).status_code == 409

        runtime.on_qr_capture("cam-qr", {"test": True, "t": 1.0, "status": "read", "timestamp": "2026-01-01T00:00:00+00:00",
                                         "codes": [{"code": "C1", "format": "QR_CODE", "known": False, "polygon": [[0, 0]]}]}, b"\xff\xd8jpeg")
        res = client.get("/api/v1/lines/line-1/qr/capture", headers=admin_headers)
        assert res.status_code == 200 and res.headers["content-type"] == "image/jpeg" and res.content == b"\xff\xd8jpeg"
        last = client.get("/api/v1/lines/line-1/qr/recent", headers=admin_headers).json()["last_capture"]
        assert last["codes"] == [{"code": "C1", "format": "QR_CODE", "known": False}] and "t" not in last
    finally:
        runtime.last_capture, runtime._capture_jpeg = saved


def test_capture_route_scopes():
    assert required_api_key_scopes("GET", "/api/v1/lines/line-1/qr/capture") == {"monitor:read"}
    assert required_api_key_scopes("POST", "/api/v1/lines/line-1/qr/capture") == {"configuration:write"}
