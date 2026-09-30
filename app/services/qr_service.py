from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession

from app.engines.qr_engine import QREngine
from app.schemas.qr import BarcodeDecodeResponse, LiveCameraBarcodeResponse
from app.services.camera_service import CameraService
from app.utils.logger import LogThrottle, get_logger
from app.utils.threads import run_supervised

logger = get_logger(__name__)
_error_log = LogThrottle(60.0)

_qr_engine = QREngine()
_qr_engine_lock = threading.Lock()


def _decode_qr(image_input: Any) -> BarcodeDecodeResponse:
    # OpenCV detector instances are shared and are not guaranteed to be thread-safe.
    with _qr_engine_lock:
        return _qr_engine.decode(image_input)


class QRService:
    @staticmethod
    async def decode_image(image_bytes: bytes) -> BarcodeDecodeResponse:
        return await asyncio.to_thread(_decode_qr, image_bytes)

    @staticmethod
    async def decode_live_camera(db: AsyncSession, camera_id: str) -> LiveCameraBarcodeResponse:
        from app.state.application_state import app_state
        cam_start = time.perf_counter()
        driver = app_state.cameras.get(camera_id)
        if not driver or not driver.is_connected:
            raise ValueError(f"Camera {camera_id} is not connected")

        success, mat, _ = driver.grab_raw_frame()
        if not success or mat is None:
            raise ValueError(f"Failed to acquire raw frame from camera {camera_id}")
        acq_ms = round((time.perf_counter() - cam_start) * 1000, 2)

        decode_response = await asyncio.to_thread(_decode_qr, mat)
        camera = await CameraService.get_camera_by_id(db, camera_id)
        camera_name = camera.name if camera else "Camera"

        total_latency = round(acq_ms + decode_response.decode_time_ms, 2)

        return LiveCameraBarcodeResponse(
            camera_id=camera_id,
            camera_name=camera_name,
            total_found=decode_response.total_found,
            codes=decode_response.codes,
            decode_time_ms=decode_response.decode_time_ms,
            acquisition_time_ms=acq_ms,
            total_latency_ms=total_latency,
        )

    @staticmethod
    async def get_annotated_frame(db: AsyncSession, camera_id: str) -> bytes:
        from app.state.application_state import app_state
        driver = app_state.cameras.get(camera_id)
        if not driver or not driver.is_connected:
            raise ValueError(f"Camera {camera_id} is not connected")

        success, mat, _ = driver.grab_raw_frame()
        if not success or mat is None:
            raise ValueError("Frame grab failed")

        decode_response = await asyncio.to_thread(_decode_qr, mat)
        return await asyncio.to_thread(
            _qr_engine.draw_annotations,
            mat,
            decode_response.codes,
            decode_response.decode_time_ms,
        )


