"""The Line Dashboard's video has no AI view / Raw switch, and a code reader's tile has no
Last picture / Live switch and no Test picture button (owner's request, 2026-10-24).

The large camera always shows the AI view, a code reader's tile always shows its live picture, and
the server has no raw video stream and no test picture any more.
"""
from __future__ import annotations

import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _routes(router):
    return {(method, route.path) for route in router.routes for method in route.methods}


def test_the_large_camera_has_no_ai_raw_switch():
    page = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    for gone in ("feedModeAI", "feedModeRaw", "feedModeLabel", "setFeedMode", "isAIFeed"):
        assert gone not in page, gone
    # The large camera's stream is the AI view.
    assert "/api/v1/vision/stream/camera/${cameraId}" in page
    assert "/api/v1/cameras/${cameraId}/stream" not in page


def test_a_code_reader_tile_has_no_view_switch_and_no_test_picture():
    script = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    for gone in ("secondView", "data-view-of", 'data-view="capture"', "Test picture", "pl-strip-test", "plInlineTest", "testCapture"):
        assert gone not in script, gone


def test_the_server_has_no_raw_stream_and_no_test_picture():
    from app.routes.v1 import camera, lines
    from app.services.qr_service import _QRReaderWorker
    from app.services.vision_service import CameraStreamPipeline

    assert ("GET", "/cameras/{camera_id}/stream") not in _routes(camera.router)
    assert ("POST", "/lines/{line_id}/qr/capture") not in _routes(lines.router)
    # The reads panel still shows the last picture a triggered code reader took.
    assert ("GET", "/lines/{line_id}/qr/capture") in _routes(lines.router)
    assert not hasattr(CameraStreamPipeline, "stream_raw_mjpeg")
    assert not hasattr(CameraStreamPipeline, "render_raw_frame")
    assert "test" not in inspect.signature(_QRReaderWorker.request_capture).parameters
