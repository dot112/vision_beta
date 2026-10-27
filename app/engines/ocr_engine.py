"""The text reader's OCR engine: RapidOCR (PaddleOCR's PP-OCRv6 models) on ONNX Runtime.

One engine per (model, device) is shared by every text reader (engine_for), one call at a time:
RapidOCR keeps state between calls (its switches, the character limit set here). On DirectML the
calls hold the same lock as YOLO's (two DirectML sessions running at once end the process).

Measured on 2026-10-27 (docs/superpowers/specs/2026-10-27-ocr-text-reader-design.md): RapidOCR's
default detection scales every picture up to 736 px on its short side; ``limit_type: max`` with
640 is three to four times faster on a CPU with no loss on printed text. Clean dot-matrix print is
not found at all until its dots are joined (join_dots).
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import cv2
import numpy as np

from app.config import settings
from app.engines import ocr_models
from app.utils.logger import get_logger

logger = get_logger(__name__)

DET_LIMIT_SIDE = 640
# The engine keeps lines down to this score; each camera drops the ones under its own lowest confidence.
ENGINE_MIN_SCORE = 0.30
_GPU_WORDS = ("auto", "gpu")


class OcrLine(NamedTuple):
    """One line of text read: its text, confidence, and 4 corners in pixels of the picture given."""
    text: str
    confidence: float
    box: List[List[int]]


class OcrUnavailable(RuntimeError):
    """The engine cannot read: its model is not on disk, or would not load. The message says why."""


def _device_kind(device: str) -> str:
    """"cpu", "dml" or "cuda": the same choice as InferenceEngine makes for these two GPUs."""
    dev = (device or "cpu").strip().lower()
    if dev == "cpu":
        return "cpu"
    try:
        import onnxruntime as ort
        available = ort.get_available_providers()
    except Exception:  # noqa: BLE001 - no ONNX Runtime: nothing to choose
        return "cpu"
    base = dev.split(":", 1)[0]
    if "DmlExecutionProvider" in available and base in _GPU_WORDS + ("dml", "directml"):
        return "dml"
    if "CUDAExecutionProvider" in available and base in _GPU_WORDS + ("cuda",):
        return "cuda"
    return "cpu"


def _build(model: str, kind: str) -> Any:
    """The RapidOCR object for a model on a device kind, from the model's files (never a download)."""
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import ModelType, OCRVersion

    files = ocr_models.model_files(model)
    if files is None:
        raise OcrUnavailable(f"the text reader model '{model}' is not on this server; {ocr_models.FETCH_HINT}")
    det, rec = files
    params: Dict[str, Any] = {
        "Global.log_level": "error",
        "Global.text_score": ENGINE_MIN_SCORE,
        "Det.limit_type": "max",
        "Det.limit_side_len": DET_LIMIT_SIDE,
        "Det.model_path": str(det),
        "Rec.model_path": str(rec),
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Rec.ocr_version": OCRVersion.PPOCRV6,
        "Det.model_type": ModelType(model),
        "Rec.model_type": ModelType(model),
    }
    if kind == "dml":
        params["EngineConfig.onnxruntime.use_dml"] = True
    elif kind == "cuda":
        params["EngineConfig.onnxruntime.use_cuda"] = True
    return RapidOCR(params=params)


def join_dots(img: np.ndarray, size: int, ink: str = "dark") -> np.ndarray:
    """Join the dots of dot-matrix print into strokes: grey, then an opening (dark ink on a light
    surface) or a closing (light ink on a dark one) with a round kernel ``size`` px wide."""
    if not size:
        return img
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    joined = cv2.morphologyEx(grey, cv2.MORPH_CLOSE if ink == "light" else cv2.MORPH_OPEN, kernel)
    return cv2.cvtColor(joined, cv2.COLOR_GRAY2BGR)


