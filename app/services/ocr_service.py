"""Text readers (role "ocr"): one reader thread per camera, its latest result, the corner picture.

A reader read Continuously is fed the newest frame whenever it is free (the frame runner); its reads
are only shown (the line's latest text reads, the video). A reader on One picture per trigger gets
its pictures from the trigger paths (trigger_inspection, the hardware path), through the same
``submit_frame_if_idle(..., on_result=...)`` as a vision camera's inference worker, and its result
becomes a product there.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

from app.engines.code_crop import upright
from app.engines.ocr_engine import engine_for
from app.engines.text_reading import TextResult, error_result, read_picture
from app.services.qr_service import CORNER_MAX_SIDE, DEFAULT_CROP_MARGIN_PX, CornerPicture, draw_corner
from app.utils.logger import LogThrottle, get_logger
from app.utils.threads import run_supervised

logger = get_logger(__name__)
_error_log = LogThrottle(60.0)

NOT_A_READER = "the camera is not a text reader on a line"
BANNER = "TEXT READER"
READER_NOT_READY = "READER NOT READY"
FITS_COLOR, WRONG_COLOR, REGION_COLOR = (0, 200, 0), (0, 0, 230), (200, 200, 200)
LABEL_CHARS = 40


def _entry(camera_id: str) -> Tuple[Any, Dict[str, Any]]:
    """(the camera's line runtime or None, its entry on that line or {})."""
    from app.services.line_service import line_manager

    routed = line_manager.route(camera_id)
    entry = (routed[0].camera_entry(camera_id) if routed else None) or {}
    return (routed[0] if routed else None), entry


def read_for_camera(camera_id: str, mat: np.ndarray) -> TextResult:
    """Read one picture of a camera with its saved text reader settings and its YOLO model."""
    from app.services.line_service import line_manager

    _, entry = _entry(camera_id)
    ocr = entry.get("ocr") if entry.get("role") == "ocr" else None
    if not ocr:
        return error_result({"fields": []}, NOT_A_READER)
    locate: Optional[Callable[[np.ndarray], Any]] = None
    if ocr.get("locator_model_id"):
        yolo = line_manager.engine_for_camera(camera_id)[0]
        if getattr(yolo, "is_loaded", False):
            locate = lambda img: yolo.predict_mat(img).detections  # noqa: E731
    return read_picture(mat, ocr, engine_for(ocr.get("model") or "small"), locate)


class OcrReaderWorker:
    """One text reader camera's thread: one picture in flight at a time."""

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self._input_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._trigger = threading.Event()
        self._stop = threading.Event()
        self._busy = False
        self._pending: Optional[Tuple[Any, int, Optional[float], Any, Any]] = None
        self._latest: Optional[TextResult] = None
        self._corner: Optional[CornerPicture] = None
        self.last_inferred_frame: Optional[Any] = None
        self.reads = 0
        name = f"OCRReader-{camera_id[:8]}"
        self._thread = threading.Thread(target=run_supervised, args=(name, self._loop, self._stop),
                                        kwargs={"kind": "ocr_reader"}, name=name, daemon=True)
        self._thread.start()

    @property
    def is_busy(self) -> bool:
        return self._busy

    def submit_frame_if_idle(self, mat: Any, frame_id: int, conf_thresh: Optional[float] = None, copy: bool = False,
                             arrived_at: Optional[float] = None,
                             on_result: Optional[Callable[[int, TextResult, float, Optional[float]], None]] = None,
                             on_failure: Optional[Callable[[int, str], None]] = None) -> bool:
        """Take a picture unless one is being read (the shape of the vision worker's, for the trigger paths).
        ``on_result(frame id, result, ms, arrived at)`` gets the result instead of the line's shown reads;
        ``on_failure(frame id, why)`` is called when reading raised."""
        if mat is None:
            return False
        with self._input_lock:
            if self._busy:
                return False
            self._busy = True
            self._pending = (mat.copy() if copy else mat, frame_id, arrived_at, on_result, on_failure)
        self._trigger.set()
        return True

    def latest(self) -> Optional[TextResult]:
        with self._state_lock:
            return self._latest

    def corner(self) -> Optional[CornerPicture]:
        """The first field's line of the last read, cut out straight (None: nothing since the line started)."""
        with self._state_lock:
            return self._corner

    def clear_corner(self) -> None:
        with self._state_lock:
            self._corner = None

    def _keep_corner(self, mat: np.ndarray, result: TextResult) -> None:
        import cv2

        field = next((f for f in result.fields if f.box), None)
        if field is None:
            return
        crop, _ = upright(mat, field.box, DEFAULT_CROP_MARGIN_PX, max_long=CORNER_MAX_SIDE)
        if crop.ndim == 2:
            crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
        with self._state_lock:
            self._corner = CornerPicture(crop, field.text or "", bool(field.fits))

    def _alarm(self, runtime: Any, result: TextResult) -> None:
        """camera.model_unavailable while the reader cannot read on a running line; cleared by a good read."""
        from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager
        from app.state.application_state import app_state

        source = f"camera:{self.camera_id}"
        if result.error and result.error != NOT_A_READER and runtime is not None and runtime.enabled:
            name = getattr(app_state.cameras.get(self.camera_id), "name", None) or self.camera_id
            alarm_manager.raise_alarm(
                AlarmCode.CAMERA_MODEL_UNAVAILABLE, source,
                f"Camera '{name}' (text reader) on line '{runtime.name}' cannot read: {result.error}",
                AlarmSeverity.WARNING, {"line_id": runtime.id, "camera_id": self.camera_id},
            )
        elif not result.error:
            alarm_manager.clear_alarm(AlarmCode.CAMERA_MODEL_UNAVAILABLE, source, "the text reader reads again")

    def _read(self, mat: Any, frame_id: int, arrived_at: Optional[float], on_result: Any) -> None:
        from app.state.application_state import app_state

        result = read_for_camera(self.camera_id, mat)
        self.last_inferred_frame = mat
        self.reads += 1
        with self._state_lock:
            self._latest = result
        self._keep_corner(mat, result)
        runtime, entry = _entry(self.camera_id)
        on_line = runtime is not None and entry.get("role") == "ocr"
        self._alarm(runtime if on_line else None, result)
        if on_result is not None:
            on_result(frame_id, result, result.ms, arrived_at)
            return
        if on_line and runtime.enabled:
            runtime.show_text_read(self.camera_id, result)
            runtime.record_frame(self.camera_id)
            app_state.processed_frames += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._trigger.wait(timeout=0.1)
            self._trigger.clear()
            with self._input_lock:
                pending, self._pending = self._pending, None
            if pending is None:
                continue
            mat, frame_id, arrived_at, on_result, on_failure = pending
            try:
                self._read(mat, frame_id, arrived_at, on_result)
            except Exception as exc:  # noqa: BLE001 - one bad picture never stops the reader
                _error_log.log(logger, logging.WARNING, self.camera_id, "Text reading failed on camera %s: %s: %s",
                               self.camera_id, type(exc).__name__, exc)
                if on_failure is not None:
                    on_failure(frame_id, f"{type(exc).__name__}: {exc}")
            finally:
                with self._input_lock:
                    self._busy = False

    def stop(self) -> None:
        self._stop.set()
        self._trigger.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)


