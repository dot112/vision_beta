"""
Industrial cameras: GigE Vision and USB3 Vision, through GenICam
================================================================
One driver for both cables and for every maker that follows the standard.
``source`` is the camera's serial number.

Trigger modes (the camera's setting ``trigger_mode``):

- ``off``       the camera runs freely, like a network camera.
- ``hardware``  a signal on the camera's own input takes each picture.
- ``software``  the server takes each picture with a command (``take_picture``).

In a trigger mode every picture is also kept in a queue of ``QUEUE_SIZE``, with
the time it arrived, so that no picture is skipped while the model is busy.
A triggered camera can be silent for hours: it counts as lost only when it
stops answering (checked every ``LINK_CHECK_S``), and it is then opened again
with its settings until it is back.

``trigger_mode`` is what the camera accepted, not what was asked for: a camera
that refuses its trigger settings runs freely and reports "off", so that its
pictures are never taken for one product each.
"""
from __future__ import annotations

import collections
import logging
import re
import threading
import time
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from app.hardware.camera import genicam_backend
from app.hardware.camera.base import BaseCamera
from app.hardware.camera.genicam_backend import GenICamBackend, GenICamUnavailable
from app.utils.logger import LogThrottle, get_logger

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore

logger = get_logger(__name__)
_picture_log = LogThrottle(60.0)

TRIGGER_MODES = ("off", "hardware", "software")
TRIGGER_EDGES = {"rising": "RisingEdge", "falling": "FallingEdge"}
DEFAULT_TRIGGER_LINE = "Line0"
# The camera's own settings: a change of one of them is written to the connected camera.
CAMERA_SETTING_KEYS = ("trigger_mode", "trigger_line", "trigger_edge", "trigger_delay_us",
                       "exposure_us", "gain_db", "strobe_line")
# When the camera's pixel format cannot be read, the first of these it offers is set.
PREFERRED_FORMATS = ("BGR8", "RGB8", "BayerRG8", "BayerGB8", "BayerGR8", "BayerBG8", "Mono8")
FORMAT_HINT = "set the camera to Mono8, an 8-bit Bayer format, RGB8 or BGR8"

_GREY_OR_BAYER = re.compile(r"(Mono|BayerRG|BayerGB|BayerGR|BayerBG)(8|10|12|14|16)")
_YUYV = ("YUV422_8", "YUV422_8_YUYV", "YCbCr422_8", "YUV422Packed_YUYV", "YUYVPacked")
_UYVY = ("YUV422_8_UYVY", "YCbCr422_8_CbYCrY", "YUV422Packed")


def _bayer_codes() -> Dict[str, int]:
    # GenICam names a Bayer format by its first two pixels; OpenCV names the same
    # pattern by the second row's second and third pixel, so the letters differ.
    return {"BayerRG": cv2.COLOR_BayerBG2BGR, "BayerBG": cv2.COLOR_BayerRG2BGR,
            "BayerGR": cv2.COLOR_BayerGB2BGR, "BayerGB": cv2.COLOR_BayerGR2BGR}


def format_is_supported(pixel_format: str) -> bool:
    name = str(pixel_format or "")
    return bool(_GREY_OR_BAYER.fullmatch(name)) or name in ("BGR8", "RGB8", "BGRa8", "RGBa8", "BGR8Packed", "RGB8Packed") \
        or name in _YUYV or name in _UYVY


def to_bgr(pixels: np.ndarray, pixel_format: str) -> np.ndarray:
    """A camera's picture as 8-bit BGR. Raises ValueError for a pixel format that cannot be read."""
    name = str(pixel_format or "")
    match = _GREY_OR_BAYER.fullmatch(name)
    if match:
        kind, bits = match.group(1), int(match.group(2))
        if pixels.ndim == 3:
            pixels = pixels[:, :, 0]
        if bits > 8:
            if pixels.dtype == np.uint8:
                raise ValueError(f"Pixel format '{name}' came as single bytes and cannot be read; {FORMAT_HINT}")
            pixels = (pixels >> (bits - 8)).astype(np.uint8)
        elif pixels.dtype != np.uint8:
            pixels = pixels.astype(np.uint8)
        return cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGR if kind == "Mono" else _bayer_codes()[kind])
    if name in ("BGR8", "BGR8Packed"):
        return np.ascontiguousarray(pixels[:, :, :3], dtype=np.uint8)
    if name in ("RGB8", "RGB8Packed"):
        return cv2.cvtColor(np.ascontiguousarray(pixels[:, :, :3], dtype=np.uint8), cv2.COLOR_RGB2BGR)
    if name == "BGRa8":
        return cv2.cvtColor(pixels, cv2.COLOR_BGRA2BGR)
    if name == "RGBa8":
        return cv2.cvtColor(pixels, cv2.COLOR_RGBA2BGR)
    if name in _YUYV:
        return cv2.cvtColor(pixels, cv2.COLOR_YUV2BGR_YUY2)
    if name in _UYVY:
        return cv2.cvtColor(pixels, cv2.COLOR_YUV2BGR_UYVY)
    raise ValueError(f"Pixel format '{name}' is not supported; {FORMAT_HINT}")