class OcrEngine:
    """One model on one device. Loads on first use; ``ready`` and ``error`` say how that went."""

    def __init__(self, model: str, device: Optional[str] = None) -> None:
        from app.engines import inference_engine

        self.model = model
        self.kind = _device_kind(device if device is not None else settings.INFERENCE_DEVICE)
        self._lock = inference_engine._DML_LOCK if self.kind == "dml" else threading.Lock()
        self._load_lock = threading.Lock()
        self._loaded = False
        self._rapid: Any = None
        self._decode: Any = None
        self._allowed: Optional[str] = None
        self.error: Optional[str] = None

    @property
    def ready(self) -> bool:
        self._load()
        return self._rapid is not None

    def _load(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            try:
                self._start(self.kind)
            except OcrUnavailable as exc:
                self.error = str(exc)
            except Exception as exc:  # noqa: BLE001 - a GPU that cannot start: the CPU then
                if self.kind == "cpu":
                    self.error = f"the text reader could not start: {type(exc).__name__}: {exc}"
                else:
                    logger.warning("The text reader could not start on %s (%s: %s); it reads on the CPU",
                                   self.kind, type(exc).__name__, exc)
                    self.kind, self._lock = "cpu", threading.Lock()
                    try:
                        self._start("cpu")
                    except Exception as again:  # noqa: BLE001
                        self.error = f"the text reader could not start: {type(again).__name__}: {again}"
            self._loaded = True

    def _start(self, kind: str) -> None:
        with self._lock:
            rapid = _build(self.model, kind)
            blank = np.full((64, 320, 3), 255, np.uint8)
            # RapidOCR makes its sessions on first use: run both once (a GPU that fails, fails here).
            rapid(blank, use_det=True, use_cls=False, use_rec=True)
            rapid(blank, use_det=False, use_cls=False, use_rec=True)
            self._decode = rapid.text_rec.postprocess_op
            rapid.text_rec.postprocess_op = self._limited_decode
            self._rapid = rapid

    def _limited_decode(self, preds: np.ndarray, return_word_box: bool = False, **kwargs: Any) -> Any:
        """The recognizer's decoder, with every character not in ``_allowed`` taken out (the CTC blank,
        index 0, always stays): a dot-matrix misread then cannot become a character no format allows."""
        if self._allowed is not None:
            keep = np.array([i == 0 or ch in self._allowed for i, ch in enumerate(self._decode.character)])
            preds = np.where(keep[None, None, :], preds, 0.0)
        return self._decode(preds, return_word_box, **kwargs)

    def read(self, img: np.ndarray, *, upside_down: bool = False, allowed: Optional[str] = None) -> List[OcrLine]:
        """The lines of text in ``img``. ``upside_down`` also reads text turned 180 degrees (slower);
        ``allowed``: the only characters the lines may hold (None: any). Raises OcrUnavailable."""
        self._load()
        if self._rapid is None:
            raise OcrUnavailable(self.error or "the text reader is not ready")
        with self._lock:
            self._allowed = allowed
            try:
                # Every switch on every call: RapidOCR keeps a switch a call set for the calls after it.
                out = self._rapid(img, use_det=True, use_cls=bool(upside_down), use_rec=True)
            finally:
                self._allowed = None
        boxes = getattr(out, "boxes", None)
        if boxes is None:
            return []
        return [
            OcrLine(str(text), float(score), [[int(round(x)), int(round(y))] for x, y in box])
            for box, text, score in zip(boxes, out.txts or (), out.scores or ())
        ]


_engines: Dict[Tuple[str, str], OcrEngine] = {}
_engines_lock = threading.Lock()


def engine_for(model: str) -> OcrEngine:
    """The shared engine of a model on the server's INFERENCE_DEVICE (made on first ask, loaded on first use)."""
    key = (model, settings.INFERENCE_DEVICE)
    with _engines_lock:
        engine = _engines.get(key)
        if engine is None:
            engine = _engines[key] = OcrEngine(model)
        return engine