def _reader_ready(camera_id: str, ocr: Dict[str, Any]) -> bool:
    """The reader's model can read, and so can the YOLO model when a field finds its text by class."""
    from app.services.line_service import line_manager

    if not engine_for(ocr.get("model") or "small").ready:
        return False
    if any(field.get("find") == "yolo" for field in ocr.get("fields") or []):
        return bool(getattr(line_manager.engine_for_camera(camera_id)[0], "is_loaded", False))
    return True


def _state(camera_id: str) -> Tuple[List[str], Dict[str, Any], bool]:
    """(what the bar says, the reader's ocr settings ({} off a line), whether its latest result is shown)."""
    from app.engines.inference_engine import STATUS_NO_LINE, STATUS_STOPPED, STATUS_WAITING_TRIGGER

    runtime, entry = _entry(camera_id)
    ocr = entry.get("ocr") if entry.get("role") == "ocr" else None
    if runtime is None or not ocr:
        return [BANNER, STATUS_NO_LINE], {}, False
    if not runtime.enabled:
        return [BANNER, STATUS_STOPPED], ocr, False
    if not _reader_ready(camera_id, ocr):
        return [BANNER, READER_NOT_READY], ocr, False
    if entry.get("count_mode") == "trigger":
        # The result is drawn on the picture the trigger took, not on the moving video.
        return [BANNER, STATUS_WAITING_TRIGGER], ocr, False
    worker = OcrReaderPipeline.peek(camera_id)
    result = worker.latest() if worker else None
    if result is None:
        return [BANNER], ocr, True
    ok = sum(1 for field in result.fields if field.fits)
    return [BANNER, f"FIELDS {ok}/{len(result.fields)} OK", f"{result.ms:.0f}ms"], ocr, True


def bar_parts(camera_id: str) -> List[str]:
    """What the TEXT READER bar of a camera's video says."""
    return _state(camera_id)[0]


