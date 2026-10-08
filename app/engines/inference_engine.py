from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from app.schemas.vision import BoundingBox, DetectionItem, DetectionResponse
from app.utils.logger import get_logger
from app.engines.tracker import exit_edge, exit_zone, is_horizontal_movement, is_in_exit_zone

logger = get_logger(__name__)

try:
    import onnxruntime as ort
except ImportError:
    ort = None


def letterbox(
    img: np.ndarray,
    new_shape: Tuple[int, int] = (640, 640),
    color: Tuple[int, int, int] = (114, 114, 114),
) -> Tuple[np.ndarray, float, Tuple[float, float]]:
    shape = img.shape[:2]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)

    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]

    dw /= 2
    dh /= 2

    if shape[::-1] != new_unpad:
        img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    return img, r, (dw, dh)


def _detect_best_dml_adapter() -> Tuple[int, str]:
    """
    On Windows, queries DXGI hardware adapters to select the discrete GPU with
    the highest dedicated video memory (e.g. NVIDIA GeForce GTX 1650 with ~4GB VRAM)
    over integrated CPU graphics (Intel UHD with ~128MB VRAM).
    Returns (device_id, adapter_name).
    """
    if sys.platform != "win32":
        return 0, "DirectML Default GPU"
    try:
        import ctypes
        from ctypes import wintypes

        class DXGI_ADAPTER_DESC(ctypes.Structure):
            _fields_ = [
                ("Description", wintypes.WCHAR * 128),
                ("VendorId", wintypes.UINT),
                ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT),
                ("Revision", wintypes.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuidLowPart", wintypes.DWORD),
                ("AdapterLuidHighPart", wintypes.LONG),
            ]

        dxgi = ctypes.windll.dxgi
        factory = ctypes.c_void_p()
        IID_IDXGIFactory1 = (ctypes.c_byte * 16)(
            0x78, 0xAE, 0x0A, 0x77, 0x6F, 0xF2, 0xBA, 0x4D, 0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87
        )
        if dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory)) == 0:
            vtable = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            enum_func = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(vtable[7])
            best_id = 0
            best_vram = -1
            best_name = "Default DirectML GPU"
            for i in range(8):
                ad = ctypes.c_void_p()
                if enum_func(factory, i, ctypes.byref(ad)) != 0:
                    break
                avtable = ctypes.cast(ad, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                desc_func = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(DXGI_ADAPTER_DESC))(avtable[8])
                desc = DXGI_ADAPTER_DESC()
                desc_func(ad, ctypes.byref(desc))
                name = str(desc.Description).strip()
                vram = desc.DedicatedVideoMemory
                # Discard basic software renderers
                if "basic render" not in name.lower() and vram > best_vram:
                    best_vram = vram
                    best_id = i
                    best_name = f"{name} ({vram // (1024 * 1024)} MB VRAM)"
                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(avtable[2])(ad)
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(factory)
            return best_id, best_name
    except Exception as e:
        logger.debug("DXGI adapter query error: %s", e)
    return 0, "DirectML Default GPU"


