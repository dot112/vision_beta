from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple
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

# Milliseconds a picture where no code reads may spend in the slower second pass
# (sharpening, motion deblurring): a little on every frame of a continuously
# reading camera, more for a single picture taken per product or sent to the API.
CONTINUOUS_HARD_BUDGET_MS = 40.0
SINGLE_PICTURE_HARD_BUDGET_MS = 150.0
# While the second pass finds nothing (no product in view, a busy background), a
# continuously reading camera tries it at most this often, to bound its CPU use.
CONTINUOUS_HARD_INTERVAL_S = 0.1


def _decode_qr(image_input: Any) -> BarcodeDecodeResponse:
    # OpenCV detector instances are shared and are not guaranteed to be thread-safe.
    with _qr_engine_lock:
        return _qr_engine.decode(image_input, hard_budget_ms=SINGLE_PICTURE_HARD_BUDGET_MS)


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
    """QR/barcode reading for one camera that has the QR reader role.

    Runs on its own thread with its own decoder, so QR cameras on different
    lines never wait on each other. Continuously, a code seen in consecutive
    frames counts as one read; it counts again only after it has been out of
    view for the camera's hold time. Set to capture on a wire line crossing,
    it decodes one picture per product instead (request_capture).
    """

    # A picture must be newer than the trigger; wait this long for the next frame.
    FRESH_FRAME_WAIT_S = 0.25
    # Pictures kept for the dashboard are scaled down to this width.
    CAPTURE_MAX_WIDTH = 1280
    # A picture taken for a product where no code reads is followed by the next
    # frames (blur, code not quite in view yet), within this many and this long.
    CAPTURE_FRAMES = 3
    CAPTURE_RETRY_WINDOW_S = 0.4

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
        self._hard_missed_at = float("-inf")
        self.reads = 0
        self._captures: Deque[Dict[str, Any]] = collections.deque(maxlen=32)
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

    def _camera_entry(self) -> Dict[str, Any]:
        """This camera's entry on its line (role, hold time, code type), or {}."""
        from app.services.line_service import line_manager

        routed = line_manager.route(self.camera_id)
        return (routed[0].camera_entry(self.camera_id) if routed else None) or {}

    def _hold_seconds(self) -> float:
        from app.services.line_config import DEFAULT_QR_HOLD_MS

        return int(self._camera_entry().get("qr_hold_ms") or DEFAULT_QR_HOLD_MS) / 1000.0

    def _code_type(self) -> str:
        """The code types this camera is set to read; codes of other types are ignored."""
        return self._camera_entry().get("code_type") or "all"

    def _product_list(self) -> Optional[str]:
        """The product list this camera checks its codes against (Line setup)."""
        return self._camera_entry().get("product_list_id")

    def process(self, mat: Any, now: Optional[float] = None) -> List[str]:
        """Decode one frame and report new reads to the camera's line. Returns the new codes."""
        from app.services.line_service import line_manager
        from app.services.product_service import product_catalog

        clock = time.monotonic()
        hard = CONTINUOUS_HARD_BUDGET_MS if clock - self._hard_missed_at >= CONTINUOUS_HARD_INTERVAL_S else 0.0
        response = self._engine.decode(mat, hard_budget_ms=hard, code_type=self._code_type())
        if hard and not response.codes:
            self._hard_missed_at = clock
        now = clock if now is None else now
        hold = self._hold_seconds()
        list_id = self._product_list()
        routed = line_manager.route(self.camera_id)
        new_codes: List[str] = []
        latest: List[Tuple[Any, bool]] = []
        with self._state_lock:
            for code in response.codes:
                data = (code.data or "").strip()
                if not data:
                    continue
                latest.append((code, product_catalog.lookup(data, list_id) is not None))
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
            if routed and routed[0].reads_codes(self.camera_id):
                routed[0].on_qr_read(self.camera_id, data, fmt, now)
        return new_codes

    def request_capture(self, track_id: Optional[int] = None, class_name: Optional[str] = None,
                        wire_line: Optional[int] = None, delay: float = 0.0, test: bool = False) -> None:
        """Take one picture after ``delay`` seconds, decode it and hand it to the camera's line."""
        with self._input_lock:
            self._captures.append({
                "due": time.monotonic() + max(0.0, delay),
                "track_id": track_id,
                "class_name": class_name,
                "wire_line": wire_line,
                "test": test,
            })
        self._trigger.set()

    def _next_wait(self) -> float:
        with self._input_lock:
            if not self._captures:
                return 0.1
            due = min(c["due"] for c in self._captures)
        return max(0.0, min(0.1, due - time.monotonic()))

    def _due_captures(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        with self._input_lock:
            due = [c for c in self._captures if c["due"] <= now]
            for c in due:
                self._captures.remove(c)
        return due

    def _fresh_frame(self) -> Optional[Any]:
        """A frame the camera delivered after this call, or its latest one if none comes in time."""
        from app.state.application_state import app_state

        driver = app_state.cameras.get(self.camera_id)
        if driver is None or not getattr(driver, "is_connected", False):
            return None
        first = driver.get_latest_frame_id() if hasattr(driver, "get_latest_frame_id") else 0
        deadline = time.monotonic() + self.FRESH_FRAME_WAIT_S
        while not self._stop.is_set() and time.monotonic() < deadline:
            if (driver.get_latest_frame_id() if hasattr(driver, "get_latest_frame_id") else first + 1) != first:
                break
            time.sleep(0.005)
        if hasattr(driver, "get_latest_raw_mat"):
            ok, mat, _ = driver.get_latest_raw_mat(copy=True)
        else:
            ok, mat, _ = driver.grab_raw_frame()
        return mat if ok and mat is not None else None

    def capture(self, request: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[bytes]]:
        """Take and decode one picture. Returns (capture record, annotated JPEG or None)."""
        from app.services.product_service import product_catalog

        started = time.monotonic()
        mat: Optional[Any] = None
        t = started
        codes: List[Dict[str, Any]] = []
        decode_ms = 0.0
        frames = 0
        jpeg: Optional[bytes] = None
        code_type = self._code_type()
        list_id = self._product_list()
        while frames < self.CAPTURE_FRAMES and (frames == 0 or time.monotonic() - started < self.CAPTURE_RETRY_WINDOW_S):
            frame = self._fresh_frame()
            if frame is None:
                break
            mat, t, frames = frame, time.monotonic(), frames + 1
            response = self._engine.decode(mat, hard_budget_ms=SINGLE_PICTURE_HARD_BUDGET_MS, code_type=code_type)
            decode_ms += response.decode_time_ms
            seen = set()
            for code in response.codes:
                data = (code.data or "").strip()
                if not data or data in seen:
                    continue
                seen.add(data)
                product = product_catalog.lookup(data, list_id)
                codes.append({
                    "code": data,
                    "format": code.code_type,
                    "known": product is not None,
                    "product_name": product.get("name") if product else None,
                    "polygon": code.polygon,
                })
            if codes:
                break
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "t": t,
            "camera_id": self.camera_id,
            "track_id": request.get("track_id"),
            "class_name": request.get("class_name"),
            "wire_line": request.get("wire_line"),
            "test": bool(request.get("test")),
            "status": "read" if codes else ("no_read" if mat is not None else "camera_offline"),
            "codes": codes,
            "decode_ms": round(decode_ms, 2),
            "frames": frames,
            "width": int(mat.shape[1]) if mat is not None else 0,
            "height": int(mat.shape[0]) if mat is not None else 0,
        }
        if mat is not None:
            jpeg = draw_capture(mat, record, self.CAPTURE_MAX_WIDTH)
        return record, jpeg

    def _run_capture(self, request: Dict[str, Any]) -> None:
        from app.services.line_service import line_manager

        try:
            record, jpeg = self.capture(request)
        except Exception as exc:
            _error_log.log(
                logger, logging.WARNING, ("capture", self.camera_id),
                "QR capture failed on camera %s: %s: %s", self.camera_id, type(exc).__name__, exc,
            )
            return
        routed = line_manager.route(self.camera_id)
        if routed and routed[0].reads_codes(self.camera_id):
            routed[0].on_qr_capture(self.camera_id, record, jpeg)
            if routed[1] == "qr":  # a vision camera's frame rate is its model's
                routed[0].record_frame(self.camera_id)

    def _loop(self) -> None:
        from app.state.application_state import app_state

        while not self._stop.is_set():
            self._trigger.wait(timeout=self._next_wait())
            self._trigger.clear()
            for request in self._due_captures():
                self._run_capture(request)
            with self._input_lock:
                mat, self._pending = self._pending, None
            if mat is None:
                continue
            try:
                self.process(mat)
                from app.services.line_service import line_manager
                routed = line_manager.route(self.camera_id)
                # A vision camera that also reads codes counts its frames on its model's thread.
                if routed and routed[1] == "qr":
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


