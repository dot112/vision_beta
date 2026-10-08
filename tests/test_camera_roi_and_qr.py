"""Camera ROI (crop before YOLO and the QR reader), reading motion-blurred codes, and the count line overlay."""
from __future__ import annotations

import threading

import cv2
import numpy as np
import pytest

from app.engines.qr_engine import QREngine
from app.hardware.camera.base import BaseCamera, roi_box
from app.state.application_state import app_state


class StillCamera(BaseCamera):
    """A connected camera that keeps returning one raw picture, processed like the real drivers do."""

    def __init__(self, camera_id: str, frame: np.ndarray, settings=None):
        super().__init__(camera_id, "Still", "0", settings or {})
        self.is_connected = True
        self._lock = threading.Lock()
        self._latest_raw_mat = frame
        self._latest_frame_id = 1

    def connect(self):
        return True

    def disconnect(self):
        self.is_connected = False

    def grab_frame(self):
        ok, buf = cv2.imencode(".jpg", self._process_frame(self._latest_raw_mat))
        return ok, buf.tobytes()

    def grab_raw_frame(self):
        return True, self._process_frame(self._latest_raw_mat), self._latest_frame_id


def _marked_frame(w: int = 400, h: int = 200) -> np.ndarray:
    """Grey picture with a red block in the top-left corner and a blue one in the bottom-right."""
    img = np.full((h, w, 3), 128, np.uint8)
    img[:20, :20] = (0, 0, 255)
    img[-20:, -20:] = (255, 0, 0)
    return img


def _has(img: np.ndarray, bgr) -> bool:
    return bool(np.all(img == np.array(bgr, np.uint8), axis=2).any())


# ── ROI ───────────────────────────────────────────────────────────────────────

def test_roi_box_reads_fractions_percentages_and_pixels():
    assert roi_box({"roi_enabled": True, "roi_x": 0.25, "roi_y": 0.1, "roi_w": 0.5, "roi_h": 0.8}, 400, 200) == (100, 20, 300, 180)
    assert roi_box({"roi_enabled": True, "roi_x": 25, "roi_y": 10, "roi_w": 50, "roi_h": 80}, 400, 200) == (100, 20, 300, 180)
    assert roi_box({"roi_enabled": True, "roi_x": 100, "roi_y": 20, "roi_w": 200, "roi_h": 160}, 400, 200) == (100, 20, 300, 180)
    # Off, the whole picture, or too small to be meant: no crop.
    assert roi_box({"roi_enabled": False, "roi_x": 0.25, "roi_y": 0.1, "roi_w": 0.5, "roi_h": 0.8}, 400, 200) is None
    assert roi_box({"roi_enabled": True, "roi_x": 0, "roi_y": 0, "roi_w": 1, "roi_h": 1}, 400, 200) is None
    assert roi_box({"roi_enabled": True, "roi_x": 0, "roi_y": 0, "roi_w": 0.01, "roi_h": 0.01}, 400, 200) is None
    assert roi_box({"roi_enabled": True, "roi_w": "x", "roi_h": 1}, 400, 200) is None


def test_roi_is_cropped_from_the_picture_as_shown_after_rotation():
    # Rotated 90 degrees clockwise, the red top-left corner ends up top-right.
    cam = StillCamera("c", _marked_frame(), {"rotation": 90, "roi_enabled": True, "roi_x": 0.5, "roi_y": 0, "roi_w": 0.5, "roi_h": 0.5})
    out = cam._process_frame(cam._latest_raw_mat)
    assert out.shape[:2] == (200, 100)  # rotated 200x400, then the top-right quarter
    assert _has(out, (0, 0, 255)) and not _has(out, (255, 0, 0))


def test_roi_is_not_stretched_back_to_the_configured_size():
    cam = StillCamera("c", _marked_frame(), {"width": 640, "height": 480, "roi_enabled": True,
                                             "roi_x": 0.5, "roi_y": 0.5, "roi_w": 0.5, "roi_h": 0.5})
    out = cam._process_frame(cam._latest_raw_mat)
    assert out.shape[:2] == (240, 320)
    assert _has(out, (255, 0, 0)) and not _has(out, (0, 0, 255))


