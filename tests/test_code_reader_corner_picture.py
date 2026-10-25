"""The code reader's corner picture of the last code read (cut out of the camera's picture), drawn in the
top-left of the video (and of a triggered capture), and the CODE READER banner."""
from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app.engines import inference_engine
from app.engines.inference_engine import status_bar_height
from app.services import qr_service
from app.services.line_config import normalize_line
from app.services.line_service import LineRuntime, line_manager
from app.services.counting_service import CountingService
from app.services.product_service import product_catalog
from app.state.application_state import app_state
from tests.test_qr_trigger import FrameCamera

GREEN, RED = (0, 200, 0), (0, 0, 230)
BOX = (slice(None), slice(10, 170))


def _upright_qr() -> np.ndarray:
    code = cv2.resize(cv2.QRCodeEncoder.create().encode("PROD-0042"), None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
    code = cv2.copyMakeBorder(code, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    return cv2.cvtColor(code, cv2.COLOR_GRAY2BGR)


@pytest.fixture
def reader(monkeypatch):
    """A reader worker for "cam-corner"; stopped afterwards."""
    workers = []

    def make(known=False):
        worker = qr_service._QRReaderWorker("cam-corner")
        workers.append(worker)
        monkeypatch.setattr(worker, "_camera_entry", lambda: {})
        monkeypatch.setattr(product_catalog, "lookup", lambda code, list_id=None: {"name": "Tea"} if known else None)
        return worker

    monkeypatch.setattr(line_manager, "route", lambda cid: None)
    yield make
    for worker in workers:
        worker.stop()


def _qr_bottom_right() -> np.ndarray:
    """A 640 x 480 black picture with a QR code in its bottom-right corner (its outline stays clear of the top-left)."""
    picture = np.zeros((480, 640, 3), np.uint8)
    code = _upright_qr()
    picture[480 - code.shape[0]:, 640 - code.shape[1]:] = code
    return picture


def _draw(monkeypatch, worker, enabled=True, triggers=None, **kwargs):
    monkeypatch.setattr(qr_service.QRReaderPipeline, "peek", classmethod(lambda cls, cid: worker))
    runtime = SimpleNamespace(enabled=enabled, capture_triggers=triggers or {}, reads_codes=lambda cid: False)
    monkeypatch.setattr(line_manager, "route", lambda cid: (runtime, "qr"))
    return qr_service.QRReaderPipeline.draw_reads("cam-corner", np.zeros((480, 640, 3), np.uint8), **kwargs)


def _bar():
    return status_bar_height(["CODE READER", "CODES: 1", "0ms"], 640, 480)


def test_a_read_keeps_a_corner_picture(reader):
    worker = reader()
    assert worker.corner() is None
    assert worker.process(_qr_bottom_right()) == ["PROD-0042"]
    corner = worker.corner()
    assert corner.code == "PROD-0042" and corner.known is False
    assert corner.image.ndim == 3 and max(corner.image.shape[:2]) <= 240


def test_corner_drawn_top_left_below_the_banner(monkeypatch, reader):
    for known, colour in ((True, GREEN), (False, RED)):
        worker = reader(known=known)
        worker.process(_qr_bottom_right())
        img = _draw(monkeypatch, worker)
        bar = _bar()
        assert img[bar + 10:bar + 200, 10:170].any()            # the corner picture
        assert not img[bar:bar + 8, 10:170].any()               # the gap under the bar
        assert tuple(int(v) for v in img[bar + 10, 10]) == colour


def test_no_corner_while_stopped(monkeypatch, reader):
    worker = reader()
    worker.process(_upright_qr())
    img = _draw(monkeypatch, worker, enabled=False)
    assert not img[_bar() + 4:300, 10:170].any()


def test_corner_shows_while_waiting_for_a_crossing(monkeypatch, reader):
    worker = reader()
    worker.process(_upright_qr())
    img = _draw(monkeypatch, worker, triggers={"cam-corner": (1, 0.0)})
    assert img[_bar() + 10:_bar() + 200, 10:170].any()


def test_below_a_vision_bar(monkeypatch, reader):
    worker = reader()
    worker.process(_qr_bottom_right())
    img = _draw(monkeypatch, worker, banner=False, top=60)
    assert not img[:70, 10:170].any()
    assert img[70:260, 10:170].any()


def test_triggered_capture_draws_the_corner(monkeypatch, reader):
    picture = _qr_bottom_right()
    monkeypatch.setitem(app_state.cameras, "cam-corner", FrameCamera(picture))
    worker = reader()
    record, jpeg = worker.capture({"track_id": 1, "wire_line": 1})
    assert record["status"] == "read" and worker.corner().code == "PROD-0042"
    drawn = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert drawn[40:200, 10:170].max() > 150                    # below the CAPTURE banner (30 px)
    plain = cv2.imdecode(np.frombuffer(qr_service.draw_capture(picture, record), np.uint8), cv2.IMREAD_COLOR)
    assert plain[40:200, 10:170].max() < 60                     # no corner picture without one


def test_banner_says_code_reader(monkeypatch, reader):
    seen = []
    real = inference_engine.draw_status_bar
    monkeypatch.setattr(inference_engine, "draw_status_bar", lambda img, parts: (seen.append(parts), real(img, parts))[1])
    worker = reader()
    for enabled, triggers in ((False, None), (True, {"cam-corner": (1, 0.0)}), (True, None)):
        _draw(monkeypatch, worker, enabled=enabled, triggers=triggers)
    assert [parts[0] for parts in seen] == ["CODE READER"] * 3


def test_corner_picture_is_cleared_when_the_line_starts(monkeypatch):
    monkeypatch.setattr(product_catalog, "lookup", lambda code, list_id=None: None)
    worker = qr_service.QRReaderPipeline.get_worker("cam-start")
    try:
        monkeypatch.setattr(worker, "_camera_entry", lambda: {})
        worker.process(_upright_qr())
        assert worker.corner() is not None
        raw = {"id": "line-start", "name": "Start line", "cameras": [{"camera_id": "cam-start", "role": "qr"}]}
        runtime = LineRuntime("line-start", "Start line", CountingService(line_id="line-start", line_name="Start line"))
        runtime.configure(normalize_line({**raw, "enabled": False}), {})
        assert worker.corner() is not None                      # stopping keeps it (it is hidden)
        runtime.configure(normalize_line({**raw, "enabled": True}), {})
        assert worker.corner() is None
    finally:
        qr_service.QRReaderPipeline.remove_camera("cam-start")