def _number(value: Any) -> Optional[float]:
    """A setting as a number; None when it is not set."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_LINE_NAME = re.compile(r"[A-Za-z0-9_]{1,32}")
# Lowest and highest value of the number settings.
SETTING_LIMITS = {"trigger_delay_us": (0, 10_000_000), "exposure_us": (10, 10_000_000), "gain_db": (0, 48)}


def clean_camera_settings(settings: Dict[str, Any], camera_type: str,
                          saved: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``settings`` with the industrial camera's own settings checked and tidied; other keys are left as they are.

    ``saved``: the camera's settings so far, for the check that looks at two settings together.
    Raises ValueError that names the setting.
    """
    cleaned = dict(settings)
    sent = [key for key in CAMERA_SETTING_KEYS if key in settings]
    if not sent:
        return cleaned
    if camera_type != "gige":
        raise ValueError(f"{sent[0]} is a setting of industrial cameras (GigE / USB3 Vision); "
                         f"this is a {camera_type} camera")
    for key, allowed in (("trigger_mode", TRIGGER_MODES), ("trigger_edge", tuple(TRIGGER_EDGES))):
        if key in settings:
            text = str(settings[key] or allowed[0]).strip().lower()
            if text not in allowed:
                raise ValueError(f"{key} must be one of: {', '.join(allowed)}")
            cleaned[key] = text
    for key in ("trigger_line", "strobe_line"):
        if key in settings:
            text = str(settings[key] or "").strip()
            if text and not _LINE_NAME.fullmatch(text):
                raise ValueError(f"{key} must be the name of one of the camera's lines, such as Line1 "
                                 "(letters, digits and _, 32 at most)")
            cleaned[key] = text
    for key, (low, high) in SETTING_LIMITS.items():
        if key in settings:
            value = settings[key]
            if value is None or value == "":
                cleaned[key] = 0 if key == "trigger_delay_us" else None      # not set: the camera keeps its own
                continue
            number = _number(value)
            if number is None or not low <= number <= high:
                raise ValueError(f"{key} must be a number from {low} to {high}")
            cleaned[key] = int(number) if number == int(number) else number
    together = {**(saved or {}), **cleaned}
    strobe = together.get("strobe_line")
    if strobe and together.get("trigger_mode") == "hardware" \
            and strobe == (together.get("trigger_line") or DEFAULT_TRIGGER_LINE):
        raise ValueError("strobe_line cannot be the line the trigger comes in on (trigger_line)")
    return cleaned