def test_full_view_is_the_picture_before_the_crop():
    cam = StillCamera("c", _marked_frame(), {"roi_enabled": True, "roi_x": 0, "roi_y": 0, "roi_w": 0.5, "roi_h": 0.5})
    assert cam.grab_raw_frame()[1].shape[:2] == (100, 200)
    full = cam.get_full_view_mat()
    assert full.shape[:2] == (200, 400)
    assert _has(full, (0, 0, 255)) and _has(full, (255, 0, 0))


def test_usb_camera_keeps_settings_saved_while_it_is_closed():
    from app.hardware.camera.usb_camera import USBCamera

    cam = USBCamera("usb-1", "USB", "0", {})
    assert cam.set_properties({"roi_enabled": True, "roi_w": 0.5, "roi_h": 0.5}) is True
    assert cam.settings["roi_w"] == 0.5


def test_frame_route_gives_the_cropped_or_the_full_picture(client, admin_headers, monkeypatch):
    cam = StillCamera("roi-cam", _marked_frame(), {"roi_enabled": True, "roi_x": 0, "roi_y": 0, "roi_w": 0.5, "roi_h": 0.5})
    monkeypatch.setitem(app_state.cameras, "roi-cam", cam)

    def size(res):
        assert res.status_code == 200, res.text
        return cv2.imdecode(np.frombuffer(res.content, np.uint8), cv2.IMREAD_COLOR).shape[:2]

    assert size(client.get("/api/v1/cameras/roi-cam/frame", headers=admin_headers)) == (100, 200)
    assert size(client.get("/api/v1/cameras/roi-cam/frame?full=true", headers=admin_headers)) == (200, 400)
    cam.is_connected = False
    assert client.get("/api/v1/cameras/roi-cam/frame?full=true", headers=admin_headers).status_code == 400


def test_yolo_and_the_qr_reader_get_the_cropped_picture(monkeypatch):
    """The runner hands workers what get_latest_raw_mat returns: the processed, cropped picture."""
    cam = StillCamera("c", _marked_frame(), {"roi_enabled": True, "roi_x": 0.5, "roi_y": 0.5, "roi_w": 0.5, "roi_h": 0.5})
    ok, mat, _ = cam.get_latest_raw_mat()
    assert ok and mat.shape[:2] == (100, 200) and _has(mat, (255, 0, 0)) and not _has(mat, (0, 0, 255))


# ── QR codes under motion blur ────────────────────────────────────────────────