def _intra_op_threads(cpu_only: bool) -> int:
    """
    ONNX Runtime intra-op thread count.
    A GPU does the heavy work, so 2 CPU threads are enough there. On CPU-only
    machines 2 threads leaves most cores idle, so use about one per physical core
    while leaving the rest for camera decoding and JPEG encoding.
    INFERENCE_THREADS > 0 overrides both.
    """
    cpus = os.cpu_count() or 1
    try:
        from app.config import settings
        configured = int(getattr(settings, "INFERENCE_THREADS", 0) or 0)
    except Exception:
        configured = 0
    if configured > 0:
        return min(configured, cpus)
    if cpu_only:
        return max(min(2, cpus), min(8, cpus // 2))
    return min(2, cpus)


def _blend_filled_rect(
    img: np.ndarray,
    pt1: Tuple[int, int],
    pt2: Tuple[int, int],
    color: Tuple[int, int, int],
    alpha: float,
    beta: float,
) -> None:
    """
    In-place equivalent of drawing a filled rectangle on a full-frame copy and
    cv2.addWeighted(copy, alpha, img, beta, 0, img), but touches only the
    rectangle: outside it the full-frame blend leaves pixels unchanged anyway.
    """
    h, w = img.shape[:2]
    # cv2.rectangle includes both corner pixels and clips to the image.
    x1 = max(0, min(pt1[0], pt2[0]))
    y1 = max(0, min(pt1[1], pt2[1]))
    x2 = min(w - 1, max(pt1[0], pt2[0]))
    y2 = min(h - 1, max(pt1[1], pt2[1]))
    if x2 < x1 or y2 < y1:
        return
    roi = img[y1:y2 + 1, x1:x2 + 1]
    overlay = np.empty_like(roi)
    overlay[:] = color
    roi[:] = cv2.addWeighted(overlay, alpha, roi, beta, 0)


# Count line colours (BGR): A, where products enter, and B, where they are counted.
COUNT_LINE_A_COLOR = (255, 200, 40)    # azure
COUNT_LINE_B_COLOR = (40, 190, 255)    # amber
# How long line B flashes after a product is counted.
COUNT_FLASH_SECONDS = 0.6


def _label_chip(img: np.ndarray, text: str, center: Tuple[int, int], color: Tuple[int, int, int], scale: float) -> None:
    """A rounded label in the line's colour with dark text, centred on ``center`` and kept inside the frame."""
    h, w = img.shape[:2]
    font_scale = 0.48 * scale
    thick = max(1, int(round(1.4 * scale)))
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thick)
    pad_x, pad_y = int(9 * scale), int(5 * scale)
    cw, ch = tw + 2 * pad_x, th + base + 2 * pad_y
    x1 = int(min(max(center[0] - cw // 2, 2), w - cw - 2))
    y1 = int(min(max(center[1] - ch // 2, 2), h - ch - 2))
    r = ch // 2
    # Soft shadow, then the pill.
    for dx, col in ((2, (15, 15, 15)), (0, color)):
        oy = dx
        cv2.rectangle(img, (x1 + r, y1 + oy), (x1 + cw - r, y1 + ch + oy), col, -1, cv2.LINE_AA)
        cv2.circle(img, (x1 + r, y1 + r + oy), r, col, -1, cv2.LINE_AA)
        cv2.circle(img, (x1 + cw - r, y1 + r + oy), r, col, -1, cv2.LINE_AA)
    cv2.putText(img, text, (x1 + pad_x, y1 + pad_y + th), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (24, 24, 24), thick, cv2.LINE_AA)


def _draw_count_lines(
    img: np.ndarray,
    vertical_lines: bool,
    pos_a: float,
    pos_b: float,
    top: int = 0,
    flash: float = 0.0,
) -> None:
    """Count lines A (entry) and B (count): a soft glow and a crisp core each,
    a label on the line, a faint zone between them with arrows in the flow
    direction (A towards B), and line B flashing for a moment (``flash`` 1 to 0)
    after a product is counted. ``top`` keeps labels clear of the HUD bar."""
    h, w = img.shape[:2]
    scale = max(0.6, min(2.0, max(w, h) / 1280.0))
    span = w if vertical_lines else h
    a = int(min(max(pos_a, 0.0), 1.0) * (span - 1))
    b = int(min(max(pos_b, 0.0), 1.0) * (span - 1))

    def across(c: int, half: int) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        return ((c - half, top), (c + half, h)) if vertical_lines else ((0, c - half), (w, c + half))

    # 1. The zone between the lines, with chevrons pointing from A to B.
    lo, hi = sorted((a, b))
    if hi - lo > 2:
        p1, p2 = ((lo, top), (hi, h)) if vertical_lines else ((0, lo), (w, hi))
        _blend_filled_rect(img, p1, p2, (255, 255, 255), 0.05, 0.95)
        size = int(9 * scale)
        if hi - lo > 4 * size:
            mid = (lo + hi) // 2
            sign = 1 if b >= a else -1
            region = img[top:h, lo:hi + 1] if vertical_lines else img[lo:hi + 1, 0:w]
            overlay = region.copy()
            along = (h - top) if vertical_lines else w
            for frac in (0.18, 0.5, 0.82):
                t = int(frac * along)
                for k in (-1, 1):
                    off = mid - lo + k * size  # two chevrons side by side read as an arrow
                    if vertical_lines:
                        tip = (off + sign * size // 2, t)
                        pts = np.array([(tip[0] - sign * size, t - size), tip, (tip[0] - sign * size, t + size)], np.int32)
                    else:
                        tip = (t, off + sign * size // 2)
                        pts = np.array([(t - size, tip[1] - sign * size), tip, (t + size, tip[1] - sign * size)], np.int32)
                    cv2.polylines(overlay, [pts], False, (235, 235, 235), max(2, int(2 * scale)), cv2.LINE_AA)
            cv2.addWeighted(overlay, 0.45, region, 0.55, 0, region)

    # 2. The lines themselves.
    for coord, color, label, glow in (
        (a, COUNT_LINE_A_COLOR, "A  ENTRY", 0.0),
        (b, COUNT_LINE_B_COLOR, "B  COUNT", max(0.0, min(1.0, flash))),
    ):
        half = int((6 + 8 * glow) * scale)
        p1, p2 = across(coord, half)
        _blend_filled_rect(img, p1, p2, color, 0.18 + 0.35 * glow, 0.82 - 0.35 * glow)
        core = max(2, int(round((2 + 2 * glow) * scale)))
        start, end = ((coord, top), (coord, h - 1)) if vertical_lines else ((0, coord), (w - 1, coord))
        cv2.line(img, start, end, (20, 20, 20), core + 2, cv2.LINE_AA)  # dark edge for bright scenes
        cv2.line(img, start, end, color, core, cv2.LINE_AA)
        chip_at = (coord, top + int(20 * scale)) if vertical_lines else (int(56 * scale), coord)
        _label_chip(img, label, chip_at, color, scale)


# Seconds the current thread spent holding a model's lock since
# begin_model_timing(); the API inference queue uses it to charge requests
# only for model time, not for decoding, drawing or waiting.
_model_timing = threading.local()


def begin_model_timing() -> None:
    _model_timing.seconds = None
    _model_timing.active = True


def end_model_timing() -> Optional[float]:
    """Model seconds since begin_model_timing(), or None if no model ran."""
    _model_timing.active = False
    return getattr(_model_timing, "seconds", None)


def _record_model_time(seconds: float) -> None:
    if getattr(_model_timing, "active", False):
        _model_timing.seconds = (_model_timing.seconds or 0.0) + seconds


# DirectML ends the process (a native crash, nothing to catch) when two of its
# sessions run at the same time on one GPU, or when one is freed while another
# runs. Every engine on DirectML therefore runs and frees its session under
# this one lock. The GPU works through them in turn anyway, so the frame rate
# of several models together stays about the same.
_DML_LOCK = threading.Lock()


class InferenceEngine:
    """
    High-Performance YOLO & ONNX Inference Engine for Machine Vision.
    Optimized with ONNX Runtime (multi-threaded CPU execution provider) + vectorized post-processing,
    with automatic fallback to OpenCV DNN.
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        classes: Optional[List[str]] = None,
        input_size: Tuple[int, int] = (640, 640),
        confidence_threshold: float = 0.30,
        nms_threshold: float = 0.65,
        device: str = "auto",
    ):
        self.model_path = model_path
        self.classes = classes or ["person", "defect", "part_ok"]
        self.input_size = input_size
        self.confidence_threshold = confidence_threshold
        self.nms_threshold = nms_threshold
        self.device = device
        self.task: str = "detect"  # "detect" or "segment"; updated by set_model_mode
        self.ort_session: Optional[Any] = None
        self.input_name: Optional[str] = None
        self.net: Optional[cv2.dnn.Net] = None
        self.is_loaded = False
        self._inference_lock = threading.Lock()

        if model_path and os.path.exists(model_path):
            self.load_model(model_path)

    def load_model(self, model_path: str) -> bool:
        try:
            if not os.path.exists(model_path):
                logger.error("Model file not found: %s", model_path)
                return False

            self.model_path = model_path

            # 1. Prefer ONNX Runtime — auto-select best GPU backend available
            if ort is not None and model_path.lower().endswith(".onnx"):
                try:
                    opts = ort.SessionOptions()
                    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                    opts.inter_op_num_threads = 1
                    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

                    avail = ort.get_available_providers()
                    dev = self.device.lower()
                    providers = ["CPUExecutionProvider"]

                    explicit_dml_id = None
                    if ":" in dev:
                        parts = dev.split(":", 1)
                        if parts[0] in ("dml", "gpu", "directml"):
                            try:
                                explicit_dml_id = int(parts[1])
                            except ValueError:
                                pass

                    if explicit_dml_id is not None and "DmlExecutionProvider" in avail:
                        providers = [("DmlExecutionProvider", {"device_id": explicit_dml_id}), "CPUExecutionProvider"]
                        logger.info("GPU backend: DirectML explicit adapter %d selected", explicit_dml_id)
                    elif dev in ("auto", "gpu", "cuda", "directml", "dml", "tensorrt", "trt"):
                        # Multi-platform Hardware Acceleration Auto-Detection:
                        # 1. TensorRT (Best for NVIDIA Jetson Nano/Orin/Xavier & Linux/Windows CUDA servers)
                        if "TensorrtExecutionProvider" in avail and dev in ("auto", "gpu", "tensorrt", "trt"):
                            providers = ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]
                            logger.info("GPU backend: NVIDIA TensorRT (optimal for Jetson / CUDA)")
                        # 2. CUDA (Standard NVIDIA GPU on Linux/Windows)
                        elif "CUDAExecutionProvider" in avail and dev in ("auto", "gpu", "cuda"):
                            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
                            logger.info("GPU backend: NVIDIA CUDA")
                        # 3. DirectML (Windows DirectX 12 — supports NVIDIA, AMD Radeon, Intel Arc with zero CUDA install)
                        elif "DmlExecutionProvider" in avail and dev in ("auto", "gpu", "directml", "dml"):
                            best_id, adapter_name = _detect_best_dml_adapter()
                            providers = [("DmlExecutionProvider", {"device_id": best_id}), "CPUExecutionProvider"]
                            logger.info("GPU backend: DirectML adapter %d selected [%s]", best_id, adapter_name)
                        # 4. ROCm / MIGraphX (AMD Radeon on Linux)
                        elif "MIGraphXExecutionProvider" in avail:
                            providers = ["MIGraphXExecutionProvider", "CPUExecutionProvider"]
                            logger.info("GPU backend: AMD MIGraphX (ROCm)")
                        elif "ROCMExecutionProvider" in avail:
                            providers = ["ROCMExecutionProvider", "CPUExecutionProvider"]
                            logger.info("GPU backend: AMD ROCm")
                        # 5. CoreML (Apple Silicon macOS)
                        elif "CoreMLExecutionProvider" in avail:
                            providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
                            logger.info("GPU backend: Apple CoreML")
                        else:
                            logger.warning("No GPU provider found in %s — falling back to CPU.", avail)
                    else:
                        logger.info("Inference device: CPU (forced via config)")

                    num_threads = _intra_op_threads(cpu_only=providers == ["CPUExecutionProvider"])
                    opts.intra_op_num_threads = num_threads

                    session = ort.InferenceSession(model_path, opts, providers=providers)

                    # Log which provider is actually running
                    active_prov = (session.get_providers() or ["unknown"])[0]
                    if "Dml" in active_prov:
                        self._inference_lock = _DML_LOCK
                    with self._inference_lock:
                        self.ort_session = session  # frees a session loaded before
                    is_gpu = any(x in active_prov for x in ("Dml", "CUDA", "Tensorrt", "MIGraphX", "ROCM", "CoreML"))
                    logger.info("Active execution provider: %s  [%s]", active_prov, "GPU" if is_gpu else "CPU")
                    self.input_name = self.ort_session.get_inputs()[0].name
                    self.is_loaded = True
                    try:
                        meta = self.ort_session.get_modelmeta().custom_metadata_map
                        if "task" in meta:
                            t = meta["task"].strip().lower()
                            if t in ("detect", "segment"):
                                self.task = t
                        if "names" in meta:
                            import ast
                            names_dict = ast.literal_eval(meta["names"])
                            if names_dict:
                                self.classes = [names_dict[k] for k in sorted(names_dict.keys())]
                    except Exception:
                        pass
                    logger.info("InferenceEngine loaded ONNX Runtime model: %s (%d classes, %d threads, task=%s)", model_path, len(self.classes), num_threads, self.task)
                    return True
                except Exception as ort_err:
                    logger.warning("ONNX Runtime load failed, falling back to OpenCV DNN: %s", ort_err)

            # 2. Fallback to OpenCV DNN
            self.net = cv2.dnn.readNetFromONNX(model_path)
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            self.is_loaded = True
            logger.info("InferenceEngine loaded OpenCV DNN model: %s (%d classes)", model_path, len(self.classes))
            return True

        except Exception as exc:
            logger.error("Failed to load model %s: %s", model_path, exc)
            self.is_loaded = False
            return False

    def close(self) -> None:
        """Free the model. Call it when an engine is dropped: see _DML_LOCK."""
        with self._inference_lock:
            self.is_loaded = False
            self.ort_session = None
            self.net = None

    def predict(
        self,
        image_input: Any,
        conf_threshold: Optional[float] = None,
        nms_threshold: Optional[float] = None,
    ) -> DetectionResponse:
        if isinstance(image_input, bytes):
            nparr = np.frombuffer(image_input, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        elif isinstance(image_input, np.ndarray):
            img = image_input
        else:
            raise ValueError("Unsupported image input type")

        if img is None:
            raise ValueError("Failed to decode image")

        return self.predict_mat(img, conf_threshold=conf_threshold, nms_threshold=nms_threshold)

    def predict_mat(
        self,
        img: np.ndarray,
        conf_threshold: Optional[float] = None,
        nms_threshold: Optional[float] = None,
    ) -> DetectionResponse:
        """
        High-speed inference on uncompressed BGR numpy array with ZERO decode overhead.
        """
        conf_thresh = conf_threshold if conf_threshold is not None else self.confidence_threshold
        nms_thresh = nms_threshold if nms_threshold is not None else self.nms_threshold

        orig_h, orig_w = img.shape[:2]
        start_time = time.perf_counter()
        detections: List[DetectionItem] = []

        if not self.is_loaded or (self.ort_session is None and self.net is None):
            raise RuntimeError("No valid vision model is loaded; inference is disabled")
        try:
                # Resize, then BGR->RGB, HWC->NCHW and 1/255 scaling in one
                # native pass that yields the contiguous float32 tensor ORT needs.
                resized = cv2.resize(img, self.input_size)
                blob = cv2.dnn.blobFromImage(resized, scalefactor=1.0 / 255.0, swapRB=True)

                with self._inference_lock:
                    held_from = time.perf_counter()
                    if self.ort_session is None and self.net is None:
                        raise RuntimeError("the model was unloaded while this frame waited")
                    if self.ort_session is not None:
                        outputs = self.ort_session.run(None, {self.input_name: blob})
                    else:
                        self.net.setInput(blob)
                        outputs = [self.net.forward()]
                    _record_model_time(time.perf_counter() - held_from)

                protos = outputs[1] if len(outputs) > 1 else None
                detections = self._postprocess_fast(outputs[0], orig_w, orig_h, conf_thresh, nms_thresh, protos=protos)
        except Exception as e:
            logger.exception("Model forward pass failed; refusing heuristic inference")
            raise RuntimeError("Vision model inference failed") from e

        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

        return DetectionResponse(
            model_name=Path(self.model_path).stem if self.model_path else "YOLOv8_Industrial_Base",
            total_detections=len(detections),
            detections=detections,
            inference_time_ms=latency_ms,
            image_width=orig_w,
            image_height=orig_h,
        )

    def _postprocess_fast(
        self,
        output: np.ndarray,
        orig_w: int,
        orig_h: int,
        conf_thresh: float,
        nms_thresh: float,
        protos: Optional[np.ndarray] = None,
    ) -> List[DetectionItem]:
        """
        Vectorized YOLO post-processing using NumPy array operations (10x faster),
        with support for dual-output YOLO instance segmentation masks & polygon extraction,
        and full support for end-to-end architectures (YOLO26, YOLOv10, RT-DETR).
        """
        if len(output.shape) == 3:
            output = output[0]
        if output.shape[0] == 0:
            return []

        iw, ih = self.input_size
        detections: List[DetectionItem] = []

        # Check for end2end architectures (shape [num_boxes, 6] or [num_boxes, 38])
        is_end2end = (output.ndim == 2 and output.shape[1] in (6, 38)) or (
            output.ndim == 2 and output.shape[1] <= 40 and output.shape[0] >= 50 and output.shape[0] > output.shape[1]
        )

        if is_end2end:
            has_seg = protos is not None and output.shape[1] >= 38
            confs = output[:, 4]
            mask = confs >= conf_thresh
            if not np.any(mask):
                return []

            filtered_output = output[mask]
            for i in range(len(filtered_output)):
                row = filtered_output[i]
                x1_val, y1_val, x2_val, y2_val = row[:4]
                conf_val = float(row[4])
                cid = int(round(row[5]))
                cname = self.classes[cid] if 0 <= cid < len(self.classes) else f"class_{cid}"

                if max(x2_val, y2_val) <= 1.0:
                    bx1 = int(np.clip(x1_val * orig_w, 0, orig_w))
                    by1 = int(np.clip(y1_val * orig_h, 0, orig_h))
                    bx2 = int(np.clip(x2_val * orig_w, 0, orig_w))
                    by2 = int(np.clip(y2_val * orig_h, 0, orig_h))
                else:
                    bx1 = int(np.clip((x1_val / iw) * orig_w, 0, orig_w))
                    by1 = int(np.clip((y1_val / ih) * orig_h, 0, orig_h))
                    bx2 = int(np.clip((x2_val / iw) * orig_w, 0, orig_w))
                    by2 = int(np.clip((y2_val / ih) * orig_h, 0, orig_h))

                bw = max(0, bx2 - bx1)
                bh = max(0, by2 - by1)
                if bw == 0 or bh == 0:
                    continue

                polygon: Optional[List[List[int]]] = None
                mask_area: Optional[int] = None

                if has_seg and protos is not None:
                    try:
                        coeff = row[6:38]
                        proto_tensor = protos[0] if len(protos.shape) == 4 else protos
                        proto_h, proto_w = proto_tensor.shape[1], proto_tensor.shape[2]
                        mask_raw = np.matmul(coeff, proto_tensor.reshape(32, -1)).reshape(proto_h, proto_w)
                        mask_sigmoid = 1.0 / (1.0 + np.exp(-mask_raw))
                        mask_resized = cv2.resize(mask_sigmoid, (orig_w, orig_h), interpolation=cv2.INTER_LINEAR)

                        cropped = np.zeros((orig_h, orig_w), dtype=np.uint8)
                        cropped[by1:by2, bx1:bx2] = (mask_resized[by1:by2, bx1:bx2] > 0.5).astype(np.uint8)
                        mask_area = int(np.sum(cropped))
                        contours, _ = cv2.findContours(cropped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        if contours:
                            largest = max(contours, key=cv2.contourArea)
                            if len(largest) >= 3:
                                approx = cv2.approxPolyDP(largest, 1.5, True)
                                polygon = [[int(pt[0][0]), int(pt[0][1])] for pt in approx]
                    except Exception as seg_err:
                        logger.debug("Segmentation mask extraction error: %s", seg_err)

                detections.append(
                    DetectionItem(
                        class_id=cid,
                        class_name=cname,
                        confidence=round(conf_val, 4),
                        bbox=BoundingBox(
                            x1=bx1,
                            y1=by1,
                            x2=bx2,
                            y2=by2,
                            width=bw,
                            height=bh,
                        ),
                        polygon=polygon,
                        mask_area=mask_area,
                    )
                )
            return detections

        num_classes = len(self.classes)
        if output.shape[0] in (4 + num_classes, 4 + num_classes + 32, 84, 116, 5, 37) or output.shape[0] < output.shape[1]:
            output = output.T  # Shape: (num_boxes, 4 + num_classes + [32 mask_protos])

        if output.shape[0] == 0:
            return []

        # Check if output contains mask coefficients (YOLO-seg has 4 bbox + num_classes + 32 mask coefficients)
        has_seg = protos is not None and (output.shape[1] >= 4 + num_classes + 32)

        boxes_raw = output[:, :4]
        scores_matrix = output[:, 4:4 + num_classes] if has_seg else output[:, 4:]
        mask_coeffs_matrix = output[:, 4 + num_classes:4 + num_classes + 32] if has_seg else None

        class_ids = np.argmax(scores_matrix, axis=1)
        confidences = scores_matrix[np.arange(len(scores_matrix)), class_ids]

        mask = confidences >= conf_thresh
        if not np.any(mask):
            return []

        filtered_boxes = boxes_raw[mask]
        filtered_confs = confidences[mask]
        filtered_cids = class_ids[mask]
        filtered_coeffs = mask_coeffs_matrix[mask] if has_seg and mask_coeffs_matrix is not None else None

        iw, ih = self.input_size

        # Vectorized coordinate scaling
        cx = filtered_boxes[:, 0]
        cy = filtered_boxes[:, 1]
        w = filtered_boxes[:, 2]
        h = filtered_boxes[:, 3]

        if np.max(cx) <= 1.0 and np.max(w) <= 1.0:
            cx = cx * orig_w
            cy = cy * orig_h
            w = w * orig_w
            h = h * orig_h
        else:
            cx = (cx / iw) * orig_w
            cy = (cy / ih) * orig_h
            w = (w / iw) * orig_w
            h = (h / ih) * orig_h

        x1 = np.clip(cx - w / 2, 0, orig_w).astype(int)
        y1 = np.clip(cy - h / 2, 0, orig_h).astype(int)
        box_w = np.clip(w, 0, orig_w).astype(int)
        box_h = np.clip(h, 0, orig_h).astype(int)

        boxes_list = [
            [int(x1[i]), int(y1[i]), int(box_w[i]), int(box_h[i])]
            for i in range(len(x1))
        ]
        confs_list = [float(c) for c in filtered_confs]

        indices = cv2.dnn.NMSBoxes(boxes_list, confs_list, conf_thresh, nms_thresh)
        detections: List[DetectionItem] = []

        if len(indices) > 0:
            for idx in indices.flatten():
                bx, by, bw, bh = boxes_list[idx]
                cid = int(filtered_cids[idx])
                cname = self.classes[cid] if cid < len(self.classes) else f"class_{cid}"

                x2 = min(orig_w, bx + bw)
                y2 = min(orig_h, by + bh)

                polygon: Optional[List[List[int]]] = None
                mask_area: Optional[int] = None

                if has_seg and filtered_coeffs is not None and protos is not None:
                    try:
                        proto_tensor = protos[0] if len(protos.shape) == 4 else protos
                        coeff = filtered_coeffs[idx]
                        proto_h, proto_w = proto_tensor.shape[1], proto_tensor.shape[2]
                        mask_raw = np.matmul(coeff, proto_tensor.reshape(32, -1)).reshape(proto_h, proto_w)
                        mask_sigmoid = 1.0 / (1.0 + np.exp(-mask_raw))
                        mask_resized = cv2.resize(mask_sigmoid, (orig_w, orig_h), interpolation=cv2.INTER_LINEAR)

                        cropped = np.zeros((orig_h, orig_w), dtype=np.uint8)
                        cropped[by:y2, bx:x2] = (mask_resized[by:y2, bx:x2] > 0.5).astype(np.uint8)
                        mask_area = int(np.sum(cropped))
                        contours, _ = cv2.findContours(cropped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        if contours:
                            largest = max(contours, key=cv2.contourArea)
                            if len(largest) >= 3:
                                approx = cv2.approxPolyDP(largest, 1.5, True)
                                polygon = [[int(pt[0][0]), int(pt[0][1])] for pt in approx]
                    except Exception as seg_err:
                        logger.debug("Segmentation mask extraction error: %s", seg_err)

                detections.append(
                    DetectionItem(
                        class_id=cid,
                        class_name=cname,
                        confidence=round(confs_list[idx], 4),
                        bbox=BoundingBox(
                            x1=bx,
                            y1=by,
                            x2=x2,
                            y2=y2,
                            width=max(0, x2 - bx),
                            height=max(0, y2 - by),
                        ),
                        polygon=polygon,
                        mask_area=mask_area,
                    )
                )

        return detections

    def draw_annotations_mat(
        self,
        img: np.ndarray,
        detections: List[DetectionItem],
        latency_ms: float = 0.0,
        draw_wirelines: bool = True,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        fps: float = 0.0,
        camera_rotation: Optional[int] = None,
        camera_flip_h: Optional[bool] = None,
        camera_flip_v: Optional[bool] = None,
        camera_id: Optional[str] = None,
    ) -> np.ndarray:
        """
        Directly overlays wirelines, tracking trails, HUD, and bounding boxes onto BGR array.
        Zero JPEG encode/decode overhead!
        camera_id selects that camera's tracker; without it the default tracker is used.
        camera_rotation and the flips are accepted for older callers and not used:
        the exit band is drawn where the camera's products flow out (exit_edge).
        """
        h, w = img.shape[:2]

        # Build allowed class filter from counting config (expected + defect classes)
        from app.services.line_service import line_manager
        counting_service = line_manager.counter_for_camera(camera_id)
        try:
            flow = counting_service.config
            exit_side = exit_edge(flow.orientation, flow.direction, flow.line1_position, flow.line2_position)
        except Exception:
            exit_side = exit_edge("horizontal")
        exit_rect = exit_zone(w, h, exit_side)
        try:
            _cfg = counting_service.config
            _exp = set(c.lower() for c in (_cfg.expected_classes or []))
            _def = set(c.lower() for c in (_cfg.defect_classes or []))
            _allowed = _exp | _def  # empty means draw all
        except Exception:
            _allowed = set()

        # 1. Wirelines & Object Counting HUD
        if draw_wirelines:
            try:
                cfg = counting_service.config
                total_insp = counting_service.total_inspected
                good_c = counting_service.good_count
                rej_c = counting_service.rejected_count
                ppm_val = counting_service.products_per_minute

                # Count lines A and B (vertical lines when products move sideways).
                since_count = time.time() - counting_service.last_count_at
                _draw_count_lines(
                    img,
                    is_horizontal_movement(cfg.orientation),
                    float(cfg.line1_position),
                    float(cfg.line2_position),
                    top=32,  # below the HUD bar
                    flash=1.0 - since_count / COUNT_FLASH_SECONDS if since_count < COUNT_FLASH_SECONDS else 0.0,
                )

                # Target markers — only for CONFIRMED tracks (no trail, no ghost dots)
                for obj in list(counting_service.get_tracker(camera_id).objects.values()):
                    if _allowed and obj.class_name.lower() not in _allowed:
                        continue
                    # Skip unconfirmed tracks to avoid ghost dots from newly-spawned tracks
                    if not getattr(obj, "confirmed", True):
                        continue

                    # Compute scaled smoothed center
                    if hasattr(obj, "smooth_center_x"):
                        sc_x = int(obj.smooth_center_x * scale_x)
                        sc_y = int(obj.smooth_center_y * scale_y)
                    else:
                        curr_c = obj.current_centroid
                        sc_x = int(curr_c[0] * scale_x)
                        sc_y = int(curr_c[1] * scale_y)

                    # Skip drawing if center reached the exit line zone
                    if is_in_exit_zone(sc_x, sc_y, exit_side, exit_rect):
                        continue

                    # Simple solid green dot (6px radius, no tail/trail)
                    cv2.circle(img, (sc_x, sc_y), 6, (0, 255, 0), -1)

                # HUD Banner alongside counters rendered in the video
                hud_parts = [
                    f"TOTAL: {total_insp}",
                    f"GOOD: {good_c}",
                    f"REJ: {rej_c}",
                    f"PPM: {ppm_val:.1f}",
                ]
                if fps > 0:
                    hud_parts.append(f"FPS: {fps:.1f}")
                if latency_ms > 0:
                    hud_parts.append(f"YOLO: {latency_ms:.0f}ms")

                hud_text = " | ".join(hud_parts)
                cv2.rectangle(img, (0, 0), (w, 32), (20, 24, 33), -1)
                cv2.putText(img, hud_text, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1)
            except Exception as trk_err:
                logger.debug("Tracker HUD error: %s", trk_err)

        # 2. Draw Segmentation Masks & Polygons
        colors = [
            (0, 255, 0), (255, 128, 0), (0, 200, 255), (255, 0, 128),
            (0, 255, 255), (128, 0, 255), (255, 255, 0), (0, 128, 255)
        ]

        is_segment_mode = getattr(self, "task", "detect") == "segment"

        # Polygons to fill, in img (display) coordinates. Detections are shared with
        # the inference worker and with other viewers, so they are never modified here.
        mask_polys: List[Tuple[int, np.ndarray]] = []
        for item in detections:
            if _allowed and item.class_name.lower() not in _allowed:
                continue
            if item.polygon and len(item.polygon) >= 3:
                pts = np.array([[int(p[0] * scale_x), int(p[1] * scale_y)] for p in item.polygon], dtype=np.int32)
            elif is_segment_mode and item.bbox:
                # The model gave no mask (e.g. detection weights in segment mode):
                # trace the object's outline inside its box instead.
                pts = self._trace_box_outline(img, item.bbox, scale_x, scale_y)
                if pts is None:
                    continue
            else:
                continue
            mask_polys.append((item.class_id, pts))

        # Draw semi-transparent filled masks, blending only the area they cover
        if mask_polys:
            all_pts = np.concatenate([pts for _, pts in mask_polys])
            m = 4  # outline thickness margin
            rx1 = max(0, int(all_pts[:, 0].min()) - m)
            ry1 = max(0, int(all_pts[:, 1].min()) - m)
            rx2 = min(w, int(all_pts[:, 0].max()) + m + 1)
            ry2 = min(h, int(all_pts[:, 1].max()) + m + 1)
            if rx2 > rx1 and ry2 > ry1:
                roi = img[ry1:ry2, rx1:rx2]
                mask_overlay = roi.copy()
                for class_id, pts in mask_polys:
                    color = colors[class_id % len(colors)]
                    cv2.fillPoly(mask_overlay, [pts], color, offset=(-rx1, -ry1))
                    cv2.polylines(img, [pts], isClosed=True, color=color, thickness=2)
                roi[:] = cv2.addWeighted(mask_overlay, 0.45, roi, 0.55, 0)

        # 3. Draw Bounding Boxes & HUD Labels (only for allowed classes)
        # In segmentation mode: NO bounding boxes are drawn (pixel masks only)
        active_tracks: Dict[int, Any] = {}
        try:
            active_tracks = dict(counting_service.get_tracker(camera_id).objects)
        except Exception:
            active_tracks = {}

        if active_tracks:
            # Draw persistent tracks with velocity projection across missed frames (ZERO flicker)
            for tid, obj in active_tracks.items():
                cname = getattr(obj, "class_name", "")
                cname_lower = cname.lower()
                if _allowed and cname_lower not in _allowed:
                    continue
                # Show active tracks that missed at most 5 frames
                if getattr(obj, "missed_frames", 0) > 5:
                    continue

                cid = getattr(obj, "class_id", 0)
                color = colors[cid % len(colors)]
                conf = getattr(obj, "confidence", 1.0)
                is_confirmed = getattr(obj, "confirmed", True)

                x1, y1, x2, y2 = obj.last_bbox
                bx1 = max(0, min(w - 1, int(x1 * scale_x)))
                by1 = max(0, min(h - 1, int(y1 * scale_y)))
                bx2 = max(0, min(w, int(x2 * scale_x)))
                by2 = max(0, min(h, int(y2 * scale_y)))

                # Check if object center reached the exit line zone
                obj_center_x = (bx1 + bx2) / 2.0
                obj_center_y = (by1 + by2) / 2.0
                smooth_cx_scaled = getattr(obj, "smooth_center_x", obj_center_x) * scale_x
                smooth_cy_scaled = getattr(obj, "smooth_center_y", obj_center_y) * scale_y
                if (
                    is_in_exit_zone(obj_center_x, obj_center_y, exit_side, exit_rect)
                    or is_in_exit_zone(smooth_cx_scaled, smooth_cy_scaled, exit_side, exit_rect)
                ):
                    continue

                if not is_segment_mode and bx2 > bx1 and by2 > by1:
                    cv2.rectangle(img, (bx1, by1), (bx2, by2), color, 2)

                anchor_x = bx1
                anchor_y = by1

                if is_confirmed:
                    badge = f"#{tid}"
                    (bw, bh), _ = cv2.getTextSize(badge, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)
                    badge_y0 = max(34, anchor_y - 36)
                    badge_y1 = badge_y0 + bh + 6
                    cv2.rectangle(img, (anchor_x, badge_y0), (anchor_x + bw + 6, badge_y1), (20, 24, 33), -1)
                    cv2.putText(img, badge, (anchor_x + 3, badge_y1 - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 220, 255), 1)

                    label = f"{cname} {int(conf * 100)}%"
                    label_top = badge_y1
                    (lw, lh), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
                    cv2.rectangle(img, (anchor_x, label_top), (anchor_x + lw + 6, label_top + lh + 6), color, -1)
                    cv2.putText(img, label, (anchor_x + 3, label_top + lh + 2), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1)
                else:
                    label = f"{cname} {int(conf * 100)}%"
                    label_top = max(34, anchor_y - 18)
                    (lw, lh), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
                    cv2.rectangle(img, (anchor_x, label_top), (anchor_x + lw + 6, label_top + lh + 6), color, -1)
                    cv2.putText(img, label, (anchor_x + 3, label_top + lh + 2), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1)
        else:
            # Fallback if tracker has no objects: draw raw detections directly
            for item in detections:
                cname_lower = item.class_name.lower()
                if _allowed and cname_lower not in _allowed:
                    continue
                box = item.bbox
                bx1 = max(0, min(w - 1, int(box.x1 * scale_x)))
                by1 = max(0, min(h - 1, int(box.y1 * scale_y)))
                bx2 = max(0, min(w, int(box.x2 * scale_x)))
                by2 = max(0, min(h, int(box.y2 * scale_y)))

                # Check if detection center reached the exit line zone
                det_cx_scaled = (bx1 + bx2) / 2.0
                det_cy_scaled = (by1 + by2) / 2.0
                if is_in_exit_zone(det_cx_scaled, det_cy_scaled, exit_side, exit_rect):
                    continue

                color = colors[item.class_id % len(colors)]
                if not is_segment_mode and bx2 > bx1 and by2 > by1:
                    cv2.rectangle(img, (bx1, by1), (bx2, by2), color, 2)
                anchor_x = bx1
                anchor_y = by1
                label_top = max(34, anchor_y - 18)
                label = f"{item.class_name} {int(item.confidence * 100)}%"
                (lw, lh), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
                cv2.rectangle(img, (anchor_x, label_top), (anchor_x + lw + 6, label_top + lh + 6), color, -1)
                cv2.putText(img, label, (anchor_x + 3, label_top + lh + 2), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1)

        # 4. Exit Line (20px wide transparent gray band at the edge the products flow out of;
        # none when they may move both ways).
        # Purpose: deletes box and ID, stops detecting objects when their center reaches this line,
        # and prevents IDs from jumping around when objects are cropped out of the video.
        # Clean line with no text on it.
        ex1, ey1, ex2, ey2 = exit_rect or (0, 0, 0, 0)
        if ex2 > ex1 and ey2 > ey1:
            _blend_filled_rect(img, (ex1, ey1), (ex2, ey2), (128, 128, 128), 0.40, 0.60)

        return img




    @staticmethod
    def _trace_box_outline(
        img: np.ndarray,
        bbox: BoundingBox,
        scale_x: float,
        scale_y: float,
    ) -> Optional[np.ndarray]:
        """Outline of the object inside a source-frame bbox, as img-coordinate points."""
        h, w = img.shape[:2]
        b_x1 = max(0, min(w - 1, int(bbox.x1 * scale_x)))
        b_y1 = max(0, min(h - 1, int(bbox.y1 * scale_y)))
        b_x2 = max(0, min(w, int(bbox.x2 * scale_x)))
        b_y2 = max(0, min(h, int(bbox.y2 * scale_y)))
        if not (b_x2 > b_x1 + 4 and b_y2 > b_y1 + 4):
            return None
        try:
            roi = img[b_y1:b_y2, b_x1:b_x2]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            edges = cv2.Canny(blurred, 50, 150)
            combined = cv2.bitwise_or(thresh, edges)
            contours, _ = cv2.findContours(combined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                largest = max(contours, key=cv2.contourArea)
                if cv2.contourArea(largest) > 20 and len(largest) >= 3:
                    approx = cv2.approxPolyDP(largest, 1.5, True)
                    return np.array([[int(pt[0][0] + b_x1), int(pt[0][1] + b_y1)] for pt in approx], dtype=np.int32)
            # Fallback: polygon spanning detection region
            return np.array([[b_x1, b_y1], [b_x2, b_y1], [b_x2, b_y2], [b_x1, b_y2]], dtype=np.int32)
        except Exception as poly_err:
            logger.debug("Segmentation contour generation error: %s", poly_err)
            return None

    def draw_annotations(
        self,
        image_input: Any,
        detections: List[DetectionItem],
        latency_ms: float = 0.0,
        draw_wirelines: bool = True,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        fps: float = 0.0,
        camera_rotation: Optional[int] = None,
        camera_flip_h: Optional[bool] = None,
        camera_flip_v: Optional[bool] = None,
    ) -> bytes:
        if isinstance(image_input, bytes):
            nparr = np.frombuffer(image_input, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        else:
            img = image_input.copy()

        annotated = self.draw_annotations_mat(
            img,
            detections,
            latency_ms=latency_ms,
            draw_wirelines=draw_wirelines,
            scale_x=scale_x,
            scale_y=scale_y,
            fps=fps,
            camera_rotation=camera_rotation,
            camera_flip_h=camera_flip_h,
            camera_flip_v=camera_flip_v,
        )
        _, jpeg = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return jpeg.tobytes()

