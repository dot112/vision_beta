from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple


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

    def _process_frame(self, frame: Any) -> Any:
        """
        Applies ROI cropping, rotation, flip, and resolution scaling according to camera settings.
        """
        if frame is None:
            return frame

        try:
            import cv2
            h, w = frame.shape[:2]

            # 1. ROI (Region of Interest / Crop)
            roi_enabled = self.settings.get("roi_enabled", True)
            roi_x = self.settings.get("roi_x")
            roi_y = self.settings.get("roi_y")
            roi_w = self.settings.get("roi_w")
            roi_h = self.settings.get("roi_h")
            if roi_enabled and roi_w is not None and roi_h is not None:
                rx = float(roi_x or 0)
                ry = float(roi_y or 0)
                rw = float(roi_w)
                rh = float(roi_h)
                # Percentages 0-100% or normalized 0-1
                if rw <= 1.0 and rh <= 1.0 and rw > 0 and rh > 0:
                    x1 = max(0, int(rx * w))
                    y1 = max(0, int(ry * h))
                    x2 = min(w, int((rx + rw) * w))
                    y2 = min(h, int((ry + rh) * h))
                elif rw <= 100.0 and rh <= 100.0 and (rw > 1.0 or rh > 1.0) and rw > 0 and rh > 0 and (rx + rw <= 100.0):
                    x1 = max(0, int((rx / 100.0) * w))
                    y1 = max(0, int((ry / 100.0) * h))
                    x2 = min(w, int(((rx + rw) / 100.0) * w))
                    y2 = min(h, int(((ry + rh) / 100.0) * h))
                else:
                    x1 = max(0, int(rx))
                    y1 = max(0, int(ry))
                    x2 = min(w, int(rx + rw))
                    y2 = min(h, int(ry + rh))

                if x2 > x1 + 10 and y2 > y1 + 10:
                    frame = frame[y1:y2, x1:x2]

            # 2. Rotation & Flipping
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

            # 3. Output Resolution (Resizing if target width/height set in settings)
            target_w = self.settings.get("width")
            target_h = self.settings.get("height")
            if target_w and target_h:
                tw, th = int(target_w), int(target_h)
                if tw > 0 and th > 0 and (frame.shape[1] != tw or frame.shape[0] != th):
                    frame = cv2.resize(frame, (tw, th))

            return frame
        except Exception:
            return frame

    def get_properties(self) -> Dict[str, Any]:
        """Return runtime camera properties (resolution, fps, exposure, etc)."""
        return {"settings": self.settings, "connected": self.is_connected}

    def set_properties(self, properties: Dict[str, Any]) -> bool:
        """Apply dynamic settings to camera."""
        self.settings.update(properties)
        return True