def _blurred_code_frame(text: str = "LOT-2026-000123", blur: int = 9, axis: int = 1) -> np.ndarray:
    """A 1280x720 belt picture with a 110 px QR label, smeared by straight motion blur."""
    img = np.full((720, 1280), 90, np.uint8)
    code = cv2.QRCodeEncoder.create().encode(text)
    code = cv2.resize(code, (110, 110), interpolation=cv2.INTER_NEAREST)
    label = np.full((150, 150), 235, np.uint8)
    label[20:130, 20:130] = code
    img[300:450, 560:710] = label
    kernel = np.zeros((blur, blur), np.float32)
    if axis == 1:
        kernel[blur // 2, :] = 1.0 / blur
    else:
        kernel[:, blur // 2] = 1.0 / blur
    img = cv2.filter2D(img, -1, kernel)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


@pytest.mark.parametrize("axis", [1, 0])
def test_motion_blurred_code_reads_in_the_second_pass(axis):
    engine = QREngine()
    frame = _blurred_code_frame(axis=axis)
    assert engine.decode(frame).codes == []
    result = engine.decode(frame, hard_budget_ms=500)
    assert [c.data for c in result.codes] == ["LOT-2026-000123"]
    # Its corners are in the coordinates of the whole picture.
    box = result.codes[0].bbox
    assert 540 <= box.x1 < box.x2 <= 730 and 280 <= box.y1 < box.y2 <= 470


def test_second_pass_keeps_to_its_time_budget():
    engine = QREngine()
    noise = np.random.default_rng(0).integers(0, 255, (720, 1280, 3), dtype=np.uint8)
    result = engine.decode(noise, hard_budget_ms=30)
    assert result.codes == []
    assert result.decode_time_ms < 400  # the first pass plus at most one variant past the budget


def test_continuous_reading_limits_the_second_pass_while_nothing_reads(monkeypatch):
    from app.schemas.qr import BarcodeDecodeResponse
    from app.services import qr_service

    budgets = []

    def decode(mat, hard_budget_ms=0.0, code_type="all"):
        budgets.append(hard_budget_ms)
        return BarcodeDecodeResponse(total_found=0, codes=[], decode_time_ms=1.0, image_width=10, image_height=10)

    worker = qr_service._QRReaderWorker("cam-none")
    try:
        monkeypatch.setattr(worker._engine, "decode", decode)
        for _ in range(3):
            worker.process(np.zeros((10, 10, 3), np.uint8))
        worker._hard_missed_at -= qr_service.CONTINUOUS_HARD_INTERVAL_S
        worker.process(np.zeros((10, 10, 3), np.uint8))
    finally:
        worker.stop()
    assert budgets == [qr_service.CONTINUOUS_HARD_BUDGET_MS, 0.0, 0.0, qr_service.CONTINUOUS_HARD_BUDGET_MS]


def test_capture_tries_the_next_frames_when_the_first_does_not_read(monkeypatch):
    from app.services import qr_service

    sharp = np.full((540, 960, 3), 230, np.uint8)
    code = cv2.resize(cv2.QRCodeEncoder.create().encode("PROD-0042"), None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
    sharp[60:60 + code.shape[0], 40:40 + code.shape[1]] = cv2.cvtColor(code, cv2.COLOR_GRAY2BGR)
    frames = [np.full((540, 960, 3), 230, np.uint8), sharp]

    class Sequence:
        name = "seq"
        is_connected = True
        settings: dict = {}

        def __init__(self):
            self.fid = 0
            self.pictures = 0

        def get_latest_frame_id(self):
            self.fid += 1
            return self.fid

        def get_latest_raw_mat(self, copy=True):
            # The first picture has no code in view; the next one has.
            self.pictures += 1
            return True, frames[min(self.pictures - 1, len(frames) - 1)].copy(), self.fid

    monkeypatch.setitem(app_state.cameras, "cam-seq", Sequence())
    worker = qr_service._QRReaderWorker("cam-seq")
    try:
        record, _ = worker.capture({"track_id": 1, "wire_line": 2})
    finally:
        worker.stop()
    assert record["status"] == "read" and [c["code"] for c in record["codes"]] == ["PROD-0042"]
    assert record["frames"] == 2


# ── Count line overlay ────────────────────────────────────────────────────────

@pytest.mark.parametrize("size", [(360, 640), (1080, 1920)])
@pytest.mark.parametrize("vertical", [False, True])
def test_count_lines_are_drawn_in_their_colours(size, vertical):
    from app.engines.inference_engine import COUNT_LINE_A_COLOR, COUNT_LINE_B_COLOR, _draw_count_lines

    h, w = size
    img = np.full((h, w, 3), 60, np.uint8)
    _draw_count_lines(img, vertical, 0.3, 0.7, top=32, flash=0.0)
    span = w if vertical else h
    a, b = int(0.3 * (span - 1)), int(0.7 * (span - 1))
    probe_a = img[h // 2, a] if vertical else img[a, w // 2]
    probe_b = img[h // 2, b] if vertical else img[b, w // 2]
    assert tuple(int(v) for v in probe_a) == COUNT_LINE_A_COLOR
    assert tuple(int(v) for v in probe_b) == COUNT_LINE_B_COLOR


def test_line_b_flashes_after_a_count():
    from app.engines.inference_engine import _draw_count_lines

    quiet = np.full((360, 640, 3), 60, np.uint8)
    flash = quiet.copy()
    _draw_count_lines(quiet, False, 0.3, 0.7, flash=0.0)
    _draw_count_lines(flash, False, 0.3, 0.7, flash=1.0)
    b = int(0.7 * 359)
    # The glow around line B is wider and brighter while it flashes.
    assert flash[b + 8, 320].sum() > quiet[b + 8, 320].sum()


def test_counter_reports_when_it_last_counted():
    from app.services.counting_service import CountingService

    counter = CountingService(dispatch_telemetry=False)
    assert counter.last_count_at == 0.0
    counter._inspection_timestamps.append(123.0)
    assert counter.last_count_at == 123.0
