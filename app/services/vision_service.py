from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import threading
import time
from typing import Any, AsyncGenerator, AsyncIterator, Callable, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


from app.config import settings
from app.db.models.detection import DetectionLog
from app.engines.inference_engine import begin_model_timing, end_model_timing
from app.schemas.vision import DetectionResponse, LiveInspectionResponse
from app.services.camera_service import CameraService
from app.state.application_state import app_state
from app.utils.logger import LogThrottle, get_logger
from app.utils.threads import run_supervised

logger = get_logger(__name__)
# A model or tracker that fails on every frame is reported once a minute, not 30 times a second.
_error_log = LogThrottle(60.0)

def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def shrink_jpeg(jpeg: bytes, max_width: int, quality: int = 70) -> bytes:
    """The same picture no wider than max_width. A picture that already fits is returned as it is."""
    import cv2
    import numpy as np

    mat = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if mat is None:
        return jpeg
    height, width = mat.shape[:2]
    if width <= max_width:
        return jpeg
    new_height = max(1, round(height * max_width / float(width)))
    small = cv2.resize(mat, (int(max_width), new_height), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else jpeg


class _CameraInferenceWorker:
    """
    Decoupled background inference worker per camera.
    Uses a single persistent background thread to eliminate OS thread churn at 30 FPS.
    """

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        import threading
        self._lock = threading.Lock()
        self._input_lock = threading.Lock()
        self._trigger_event = threading.Event()
        self._stop_event = threading.Event()
        self._busy = False

        self._pending_frame: Optional[Any] = None
        self._pending_fid: int = 0
        self._pending_conf: Optional[float] = None

        self._latest_detections: List[Any] = []
        self._latest_inference_ms: float = 0.0
        self._processed_frame_id: int = 0

        name = f"InferenceWorker-{camera_id[:8]}"
        self._thread = threading.Thread(
            target=run_supervised,
            args=(name, self._worker_loop, self._stop_event),
            kwargs={"kind": "inference"},
            name=name,
            daemon=True,
        )
        self._thread.start()

    def get_latest_inference(self) -> Tuple[List[Any], float]:
        with self._lock:
            return list(self._latest_detections), self._latest_inference_ms

    @property
    def is_busy(self) -> bool:
        """True while a submitted frame is queued or being inferred."""
        return self._busy

    def submit_frame_if_idle(
        self,
        frame_mat: Any,
        frame_id: int,
        conf_thresh: Optional[float] = None,
        copy: bool = True,
    ) -> bool:
        """
        Queue a frame for inference unless one is already in flight.
        copy=False hands over frame_mat as is; only pass it for an array nobody
        will draw on, such as a camera driver's latest frame, which drivers
        replace rather than modify.
        Returns True if the frame was accepted.
        """
        if frame_mat is None or frame_id == self._processed_frame_id:
            return False

        with self._input_lock:
            if self._busy:
                return False
            self._busy = True
            # Clone by default so drawing lines/boxes never corrupts the AI input
            self._pending_frame = frame_mat.copy() if copy else frame_mat
            self._pending_fid = frame_id
            self._pending_conf = conf_thresh
        self._trigger_event.set()
        return True

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self._trigger_event.wait(timeout=0.1)
            if not self._trigger_event.is_set():
                continue
            self._trigger_event.clear()

            with self._input_lock:
                mat = self._pending_frame
                fid = self._pending_fid
                conf = self._pending_conf
                self._pending_frame = None

            if mat is None:
                continue

            try:
                from app.services.line_service import line_manager
                engine, _, _ = line_manager.engine_for_camera(self.camera_id)
                if not getattr(engine, "is_loaded", True):
                    # No model runs on this camera (none picked, or it did not
                    # load): its video is still shown, with nothing detected.
                    with self._lock:
                        self._latest_detections = []
                        self._latest_inference_ms = 0.0
                        self._processed_frame_id = fid
                    continue
                det_response = engine.predict_mat(mat, conf_threshold=conf)

                # Update real-time object tracking & wireline counters of the camera's line
                try:
                    routed = line_manager.route(self.camera_id)
                    if routed:
                        routed[0].record_frame(self.camera_id)
                    line_manager.counter_for_camera(self.camera_id).process_frame(
                        det_response.detections,
                        det_response.image_width,
                        det_response.image_height,
                        camera_id=self.camera_id,
                    )
                except Exception as trk_err:
                    _error_log.log(
                        logger, logging.WARNING, ("tracker", self.camera_id),
                        "Counting failed on camera %s: %s: %s", self.camera_id, type(trk_err).__name__, trk_err,
                    )

                with self._lock:
                    self._latest_detections = det_response.detections
                    self._latest_inference_ms = det_response.inference_time_ms
                    self._processed_frame_id = fid
                    app_state.detection_count += det_response.total_detections
                    app_state.processed_frames += 1
            except Exception as exc:
                _error_log.log(
                    logger, logging.WARNING, ("inference", self.camera_id),
                    "Inference failed on camera %s: %s: %s", self.camera_id, type(exc).__name__, exc,
                )
            finally:
                with self._input_lock:
                    self._busy = False

    def stop(self) -> None:
        self._stop_event.set()
        self._trigger_event.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)


