"""The code reader's image processing step is removed (owner, 2026-10-25): zxing-cpp already binarizes,
inverts and turns the picture itself, so the extra step only made reading slower. The corner picture
shows the code as the camera saw it; the CODE READER banner stays."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.engines import qr_engine
from app.services import qr_service
from app.services.line_config import READER_KEPT_KEYS, normalize_camera
from app.services.line_service import line_manager
from app.services.product_service import product_catalog
from tests.test_code_reader_corner_picture import _qr_bottom_right

ROOT = Path(__file__).resolve().parents[1]


def test_decode_has_no_processing():
    assert "processing" not in inspect.signature(qr_engine.QREngine.decode).parameters
    assert not hasattr(qr_engine, "LastRead")
    assert not (ROOT / "app" / "engines" / "code_processing.py").exists()


def test_corner_picture_is_the_camera_crop(monkeypatch):
    assert qr_service.CornerPicture._fields == ("image", "code", "known")
    monkeypatch.setattr(line_manager, "route", lambda cid: None)
    monkeypatch.setattr(product_catalog, "lookup", lambda code, list_id=None: None)
    worker = qr_service._QRReaderWorker("cam-removed")
    try:
        # A line saved while processing existed may still carry its settings: they are ignored.
        monkeypatch.setattr(worker, "_camera_entry", lambda: {"code_processing": {"enabled": True,
                                                                                  "binarize": {"on": True, "method": "fixed", "level": 255}}})
        assert worker.process(_qr_bottom_right()) == ["PROD-0042"]
    finally:
        worker.stop()
    corner = worker.corner()
    assert corner.code == "PROD-0042" and corner.image.max() > 200      # the camera's own crop, not a black processed one


def test_saved_processing_settings_are_ignored():
    camera = normalize_camera({"camera_id": "c1", "role": "qr", "code_processing": {"enabled": True}}, 0)
    assert "code_processing" not in camera
    assert "code_processing" not in READER_KEPT_KEYS


def test_preview_route_is_gone(client, admin_headers):
    res = client.post("/api/v1/qr/preview/camera/cam-1", headers=admin_headers, json={"processing": {}})
    assert res.status_code in (404, 405)


def test_no_image_processing_fold():
    cards = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    page = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    assert "CodeProcessing" not in cards and '"processing"' not in cards
    assert "code_processing.js" not in page
    assert not (ROOT / "assets" / "code_processing.js").exists()


def test_corner_text_is_the_code_only(monkeypatch):
    import cv2
    import numpy as np

    texts = []
    real = cv2.putText
    monkeypatch.setattr(cv2, "putText", lambda img, text, *a, **k: (texts.append(text), real(img, text, *a, **k))[1])
    corner = qr_service.CornerPicture(np.full((40, 80, 3), 255, np.uint8), "PROD-0042", True)
    qr_service.draw_corner(np.zeros((480, 640, 3), np.uint8), corner, 30)
    assert texts == ["PROD-0042"]
