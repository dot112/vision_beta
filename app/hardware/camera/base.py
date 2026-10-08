from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

from app.utils.logger import LogThrottle, get_logger

logger = get_logger(__name__)
# A camera whose settings cannot be applied is reported once a minute, not on every frame.
_process_log = LogThrottle(60.0)

# Set while the server shuts down: a connect() that is trying several stream
# URLs stops after the current one instead of holding up the exit.
CONNECT_ABORT = threading.Event()


def roi_box(settings: Dict[str, Any], w: int, h: int) -> Optional[Tuple[int, int, int, int]]:
    """The ROI as pixel corners (x1, y1, x2, y2) inside a w x h picture, or None for the whole picture.

    roi_x/roi_y/roi_w/roi_h are fractions of the picture (0-1, what the dashboard
    saves); values above 1 are read as percentages (up to 100) or else pixels.
    """
    if not settings.get("roi_enabled", True):
        return None
    try:
        rx = float(settings.get("roi_x") or 0)
        ry = float(settings.get("roi_y") or 0)
        rw = settings.get("roi_w")
        rh = settings.get("roi_h")
        if rw is None or rh is None:
            return None
        rw, rh = float(rw), float(rh)
    except (TypeError, ValueError):
        return None
    if rw <= 0 or rh <= 0:
        return None
    if max(rx, ry, rw, rh) <= 1.0:
        fx, fy = float(w), float(h)
    elif max(rx + rw, ry + rh) <= 100.0:
        fx, fy = w / 100.0, h / 100.0
    else:
        fx = fy = 1.0
    x1, y1 = max(0, int(round(rx * fx))), max(0, int(round(ry * fy)))
    x2, y2 = min(w, int(round((rx + rw) * fx))), min(h, int(round((ry + rh) * fy)))
    if x2 - x1 < 16 or y2 - y1 < 16:
        return None  # too small to be meant; keep the whole picture
    if (x1, y1, x2, y2) == (0, 0, w, h):
        return None
    return x1, y1, x2, y2


class BaseCamera(ABC):
    """
    Abstract base class for all industrial vision camera drivers.
    """

    def __init__(self, camera_id: str, name: str, source: str, settings: Optional[Dict[str, Any]] = None):
        self.camera_id = camera_id
        self.name = name
        self.source = source
        self.settings = settings or {}
        self.is_connected = False
        # True while a connected camera has lost its stream or device and its
        # reader is trying to get it back (nobody disconnected it).
        self.reconnecting = False
        self.last_error: Optional[str] = None

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection to camera hardware / stream."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection and release hardware resources."""
        pass

    @abstractmethod
    def grab_frame(self) -> Tuple[bool, Optional[bytes]]:
        """
        Grab a single frame encoded as JPEG.
        Returns:
            (success: bool, jpeg_bytes: Optional[bytes])
        """
        pass

    @abstractmethod
    def grab_raw_frame(self) -> Tuple[bool, Optional[Any], int]:
        """
        Grab the freshest uncompressed BGR numpy array frame directly with 0ms buffering.
        Returns:
            (success: bool, frame_mat: Optional[np.ndarray], frame_id: int)
        """
        pass

    def get_latest_frame_id(self) -> int:
        """Returns the current monotonic frame ID without copying frame data."""
        return getattr(self, "_latest_frame_id", 0)

    def get_latest_raw_mat(self, copy: bool = True) -> Tuple[bool, Optional[Any], int]:
        """
        Returns the latest processed BGR mat and its frame ID.
        If copy=False, returns direct reference avoiding multi-megabyte RAM copies.
        """
        return self.grab_raw_frame()

    def wait_for_new_frame(self, timeout: float = 0.1) -> bool:
        """Waits until a new frame has been acquired by the reader thread."""
        event = getattr(self, "_new_frame_event", None)
        if event is not None:
            return event.wait(timeout=timeout)
        import time
        time.sleep(min(timeout, 0.03))
        return self.is_connected

    def _process_frame(self, frame: Any, crop: bool = True) -> Any:
        """
        The picture every consumer gets (live stream, YOLO, QR reader): scaled to
        the camera's width x height, rotated and flipped, then cropped to the ROI.

        The ROI is cropped last, so it is drawn on the picture as the stream shows
        it, and the crop keeps its own size and aspect ratio (it is not stretched
        back to width x height). crop=False skips the ROI, for drawing it.
        """
        if frame is None:
            return frame

        try:
            import cv2

            # 1. Picture size
            target_w = self.settings.get("width")
            target_h = self.settings.get("height")
            if target_w and target_h:
                tw, th = int(target_w), int(target_h)
                if tw > 0 and th > 0 and (frame.shape[1] != tw or frame.shape[0] != th):
                    frame = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)

            # 2. Rotation & flipping
            rot = self.settings.get("rotation")
            if rot == 90 or rot == "90":
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
            elif rot == 180 or rot == "180":
                frame = cv2.rotate(frame, cv2.ROTATE_180)
            elif rot == 270 or rot == "270":
                frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
            # rot == 0 / "" / None → no rotation applied (intentional no-op)

            if self.settings.get("flip_h"):
                frame = cv2.flip(frame, 1)
            if self.settings.get("flip_v"):
                frame = cv2.flip(frame, 0)

            # 3. ROI (region of interest): only this part goes to YOLO and the QR reader
            if crop:
                box = roi_box(self.settings, frame.shape[1], frame.shape[0])
                if box is not None:
                    x1, y1, x2, y2 = box
                    frame = frame[y1:y2, x1:x2]

            return frame
        except Exception as exc:
            _process_log.log(
                logger, logging.WARNING, self.camera_id,
                "Camera '%s': could not apply size/rotation/ROI settings, using the picture as it came: %s: %s",
                self.name, type(exc).__name__, exc,
            )
            return frame

    def get_full_view_mat(self) -> Optional[Any]:
        """The latest picture as the stream shows it, but before the ROI crop (for drawing the ROI)."""
        lock = getattr(self, "_lock", None)
        if lock is None or not self.is_connected:
            return None
        with lock:
            raw = getattr(self, "_latest_raw_mat", None)
        # Drivers replace the raw frame rather than modify it, so it can be processed outside the lock.
        return self._process_frame(raw, crop=False) if raw is not None else None

    def get_properties(self) -> Dict[str, Any]:
        """Return runtime camera properties (resolution, fps, exposure, etc)."""
        return {"settings": self.settings, "connected": self.is_connected}

    def set_properties(self, properties: Dict[str, Any]) -> bool:
        """Apply dynamic settings to camera."""
        self.settings.update(properties)
        return True