def _draw_fields(img: np.ndarray, result: Optional[TextResult], ocr: Dict[str, Any], sx: float, sy: float) -> int:
    """The regions (light grey) and each field's line (green: fits, red: does not) with its name and text.
    Returns the overlay scale's label height in px."""
    import cv2

    from app.engines.inference_engine import overlay_scale

    h, w = img.shape[:2]
    s = overlay_scale(w, h)
    thick = max(2, int(round(2 * s)))
    for field in ocr.get("fields") or []:
        if field.get("find") == "region":
            x1, y1, x2, y2 = field["region"]
            cv2.rectangle(img, (int(round(x1 * w)), int(round(y1 * h))), (int(round(x2 * w)) - 1, int(round(y2 * h)) - 1),
                          REGION_COLOR, max(1, int(round(s))))
    font, text_thick = 0.5 * s, max(1, int(round(s)))
    for field in (result.fields if result else []):
        if not field.box:
            continue
        color = FITS_COLOR if field.fits else WRONG_COLOR
        pts = np.array([[int(round(x * sx)), int(round(y * sy))] for x, y in field.box], np.int32)
        cv2.polylines(img, [pts.reshape((-1, 1, 2))], isClosed=True, color=color, thickness=thick)
        label = f"{field.name}: {field.text or ''}"[:LABEL_CHARS]
        x, y = int(pts[:, 0].min()), max(int(round(20 * s)), int(pts[:, 1].min()))
        (lw, _), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font, text_thick)
        cv2.rectangle(img, (x, y - int(round(20 * s))), (x + lw + int(round(8 * s)), y), color, -1)
        cv2.putText(img, label, (x + int(round(4 * s)), y - int(round(6 * s))), cv2.FONT_HERSHEY_SIMPLEX, font,
                    (255, 255, 255), text_thick)
    return int(round(20 * s))


def render_capture(frame: Any, result: TextResult) -> Optional[bytes]:
    """The picture a trigger took for a text reader, with each field's line drawn (JPEG; None on any failure)."""
    try:
        import cv2

        if frame is None:
            return None
        img = frame.copy()
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        _draw_fields(img, result, {}, 1.0, 1.0)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return buf.tobytes() if ok else None
    except Exception as exc:  # noqa: BLE001 - a picture that cannot be drawn is not shown
        logger.warning("The text reader's picture could not be drawn: %s: %s", type(exc).__name__, exc)
        return None


class OcrReaderPipeline:
    """The text readers, one per camera with the text reader role."""

    _workers: Dict[str, OcrReaderWorker] = {}
    _lock = threading.Lock()

    @classmethod
    def get_worker(cls, camera_id: str) -> OcrReaderWorker:
        with cls._lock:
            worker = cls._workers.get(camera_id)
            if worker is None:
                worker = cls._workers[camera_id] = OcrReaderWorker(camera_id)
            return worker

    @classmethod
    def peek(cls, camera_id: str) -> Optional[OcrReaderWorker]:
        with cls._lock:
            return cls._workers.get(camera_id)

    @classmethod
    def remove_camera(cls, camera_id: str) -> None:
        with cls._lock:
            worker = cls._workers.pop(camera_id, None)
        if worker:
            worker.stop()

    @classmethod
    def clear_corners(cls, camera_ids: Iterable[str]) -> None:
        """Forgets the corner pictures of these cameras (their line starts: a picture from before is not new)."""
        for camera_id in camera_ids:
            worker = cls.peek(camera_id)
            if worker is not None:
                worker.clear_corner()

    @classmethod
    def stop_all(cls) -> None:
        with cls._lock:
            workers = list(cls._workers.values())
            cls._workers.clear()
        for worker in workers:
            worker.stop()

    @classmethod
    def draw_reads(cls, camera_id: str, img: np.ndarray, scale_x: float = 1.0, scale_y: float = 1.0,
                   fps: float = 0.0) -> np.ndarray:
        """A text reader's video: its regions, its latest fields while it reads Continuously on a running
        line, the TEXT READER bar, and the corner picture of the last line read. Draws on img and returns it."""
        from app.engines.inference_engine import STATUS_STOPPED, draw_status_bar

        parts, ocr, shown = _state(camera_id)
        worker = cls.peek(camera_id)
        result = worker.latest() if (shown and worker) else None
        if ocr and parts[1:2] != [STATUS_STOPPED]:
            _draw_fields(img, result, ocr, scale_x, scale_y)
        if fps > 0:
            parts = parts + [f"FPS: {fps:.1f}"]
        top = draw_status_bar(img, parts)
        corner = worker.corner() if (shown and worker) else None
        if corner is not None:
            draw_corner(img, corner, top)
        return img
