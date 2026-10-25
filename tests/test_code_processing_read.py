"""Reading with image processing: the processed picture, then straightened codes, then the original picture."""
from __future__ import annotations

import warnings

import cv2
import numpy as np
import pytest

from app.engines import qr_engine
from app.engines.code_processing import map_points, normalize_processing, upright
from app.engines.qr_engine import QREngine

zxingcpp = pytest.importorskip("zxingcpp")

EAN = "4006381333931"


def _on(**steps):
    raw = {"enabled": True}
    for name, values in steps.items():
        raw[name] = {"on": True, **values}
    return normalize_processing(raw)


def _label() -> np.ndarray:
    """An EAN-13 with its digits on a white label (BGR), 60 px of white around the code's own picture."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # zxing-cpp 3 calls write_barcode deprecated
        code = np.array(zxingcpp.write_barcode(zxingcpp.BarcodeFormat.EAN13, EAN, 400, 120))
    code = cv2.copyMakeBorder(code, 60, 60, 60, 60, cv2.BORDER_CONSTANT, value=255)
    return cv2.cvtColor(code, cv2.COLOR_GRAY2BGR)


def _turned(angle: float = 45.0):
    """The label turned ``angle`` degrees clockwise in the middle of a white 900 x 900 picture.
    Returns (picture, the 3 x 3 matrix from label coordinates to the picture)."""
    label = _label()
    canvas = np.full((900, 900, 3), 255, np.uint8)
    y, x = (900 - label.shape[0]) // 2, (900 - label.shape[1]) // 2
    canvas[y:y + label.shape[0], x:x + label.shape[1]] = label
    turn = np.vstack([cv2.getRotationMatrix2D((450, 450), -angle, 1.0), [0, 0, 1]])
    picture = cv2.warpAffine(canvas, turn[:2], (900, 900), borderValue=(255, 255, 255))
    return picture, turn @ np.array([[1, 0, x], [0, 1, y], [0, 0, 1]], np.float64)


def _upright_qr() -> np.ndarray:
    code = cv2.resize(cv2.QRCodeEncoder.create().encode("PROD-0042"), None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
    code = cv2.copyMakeBorder(code, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    return cv2.cvtColor(code, cv2.COLOR_GRAY2BGR)


def _centre(polygon):
    return np.mean(np.asarray(polygon, np.float64), axis=0)


def _bars_centre_on_label():
    """The centre of the barcode's outline as the reader gives it on the upright label."""
    p = zxingcpp.read_barcodes(_label())[0].position
    return _centre([[c.x, c.y] for c in (p.top_left, p.top_right, p.bottom_right, p.bottom_left)])


def test_turned_1d_code_reads_only_with_rotate():
    picture, to_picture = _turned(45)
    engine = QREngine()
    assert engine.decode(picture).codes == []
    result = engine.decode(picture, processing=_on(rotate={"angle": -45}))
    assert [c.data for c in result.codes] == [EAN]
    assert engine.last_read.read_by == "processed"
    # The outline is mapped back onto the code in the camera picture.
    expected = map_points([_bars_centre_on_label()], to_picture)[0]
    assert np.allclose(_centre(result.codes[0].polygon), expected, atol=3)


def test_perspective_reads_a_tilted_code():
    turned, to_turned = _turned(45)
    trapezoid = cv2.getPerspectiveTransform(np.float32([[0, 0], [900, 0], [900, 900], [0, 900]]),
                                            np.float32([[150, 0], [750, 0], [900, 900], [0, 900]]))
    picture = cv2.warpPerspective(turned, trapezoid, (900, 900), borderValue=(255, 255, 255))
    engine = QREngine()
    assert engine.decode(picture).codes == []
    lh, lw = _label().shape[:2]
    label_corners = [[0, 0], [lw, 0], [lw, lh], [0, lh]]
    corners =[[x / 900, y / 900] for x, y in map_points(label_corners, trapezoid.astype(np.float64) @ to_turned)]
    result = engine.decode(picture, processing=_on(perspective={"corners": corners}))
    assert [c.data for c in result.codes] == [EAN]
    assert engine.last_read.read_by == "processed"


def test_ruined_processing_falls_back_to_the_original():
    engine = QREngine()
    result = engine.decode(_upright_qr(), processing=_on(binarize={"method": "fixed", "level": 255}))
    assert [c.data for c in result.codes] == ["PROD-0042"]
    assert engine.last_read.read_by == "original"
    assert engine.last_read.processed is not None


def test_straightened_stage(monkeypatch):
    """A code the processed picture only finds (with an error) is read from its straightened crop."""
    picture = np.random.default_rng(3).integers(0, 256, (300, 400, 3), dtype=np.uint8)
    error_outline = [[100, 100], [200, 110], [195, 160], [95, 150]]
    calls = []

    def fake_read(self, img, w, h, only, errors=None):
        calls.append(img.shape)
        if errors is not None:
            errors.append(error_outline)
            return []
        if len(calls) == 2:      # the straightened crop
            return [self._item("EAN_13", EAN, [[10, 10], [50, 10], [50, 50], [10, 50]], w, h)]
        return []

    monkeypatch.setattr(QREngine, "_read", fake_read)
    engine = QREngine()
    settings = _on(grayscale={})
    result = engine.decode(picture, processing=settings)
    assert engine.last_read.read_by == "straightened"
    grey = cv2.cvtColor(picture, cv2.COLOR_BGR2GRAY)
    crop, crop_matrix = upright(grey, error_outline, settings["crop_margin_px"], min_short=120)
    assert calls[1] == crop.shape
    expected = map_points([[10, 10], [50, 10], [50, 50], [10, 50]], np.linalg.inv(crop_matrix))
    assert np.allclose(result.codes[0].polygon, expected, atol=1)


def test_processing_off_is_todays_path(monkeypatch):
    seen = []
    real = qr_engine.zxingcpp.read_barcodes

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(qr_engine.zxingcpp, "read_barcodes", spy)
    engine = QREngine()
    for processing in (None, normalize_processing({"enabled": False, "grayscale": {"on": True}})):
        result = engine.decode(_upright_qr(), processing=processing)
        assert [c.data for c in result.codes] == ["PROD-0042"]
        assert engine.last_read.read_by == "original"
        assert engine.last_read.processed is None
    assert seen and not any(kwargs.get("return_errors") for kwargs in seen)


def test_code_type_applies_to_processed_reads():
    picture, _ = _turned(45)
    engine = QREngine()
    assert engine.decode(picture, code_type="2d", processing=_on(rotate={"angle": -45})).codes == []


def test_nothing_read_says_none():
    engine = QREngine()
    engine.decode(np.full((120, 160, 3), 255, np.uint8), processing=_on(grayscale={}))
    assert engine.last_read.read_by == "none"
    assert engine.last_read.processed.shape == (120, 160)
