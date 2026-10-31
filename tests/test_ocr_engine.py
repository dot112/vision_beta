"""The text reader's OCR engine (RapidOCR, PP-OCRv6 on ONNX Runtime) and its models.

The engines here run the real *small* model on the CPU, on pictures drawn the way the probe of
2026-10-27 drew them: two printed lines on a light carton side, or the same as inkjet-like dots.
"""
from __future__ import annotations

import threading

import cv2
import numpy as np
import pytest

from app.config import settings
from app.engines import ocr_engine, ocr_models
from app.engines.ocr_engine import OcrEngine, OcrUnavailable, engine_for, join_dots

BACKGROUND = (170, 190, 205)
INK = (30, 30, 35)


def _ink_mask(lines, w=1280, h=720):
    ink = np.zeros((h, w), np.uint8)
    for i, text in enumerate(lines):
        cv2.putText(ink, text, (340, 320 + i * 110), cv2.FONT_HERSHEY_SIMPLEX, 2.0, 255, 9, cv2.LINE_AA)
    return ink


def _picture(lines):
    img = np.full((720, 1280, 3), BACKGROUND, np.uint8)
    img[_ink_mask(lines) > 128] = INK
    return img


def _dot_picture(text):
    """Dots 6 px wide, 9 px apart, where the stroke is: an inkjet coder's print."""
    stroke = _ink_mask([text])
    dots = np.zeros_like(stroke)
    for y in range(0, stroke.shape[0], 9):
        for x in range(0, stroke.shape[1], 9):
            if stroke[y, x] > 128:
                cv2.circle(dots, (x, y), 3, 255, -1)
    img = np.full((720, 1280, 3), BACKGROUND, np.uint8)
    img[dots > 128] = INK
    return img


def _parts(img, dark=True):
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = grey < 100 if dark else grey > 155
    return cv2.connectedComponents(mask.astype("uint8"))[0]


def test_small_is_always_there_and_the_others_come_from_the_model_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OCR_MODEL_DIR", str(tmp_path))
    assert ocr_models.available() == ["small"] and ocr_models.new_reader_model() == "small"
    (tmp_path / "PP-OCRv6_det_tiny.onnx").write_bytes(b"x")
    assert ocr_models.available() == ["small"]                     # its recognizer is missing
    (tmp_path / "PP-OCRv6_rec_tiny.onnx").write_bytes(b"x")
    assert ocr_models.available() == ["tiny", "small"] and ocr_models.new_reader_model() == "tiny"


