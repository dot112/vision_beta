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
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

import numpy as np

from app.engines.code_crop import upright
from app.engines.ocr_engine import engine_for
from app.engines.text_reading import TextResult, error_result, read_picture
from app.services.qr_service import CORNER_MAX_SIDE, DEFAULT_CROP_MARGIN_PX, CornerPicture
from app.utils.logger import LogThrottle, get_logger
from app.utils.threads import run_supervised

logger = get_logger(__name__)
_error_log = LogThrottle(60.0)

NOT_A_READER = "the camera is not a text reader on a line"


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
