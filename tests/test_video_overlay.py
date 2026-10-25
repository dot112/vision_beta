"""What a camera's video shows on top of the picture: the status bar, boxes and code outlines.

The dashboard fits every video into its box, so a bar drawn in fixed pixels looked big on a
small ROI crop, tiny on a large picture, and was cut off on a narrow one (owner, 2026-10-14).
A camera whose line is stopped is not inspected: its video showed the last boxes, frozen on
the moving picture, which looked like a model that does not work.
"""
from __future__ import annotations


from types import SimpleNamespace

import numpy as np
import pytest

from app.engines.inference_engine import (
    STATUS_NO_LINE,
    STATUS_STOPPED,
    InferenceEngine,
    overlay_scale,
    status_bar_layout,
)
from app.state.application_state import app_state
from tests.test_ip_camera_http import _Still, _wait_for
from tests.test_vision_pipeline import _det

BAR = np.array((20, 24, 33), np.uint8)


def _bar_height(img: np.ndarray) -> int:
    """Rows from the top whose left edge is the status bar's colour."""
    rows = 0
    while rows < img.shape[0] and np.array_equal(img[rows, 1], BAR):
        rows += 1
    return rows


def _on_screen(img: np.ndarray, box_w: int = 640, box_h: int = 480) -> float:
    """The bar's height on screen when the picture is fitted into a 4:3 box, as the dashboard does."""
    h, w = img.shape[:2]
    return _bar_height(img) * min(box_w / w, box_h / h)


# ── Size ──────────────────────────────────────────────────────────────────────


def test_a_640_picture_keeps_the_bar_it_always_had():
    assert overlay_scale(640, 480) == 1.0 and overlay_scale(640, 360) == 1.0
    img = InferenceEngine(device="cpu").draw_annotations_mat(np.zeros((360, 640, 3), np.uint8), [], 12.0, fps=25.0)
    assert _bar_height(img) == 32


@pytest.mark.parametrize("w, h", [(640, 360), (615, 720), (288, 720), (1280, 720), (1759, 1573), (3840, 2160)])
def test_the_bar_looks_the_same_size_on_screen_for_any_picture(w, h):
    from app.engines.inference_engine import hud_parts
    from app.services.line_service import line_manager

    img = InferenceEngine(device="cpu").draw_annotations_mat(np.zeros((h, w, 3), np.uint8), [], 12.0, fps=25.0)
    lines = len(status_bar_layout(hud_parts(line_manager.counter_for_camera(None), 25.0, 12.0, None), w, h)[0])
    assert _on_screen(img) == pytest.approx(6 + 26 * lines, abs=2.5), (w, h, _bar_height(img))


def test_a_narrow_picture_puts_the_bar_on_more_lines_instead_of_cutting_it():
    import cv2

    parts = ["LINE STOPPED", "TOTAL: 1935", "GOOD: 1931", "REJ: 4", "PPM: 18.1", "FPS: 25.0"]
    for w, h in ((200, 720), (288, 720), (615, 720), (640, 360)):
        lines, font, thick, line_h, pad = status_bar_layout(parts, w, h)
        assert " | ".join(lines).split(" | ") == parts            # nothing left out, nothing reordered
        for line in lines:
            assert cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, font, thick)[0][0] <= w - 2 * pad, (w, line)
    assert len(status_bar_layout(parts, 640, 360)[0]) == 1
    assert len(status_bar_layout(parts, 200, 720)[0]) > 1


def test_an_object_at_the_top_never_covers_the_bar():
    engine = InferenceEngine(device="cpu")
    engine.task = "segment"
    top_object = _det(0, 0, 300, 120, polygon=[[0, 0], [300, 0], [300, 120], [0, 120]])
    img = engine.draw_annotations_mat(np.zeros((720, 615, 3), np.uint8), [top_object], 53.0, fps=23.5)
    bar = _bar_height(img)
    assert bar >= 48                                   # the bar's left edge is its own colour all the way down
    assert np.array_equal(img[bar - 2, 2], BAR)


def test_count_lines_and_labels_stay_below_a_taller_bar():
    engine = InferenceEngine(device="cpu")
    img = engine.draw_annotations_mat(np.zeros((720, 200, 3), np.uint8), [], 12.0, fps=25.0)
    bar = _bar_height(img)
    assert bar > 48                                    # a 200 px wide crop needs more than one line
    assert not np.array_equal(img[bar + 1, 1], BAR)


# ── A stopped line ────────────────────────────────────────────────────────────


@pytest.fixture
def tracked(monkeypatch):
    """Camera cam-S has a confirmed track at 280..360 x 200..280, as after a line ran."""
    from app.engines.tracker import WirelineTracker
    from app.schemas.counting import CountingConfig
    from app.services.counting_service import counting_service

    tracker = WirelineTracker(track_high_thresh=0.5, min_hits=1)
    tracker.update([_det(280, 200, 360, 280)], 640, 480, 0.35, 0.65, "horizontal", "forward", ["bottle"], [],
                   camera_rotation=90)
    monkeypatch.setitem(counting_service._trackers, "cam-S", tracker)
    monkeypatch.setattr(counting_service, "config", CountingConfig(expected_classes=["bottle"], defect_classes=[]))
    return tracker