class GigECamera(BaseCamera):
    """A GigE Vision or USB3 Vision camera."""

    QUEUE_SIZE = 64             # triggered pictures that can wait for the model
    FETCH_S = 0.2               # one wait for a picture
    LINK_CHECK_S = 5.0          # how often a silent camera is asked whether it is still there
    STALL_S = 10.0              # a free running camera that gives no picture for this long is opened again
    TRIGGER_WAIT_S = 2.0        # how long take_picture waits for its picture
    REOPEN_FIRST_S = 2.0        # the wait before a lost camera is opened again; doubled up to REOPEN_MAX_S
    REOPEN_MAX_S = 30.0

    def __init__(self, camera_id: str, name: str, source: str, settings: Optional[Dict[str, Any]] = None,
                 backend: Optional[GenICamBackend] = None):
        super().__init__(camera_id=camera_id, name=name, source=source, settings=settings)
        self._given_backend = backend
        self._backend: Optional[GenICamBackend] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)     # a picture joined the queue
        self._shot_lock = threading.Lock()                  # one software trigger at a time
        self._new_frame_event = threading.Event()
        self._reapply = threading.Event()                   # the settings changed: write them to the camera

        self._latest_raw_mat: Optional[Any] = None
        self._latest_processed_mat: Optional[Any] = None
        self._latest_jpeg: Optional[bytes] = None
        self._latest_frame_id = 0
        self._latest_jpeg_frame_id = 0
        self._latest_timestamp = 0.0
        self.last_picture_at: Optional[float] = None        # clock time of the newest picture

        self._queue: Deque[Tuple[Any, int, float]] = collections.deque()
        self.dropped_frames = 0
        self._applied_trigger = "off"
        self._problems: List[str] = []
        self._picture_error: Optional[str] = None
        self._device: Dict[str, str] = {}
        self._inputs: List[str] = []
        self._lines: List[str] = []
        self._pixel_format: Optional[str] = None

    # ── The trigger mode the camera really is in ─────────────────────────────

    @property
    def trigger_mode(self) -> str:  # type: ignore[override]
        return self._applied_trigger if self.is_connected else "off"

    @property
    def pending_triggered(self) -> int:
        """Triggered pictures waiting in the queue."""
        with self._lock:
            return len(self._queue)

    # ── Connect and disconnect ────────────────────────────────────────────────

    def connect(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            self.disconnect()
        label = f"Industrial camera '{self.name}'"
        try:
            backend = self._given_backend or genicam_backend.default_backend()
            self._device = backend.open(self.source) or {}
        except GenICamUnavailable as exc:
            self.last_error = str(exc)
        except LookupError:
            self.last_error = (f"{label}: no camera with the serial number {self.source} was found. "
                               "Is it powered, on the same network, and not open in another program?")
        except Exception as exc:
            self.last_error = f"{label} ({self.source}) could not be opened: {type(exc).__name__}: {exc}"
        else:
            self._backend = backend
            try:
                self._apply_settings()
                backend.start()
            except Exception as exc:
                self.last_error = f"{label} ({self.source}) could not be started: {type(exc).__name__}: {exc}"
                self._close_backend()
            else:
                self.last_error = None
                self.reconnecting = False
                self.is_connected = True
                self._stop_event = threading.Event()
                self._thread = threading.Thread(
                    target=self._read_frames, args=(self._stop_event,),
                    name=f"GenICam-{self.camera_id[:8]}", daemon=True,
                )
                self._thread.start()
                logger.info("%s (%s, %s) connected; trigger: %s", label, self._device.get("model") or "?",
                            self.source, self._applied_trigger)
                return True
        logger.warning(self.last_error)
        self.is_connected = False
        return False

    def disconnect(self) -> None:
        self.is_connected = False
        self.reconnecting = False
        self._stop_event.set()
        with self._arrived:
            self._arrived.notify_all()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=3.0)        # the reader closes the camera as it ends
        if thread is None or not thread.is_alive():
            self._close_backend()
        with self._lock:
            self._latest_raw_mat = None
            self._latest_processed_mat = None
            self._latest_jpeg = None
            self._queue.clear()
        self._applied_trigger = "off"
        logger.info("Industrial camera '%s' disconnected", self.name)

    def _close_backend(self) -> None:
        backend, self._backend = self._backend, None
        if backend is None:
            return
        for call in (backend.stop, backend.close):
            try:
                call()
            except Exception:
                logger.debug("Industrial camera '%s': %s failed while closing", self.name, call.__name__, exc_info=True)

    # ── The camera's settings ─────────────────────────────────────────────────

    def _set(self, feature: str, value: Any, needed: bool = True) -> bool:
        """Write one feature. A refusal is noted in the problems list (when ``needed``); returns whether it was taken."""
        backend = self._backend
        try:
            backend.set(feature, value)
            return True
        except Exception as exc:
            if needed:
                self._problems.append(str(exc) if isinstance(exc, ValueError) else f"{feature} = {value}: {exc}")
            return False

    def _first(self, *features: str) -> Optional[str]:
        """The first of these features the camera has."""
        return next((f for f in features if self._backend.has(f)), None)

    def _apply_settings(self) -> None:
        """Write the camera's settings to it. Called before the pictures start, on the reader's thread or in connect."""
        backend = self._backend
        self._problems = []

        current = str(backend.get("PixelFormat")) if backend.has("PixelFormat") else ""
        if current and not format_is_supported(current):
            offered = backend.choices("PixelFormat")
            better = next((f for f in PREFERRED_FORMATS if f in offered), None)
            if better is None or not self._set("PixelFormat", better):
                self._problems.append(f"Pixel format '{current}' is not supported; {FORMAT_HINT}")
            else:
                logger.info("Industrial camera '%s': pixel format changed from %s to %s", self.name, current, better)
        if backend.has("AcquisitionMode"):
            self._set("AcquisitionMode", "Continuous", needed=False)

        self._applied_trigger = self._apply_trigger()

        exposure = _number(self.settings.get("exposure_us"))
        if exposure is not None:
            feature = self._first("ExposureTime", "ExposureTimeAbs")
            if feature is None:
                self._problems.append("Exposure: this camera has no exposure time in microseconds")
            else:
                if backend.has("ExposureMode"):
                    self._set("ExposureMode", "Timed", needed=False)
                if backend.has("ExposureAuto"):
                    self._set("ExposureAuto", "Off", needed=False)
                self._set(feature, exposure)
        gain = _number(self.settings.get("gain_db"))
        if gain is not None:
            if not backend.has("Gain"):
                self._problems.append("Gain: this camera has no gain in dB")
            else:
                if backend.has("GainAuto"):
                    self._set("GainAuto", "Off", needed=False)
                self._set("Gain", gain)

        strobe = str(self.settings.get("strobe_line") or "").strip()
        if strobe:
            if self._set("LineSelector", strobe) and self._set("LineMode", "Output"):
                self._set("LineSource", "ExposureActive")

        sources = backend.choices("TriggerSource")
        self._inputs = [s for s in sources if s.lower() != "software" and not s.lower().startswith("action")]
        self._lines = backend.choices("LineSelector")
        if self._problems:
            logger.warning("Industrial camera '%s' did not take every setting: %s", self.name, "; ".join(self._problems))

    def _apply_trigger(self) -> str:
        """Set the trigger and return the mode the camera is now in ("off" when it refused)."""
        backend = self._backend
        wanted = str(self.settings.get("trigger_mode") or "off").strip().lower()
        if wanted not in TRIGGER_MODES:
            wanted = "off"
        if not backend.has("TriggerMode"):
            if wanted != "off":
                self._problems.append("Trigger: this camera has no trigger (no TriggerMode); it runs freely")
            return "off"
        if backend.has("TriggerSelector"):
            self._set("TriggerSelector", "FrameStart", needed=wanted != "off")
        # Off first: some cameras take a new trigger source only while the trigger is off.
        self._set("TriggerMode", "Off", needed=wanted == "off")
        if wanted == "off":
            return "off"

        if wanted == "software":
            ok = self._set("TriggerSource", "Software")
            if ok and not backend.has("TriggerSoftware"):
                self._problems.append("Trigger: this camera has no software trigger command (TriggerSoftware)")
                ok = False
        else:
            line = str(self.settings.get("trigger_line") or "").strip() or DEFAULT_TRIGGER_LINE
            ok = self._set("TriggerSource", line)
            if ok and backend.has("TriggerActivation"):
                edge = TRIGGER_EDGES.get(str(self.settings.get("trigger_edge") or "rising").strip().lower(), "RisingEdge")
                self._set("TriggerActivation", edge)
        delay = _number(self.settings.get("trigger_delay_us")) or 0.0
        feature = self._first("TriggerDelay", "TriggerDelayAbs")
        if feature is not None:
            self._set(feature, delay)
        elif delay > 0:
            self._problems.append("Trigger delay: this camera has none")
        if ok and self._set("TriggerMode", "On"):
            return wanted
        self._set("TriggerMode", "Off", needed=False)
        self._problems.append(f"Trigger: the camera did not take the {wanted} trigger, so it runs freely "
                              "and its pictures are not counted one product each")
        return "off"

    def set_properties(self, properties: Dict[str, Any]) -> bool:
        changed = any(key in properties and properties[key] != self.settings.get(key) for key in CAMERA_SETTING_KEYS)
        self.settings.update(properties)
        if changed and self.is_connected:
            self._reapply.set()     # written by the reader, between two pictures
        return True

    def get_properties(self) -> Dict[str, Any]:
        with self._lock:
            mat = self._latest_processed_mat
            height, width = (mat.shape[0], mat.shape[1]) if mat is not None else (0, 0)
            waiting = len(self._queue)
        return {
            "source": self.source,
            "connected": self.is_connected,
            "type": "gige",
            "width": width,
            "height": height,
            "model": self._device.get("model") or "",
            "vendor": self._device.get("vendor") or "",
            "interface": self._device.get("interface") or "",
            "pixel_format": self._pixel_format,
            "trigger_mode": self.trigger_mode,
            "trigger_inputs": list(self._inputs),
            "lines": list(self._lines),
            "setting_problems": list(self._problems),
            "picture_error": self._picture_error,
            "pictures": self._latest_frame_id,
            "last_picture_at": self.last_picture_at,
            "waiting_pictures": waiting,
            "dropped_frames": self.dropped_frames,
        }

    # ── Reading pictures ──────────────────────────────────────────────────────

    def _read_frames(self, stop: threading.Event) -> None:
        """The reader thread: it owns the camera from connect until it ends, and closes it."""
        try:
            failures = 0
            last_seen = time.monotonic()
            next_check = last_seen + self.LINK_CHECK_S
            while not stop.is_set():
                if self._reapply.is_set():
                    self._reapply.clear()
                    self._write_changed_settings()
                try:
                    got = self._backend.fetch(self.FETCH_S)
                except Exception as exc:
                    got = None
                    _picture_log.log(logger, logging.WARNING, self.camera_id,
                                     "Industrial camera '%s': a picture could not be fetched: %s: %s",
                                     self.name, type(exc).__name__, exc)
                    next_check = 0.0        # ask the camera now whether it is still there
                    stop.wait(self.FETCH_S)
                now = time.monotonic()
                if got is not None:
                    failures, last_seen, next_check = 0, now, now + self.LINK_CHECK_S
                    self._publish(*got)
                    continue
                lost = False
                if now >= next_check:
                    next_check = now + self.LINK_CHECK_S
                    try:
                        self._backend.ping()
                        failures = 0
                    except Exception:
                        failures += 1
                        lost = failures >= 2        # one lost answer is not a lost camera
                if not lost and self._applied_trigger == "off" and now - last_seen > self.STALL_S:
                    lost = True                     # it answers but sends no pictures
                if lost:
                    self._reopen(stop)
                    failures, last_seen = 0, time.monotonic()
                    next_check = last_seen + self.LINK_CHECK_S
        finally:
            self._close_backend()

    def _write_changed_settings(self) -> None:
        try:
            self._backend.stop()
            self._apply_settings()
            self._backend.start()
        except Exception as exc:
            self._problems.append(f"The settings could not be written: {type(exc).__name__}: {exc}")
            logger.warning("Industrial camera '%s': the changed settings could not be written: %s", self.name, exc)
        with self._lock:
            self._queue.clear()     # pictures taken under the old settings are not kept

    def _reopen(self, stop: threading.Event) -> None:
        """The camera stopped answering: open it again with its settings, until it is back or disconnected."""
        self.reconnecting = True
        self.last_error = f"Industrial camera '{self.name}' ({self.source}) is not answering; reconnecting"
        logger.warning(self.last_error)
        backend = self._backend
        delay = self.REOPEN_FIRST_S
        while not stop.is_set():
            for call in (backend.stop, backend.close):
                try:
                    call()
                except Exception:
                    pass
            if stop.wait(delay):
                return
            try:
                self._device = backend.open(self.source) or self._device
                self._apply_settings()
                backend.start()
            except Exception:
                delay = min(delay * 2, self.REOPEN_MAX_S)
                continue
            with self._lock:
                self._queue.clear()
            self.reconnecting = False
            self.last_error = None
            logger.info("Industrial camera '%s' is back; trigger: %s", self.name, self._applied_trigger)
            return

    def _publish(self, pixels: np.ndarray, pixel_format: str) -> None:
        """Make a picture the camera's latest one and, in a trigger mode, queue it."""
        try:
            frame = to_bgr(pixels, pixel_format)
        except Exception as exc:
            self._picture_error = str(exc)
            _picture_log.log(logger, logging.WARNING, ("format", self.camera_id), "Industrial camera '%s': %s", self.name, exc)
            return
        self._picture_error = None
        self._pixel_format = pixel_format
        processed = self._process_frame(frame)
        arrived = time.time()
        with self._arrived:
            self._latest_raw_mat = frame
            self._latest_processed_mat = processed
            self._latest_frame_id += 1
            self._latest_timestamp = time.monotonic()
            self.last_picture_at = arrived
            self._latest_jpeg = None
            if self._applied_trigger != "off":
                if len(self._queue) >= self.QUEUE_SIZE:
                    self._queue.popleft()
                    self.dropped_frames += 1
                self._queue.append((processed, self._latest_frame_id, arrived))
                self._arrived.notify_all()
        self._new_frame_event.set()

    # ── Triggered pictures ────────────────────────────────────────────────────

    def next_triggered_frame(self) -> Optional[Tuple[Any, int, float]]:
        """The oldest waiting picture as (BGR picture, frame id, time it arrived), or None."""
        with self._lock:
            return self._queue.popleft() if self._queue else None

    def clear_triggered_frames(self) -> int:
        """Empty the queue. Returns how many pictures were waiting."""
        with self._lock:
            waiting = len(self._queue)
            self._queue.clear()
            return waiting

    def trigger_software(self) -> bool:
        """Send the camera its trigger command. False, with the reason in ``last_error``, when it is not sent."""
        label = f"Industrial camera '{self.name}'"
        if not self.is_connected or self.reconnecting:
            self.last_error = self.last_error or f"{label} is not connected"
            return False
        if self._applied_trigger != "software":
            return False
        try:
            self._backend.execute("TriggerSoftware")
            return True
        except Exception as exc:
            self.last_error = f"{label} did not take the trigger: {type(exc).__name__}: {exc}"
            return False

    def take_picture(self, timeout: Optional[float] = None) -> Optional[Tuple[Any, int, float]]:
        """One picture by software trigger: (BGR picture, frame id, time it arrived).

        Returns None when no picture came within ``timeout``. A picture that
        comes later is thrown away by the next call, never taken for its own.
        Raises RuntimeError with the reason when the camera cannot be triggered.
        """
        mode = self.trigger_mode
        if mode == "hardware":
            raise RuntimeError(f"Camera '{self.name}' is triggered by its own input; the server cannot trigger it")
        if mode != "software":
            raise RuntimeError(self.last_error if self.reconnecting or not self.is_connected else
                               f"Camera '{self.name}' is not set to Software trigger")
        timeout = self.TRIGGER_WAIT_S if timeout is None else timeout
        with self._shot_lock:
            self.clear_triggered_frames()
            if not self.trigger_software():
                raise RuntimeError(self.last_error or f"Camera '{self.name}' did not take the trigger")
            deadline = time.monotonic() + timeout
            with self._arrived:
                while not self._queue:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self.is_connected:
                        return None
                    self._arrived.wait(remaining)
                return self._queue.popleft()

    # ── What every camera driver gives ────────────────────────────────────────

    def get_latest_frame_id(self) -> int:
        return self._latest_frame_id

    def get_latest_raw_mat(self, copy: bool = True) -> Tuple[bool, Optional[Any], int]:
        if not self.is_connected:
            return False, None, 0
        with self._lock:
            mat = self._latest_processed_mat
            if mat is None:
                return False, None, 0
            return True, (mat.copy() if copy else mat), self._latest_frame_id

    def wait_for_new_frame(self, timeout: float = 0.1) -> bool:
        if not self.is_connected:
            return False
        signaled = self._new_frame_event.wait(timeout=timeout)
        if signaled:
            self._new_frame_event.clear()
        return signaled

    def grab_raw_frame(self) -> Tuple[bool, Optional[Any], int]:
        return self.get_latest_raw_mat(copy=True)

    def grab_frame(self) -> Tuple[bool, Optional[bytes]]:
        if not self.is_connected or cv2 is None:
            return False, None
        with self._lock:
            mat = self._latest_processed_mat
            if mat is None:
                return False, None
            if self._latest_jpeg is not None and self._latest_jpeg_frame_id == self._latest_frame_id:
                return True, self._latest_jpeg
            frame_id = self._latest_frame_id
        try:
            quality = int(self.settings.get("stream_jpeg_quality", 75))
            ok, buffer = cv2.imencode(".jpg", mat, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if not ok:
                return False, None
            jpeg = buffer.tobytes()
            with self._lock:
                self._latest_jpeg = jpeg
                self._latest_jpeg_frame_id = frame_id
            return True, jpeg
        except Exception as exc:
            self.last_error = f"Industrial camera '{self.name}': the picture could not be encoded: {exc}"
            return False, None