class _CameraStreamPublisher:
    """
    Dedicated video publishing pipeline for a camera.
    Completely decouples camera frame capture and AI inference from video delivery.
    Features:
    - Runs in a dedicated background thread.
    - Encodes JPEG frames ONCE per camera at target ~30 FPS regardless of subscriber count.
    - Zero OpenCV or image encoding work happens on the FastAPI async event loop.
    - Subscriber-aware: enters low-power sleep mode when no HTTP/WebSocket clients are viewing.
    - Thread-safe frame caching for instantaneous snapshot grabs and WebSocket streaming.
    """

    # A camera whose picture has not changed is drawn again this often, so that
    # what was found in it after it arrived (detections, codes, a count) reaches
    # the viewers. A camera showing a still picture is never redrawn otherwise.
    STILL_REFRESH_SECONDS = 0.25

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._subscribers = 0
        self._annotated_subscribers = 0
        self._raw_subscribers = 0
        self._last_active_time = time.time()

        self._latest_annotated_jpeg: Optional[bytes] = None
        self._latest_raw_jpeg: Optional[bytes] = None
        # Each goes up when a picture that differs from the last one is ready to send.
        self._annotated_seq: int = 0
        self._raw_seq: int = 0
        # Smaller copies of the latest annotated picture, {width: (seq, jpeg)},
        # so every viewer of a small tile shares one resize per picture.
        self._scaled: Dict[int, Tuple[int, bytes]] = {}
        self._fps_tracker: collections.deque = collections.deque(maxlen=30)
        self._current_fps: float = 0.0

        name = f"StreamPub-{camera_id[:8]}"
        self._thread = threading.Thread(
            target=run_supervised,
            args=(name, self._publisher_loop, self._stop_event),
            kwargs={"kind": "stream_publisher"},
            name=name,
            daemon=True,
        )
        self._thread.start()

    def add_subscriber(self, stream_type: str = "annotated") -> None:
        with self._lock:
            self._subscribers += 1
            if stream_type == "raw":
                self._raw_subscribers += 1
            else:
                self._annotated_subscribers += 1
            self._last_active_time = time.time()

    def remove_subscriber(self, stream_type: str = "annotated") -> None:
        with self._lock:
            self._subscribers = max(0, self._subscribers - 1)
            if stream_type == "raw":
                self._raw_subscribers = max(0, self._raw_subscribers - 1)
            else:
                self._annotated_subscribers = max(0, self._annotated_subscribers - 1)

    @property
    def has_subscribers(self) -> bool:
        with self._lock:
            return self._subscribers > 0 or (time.time() - self._last_active_time < 3.0)

    def get_latest_annotated(self) -> Optional[bytes]:
        with self._lock:
            self._last_active_time = time.time()
            return self._latest_annotated_jpeg

    def get_latest_raw(self) -> Optional[bytes]:
        with self._lock:
            self._last_active_time = time.time()
            return self._latest_raw_jpeg

    _MAX_SCALED_WIDTHS = 4

    def get_latest_annotated_scaled(self, max_width: int) -> Tuple[Optional[bytes], Optional[bytes]]:
        """(picture, ready) for a viewer that wants it no wider than max_width.

        ``ready`` is the smaller copy when it was already made for this
        picture; otherwise the caller shrinks ``picture`` off the event loop
        and hands the result to store_scaled().
        """
        with self._lock:
            self._last_active_time = time.time()
            jpeg = self._latest_annotated_jpeg
            cached = self._scaled.get(max_width)
            if jpeg is not None and cached is not None and cached[0] == self._annotated_seq:
                return jpeg, cached[1]
            return jpeg, None

    def store_scaled(self, max_width: int, source: bytes, scaled: bytes) -> None:
        with self._lock:
            if source is not self._latest_annotated_jpeg:
                return  # a newer picture arrived meanwhile
            if max_width not in self._scaled and len(self._scaled) >= self._MAX_SCALED_WIDTHS:
                self._scaled.pop(next(iter(self._scaled)))
            self._scaled[max_width] = (self._annotated_seq, scaled)

    def get_latest_frame_id(self, stream_type: str = "annotated") -> int:
        """A number that changes whenever a new picture of that kind is ready to send."""
        with self._lock:
            return self._raw_seq if stream_type == "raw" else self._annotated_seq

    def _publisher_loop(self) -> None:
        import cv2

        last_rendered_fid = -1
        last_render_at = 0.0
        paused = False
        target_interval = 1.0 / 30.0  # 30 FPS target

        while not self._stop_event.is_set():
            loop_start = time.perf_counter()

            # If no active viewers, sleep and avoid wasting CPU/GPU
            if not self.has_subscribers:
                if not paused:
                    # The next viewer gets a picture drawn for them, not the one
                    # left over from whenever the last viewer went away.
                    paused = True
                    with self._lock:
                        self._latest_annotated_jpeg = None
                        self._latest_raw_jpeg = None
                        self._scaled.clear()
                time.sleep(0.1)
                continue
            paused = False

            driver = app_state.cameras.get(self.camera_id)
            if not driver or not getattr(driver, "is_connected", False):
                time.sleep(0.1)
                continue

            # Check if camera has a fresh frame
            cur_fid = driver.get_latest_frame_id() if hasattr(driver, "get_latest_frame_id") else getattr(driver, "_latest_frame_id", 0)
            fresh = cur_fid != last_rendered_fid
            if cur_fid == 0 or (not fresh and loop_start - last_render_at < self.STILL_REFRESH_SECONDS):
                if hasattr(driver, "wait_for_new_frame"):
                    driver.wait_for_new_frame(timeout=0.015)
                else:
                    time.sleep(0.005)
                continue

            # Zero-copy access to camera's latest processed BGR mat
            if hasattr(driver, "get_latest_raw_mat"):
                success, mat, fid = driver.get_latest_raw_mat(copy=False)
            else:
                success, mat, fid = driver.grab_raw_frame()

            if not success or mat is None:
                time.sleep(0.005)
                continue

            last_rendered_fid = fid
            last_render_at = loop_start
            orig_h, orig_w = mat.shape[:2]

            # Calculate actual video publishing FPS (new camera pictures, not redraws of a still one)
            now = time.time()
            if fresh:
                self._fps_tracker.append(now)
            while self._fps_tracker and (now - self._fps_tracker[0]) > 2.0:
                self._fps_tracker.popleft()
            fps = (len(self._fps_tracker) - 1) / (now - self._fps_tracker[0]) if len(self._fps_tracker) > 1 and (now - self._fps_tracker[0]) > 0.05 else 0.0
            self._current_fps = fps

            # 1. Bandwidth optimization: resize display copy (default 640px max width).
            # mat is the camera's own buffer, so draw only on a private array: the
            # resize output, or a copy when no resize is needed.
            # The camera's own stream settings (Cameras & Capture page).
            stream_settings = getattr(driver, "settings", None) or {}
            max_width = _clamp_int(stream_settings.get("stream_max_width"), 640, 160, 3840)
            quality = _clamp_int(stream_settings.get("stream_jpeg_quality"), 70, 20, 95)
            scale_x = 1.0
            scale_y = 1.0
            if orig_w > max_width:
                scale_x = max_width / float(orig_w)
                new_h = max(1, int(orig_h * scale_x))
                scale_y = new_h / float(orig_h)
                display_mat = cv2.resize(mat, (max_width, new_h), interpolation=cv2.INTER_LINEAR)
            else:
                display_mat = mat.copy()

            with self._lock:
                want_raw = (self._raw_subscribers > 0)
                want_ann = (self._annotated_subscribers > 0 or self._subscribers == 0)

            # 2. Render Raw JPEG only if raw subscribers exist
            raw_jpeg = None
            if want_raw:
                ret_raw, raw_buf = cv2.imencode(".jpg", display_mat, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                raw_jpeg = raw_buf.tobytes() if ret_raw else None

            # 3. Retrieve latest AI detections & tracking info
            annotated_jpeg = None
            from app.services.line_service import line_manager
            routed = line_manager.route(self.camera_id)
            if want_ann and routed and routed[1] == "qr":
                from app.services.qr_service import QRReaderPipeline
                annotated_mat = QRReaderPipeline.draw_reads(self.camera_id, display_mat, scale_x, scale_y, fps)
                ret_ann, ann_buf = cv2.imencode(".jpg", annotated_mat, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                annotated_jpeg = ann_buf.tobytes() if ret_ann else None
            elif want_ann:
                worker = CameraStreamPipeline.get_worker(self.camera_id)
                detections, latency_ms = worker.get_latest_inference()

                # 4. Draw annotations directly onto BGR display_mat
                engine, _, _ = line_manager.engine_for_camera(self.camera_id)
                annotated_mat = engine.draw_annotations_mat(
                    display_mat,
                    detections,
                    latency_ms=latency_ms,
                    draw_wirelines=True,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    fps=fps,
                    camera_id=self.camera_id,
                )

                # Codes a vision camera read beside its model are outlined on its video too.
                if routed and routed[1] == "vision" and routed[0].reads_codes(self.camera_id):
                    from app.services.qr_service import QRReaderPipeline
                    QRReaderPipeline.draw_reads(self.camera_id, annotated_mat, scale_x, scale_y, banner=False)

                # 5. Render Annotated JPEG (single encode per frame)
                ret_ann, ann_buf = cv2.imencode(".jpg", annotated_mat, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                annotated_jpeg = ann_buf.tobytes() if ret_ann else None

            # 6. Atomic update of cached frame bytes. A redraw that came out the
            # same as the picture already sent is not sent again.
            with self._lock:
                if annotated_jpeg is not None and annotated_jpeg != self._latest_annotated_jpeg:
                    self._latest_annotated_jpeg = annotated_jpeg
                    self._annotated_seq += 1
                if raw_jpeg is not None and raw_jpeg != self._latest_raw_jpeg:
                    self._latest_raw_jpeg = raw_jpeg
                    self._raw_seq += 1

            # Maintain smooth 30 FPS timing
            elapsed = time.perf_counter() - loop_start
            remainder = target_interval - elapsed
            if remainder > 0:
                time.sleep(remainder)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)


class CameraStreamPipeline:
    """Manages high-throughput zero-latency streaming pipelines for all connected cameras."""
    _workers: Dict[str, _CameraInferenceWorker] = {}
    _publishers: Dict[str, _CameraStreamPublisher] = {}
    _fps_trackers: Dict[str, collections.deque] = {}
    import threading
    _workers_lock = threading.Lock()

    @classmethod
    def get_worker(cls, camera_id: str) -> _CameraInferenceWorker:
        with cls._workers_lock:
            if camera_id not in cls._workers:
                cls._workers[camera_id] = _CameraInferenceWorker(camera_id)
            return cls._workers[camera_id]

    @classmethod
    def get_publisher(cls, camera_id: str) -> _CameraStreamPublisher:
        with cls._workers_lock:
            if camera_id not in cls._publishers:
                cls._publishers[camera_id] = _CameraStreamPublisher(camera_id)
            return cls._publishers[camera_id]

    @classmethod
    def remove_camera(cls, camera_id: str) -> None:
        with cls._workers_lock:
            worker = cls._workers.pop(camera_id, None)
            publisher = cls._publishers.pop(camera_id, None)
            cls._fps_trackers.pop(camera_id, None)
        if worker:
            worker.stop()
        if publisher:
            publisher.stop()
        from app.services.qr_service import QRReaderPipeline
        QRReaderPipeline.remove_camera(camera_id)

    @classmethod
    def stop_all(cls) -> None:
        with cls._workers_lock:
            workers = list(cls._workers.values())
            publishers = list(cls._publishers.values())
            cls._workers.clear()
            cls._publishers.clear()
            cls._fps_trackers.clear()
        for worker in workers:
            worker.stop()
        for publisher in publishers:
            publisher.stop()
        from app.services.qr_service import QRReaderPipeline
        QRReaderPipeline.stop_all()

    @classmethod
    async def stream_annotated_mjpeg(cls, camera_id: str) -> AsyncGenerator[bytes, None]:
        """
        High-efficiency async generator for MJPEG streaming.
        Yields pre-encoded JPEG frames from dedicated publisher thread.
        Includes Content-Length header for instant browser frame rendering without stutter.
        ZERO OpenCV work runs on the FastAPI asyncio event loop.
        """
        publisher = cls.get_publisher(camera_id)
        publisher.add_subscriber(stream_type="annotated")
        last_fid = -1

        try:
            while not app_state.shutting_down:
                driver = app_state.cameras.get(camera_id)
                if not driver or not getattr(driver, "is_connected", False):
                    break

                fid = publisher.get_latest_frame_id()
                jpeg = publisher.get_latest_annotated()

                if jpeg and fid != last_fid:
                    last_fid = fid
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode("ascii") + b"\r\n\r\n"
                    )
                    yield header + jpeg + b"\r\n"

                await asyncio.sleep(0.010)
        finally:
            publisher.remove_subscriber(stream_type="annotated")

    @classmethod
    async def stream_raw_mjpeg(cls, camera_id: str) -> AsyncGenerator[bytes, None]:
        """
        High-efficiency async generator for raw MJPEG streaming.
        """
        publisher = cls.get_publisher(camera_id)
        publisher.add_subscriber(stream_type="raw")
        last_fid = -1

        try:
            while not app_state.shutting_down:
                driver = app_state.cameras.get(camera_id)
                if not driver or not getattr(driver, "is_connected", False):
                    break

                fid = publisher.get_latest_frame_id("raw")
                jpeg = publisher.get_latest_raw()

                if jpeg and fid != last_fid:
                    last_fid = fid
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode("ascii") + b"\r\n\r\n"
                    )
                    yield header + jpeg + b"\r\n"

                await asyncio.sleep(0.010)
        finally:
            publisher.remove_subscriber(stream_type="raw")

    @classmethod
    def render_annotated_frame(
        cls,
        camera_id: str,
        max_width: int = 640,
        jpeg_quality: int = 70,
        conf_thresh: Optional[float] = None,
    ) -> Tuple[bool, Optional[bytes]]:
        """
        Returns annotated frame JPEG. Uses publisher cache first to avoid duplicate work.
        """
        publisher = cls.get_publisher(camera_id)
        cached = publisher.get_latest_annotated()
        if cached:
            return True, cached

        driver = app_state.cameras.get(camera_id)
        if not driver or not driver.is_connected:
            return False, None

        import cv2
        success, mat, fid = driver.grab_raw_frame()
        if not success or mat is None:
            return False, None

        orig_h, orig_w = mat.shape[:2]
        worker = cls.get_worker(camera_id)
        # grab_raw_frame() returned a private copy and nothing below draws on mat.
        worker.submit_frame_if_idle(mat, fid, conf_thresh, copy=False)
        detections, latency_ms = worker.get_latest_inference()

        now = time.time()
        with cls._workers_lock:
            if camera_id not in cls._fps_trackers:
                cls._fps_trackers[camera_id] = collections.deque(maxlen=30)
            dq = cls._fps_trackers[camera_id]
            dq.append(now)
            while dq and (now - dq[0]) > 2.0:
                dq.popleft()
            fps = (len(dq) - 1) / (now - dq[0]) if len(dq) > 1 and (now - dq[0]) > 0.05 else 0.0

        # mat is handed to the inference worker, so draw on a separate array.
        scale_x = 1.0
        scale_y = 1.0
        if max_width and max_width > 0 and orig_w > max_width:
            scale_x = max_width / float(orig_w)
            new_h = max(1, int(orig_h * scale_x))
            scale_y = new_h / float(orig_h)
            display_mat = cv2.resize(mat, (max_width, new_h), interpolation=cv2.INTER_LINEAR)
        else:
            display_mat = mat.copy()

        from app.services.line_service import line_manager
        engine, _, _ = line_manager.engine_for_camera(camera_id)
        annotated_mat = engine.draw_annotations_mat(
            display_mat,
            detections,
            latency_ms=latency_ms,
            draw_wirelines=True,
            scale_x=scale_x,
            scale_y=scale_y,
            fps=fps,
            camera_id=camera_id,
        )

        ret, buf = cv2.imencode(".jpg", annotated_mat, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
        if not ret:
            return False, None

        return True, buf.tobytes()

    @classmethod
    def render_raw_frame(
        cls,
        camera_id: str,
        max_width: int = 640,
        jpeg_quality: int = 70,
    ) -> Tuple[bool, Optional[bytes]]:
        """Fast raw video frame without annotations, encoded once."""
        publisher = cls.get_publisher(camera_id)
        cached = publisher.get_latest_raw()
        if cached:
            return True, cached

        driver = app_state.cameras.get(camera_id)
        if not driver or not driver.is_connected:
            return False, None

        import cv2
        success, mat, _ = driver.grab_raw_frame()
        if not success or mat is None:
            return False, None

        h, w = mat.shape[:2]
        if max_width and max_width > 0 and w > max_width:
            scale = max_width / float(w)
            new_h = max(1, int(h * scale))
            mat = cv2.resize(mat, (max_width, new_h), interpolation=cv2.INTER_LINEAR)

        ret, buf = cv2.imencode(".jpg", mat, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
        if not ret:
            return False, None

        return True, buf.tobytes()


class ContinuousVisionRunner:
    """
    Background runner that continuously feeds connected camera frames
    to their AI inference worker and counting pipeline 24/7.
    Ensures that object tracking, wireline crossing, defect detection, and MQTT/Modbus
    dispatching never stop when users navigate away from the video streaming page.
    """
    _thread: Optional[threading.Thread] = None
    _stop_event: threading.Event = threading.Event()
    _running: bool = False
    _lock = threading.Lock()

    @classmethod
    def start(cls) -> None:
        with cls._lock:
            if cls._running:
                return
            cls._stop_event.clear()
            cls._running = True
            cls._thread = threading.Thread(
                target=run_supervised,
                args=("ContinuousVisionRunner", cls._loop, cls._stop_event),
                kwargs={"kind": "vision_runner"},
                name="ContinuousVisionRunner",
                daemon=True,
            )
            cls._thread.start()
            logger.info("Continuous 24/7 Server Vision & Counting Runner started")

    @classmethod
    def stop(cls) -> None:
        with cls._lock:
            cls._stop_event.set()
            cls._running = False
            if cls._thread and cls._thread.is_alive():
                cls._thread.join(timeout=1.0)
                cls._thread = None
            logger.info("Continuous Server Vision Runner stopped")

    @classmethod
    def _loop(cls) -> None:
        # The last frame handed over per camera, as (driver, frame id): frame ids
        # start again at 1 when a camera is reconnected, and a still picture's
        # only frame must not be mistaken for the one already processed.
        last_submitted_fids: Dict[str, Tuple[Any, int]] = {}
        # Frames handed to the code reader, kept apart: a vision camera with
        # Read codes on feeds its model and its code reader independently.
        last_code_fids: Dict[str, Tuple[Any, int]] = {}
        while not cls._stop_event.is_set():
            cams = list(app_state.cameras.items())
            if not cams:
                time.sleep(0.04)
                continue

            from app.services.line_service import line_manager
            for camera_id, driver in cams:
                if not getattr(driver, "is_connected", False):
                    continue
                try:
                    # Each camera feeds the production line that owns it. A camera no
                    # line claims, or one on a stopped line, is streamed but not processed.
                    routed = line_manager.route(camera_id)
                    if routed is None or not routed[0].enabled:
                        continue
                    cur_fid = driver.get_latest_frame_id() if hasattr(driver, "get_latest_frame_id") else getattr(driver, "_latest_frame_id", 0)
                    if cur_fid == 0:
                        continue

                    runtime, role = routed
                    # Codes: a QR reader, or a vision camera with Read codes on. A camera
                    # set to one picture per product decodes on a wire line crossing instead.
                    if (role == "qr" or runtime.reads_codes(camera_id)) and not runtime.qr_triggered(camera_id) \
                            and (driver, cur_fid) != last_code_fids.get(camera_id):
                        from app.services.qr_service import QRReaderPipeline
                        reader = QRReaderPipeline.get_worker(camera_id)
                        if not reader.is_busy:
                            if hasattr(driver, "get_latest_raw_mat"):
                                success, mat, fid = driver.get_latest_raw_mat(copy=False)
                            else:
                                success, mat, fid = driver.grab_raw_frame()
                            if success and mat is not None and reader.submit_frame_if_idle(mat, fid):
                                last_code_fids[camera_id] = (driver, fid)
                    if role == "qr" or (driver, cur_fid) == last_submitted_fids.get(camera_id):
                        continue

                    # Leave the frame for the next pass while inference is running,
                    # so the newest frame goes in as soon as the worker is free.
                    worker = CameraStreamPipeline.get_worker(camera_id)
                    if worker.is_busy:
                        continue

                    # Inference only reads the frame, so take the driver's latest
                    # frame without copying it; drivers replace it, never modify it.
                    if hasattr(driver, "get_latest_raw_mat"):
                        success, mat, fid = driver.get_latest_raw_mat(copy=False)
                    else:
                        success, mat, fid = driver.grab_raw_frame()
                    if success and mat is not None and (driver, fid) != last_submitted_fids.get(camera_id):
                        # Runs AI inference + counting tracker on the worker thread
                        if worker.submit_frame_if_idle(mat, fid, copy=False):
                            last_submitted_fids[camera_id] = (driver, fid)
                except Exception as ex:
                    _error_log.log(
                        logger, logging.WARNING, ("runner", camera_id),
                        "Could not hand the frame of camera %s to its worker: %s: %s", camera_id, type(ex).__name__, ex,
                    )

            # Polling loop (~100 Hz) with zero-copy frame ID check
            time.sleep(0.010)


class ApiInferenceBusy(RuntimeError):
    """Too many on-demand detection requests are already waiting for the model."""


class _ApiInferenceGate:
    """Queue for inference asked for through the API.

    The detect endpoints and the annotated-frame fallback share the model's lock
    with the 24/7 counting pipeline. Without a queue, eight clients calling
    detect in a loop took most of the model's time and live counting fell from
    about 23 to 3 frames per second. While a camera is connected, API requests
    here take turns one at a time, in arrival order, and get at most
    API_INFERENCE_MAX_SHARE of the model's time. With no camera connected there
    is no counting to protect, so up to _PARALLEL_WITHOUT_CAMERAS run at once.
    Requests past API_INFERENCE_QUEUE_LIMIT waiting get ApiInferenceBusy
    instead of piling up. Callers grab their frame and finish any database
    work before queueing, so waiting requests hold no pooled connection.
    """

    _PARALLEL_WITHOUT_CAMERAS = 4
    # A one-off slow call (a model's first run on a GPU can take seconds) is
    # charged at most this much, so it cannot hold up the queue for long.
    _MAX_CHARGE_SECONDS = 0.5

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._active = 0
        self._waiters: collections.deque = collections.deque()
        self._next_start = 0.0

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._active = loop, 0
            self._waiters.clear()
        return loop

    @staticmethod
    def _pipeline_running() -> bool:
        return any(getattr(driver, "is_connected", False) for driver in list(app_state.cameras.values()))

    def _capacity(self) -> int:
        return 1 if self._pipeline_running() else self._PARALLEL_WITHOUT_CAMERAS

    def check_capacity(self) -> None:
        """Raise ApiInferenceBusy now if a new request would have to be turned away."""
        limit = settings.API_INFERENCE_QUEUE_LIMIT
        if limit > 0 and self._active >= self._capacity() and len(self._waiters) >= limit:
            raise ApiInferenceBusy("Too many detection requests are waiting for the model; retry shortly")

    async def _acquire(self) -> None:
        loop = self._bind_loop()
        if self._active < self._capacity() and not self._waiters:
            self._active += 1
            return
        self.check_capacity()
        turn = loop.create_future()
        self._waiters.append(turn)
        try:
            await turn
        except BaseException:
            if turn.done() and not turn.cancelled():
                self._release()  # given a turn just as the request went away
            else:
                with contextlib.suppress(ValueError):
                    self._waiters.remove(turn)
            raise

    def _release(self) -> None:
        self._active = max(0, self._active - 1)
        while self._waiters and self._active < self._capacity():
            turn = self._waiters.popleft()
            if not turn.done():
                self._active += 1
                turn.set_result(None)

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[Callable[..., Any]]:
        """Wait for a turn; yields run(fn, *args), which runs fn in a worker thread."""
        await self._acquire()

        async def run(fn: Callable[..., Any], *args: Any) -> Any:
            model_seconds: Optional[float] = None

            def timed() -> Any:
                nonlocal model_seconds
                begin_model_timing()
                try:
                    return fn(*args)
                finally:
                    model_seconds = end_model_timing()

            start = time.perf_counter()
            try:
                return await asyncio.to_thread(timed)
            finally:
                share = settings.API_INFERENCE_MAX_SHARE
                if share < 1.0 and self._pipeline_running():
                    # Only the time this request held the model counts; engines
                    # that do not report it are charged their wall time.
                    used = model_seconds if model_seconds is not None else time.perf_counter() - start
                    used = min(used, self._MAX_CHARGE_SECONDS)
                    # Leave the model to the cameras for (1 - share) / share of
                    # that time before the next API request starts.
                    self._next_start = max(self._next_start, time.monotonic() + used * (1.0 - share) / share)

        try:
            if self._pipeline_running():
                delay = self._next_start - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            yield run
        finally:
            self._release()


api_inference_gate = _ApiInferenceGate()


@contextlib.asynccontextmanager
async def _unqueued() -> AsyncIterator[Callable[..., Any]]:
    async def run(fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    yield run


class VisionService:
    @staticmethod
    async def detect_image(
        db: AsyncSession,
        image_bytes: bytes,
        conf_threshold: Optional[float] = None,
        nms_threshold: Optional[float] = None,
        model_id: Optional[str] = None,
    ) -> DetectionResponse:
        """model_id: the model to run. Without it, the model of Line 1's counting
        camera (what version 1 called the active model)."""
        from app.services.line_service import line_manager
        engine, model_name, model_id = await line_manager.api_engine(model_id)
        async with api_inference_gate.slot() as run:
            response = await run(engine.predict, image_bytes, conf_threshold, nms_threshold)

        log = DetectionLog(
            model_id=model_id,
            model_name=model_name,
            total_detections=response.total_detections,
            detections=[d.model_dump() for d in response.detections],
            inference_time_ms=response.inference_time_ms,
        )
        db.add(log)
        await db.commit()

        app_state.detection_count += response.total_detections
        app_state.processed_frames += 1
        return response

    @staticmethod
    async def detect_live_camera(
        db: AsyncSession,
        camera_id: str,
        conf_threshold: Optional[float] = None,
        nms_threshold: Optional[float] = None,
        queued: bool = True,
        model_id: Optional[str] = None,
    ) -> LiveInspectionResponse:
        """queued=False is for production triggers (photo-eye or PLC): they skip
        the API queue so they never wait behind ad-hoc detect calls.
        model_id: the model to run. Without it, the camera's own model, else
        the model of Line 1's counting camera."""
        if queued:
            api_inference_gate.check_capacity()
        # Grab the frame at request time, before any wait for the model. A
        # connected camera is read from memory; reconnecting an offline one
        # commits its own database work, so waiting holds no connection.
        cam_start = time.perf_counter()
        success, mat, fid, error = await CameraService.grab_raw_frame(db, camera_id)
        if not success or mat is None:
            raise ValueError(error or f"Failed to acquire frame from camera {camera_id}")
        acq_ms = round((time.perf_counter() - cam_start) * 1000, 2)

        from app.services.line_service import line_manager
        engine, model_name, model_id = await line_manager.api_engine(model_id, camera_id)
        async with (api_inference_gate.slot() if queued else _unqueued()) as run:
            det_response = await run(engine.predict_mat, mat, conf_threshold, nms_threshold)

        camera = await CameraService.get_camera_by_id(db, camera_id)
        camera_name = camera.name if camera else "Camera"

        total_latency = round(acq_ms + det_response.inference_time_ms, 2)
        # A defect is what the camera's own counting settings call one (Line setup).
        from app.engines.tracker import is_defect_class
        config = line_manager.counter_for_camera(camera_id).config
        has_defect = any(
            is_defect_class(d.class_name, config.defect_classes, config.name_based_defects)
            for d in det_response.detections
        )

        log = DetectionLog(
            camera_id=camera_id,
            model_id=model_id,
            model_name=model_name,
            total_detections=det_response.total_detections,
            detections=[d.model_dump() for d in det_response.detections],
            inference_time_ms=det_response.inference_time_ms,
        )
        db.add(log)
        await db.commit()

        app_state.detection_count += det_response.total_detections
        app_state.processed_frames += 1

        return LiveInspectionResponse(
            camera_id=camera_id,
            camera_name=camera_name,
            model_name=model_name,
            total_detections=det_response.total_detections,
            detections=det_response.detections,
            inference_time_ms=det_response.inference_time_ms,
            acquisition_time_ms=acq_ms,
            total_latency_ms=total_latency,
            passed=not has_defect,
        )

    @staticmethod
    async def get_annotated_frame(
        db: AsyncSession,
        camera_id: str,
        conf_threshold: Optional[float] = None,
        max_width: Optional[int] = None,
    ) -> bytes:
        """max_width: send the picture no wider than this (a small tile needs no full picture)."""
        driver = app_state.cameras.get(camera_id)
        if not driver or not getattr(driver, "is_connected", False):
            raise ValueError(f"Camera {camera_id} is offline")
        # Check publisher cache first for instant lock-free retrieval
        publisher = CameraStreamPipeline.get_publisher(camera_id)
        if max_width:
            cached, ready = publisher.get_latest_annotated_scaled(max_width)
            if ready:
                return ready
            if cached:
                scaled = await asyncio.to_thread(shrink_jpeg, cached, max_width)
                publisher.store_scaled(max_width, cached, scaled)
                return scaled
        else:
            cached = publisher.get_latest_annotated()
            if cached:
                return cached

        api_inference_gate.check_capacity()
        success, mat, fid, error = await CameraService.grab_raw_frame(db, camera_id)
        if not success or mat is None:
            raise ValueError(error or "Frame grab failed")

        from app.services.line_service import line_manager
        engine, _, _ = line_manager.engine_for_camera(camera_id)

        def _infer_and_render() -> bytes:
            # Inference, drawing and JPEG encoding all run off the event loop.
            import cv2
            if getattr(engine, "is_loaded", True):
                det_response = engine.predict_mat(mat, conf_threshold)
                detections, latency_ms = det_response.detections, det_response.inference_time_ms
            else:
                detections, latency_ms = [], 0.0  # no model runs on this camera
            annotated_mat = engine.draw_annotations_mat(mat, detections, latency_ms, camera_id=camera_id)
            _, jpeg = cv2.imencode(".jpg", annotated_mat, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            return shrink_jpeg(jpeg.tobytes(), max_width) if max_width else jpeg.tobytes()

        async with api_inference_gate.slot() as run:
            return await run(_infer_and_render)

    @staticmethod
    async def get_detection_history(db: AsyncSession, limit: int = 50) -> List[DetectionLog]:
        stmt = select(DetectionLog).order_by(DetectionLog.created_at.desc()).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())