def test_the_fetch_command_runs_without_the_servers_settings(tmp_path):
    """The Docker build fetches the models with no .env and no SECRET_KEY: the command must not need them."""
    import os
    import subprocess
    import sys

    from pathlib import Path

    env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
    env["OCR_MODEL_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])    # run from tmp_path: no .env there
    run = subprocess.run([sys.executable, "-m", "app.engines.ocr_models"], capture_output=True, text=True,
                         env=env, cwd=tmp_path, timeout=120)
    assert run.returncode == 0, run.stderr[-800:]
    assert "small   on disk" in run.stdout and "tiny    not on disk" in run.stdout


def test_the_fetch_reads_text_so_recognition_fetches_its_model_too(tmp_path, monkeypatch):
    """RapidOCR fetches a recognition model the first time it recognises a line: a blank picture fetched
    only the detection models (found on a real fetch, 2026-10-31)."""
    import sys
    import types

    from app.engines import ocr_models

    class FakeRapidOCR:
        def __init__(self, params):
            self.name = params["Det.model_type"]
            (tmp_path / f"PP-OCRv6_det_{self.name}.onnx").write_bytes(b"x")

        def __call__(self, img, **switches):
            if (img < 128).any():                       # a line found: recognised, its model fetched
                (tmp_path / f"PP-OCRv6_rec_{self.name}.onnx").write_bytes(b"x")

    package = types.ModuleType("rapidocr")
    package.RapidOCR = FakeRapidOCR
    typings = types.ModuleType("rapidocr.utils.typings")
    typings.ModelType = str
    typings.OCRVersion = types.SimpleNamespace(PPOCRV6="PP-OCRv6")
    monkeypatch.setitem(sys.modules, "rapidocr", package)
    monkeypatch.setitem(sys.modules, "rapidocr.utils", types.ModuleType("rapidocr.utils"))
    monkeypatch.setitem(sys.modules, "rapidocr.utils.typings", typings)
    monkeypatch.setattr(ocr_models, "model_dir", lambda: tmp_path)
    assert ocr_models.fetch(["tiny", "medium"]) == ["tiny", "medium"]


def test_a_download_cut_short_is_not_left_on_disk(tmp_path, monkeypatch, capsys):
    """A real fetch timed out 14 MB into the 77 MB medium model and still said "on disk" (2026-10-31): a file
    whose SHA-256 is not the package's is removed, every model is tried, and the command fails."""
    import hashlib
    import sys
    import types

    from app.engines import ocr_models

    good = b"model"
    tried = []

    class FakeRapidOCR:
        def __init__(self, params):
            self.name = params["Det.model_type"]
            tried.append(self.name)
            (tmp_path / f"PP-OCRv6_det_{self.name}.onnx").write_bytes(good)

        def __call__(self, img, **switches):
            if self.name == "medium":
                (tmp_path / "PP-OCRv6_rec_medium.onnx").write_bytes(b"mod")      # cut short
                raise ConnectionError("Read timed out.")
            (tmp_path / f"PP-OCRv6_rec_{self.name}.onnx").write_bytes(good)

    package = types.ModuleType("rapidocr")
    package.RapidOCR = FakeRapidOCR
    typings = types.ModuleType("rapidocr.utils.typings")
    typings.ModelType = str
    typings.OCRVersion = types.SimpleNamespace(PPOCRV6="PP-OCRv6")
    monkeypatch.setitem(sys.modules, "rapidocr", package)
    monkeypatch.setitem(sys.modules, "rapidocr.utils", types.ModuleType("rapidocr.utils"))
    monkeypatch.setitem(sys.modules, "rapidocr.utils.typings", typings)
    monkeypatch.setattr(ocr_models, "model_dir", lambda: tmp_path)
    sha = hashlib.sha256(good).hexdigest()
    monkeypatch.setattr(ocr_models, "_expected_sha256", lambda: {
        f"PP-OCRv6_{kind}_{name}.onnx": sha for kind in ("det", "rec") for name in ("tiny", "medium")})

    assert ocr_models.main(["--fetch"]) == 1
    out = capsys.readouterr().out
    assert "Fetching failed" in out and "medium: ConnectionError: Read timed out." in out
    assert "medium  not on disk" in out and "tiny    on disk" in out
    assert not (tmp_path / "PP-OCRv6_rec_medium.onnx").exists() and (tmp_path / "PP-OCRv6_det_medium.onnx").exists()
    assert tried == ["tiny", "medium"]
    # Verified models are not fetched again; the medium one is.
    tried.clear()
    ocr_models.main(["--fetch"])
    assert tried == ["medium"]


def test_reads_printed_text_with_boxes_in_picture_pixels():
    lines = OcrEngine("small", "cpu").read(_picture(["EXP 12/03/2027", "LOT A12345"]))
    assert sorted(line.text for line in lines) == ["EXP 12/03/2027", "LOT A12345"]
    lot = next(line for line in lines if line.text.startswith("LOT"))
    xs, ys = [p[0] for p in lot.box], [p[1] for p in lot.box]
    assert 290 < min(xs) < 380 and 350 < min(ys) < 440 and len(lot.box) == 4
    assert all(isinstance(v, int) for point in lot.box for v in point) and 0.5 < lot.confidence <= 1.0


def test_the_character_limit_keeps_only_allowed_characters():
    lines = OcrEngine("small", "cpu").read(_picture(["LOT 12345"]), allowed=" 0123456789")
    assert lines and all(set(line.text) <= set(" 0123456789") for line in lines)


def test_every_call_passes_its_switches():
    engine = OcrEngine("small", "cpu")
    assert engine.ready
    calls = []
    real = engine._rapid
    engine._rapid = lambda img, **kw: calls.append(kw) or real(img, **kw)
    engine.read(_picture(["LOT 1"]), upside_down=True)
    engine.read(_picture(["LOT 1"]))
    assert [(c["use_det"], c["use_cls"], c["use_rec"]) for c in calls] == [(True, True, True), (True, False, True)]


def test_a_missing_model_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OCR_MODEL_DIR", str(tmp_path))
    engine = OcrEngine("tiny", "cpu")
    assert engine.ready is False and "python -m app.engines.ocr_models --fetch" in engine.error
    with pytest.raises(OcrUnavailable):
        engine.read(_picture(["X"]))


def test_join_dots_joins_the_dots_of_a_stroke():
    dots = _dot_picture("EXP 12")
    before = _parts(dots)
    joined = join_dots(dots, 7)
    assert _parts(joined) * 2 < before and joined.shape == dots.shape     # 39 parts -> 12, measured
    assert join_dots(dots, 0) is dots
    light = 255 - dots                           # light print on a dark surface: closing
    assert _parts(join_dots(light, 7, "light"), dark=False) * 2 < _parts(light, dark=False)


def test_calls_take_turns_and_dml_uses_the_yolo_lock(monkeypatch):
    from app.engines import inference_engine

    engine = OcrEngine("small", "cpu")
    assert engine.ready and engine._lock is not inference_engine._DML_LOCK
    monkeypatch.setattr(ocr_engine, "_device_kind", lambda device: "dml")
    assert OcrEngine("small", "auto")._lock is inference_engine._DML_LOCK
    results = []
    threads = [threading.Thread(target=lambda: results.append(engine.read(_picture(["LOT A12345"])))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [[line.text for line in r] for r in results] == [["LOT A12345"], ["LOT A12345"]]


def test_a_gpu_that_cannot_start_falls_back_to_the_cpu(monkeypatch):
    real = ocr_engine._build
    monkeypatch.setattr(ocr_engine, "_device_kind", lambda device: "cuda")
    monkeypatch.setattr(ocr_engine, "_build", lambda model, kind: real(model, kind) if kind == "cpu" else 1 / 0)
    engine = OcrEngine("small", "auto")
    assert engine.ready and engine.kind == "cpu"


def test_engine_for_shares_one_engine_per_model():
    assert engine_for("small") is engine_for("small")
    assert engine_for("small") is not engine_for("tiny")
