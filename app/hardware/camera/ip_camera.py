from __future__ import annotations

import os
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from app.hardware.camera.base import BaseCamera
from app.utils.logger import get_logger

logger = get_logger(__name__)

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None


class IPCamera(BaseCamera):
    """
    High-Performance Zero-Latency Driver for IP Cameras (RTSP, RTMP, HTTP MJPEG, IP Webcam, DroidCam).
    Features:
    - Dedicated background reader thread to drain FFmpeg buffers and eliminate lag.
    - Zero-copy freshest frame distribution (0ms capture latency).
    - Automatic connection recovery and stream health watchdog.
    - Direct BGR numpy array access (grab_raw_frame) avoiding multi-pass JPEG encoding.
    """

    def __init__(self, camera_id: str, name: str, source: str, settings: Optional[Dict[str, Any]] = None):
        super().__init__(camera_id=camera_id, name=name, source=source, settings=settings)
        self._cap: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._new_frame_event = threading.Event()

        # Thread-safe double buffer for freshest frame
        self._latest_raw_mat: Optional[Any] = None
        self._latest_processed_mat: Optional[Any] = None
        self._latest_jpeg: Optional[bytes] = None
        self._latest_frame_id: int = 0
        self._latest_jpeg_frame_id: int = 0
        self._latest_timestamp: float = 0.0
        self._consecutive_failures: int = 0
        self._active_url: Optional[str] = None

    def _get_candidate_urls(self) -> List[str]:
        raw = (self.source or self.settings.get("url", "")).strip()
        if not raw:
            return []

        if "://" not in raw:
            if any(p in raw for p in (":8080", ":8000", ":8088", ":80", "http")):
                url = f"http://{raw}"
            else:
                url = f"rtsp://{raw}"
        else:
            url = raw

        username = self.settings.get("username")
        password = self.settings.get("password")
        if username and password and "@" not in url:
            parsed = urllib.parse.urlsplit(url)
            netloc = f"{urllib.parse.quote(username)}:{urllib.parse.quote(password)}@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            url = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc
        path = parsed.path

        candidates: List[str] = []
        if scheme in ("http", "https"):
            if not path or path == "/":
                candidates.extend([
                    f"{scheme}://{netloc}/video",
                    f"{scheme}://{netloc}/mjpegfeed",
                    f"{scheme}://{netloc}/shot.jpg",
                    f"{scheme}://{netloc}/photo.jpg",
                    f"{scheme}://{netloc}/live",
                    url,
                ])
            else:
                candidates.append(url)
                if not path.endswith("/video"):
                    candidates.append(f"{scheme}://{netloc}/video")
        elif scheme == "rtsp":
            candidates.append(url)
            if not path or path == "/":
                candidates.extend([
                    f"rtsp://{netloc}/h264_pcm.sdp",
                    f"rtsp://{netloc}/live",
                    f"rtsp://{netloc}/stream1",
                ])
        else:
            candidates.append(url)

        seen = set()
        result: List[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                result.append(c)
        return result

    def _open_capture(self, stream_url: str) -> Optional[Any]:
        if cv2 is None:
            return None

        transport = self.settings.get("transport", "tcp").lower()
        buffer_size = int(self.settings.get("buffer_size", 1))

        # Set aggressive low-latency FFmpeg parameters
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
            f"rtsp_transport;{transport}|"
            "fflags;nobuffer|flags;low_delay|max_delay;0|probesize;32768|analyzeduration;100000|timeout;2500000"
        )

        try:
            cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, buffer_size)
            return cap
        except Exception as e:
            logger.debug("Failed opening stream %s: %s", stream_url, e)
            return None

    def connect(self) -> bool:
        if cv2 is None:
            self.last_error = "OpenCV is not installed."
            logger.error(self.last_error)
            return False

        candidates = self._get_candidate_urls()
        if not candidates:
            self.last_error = "No valid IP camera stream URL provided."
            return False

        if self.is_connected:
            self.disconnect()

        self._stop_event.clear()
        tested_errors: List[str] = []

        for stream_url in candidates:
            try:
                logger.info("Connecting IP camera '%s' via endpoint: %s", self.name, stream_url)
                cap = self._open_capture(stream_url)
                if cap and cap.isOpened():
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        self._cap = cap
                        self._active_url = stream_url
                        self.is_connected = True
                        self.last_error = None
                        self.settings["active_stream_url"] = stream_url

                        # Store initial frame
                        with self._lock:
                            self._latest_raw_mat = frame
                            self._latest_processed_mat = self._process_frame(frame)
                            self._latest_frame_id = 1
                            self._latest_timestamp = time.monotonic()
                            self._latest_jpeg = None

                        # Start dedicated continuous background capture thread
                        self._thread = threading.Thread(
                            target=self._reader_loop,
                            name=f"IPCamReader-{self.camera_id[:8]}",
                            daemon=True,
                        )
                        self._thread.start()

                        logger.info("Successfully connected IP camera '%s' (low-latency worker active): %s", self.name, stream_url)
                        return True
                    else:
                        cap.release()
                        tested_errors.append(f"{stream_url} (no frame received)")
                else:
                    if cap:
                        cap.release()
                    tested_errors.append(f"{stream_url} (could not open)")
            except Exception as candidate_err:
                tested_errors.append(f"{stream_url} ({candidate_err})")

        self.last_error = f"Failed to connect to IP camera '{self.name}'. Tested endpoints: {', '.join(candidates[:3])}. Ensure the camera/app is running."
        self.is_connected = False
        logger.warning("IP camera '%s' failed to connect. Errors: %s", self.name, tested_errors)
        return False

    def _reader_loop(self) -> None:
        """
        Ultra-fast background reader thread.
        Continuously drains incoming video stream packets from network socket
        so OpenCV FFmpeg internal queue NEVER accumulates stale buffered frames.
        """
        logger.debug("IP camera reader loop started for %s", self.name)
        consecutive_errs = 0

        while not self._stop_event.is_set():
            if self._cap is None or not self._cap.isOpened():
                if not self._reconnect_in_background():
                    time.sleep(0.5)
                continue

            try:
                # Fast grab + retrieve
                grabbed = self._cap.grab()
                if not grabbed:
                    consecutive_errs += 1
                    if consecutive_errs > 30:
                        logger.warning("IP camera '%s' stream stalled, reconnecting...", self.name)
                        self._reconnect_in_background()
                        consecutive_errs = 0
                    else:
                        time.sleep(0.01)
                    continue

                ret, frame = self._cap.retrieve()
                if not ret or frame is None:
                    consecutive_errs += 1
                    time.sleep(0.01)
                    continue

                consecutive_errs = 0
                now = time.monotonic()

                # Process ROI, rotation, flip in-place or fast path
                processed = self._process_frame(frame)

                with self._lock:
                    self._latest_raw_mat = frame
                    self._latest_processed_mat = processed
                    self._latest_frame_id += 1
                    self._latest_timestamp = now
                    # Invalidate cached JPEG
                    self._latest_jpeg = None
                self._new_frame_event.set()

            except Exception as e:
                consecutive_errs += 1
                if consecutive_errs > 20:
                    time.sleep(0.1)

        logger.debug("IP camera reader loop stopped for %s", self.name)

    def _reconnect_in_background(self) -> bool:
        if self._stop_event.is_set() or not self._active_url:
            return False

        try:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None

            logger.info("Watchdog reconnecting to %s for camera '%s'...", self._active_url, self.name)
            cap = self._open_capture(self._active_url)
            if cap and cap.isOpened():
                ret, _ = cap.read()
                if ret:
                    self._cap = cap
                    self.is_connected = True
                    self.last_error = None
                    return True
                else:
                    cap.release()
        except Exception as e:
            logger.debug("Reconnect error: %s", e)

        return False

    def disconnect(self) -> None:
        self.is_connected = False
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
            self._thread = None

        if self._cap is not None:
            try:
                self._cap.release()
            except Exception as exc:
                logger.warning("Error releasing IP camera stream: %s", exc)
            self._cap = None

        with self._lock:
            self._latest_raw_mat = None
            self._latest_processed_mat = None
            self._latest_jpeg = None

        logger.info("IP camera '%s' disconnected cleanly", self.name)

    def get_latest_frame_id(self) -> int:
        return self._latest_frame_id

    def get_latest_raw_mat(self, copy: bool = True) -> Tuple[bool, Optional[Any], int]:
        if not self.is_connected or self._lock is None:
            return False, None, 0

        with self._lock:
            if self._latest_processed_mat is None:
                return False, None, 0
            mat = self._latest_processed_mat.copy() if copy else self._latest_processed_mat
            return True, mat, self._latest_frame_id

    def wait_for_new_frame(self, timeout: float = 0.1) -> bool:
        if self._new_frame_event is None or not self.is_connected:
            return False
        signaled = self._new_frame_event.wait(timeout=timeout)
        if signaled:
            self._new_frame_event.clear()
        return signaled

    def grab_raw_frame(self) -> Tuple[bool, Optional[Any], int]:
        """
        Instantly returns the latest BGR frame mat and monotonic frame ID with ZERO network delay.
        """
        return self.get_latest_raw_mat(copy=True)

    def grab_frame(self) -> Tuple[bool, Optional[bytes]]:
        """
        Returns latest JPEG bytes (with internal single-pass caching per frame ID).
        """
        if not self.is_connected or cv2 is None:
            return False, None

        with self._lock:
            if self._latest_processed_mat is None:
                return False, None

            # Fast path: return cached JPEG if already compressed for this frame_id
            if self._latest_jpeg is not None and self._latest_jpeg_frame_id == self._latest_frame_id:
                return True, self._latest_jpeg

            mat = self._latest_processed_mat
            cur_fid = self._latest_frame_id

        try:
            q = int(self.settings.get("stream_jpeg_quality", 75))
            ret, buffer = cv2.imencode(".jpg", mat, [int(cv2.IMWRITE_JPEG_QUALITY), q])
            if not ret:
                return False, None

            jpeg_bytes = buffer.tobytes()
            with self._lock:
                self._latest_jpeg = jpeg_bytes
                self._latest_jpeg_frame_id = cur_fid

            return True, jpeg_bytes
        except Exception as exc:
            self.last_error = f"IP camera encode exception: {exc}"
            return False, None

    def get_properties(self) -> Dict[str, Any]:
        with self._lock:
            has_frame = self._latest_processed_mat is not None
            w = self._latest_processed_mat.shape[1] if has_frame else 0
            h = self._latest_processed_mat.shape[0] if has_frame else 0

        return {
            "source": self.source,
            "connected": self.is_connected,
            "width": w,
            "height": h,
            "fps": 30.0,
            "backend": "cv2.CAP_FFMPEG (Threaded 0-Lag)",
            "transport": self.settings.get("transport", "tcp"),
            "active_stream_url": self._active_url or self.source,
        }

    def set_properties(self, properties: Dict[str, Any]) -> bool:
        self.settings.update(properties)
        return True