def draw_capture(mat: Any, record: Dict[str, Any], max_width: int = 1280) -> bytes:
    """A captured picture with a box around every code: green for known products, red for unknown."""
    import cv2
    import numpy as np

    img = mat.copy()
    h, w = img.shape[:2]
    scale = max(1.0, w / 640.0)
    thickness = max(2, int(round(2 * scale)))
    font = 0.5 * scale
    for code in record.get("codes") or []:
        color = (0, 200, 0) if code.get("known") else (0, 0, 230)
        polygon = code.get("polygon") or []
        if len(polygon) >= 3:
            pts = np.array(polygon, np.int32).reshape((-1, 1, 2))
            cv2.polylines(img, [pts], isClosed=True, color=color, thickness=thickness)
            x = int(min(p[0] for p in polygon))
            y = int(min(p[1] for p in polygon))
            label = code.get("product_name") or ("UNKNOWN" if not code.get("known") else "")
            text = f"{code.get('code', '')[:40]}" + (f"  {label[:30]}" if label else "")
            (lw, lh), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font, max(1, thickness // 2))
            top = max(0, y - lh - 12)
            cv2.rectangle(img, (x, top), (x + lw + 10, top + lh + 10), color, -1)
            cv2.putText(img, text, (x + 5, top + lh + 4), cv2.FONT_HERSHEY_SIMPLEX, font, (255, 255, 255), max(1, thickness // 2))

    codes = record.get("codes") or []
    when = str(record.get("timestamp") or "")[11:19]
    parts = ["TEST CAPTURE" if record.get("test") else "CAPTURE"]
    if record.get("wire_line"):
        parts.append(f"wire line {record['wire_line']}")
    if record.get("track_id") is not None:
        parts.append(f"product #{record['track_id']}")
    parts.append(f"{len(codes)} code(s)" if codes else "NO CODE READ")
    if when:
        parts.append(f"{when} UTC")
    banner_h = int(30 * scale)
    cv2.rectangle(img, (0, 0), (w, banner_h), (20, 24, 33) if codes else (0, 0, 150), -1)
    cv2.putText(img, "  |  ".join(parts), (int(10 * scale), int(21 * scale)), cv2.FONT_HERSHEY_SIMPLEX, font, (255, 255, 255), max(1, thickness // 2))

    if max_width and w > max_width:
        img = cv2.resize(img, (max_width, max(1, int(h * max_width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return buf.tobytes() if ok else b""


class QRReaderPipeline:
    """The QR readers, one per connected camera with the QR reader role."""

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
    def draw_reads(cls, camera_id: str, img: Any, scale_x: float = 1.0, scale_y: float = 1.0, fps: float = 0.0,
                   banner: bool = True) -> Any:
        """Outline each code in green when known and red when unknown. Draws on img and returns it.
        banner=False leaves out the QR READER bar (a vision camera's video has its own)."""
        import cv2
        import numpy as np

        from app.services.line_service import line_manager

        worker = cls.peek(camera_id)
        reads, decode_ms = worker.latest() if worker else ([], 0.0)
        h, w = img.shape[:2]
        routed = line_manager.route(camera_id)
        trigger = routed[0].capture_triggers.get(camera_id) if routed else None
        if trigger:
            # The codes are drawn on the captured picture, not on live video.
            reads = []
            title = f"QR READER | CAPTURES WHEN A PRODUCT CROSSES WIRE LINE {trigger[0]}"
        else:
            title = f"QR READER | CODES: {len(reads)} | {decode_ms:.0f}ms"
        if fps > 0:
            title += f" | FPS: {fps:.1f}"
        if banner:
            cv2.rectangle(img, (0, 0), (w, 32), (20, 24, 33), -1)
            cv2.putText(img, title, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1)
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
