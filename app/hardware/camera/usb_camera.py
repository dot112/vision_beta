from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings as app_settings
from app.hardware.camera.base import BaseCamera
from app.utils.logger import LogThrottle, get_logger
from app.utils.threads import run_supervised

logger = get_logger(__name__)
# A device that stays unplugged is reported once a minute while it is retried.
_reconnect_log = LogThrottle(60.0)

try:
    import cv2
except ImportError:
    cv2 = None


class USBCamera(BaseCamera):
    """
    High-Performance Zero-Latency Driver for USB / UVC / CSI cameras using OpenCV.
    Features:
    - Threaded background capture worker for zero-lag real-time streaming.
    - Direct BGR numpy array access (grab_raw_frame).
    """

    # About 2 s of failed reads (10 ms apart) before the device is treated as gone and reopened.
    REOPEN_AFTER_FAILED_READS = 200

    def __init__(self, camera_id: str, name: str, source: str, settings: Optional[Dict[str, Any]] = None):
        super().__init__(camera_id=camera_id, name=name, source=source, settings=settings)
        try:
            self.device_index = int(self.source)
        except ValueError:
            self.device_index = int(self.settings.get("device_index", 0))
        self._cap: Optional[Any] = None
        self._thread: Optional[Any] = None
        self._stop_event = None
        self._lock = None
        self._latest_raw_mat = None
        self._latest_processed_mat = None
        self._latest_jpeg = None
        self._latest_frame_id = 0
        self._latest_jpeg_frame_id = 0

        import threading
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._new_frame_event = threading.Event()

    def _open_device(self) -> Optional[Any]:
        """Open the device and read one frame. Returns the capture, or None (see last_error)."""
        # On Windows, prefer DirectShow for fast opening and reliable resolution negotiation
        backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.device_index, backend)

        if not cap.isOpened():
            # Fallback to default backend if DSHOW fails
            cap.release()
            cap = cv2.VideoCapture(self.device_index)

        if not cap.isOpened():
            cap.release()
            self.last_error = f"Failed to open USB camera at index {self.device_index}"
            return None

        # Apply initial settings
        self._apply_properties(cap, self.settings)

        # Test grab a single frame to verify feed
        ret, frame = cap.read()
        if not ret or frame is None:
            cap.release()
            self.last_error = f"Camera opened at index {self.device_index} but could not read frame."
            return None

        with self._lock:
            self._latest_raw_mat = frame
            self._latest_processed_mat = self._process_frame(frame)
            self._latest_frame_id += 1
            self._latest_jpeg = None
        return cap

    def connect(self) -> bool:
        if cv2 is None:
            self.last_error = "OpenCV (cv2) is not installed."
            logger.error(self.last_error)
            return False

        try:
            if self.is_connected:
                self.disconnect()

            cap = self._open_device()
            if cap is None:
                self.is_connected = False
                logger.error(self.last_error)
                return False

            self._cap = cap
            self.is_connected = True
            self.last_error = None

            # Start reader thread to keep USB DirectShow buffer at 0ms latency.
            # Each reader thread has its own stop flag and owns its capture.
            import threading
            stop = threading.Event()
            self._stop_event = stop
            name = f"USBCamReader-{self.camera_id[:8]}"
            self._thread = threading.Thread(
                target=run_supervised,
                args=(name, lambda: self._reader_loop(cap, stop), stop),
                kwargs={"kind": "camera_reader"},
                name=name,
                daemon=True,
            )
            self._thread.start()

            logger.info("USB camera '%s' connected at device index %s (0-lag worker started)", self.name, self.device_index)
            return True

        except Exception as exc:
            self.last_error = f"Exception connecting to USB camera {self.device_index}: {exc}"
            logger.error(self.last_error)
            self.is_connected = False
            return False

    def _reader_loop(self, cap: Optional[Any], stop: Any) -> None:
        """Read frames until stopped; reopen the device when it stops delivering (unplugged, reset)."""
        import time
        failures = 0
        retry_delay = 1.0
        lost_at: Optional[float] = None
        attempts = 0
        reopen_after = self.REOPEN_AFTER_FAILED_READS

        def release(c: Optional[Any]) -> None:
            if c is None:
                return
            if self._cap is c:
                self._cap = None
            try:
                c.release()
            except Exception as exc:
                logger.warning("Error releasing USB camera %s: %s", self.device_index, exc)

        try:
            while not stop.is_set():
                if cap is None or not cap.isOpened():
                    release(cap)
                    cap = None
                    if lost_at is None:
                        lost_at = time.monotonic()
                        logger.warning("USB camera '%s' (device %s) stopped delivering frames; reopening it",
                                       self.name, self.device_index)
                    attempts += 1
                    try:
                        cap = self._open_device()
                    except Exception as exc:
                        self.last_error = f"Exception reopening USB camera {self.device_index}: {exc}"
                        cap = None
                    if cap is None:
                        _reconnect_log.log(
                            logger, logging.WARNING, self.camera_id,
                            "USB camera '%s' is still unavailable after %d attempt(s): %s",
                            self.name, attempts, self.last_error,
                        )
                        if stop.wait(retry_delay):
                            break
                        retry_delay = min(retry_delay * 2, max(1.0, app_settings.CAMERA_RECONNECT_MAX_SECONDS))
                        continue
                    if stop.is_set():
                        break
                    self._cap = cap
                    logger.info("USB camera '%s' reopened after %.0f s (%d attempt(s))",
                                self.name, time.monotonic() - lost_at, attempts)
                    _reconnect_log.clear(self.camera_id)
                    self.last_error = None
                    lost_at, attempts, retry_delay, failures = None, 0, 1.0, 0
                    continue

                try:
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        failures += 1
                        if failures >= reopen_after:
                            release(cap)
                            cap = None
                            failures = 0
                        else:
                            time.sleep(0.01)
                        continue

                    failures = 0
                    processed = self._process_frame(frame)
                    with self._lock:
                        self._latest_raw_mat = frame
                        self._latest_processed_mat = processed
                        self._latest_frame_id += 1
                        self._latest_jpeg = None
                    self._new_frame_event.set()
                except Exception as exc:
                    failures += 1
                    _reconnect_log.log(
                        logger, logging.WARNING, ("read", self.camera_id),
                        "USB camera '%s' frame read failed: %s: %s", self.name, type(exc).__name__, exc,
                    )
                    time.sleep(0.02)
        finally:
            release(cap)

    def disconnect(self) -> None:
        self.is_connected = False
        if self._stop_event:
            self._stop_event.set()

        # A running reader releases its own capture as it exits; releasing it
        # from here while a read is in progress could crash the driver.
        thread = self._thread
        if thread is not None and thread.is_alive():
            import threading
            if threading.current_thread() is not thread:
                thread.join(timeout=1.0)
            if thread.is_alive():
                logger.warning("USB camera '%s' reader is still finishing a read; it will release the device itself", self.name)
        elif self._cap is not None:
            try:
                self._cap.release()
            except Exception as exc:
                logger.warning("Error releasing USB camera %s: %s", self.device_index, exc)
            self._cap = None
        self._thread = None

        if self._lock:
            with self._lock:
                self._latest_raw_mat = None
                self._latest_processed_mat = None
                self._latest_jpeg = None

        logger.info("USB camera '%s' disconnected cleanly", self.name)

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
        return self.get_latest_raw_mat(copy=True)

    def grab_frame(self) -> Tuple[bool, Optional[bytes]]:
        if not self.is_connected or self._lock is None or cv2 is None:
            return False, None

        with self._lock:
            if self._latest_processed_mat is None:
                return False, None
            if self._latest_jpeg is not None and self._latest_jpeg_frame_id == self._latest_frame_id:
                return True, self._latest_jpeg
            mat = self._latest_processed_mat
            cur_fid = self._latest_frame_id

        try:
            q = int(self.settings.get("stream_jpeg_quality", 80))
            ret, buffer = cv2.imencode(".jpg", mat, [int(cv2.IMWRITE_JPEG_QUALITY), q])
            if not ret:
                return False, None

            jpeg_bytes = buffer.tobytes()
            with self._lock:
                self._latest_jpeg = jpeg_bytes
                self._latest_jpeg_frame_id = cur_fid

            return True, jpeg_bytes
        except Exception as exc:
            self.last_error = f"Frame grab exception: {exc}"
            return False, None

    def get_properties(self) -> Dict[str, Any]:
        if not self.is_connected or self._cap is None or cv2 is None:
            return {
                "device_index": self.device_index,
                "connected": False,
                "settings": self.settings,
            }

        try:
            width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(self._cap.get(cv2.CAP_PROP_FPS))
            brightness = float(self._cap.get(cv2.CAP_PROP_BRIGHTNESS))
            contrast = float(self._cap.get(cv2.CAP_PROP_CONTRAST))
            saturation = float(self._cap.get(cv2.CAP_PROP_SATURATION))
            exposure = float(self._cap.get(cv2.CAP_PROP_EXPOSURE))

            return {
                "device_index": self.device_index,
                "connected": True,
                "width": width,
                "height": height,
                "fps": fps,
                "brightness": brightness,
                "contrast": contrast,
                "saturation": saturation,
                "exposure": exposure,
                "backend": "cv2.CAP_DSHOW" if sys.platform.startswith("win") else "cv2.CAP_ANY",
            }
        except Exception as exc:
            return {"device_index": self.device_index, "connected": True, "error": str(exc)}

    def set_properties(self, properties: Dict[str, Any]) -> bool:
        cap = self._cap
        if cap is None or not cap.isOpened() or cv2 is None:
            return False
        if not self._apply_properties(cap, properties):
            return False
        # Keep self.settings updated
        self.settings.update(properties)
        return True

    @staticmethod
    def _apply_properties(cap: Any, properties: Dict[str, Any]) -> bool:
        try:
            if "width" in properties and properties["width"]:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(properties["width"]))
            if "height" in properties and properties["height"]:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(properties["height"]))
            if "fps" in properties and properties["fps"]:
                cap.set(cv2.CAP_PROP_FPS, float(properties["fps"]))
            if "brightness" in properties and properties["brightness"] is not None:
                cap.set(cv2.CAP_PROP_BRIGHTNESS, float(properties["brightness"]))
            if "contrast" in properties and properties["contrast"] is not None:
                cap.set(cv2.CAP_PROP_CONTRAST, float(properties["contrast"]))
            if "saturation" in properties and properties["saturation"] is not None:
                cap.set(cv2.CAP_PROP_SATURATION, float(properties["saturation"]))
            if "exposure" in properties and properties["exposure"] is not None:
                cap.set(cv2.CAP_PROP_EXPOSURE, float(properties["exposure"]))
            if "auto_exposure" in properties and properties["auto_exposure"] is not None:
                # Value 0.25 vs 0.75 / 1.0 depending on DirectShow driver
                val = 0.75 if properties["auto_exposure"] else 0.25
                cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, val)
            return True
        except Exception as exc:
            logger.warning("Failed setting USB camera properties: %s", exc)
            return False

    @classmethod
    def discover_cameras(cls, max_devices: int = 8) -> List[Dict[str, Any]]:
        """
        Universal Hardware Camera Scanner:
        Scans all physical camera ports on the system:
        - USB / UVC Video Devices (Windows DirectShow / Linux V4L2)
        - Raspberry Pi Camera Modules (CSI / Unicam / V4L2 / libcamera)
        - NVIDIA Jetson Nano / Xavier / Orin (CSI Ports / Tegra vi-output / nvarguscamerasrc)
        """
        discovered: List[Dict[str, Any]] = []
        if cv2 is None:
            return discovered

        seen_sources = set()

        # 1. Linux sysfs inspection for hardware names (Raspberry Pi & Jetson CSI cameras)
        linux_dev_names: Dict[int, str] = {}
        if sys.platform.startswith("linux") and os.path.exists("/sys/class/video4linux"):
            try:
                for vnode in sorted(os.listdir("/sys/class/video4linux")):
                    if vnode.startswith("video") and vnode[5:].isdigit():
                        idx = int(vnode[5:])
                        name_path = os.path.join("/sys/class/video4linux", vnode, "name")
                        if os.path.exists(name_path):
                            with open(name_path, "r", encoding="utf-8", errors="ignore") as nf:
                                raw_name = nf.read().strip()
                                if raw_name:
                                    linux_dev_names[idx] = raw_name
            except Exception as e:
                logger.debug("Error reading V4L2 sysfs: %s", e)

        # 2. Probe standard hardware indices (DirectShow on Windows, V4L2/CAP_ANY on Linux)
        backends = [cv2.CAP_DSHOW] if sys.platform.startswith("win") else [cv2.CAP_V4L2, cv2.CAP_ANY]

        for idx in range(max_devices):
            cap = None
            opened = False
            used_backend = None

            for b in backends:
                try:
                    cap = cv2.VideoCapture(idx, b)
                    if cap.isOpened():
                        opened = True
                        used_backend = b
                        break
                    else:
                        cap.release()
                except Exception:
                    pass

            if opened and cap:
                try:
                    ret, _ = cap.read()
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
                    cap.release()

                    # Classify camera type & name based on hardware info
                    sys_name = linux_dev_names.get(idx, "")
                    if sys_name:
                        cam_name = f"{sys_name} - {w}x{h}"
                    else:
                        cam_name = f"Camera {idx} - {w}x{h}"

                    discovered.append({
                        "device_index": idx,
                        "name": cam_name,
                        "is_available": True,
                        "suggested_source": str(idx),
                    })
                    seen_sources.add(str(idx))
                except Exception as exc:
                    logger.debug("Error probing camera index %s: %s", idx, exc)
                    if cap:
                        cap.release()

        # 3. Check GStreamer hardware pipelines if on aarch64 Linux
        if sys.platform.startswith("linux") and hasattr(os, "uname") and "aarch64" in os.uname().machine:
            for sensor_id in [0, 1]:
                csi_src = f"nvarguscamerasrc sensor-id={sensor_id} ! video/x-raw(memory:NVMM), width=1920, height=1080, format=NV12, framerate=30/1 ! nvvidconv ! video/x-raw, format=BGRx ! videoconvert ! video/x-raw, format=BGR ! appsink"
                if csi_src not in seen_sources:
                    try:
                        gcap = cv2.VideoCapture(csi_src, cv2.CAP_GSTREAMER)
                        if gcap.isOpened():
                            ret, _ = gcap.read()
                            gcap.release()
                            if ret:
                                discovered.append({
                                    "device_index": 100 + sensor_id,
                                    "name": f"Camera Sensor {sensor_id} - 1080p",
                                    "is_available": True,
                                    "suggested_source": csi_src,
                                })
                                seen_sources.add(csi_src)
                    except Exception:
                        pass

        return discovered

