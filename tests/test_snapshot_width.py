"""The camera snapshot can be asked for at a smaller width (Plant overview tiles)."""
from __future__ import annotations

import asyncio

import cv2
import numpy as np
import pytest

from app.state.application_state import app_state


def _jpeg(width: int, height: int) -> bytes:
    picture = np.full((height, width, 3), 120, np.uint8)
    return cv2.imencode(".jpg", picture)[1].tobytes()


def _size(jpeg: bytes):
    mat = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    return mat.shape[1], mat.shape[0]


def test_shrink_jpeg_keeps_the_shape_and_leaves_small_pictures_alone():
    from app.services.vision_service import shrink_jpeg

    wide = _jpeg(640, 480)
    assert _size(shrink_jpeg(wide, 480)) == (480, 360)
    assert shrink_jpeg(wide, 640) is wide
    assert shrink_jpeg(wide, 1280) is wide
    assert shrink_jpeg(b"not a picture", 480) == b"not a picture"


@pytest.fixture
def publisher(monkeypatch, fake_camera):
    """A camera whose publisher already holds a 640 px picture; its thread is not left running."""
    from app.services.vision_service import CameraStreamPipeline

    fake_camera("cam-tile")
    pub = CameraStreamPipeline.get_publisher("cam-tile")
    pub.stop()
    with pub._lock:
        pub._latest_annotated_jpeg = _jpeg(640, 480)
        pub._annotated_seq = 1
    yield pub
    CameraStreamPipeline.remove_camera("cam-tile")


def test_snapshot_at_a_width_is_made_once_per_picture(publisher, monkeypatch):
    from app.services import vision_service
    from app.services.vision_service import VisionService

    calls = []
    real = vision_service.shrink_jpeg
    monkeypatch.setattr(vision_service, "shrink_jpeg", lambda *a, **k: calls.append(a[1]) or real(*a, **k))

    async def ask(width):
        return await VisionService.get_annotated_frame(None, "cam-tile", max_width=width)

    first = asyncio.run(ask(480))
    assert _size(first) == (480, 360)
    assert asyncio.run(ask(480)) is first, "the same picture was shrunk twice"
    assert calls == [480]
    # Without a width the full picture comes back, as before.
    assert _size(asyncio.run(ask(None))) == (640, 480)

    # A new picture is shrunk again.
    with publisher._lock:
        publisher._latest_annotated_jpeg = _jpeg(640, 360)
        publisher._annotated_seq = 2
    assert _size(asyncio.run(ask(480))) == (480, 270)
    assert calls == [480, 480]


def test_only_a_few_widths_are_kept(publisher):
    from app.services.vision_service import VisionService

    for width in (160, 200, 240, 280, 320, 360):
        asyncio.run(VisionService.get_annotated_frame(None, "cam-tile", max_width=width))
    assert len(publisher._scaled) == publisher._MAX_SCALED_WIDTHS
    assert 360 in publisher._scaled and 160 not in publisher._scaled


def test_snapshot_endpoint_takes_a_width(client, admin_headers, publisher):
    res = client.get("/api/v1/vision/annotated/camera/cam-tile?max_width=320", headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "image/jpeg"
    assert _size(res.content) == (320, 240)
    assert client.get("/api/v1/vision/annotated/camera/cam-tile?max_width=20", headers=admin_headers).status_code == 422
    assert app_state.cameras["cam-tile"].is_connected