def test_a_stopped_line_draws_no_old_boxes(tracked):
    engine = InferenceEngine(device="cpu")
    blank = np.zeros((480, 640, 3), np.uint8)
    running = engine.draw_annotations_mat(blank.copy(), [], 12.0, draw_wirelines=False, camera_id="cam-S")
    stopped = engine.draw_annotations_mat(blank.copy(), [], 0.0, draw_wirelines=False, camera_id="cam-S",
                                          status=STATUS_STOPPED)
    assert running[200:282, 278:362].any()
    assert not stopped[200:282, 278:362].any()


def test_a_stopped_line_still_draws_the_boxes_of_a_picture_inspected_on_request(tracked):
    # The detect API inspects one picture on request: its own boxes are current.
    engine = InferenceEngine(device="cpu")
    out = engine.draw_annotations_mat(np.zeros((480, 640, 3), np.uint8), [_det(40, 300, 120, 380)], 9.0,
                                      draw_wirelines=False, camera_id="cam-S", status=STATUS_STOPPED)
    assert out[300:382, 38:122].any() and not out[200:282, 278:362].any()


def test_the_bar_says_why_nothing_is_inspected():
    from app.engines.inference_engine import hud_parts

    counter = SimpleNamespace(total_inspected=13, good_count=9, rejected_count=4, products_per_minute=18.1)
    assert hud_parts(counter, 25.0, 39.0, None) == ["TOTAL: 13", "GOOD: 9", "REJ: 4", "PPM: 18.1", "FPS: 25.0", "YOLO: 39ms"]
    assert hud_parts(counter, 25.0, 0.0, STATUS_STOPPED) == ["LINE STOPPED", "TOTAL: 13", "GOOD: 9", "REJ: 4",
                                                               "PPM: 18.1", "FPS: 25.0"]
    # A camera on no line has no counts of its own.
    assert hud_parts(counter, 25.0, 0.0, STATUS_NO_LINE) == ["NOT ON A LINE", "FPS: 25.0"]


# ── The video the dashboard gets ──────────────────────────────────────────────


class _RecordingEngine:
    is_loaded = True

    def __init__(self):
        self.calls = []

    def draw_annotations_mat(self, mat, detections, **kwargs):
        self.calls.append((list(detections), kwargs))
        return mat


@pytest.mark.parametrize("enabled, expected", [(True, None), (False, STATUS_STOPPED)])
def test_the_video_of_a_stopped_line_is_drawn_without_the_last_results(monkeypatch, enabled, expected):
    from app.services.line_service import line_manager
    from app.services.vision_service import CameraStreamPipeline

    engine = _RecordingEngine()
    runtime = SimpleNamespace(enabled=enabled, reads_codes=lambda camera_id: False)
    camera_id = f"cam-video-{enabled}"
    monkeypatch.setattr(line_manager, "engine_for_camera", lambda cid: (engine, "fake", None))
    monkeypatch.setattr(line_manager, "route", lambda cid: (runtime, "vision"))
    monkeypatch.setitem(app_state.cameras, camera_id, _Still(camera_id, 90))
    worker = CameraStreamPipeline.get_worker(camera_id)
    with worker._lock:
        worker._latest_detections, worker._latest_inference_ms = [_det(1, 1, 9, 9)], 40.0
    publisher = CameraStreamPipeline.get_publisher(camera_id)
    publisher.add_subscriber()
    try:
        assert _wait_for(lambda: engine.calls)
        detections, kwargs = engine.calls[-1]
        assert kwargs.get("status") == expected
        assert (detections, kwargs["latency_ms"]) == (([_det(1, 1, 9, 9)], 40.0) if enabled else ([], 0.0))
    finally:
        publisher.remove_subscriber()
        CameraStreamPipeline.remove_camera(camera_id)


def test_the_code_readers_video_of_a_stopped_line_shows_no_old_codes(monkeypatch):
    from app.services.line_service import line_manager
    from app.services.qr_service import QRReaderPipeline

    code = SimpleNamespace(data="439108237", polygon=[[100, 100], [200, 100], [200, 200], [100, 200]],
                           bbox=SimpleNamespace(x1=100, y1=100))
    worker = SimpleNamespace(latest=lambda: ([(code, True)], 4.0), corner=lambda: None)
    monkeypatch.setattr(QRReaderPipeline, "peek", classmethod(lambda cls, cid: worker))
    stopped = SimpleNamespace(enabled=False, capture_triggers={})
    running = SimpleNamespace(enabled=True, capture_triggers={})

    def draw(runtime):
        monkeypatch.setattr(line_manager, "route", lambda cid: (runtime, "qr"))
        return QRReaderPipeline.draw_reads("qr-1", np.zeros((480, 640, 3), np.uint8))

    assert draw(running)[150, 98:103].any()           # the code's outline
    assert not draw(stopped)[150, 98:103].any()
    assert _bar_height(draw(stopped)) == 32


def test_the_code_readers_bar_has_the_same_size_on_screen(monkeypatch):
    from app.services.line_service import line_manager
    from app.services.qr_service import QRReaderPipeline

    monkeypatch.setattr(QRReaderPipeline, "peek", classmethod(lambda cls, cid: None))
    monkeypatch.setattr(line_manager, "route", lambda cid: (SimpleNamespace(enabled=True, capture_triggers={}), "qr"))
    for w, h in ((640, 480), (1759, 1573), (300, 720)):
        img = QRReaderPipeline.draw_reads("qr-1", np.zeros((h, w, 3), np.uint8))
        lines = len(status_bar_layout(["CODE READER", "CODES: 0", "0ms"], w, h)[0])
        assert _on_screen(img) == pytest.approx(6 + 26 * lines, abs=2.5), (w, h)