class _QRReaderWorker:
    """Continuous QR/barcode reading for one camera that has the QR reader role.

    Runs on its own thread with its own decoder, so QR cameras on different
    lines never wait on each other. A code seen in consecutive frames counts
    as one read; it counts again only after it has been out of view for the
    camera's hold time.
    """

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self._engine = QREngine()
        self._input_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._trigger = threading.Event()
        self._stop = threading.Event()
        self._busy = False
        self._pending: Optional[Any] = None
        self._last_seen: dict = {}
        self._latest: List[Tuple[Any, bool]] = []
        self._latest_ms = 0.0
        self.reads = 0
        name = f"QRReader-{camera_id[:8]}"
        self._thread = threading.Thread(
            target=run_supervised,
            args=(name, self._loop, self._stop),
            kwargs={"kind": "qr_reader"},
            name=name,
            daemon=True,
        )
        self._thread.start()

    @property
    def is_busy(self) -> bool:
        return self._busy

    def submit_frame_if_idle(self, mat: Any, frame_id: int) -> bool:
        # Decoding only reads the frame, and drivers replace frames rather than
        # modify them, so no copy is needed.
        if mat is None:
            return False
        with self._input_lock:
            if self._busy:
                return False
            self._busy = True
            self._pending = mat
        self._trigger.set()
        return True

    def _hold_seconds(self) -> float:
        from app.services.line_config import DEFAULT_QR_HOLD_MS
        from app.services.line_service import line_manager

        routed = line_manager.route(self.camera_id)
        entry = routed[0].camera_entry(self.camera_id) if routed else None
        return int((entry or {}).get("qr_hold_ms") or DEFAULT_QR_HOLD_MS) / 1000.0

    def process(self, mat: Any, now: Optional[float] = None) -> List[str]:
        """Decode one frame and report new reads to the camera's line. Returns the new codes."""
        from app.services.line_service import line_manager
        from app.services.product_service import product_catalog

        response = self._engine.decode(mat)
        now = time.monotonic() if now is None else now
        hold = self._hold_seconds()
        routed = line_manager.route(self.camera_id)
        new_codes: List[str] = []
        latest: List[Tuple[Any, bool]] = []
        with self._state_lock:
            for code in response.codes:
                data = (code.data or "").strip()
                if not data:
                    continue
                latest.append((code, product_catalog.lookup(data) is not None))
                previous = self._last_seen.get(data)
                self._last_seen[data] = now
                if previous is None or now - previous > hold:
                    new_codes.append(data)
            for data in [d for d, t in self._last_seen.items() if now - t > hold]:
                del self._last_seen[data]
            self._latest = latest
            self._latest_ms = response.decode_time_ms
        for data in new_codes:
            self.reads += 1
            fmt = next((c.code_type for c, _ in latest if (c.data or "").strip() == data), "UNKNOWN")
            if routed and routed[1] == "qr":
                routed[0].on_qr_read(self.camera_id, data, fmt, now)
        return new_codes

    def _loop(self) -> None:
        from app.state.application_state import app_state

        while not self._stop.is_set():
            self._trigger.wait(timeout=0.1)
            if not self._trigger.is_set():
                continue
            self._trigger.clear()
            with self._input_lock:
                mat, self._pending = self._pending, None
            try:
                if mat is not None:
                    self.process(mat)
                    from app.services.line_service import line_manager
                    routed = line_manager.route(self.camera_id)
                    if routed:
                        routed[0].record_frame(self.camera_id)
                    app_state.processed_frames += 1
            except Exception as exc:
                _error_log.log(
                    logger, logging.WARNING, self.camera_id,
                    "QR reading failed on camera %s: %s: %s", self.camera_id, type(exc).__name__, exc,
                )
            finally:
                with self._input_lock:
                    self._busy = False

    def latest(self) -> Tuple[List[Tuple[Any, bool]], float]:
        with self._state_lock:
            return list(self._latest), self._latest_ms

    def stop(self) -> None:
        self._stop.set()
        self._trigger.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)


class QRReaderPipeline:
    """The continuous QR readers, one per connected camera with the QR reader role."""

    _workers: dict = {}
    _lock = threading.Lock()

    @classmethod
    def get_worker(cls, camera_id: str) -> _QRReaderWorker:
        with cls._lock:
            worker = cls._workers.get(camera_id)
            if worker is None:
                worker = _QRReaderWorker(camera_id)
                cls._workers[camera_id] = worker
            return worker

    @classmethod
    def peek(cls, camera_id: str) -> Optional[_QRReaderWorker]:
        with cls._lock:
            return cls._workers.get(camera_id)

    @classmethod
    def remove_camera(cls, camera_id: str) -> None:
        with cls._lock:
            worker = cls._workers.pop(camera_id, None)
        if worker:
            worker.stop()

    @classmethod
    def stop_all(cls) -> None:
        with cls._lock:
            workers = list(cls._workers.values())
            cls._workers.clear()
        for worker in workers:
            worker.stop()

    @classmethod
    def draw_reads(cls, camera_id: str, img: Any, scale_x: float = 1.0, scale_y: float = 1.0, fps: float = 0.0) -> Any:
        """Outline each code in green when known and red when unknown. Draws on img and returns it."""
        import cv2
        import numpy as np

        worker = cls.peek(camera_id)
        reads, decode_ms = worker.latest() if worker else ([], 0.0)
        h, w = img.shape[:2]
        banner = f"QR READER | CODES: {len(reads)} | {decode_ms:.0f}ms"
        if fps > 0:
            banner += f" | FPS: {fps:.1f}"
        cv2.rectangle(img, (0, 0), (w, 32), (20, 24, 33), -1)
        cv2.putText(img, banner, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1)
        for code, known in reads:
            color = (0, 200, 0) if known else (0, 0, 230)
            if code.polygon and len(code.polygon) >= 3:
                pts = np.array([[int(p[0] * scale_x), int(p[1] * scale_y)] for p in code.polygon], np.int32).reshape((-1, 1, 2))
                cv2.polylines(img, [pts], isClosed=True, color=color, thickness=3)
            if code.bbox:
                label = f"{'KNOWN' if known else 'UNKNOWN'}: {code.data[:40]}"
                x = int(code.bbox.x1 * scale_x)
                y = max(48, int(code.bbox.y1 * scale_y))
                (lw, _), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                cv2.rectangle(img, (x, y - 20), (x + lw + 8, y), color, -1)
                cv2.putText(img, label, (x + 4, y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        return img
