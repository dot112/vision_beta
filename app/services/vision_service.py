from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import threading
import time
from typing import Any, AsyncGenerator, AsyncIterator, Callable, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


from app.config import settings
from app.db.models.detection import DetectionLog
from app.engines.inference_engine import InferenceEngine, begin_model_timing, end_model_timing
from app.schemas.vision import DetectionResponse, LiveInspectionResponse
from app.services.camera_service import CameraService
from app.state.application_state import app_state
from app.utils.logger import get_logger

logger = get_logger(__name__)

COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake",
    "chair", "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop",
    "mouse", "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush"
]

DEFAULT_ONNX_CANDIDATES = [
    "model_store/yolov8n/v1.0/yolov8n.onnx",
    "model_store/model/v1.0/model.onnx",
    "model_store/yolov8n_classes/v1.0/yolov8n.onnx",
]
DEFAULT_ONNX_PATH = next((p for p in DEFAULT_ONNX_CANDIDATES if os.path.exists(p)), "model_store/yolov8n/v1.0/yolov8n.onnx")

# Initialize default engine with YOLOv8n if file exists
_default_engine = InferenceEngine(device=settings.INFERENCE_DEVICE)


def _get_active_engine() -> Tuple[InferenceEngine, str, Optional[str]]:
    """Returns (engine_instance, model_name, model_id)."""
    if app_state.active_model and "engine" in app_state.active_model:
        return (
            app_state.active_model["engine"],
            app_state.active_model.get("name", "ActiveModel"),
            app_state.active_model.get("id"),
        )
    return _default_engine, "YOLOv8n_COCO" if _default_engine.is_loaded else "Default_Industrial_Vision", None


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

        self._thread = threading.Thread(
            target=self._worker_loop,
            name=f"InferenceWorker-{camera_id[:8]}",
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
                engine, _, _ = _get_active_engine()
                det_response = engine.predict_mat(mat, conf_threshold=conf)

                # Update real-time object tracking & wireline counters
                try:
                    driver = app_state.cameras.get(self.camera_id)
                    s = getattr(driver, "settings", {}) if driver else {}
                    c_rot = int(s.get("rotation") or 90) if "rotation" in s else 90
                    c_fliph = bool(s.get("flip_h", False))
                    c_flipv = bool(s.get("flip_v", False))

                    from app.services.counting_service import counting_service
                    counting_service.process_frame(
                        det_response.detections,
                        det_response.image_width,
                        det_response.image_height,
                        camera_rotation=c_rot,
                        camera_flip_h=c_fliph,
                        camera_flip_v=c_flipv,
                        camera_id=self.camera_id,
                    )
                except Exception as trk_err:
                    logger.debug("Counting tracker process error: %s", trk_err)

                with self._lock:
                    self._latest_detections = det_response.detections
                    self._latest_inference_ms = det_response.inference_time_ms
                    self._processed_frame_id = fid
                    app_state.detection_count += det_response.total_detections
                    app_state.processed_frames += 1
            except Exception as exc:
                logger.debug("Async inference error on cam %s: %s", self.camera_id, exc)
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
        self._latest_frame_id: int = 0
        self._fps_tracker: collections.deque = collections.deque(maxlen=30)
        self._current_fps: float = 0.0

        self._thread = threading.Thread(
            target=self._publisher_loop,
            name=f"StreamPub-{camera_id[:8]}",
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

    def get_latest_frame_id(self) -> int:
        with self._lock:
            return self._latest_frame_id

    def _publisher_loop(self) -> None:
        import cv2

        last_rendered_fid = -1
        target_interval = 1.0 / 30.0  # 30 FPS target

        while not self._stop_event.is_set():
            loop_start = time.perf_counter()

            # If no active viewers, sleep and avoid wasting CPU/GPU
            if not self.has_subscribers:
                time.sleep(0.1)
                continue

            driver = app_state.cameras.get(self.camera_id)
            if not driver or not getattr(driver, "is_connected", False):
                time.sleep(0.1)
                continue

            # Check if camera has a fresh frame
            cur_fid = driver.get_latest_frame_id() if hasattr(driver, "get_latest_frame_id") else getattr(driver, "_latest_frame_id", 0)
            if cur_fid == last_rendered_fid or cur_fid == 0:
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
            orig_h, orig_w = mat.shape[:2]

            # Calculate actual video publishing FPS
            now = time.time()
            self._fps_tracker.append(now)
            while self._fps_tracker and (now - self._fps_tracker[0]) > 2.0:
                self._fps_tracker.popleft()
            fps = (len(self._fps_tracker) - 1) / (now - self._fps_tracker[0]) if len(self._fps_tracker) > 1 and (now - self._fps_tracker[0]) > 0.05 else 0.0
            self._current_fps = fps

            # 1. Bandwidth optimization: resize display copy (default 640px max width).
            # mat is the camera's own buffer, so draw only on a private array: the
            # resize output, or a copy when no resize is needed.
            max_width = 640
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
                ret_raw, raw_buf = cv2.imencode(".jpg", display_mat, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                raw_jpeg = raw_buf.tobytes() if ret_raw else None

            # 3. Retrieve latest AI detections & tracking info
            annotated_jpeg = None
            if want_ann:
                worker = CameraStreamPipeline.get_worker(self.camera_id)
                detections, latency_ms = worker.get_latest_inference()

                # 4. Draw annotations directly onto BGR display_mat
                engine, _, _ = _get_active_engine()
                s = getattr(driver, "settings", {}) if driver else {}
                c_rot = int(s.get("rotation") or 90) if "rotation" in s else 90
                c_fliph = bool(s.get("flip_h", False))
                c_flipv = bool(s.get("flip_v", False))

                annotated_mat = engine.draw_annotations_mat(
                    display_mat,
                    detections,
                    latency_ms=latency_ms,
                    draw_wirelines=True,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    fps=fps,
                    camera_rotation=c_rot,
                    camera_flip_h=c_fliph,
                    camera_flip_v=c_flipv,
                    camera_id=self.camera_id,
                )

                # 5. Render Annotated JPEG (single encode per frame)
                ret_ann, ann_buf = cv2.imencode(".jpg", annotated_mat, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                annotated_jpeg = ann_buf.tobytes() if ret_ann else None

            # 6. Atomic update of cached frame bytes
            with self._lock:
                if annotated_jpeg is not None:
                    self._latest_annotated_jpeg = annotated_jpeg
                if raw_jpeg is not None:
                    self._latest_raw_jpeg = raw_jpeg
                self._latest_frame_id = fid

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
            while True:
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
            while True:
                driver = app_state.cameras.get(camera_id)
                if not driver or not getattr(driver, "is_connected", False):
                    break

                fid = publisher.get_latest_frame_id()
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

        s = getattr(driver, "settings", {}) if driver else {}
        c_rot = int(s.get("rotation") or 90) if "rotation" in s else 90
        c_fliph = bool(s.get("flip_h", False))
        c_flipv = bool(s.get("flip_v", False))

        engine, _, _ = _get_active_engine()
        annotated_mat = engine.draw_annotations_mat(
            display_mat,
            detections,
            latency_ms=latency_ms,
            draw_wirelines=True,
            scale_x=scale_x,
            scale_y=scale_y,
            fps=fps,
            camera_rotation=c_rot,
            camera_flip_h=c_fliph,
            camera_flip_v=c_flipv,
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
                target=cls._loop,
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
        last_submitted_fids: Dict[str, int] = {}
        while not cls._stop_event.is_set():
            cams = list(app_state.cameras.items())
            if not cams:
                time.sleep(0.04)
                continue

            for camera_id, driver in cams:
                if not getattr(driver, "is_connected", False):
                    continue
                try:
                    cur_fid = driver.get_latest_frame_id() if hasattr(driver, "get_latest_frame_id") else getattr(driver, "_latest_frame_id", 0)
                    if cur_fid == last_submitted_fids.get(camera_id, 0) or cur_fid == 0:
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
                    if success and mat is not None and fid != last_submitted_fids.get(camera_id, 0):
                        # Runs AI inference + counting tracker on the worker thread
                        if worker.submit_frame_if_idle(mat, fid, copy=False):
                            last_submitted_fids[camera_id] = fid
                except Exception as ex:
                    logger.debug("Continuous runner error for cam %s: %s", camera_id, ex)

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
    ) -> DetectionResponse:
        engine, model_name, model_id = _get_active_engine()
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
    ) -> LiveInspectionResponse:
        """queued=False is for production triggers (photo-eye or PLC): they skip
        the API queue so they never wait behind ad-hoc detect calls."""
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

        engine, model_name, model_id = _get_active_engine()
        async with (api_inference_gate.slot() if queued else _unqueued()) as run:
            det_response = await run(engine.predict_mat, mat, conf_threshold, nms_threshold)

        camera = await CameraService.get_camera_by_id(db, camera_id)
        camera_name = camera.name if camera else "Camera"

        total_latency = round(acq_ms + det_response.inference_time_ms, 2)
        has_defect = any("defect" in d.class_name.lower() or "scratch" in d.class_name.lower() for d in det_response.detections)

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
    ) -> bytes:
        driver = app_state.cameras.get(camera_id)
        if not driver or not getattr(driver, "is_connected", False):
            raise ValueError(f"Camera {camera_id} is offline")
        # Check publisher cache first for instant lock-free retrieval
        publisher = CameraStreamPipeline.get_publisher(camera_id)
        cached = publisher.get_latest_annotated()
        if cached:
            return cached

        api_inference_gate.check_capacity()
        success, mat, fid, error = await CameraService.grab_raw_frame(db, camera_id)
        if not success or mat is None:
            raise ValueError(error or "Frame grab failed")

        engine, _, _ = _get_active_engine()

        def _infer_and_render() -> bytes:
            # Inference, drawing and JPEG encoding all run off the event loop.
            import cv2
            det_response = engine.predict_mat(mat, conf_threshold)
            annotated_mat = engine.draw_annotations_mat(
                mat, det_response.detections, det_response.inference_time_ms, camera_id=camera_id
            )
            _, jpeg = cv2.imencode(".jpg", annotated_mat, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            return jpeg.tobytes()

        async with api_inference_gate.slot() as run:
            return await run(_infer_and_render)

    @staticmethod
    async def get_detection_history(db: AsyncSession, limit: int = 50) -> List[DetectionLog]:
        stmt = select(DetectionLog).order_by(DetectionLog.created_at.desc()).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())
