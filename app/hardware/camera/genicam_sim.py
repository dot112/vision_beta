"""
A stand-in industrial camera
============================
``SimulatedBackend`` behaves like a GenICam camera without any hardware or
library. The tests push pictures into it by hand. With ``GENTL_CTI_PATH=simulator``
the server uses it for real (``auto=True``): it then makes its own pictures, so
the whole path from a trigger to a counted product can be tried without a camera.

In ``auto`` mode a free running camera gives 5 pictures a second, a hardware
triggered one a picture every 2 seconds (as if a sensor fired), and a software
triggered one a picture per trigger command.
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

import numpy as np

SIM_DEVICES: List[Dict[str, str]] = [
    {"serial": "SIM-0001", "model": "Simulated mono camera", "vendor": "Simulator", "interface": "GigE"},
    {"serial": "SIM-0002", "model": "Simulated colour camera", "vendor": "Simulator", "interface": "USB3"},
]

FREE_RUN_PERIOD_S = 0.2
HARDWARE_PULSE_S = 2.0

_DEFAULT_FEATURES: Dict[str, Any] = {
    "PixelFormat": "Mono8", "AcquisitionMode": "Continuous", "ExposureMode": "Timed",
    "TriggerSelector": "FrameStart", "TriggerMode": "Off", "TriggerSource": "Line0",
    "TriggerActivation": "RisingEdge", "TriggerDelay": 0.0,
    "ExposureAuto": "Continuous", "ExposureTime": 10000.0, "GainAuto": "Continuous", "Gain": 0.0,
    "LineSelector": "Line0", "LineMode": "Input", "LineSource": "Off", "DeviceTemperature": 35.0,
}
_DEFAULT_CHOICES: Dict[str, List[str]] = {
    "PixelFormat": ["Mono8", "BayerRG8", "RGB8", "BGR8"],
    "TriggerSource": ["Software", "Line0", "Line1"],
    "TriggerActivation": ["RisingEdge", "FallingEdge"],
    "LineSelector": ["Line0", "Line1", "Line2"],
}


class SimulatedBackend:
    """One stand-in camera. It records every call in ``calls`` as a tuple, e.g. ``("set", "Gain", 6.0)``."""

    def __init__(self, devices: Optional[List[Dict[str, str]]] = None, features: Optional[Dict[str, Any]] = None,
                 auto: bool = False):
        self.devices = [dict(d) for d in SIM_DEVICES] if devices is None else devices
        self.features: Dict[str, Any] = {**_DEFAULT_FEATURES, **(features or {})}
        self.choices_of: Dict[str, List[str]] = {k: list(v) for k, v in _DEFAULT_CHOICES.items()}
        self.commands: Set[str] = {"TriggerSoftware"}
        self.missing: Set[str] = set()          # features this camera does not have
        self.stream_drivers_offered: List[str] = []     # the stand-in's producer offers no choice of stream driver
        self.refuses: Set[str] = set()          # features it has but takes no value for
        self.fail: Dict[str, BaseException] = {}        # "open" | "ping" | "execute" | "fetch" -> raised every time
        self.fail_once: Dict[str, BaseException] = {}   # ... raised once
        self.calls: List[Tuple[Any, ...]] = []
        self.on_trigger: Optional[Callable[[], None]] = None
        self.auto = auto
        self.opened: Optional[str] = None
        self.acquiring = False
        self._frames: Deque[Tuple[np.ndarray, str]] = collections.deque()
        self._arrived = threading.Condition()
        self._next_auto = 0.0
        self._count = 0

    # ── What a test does ──────────────────────────────────────────────────────

    def push(self, pixels: np.ndarray, pixel_format: str = "Mono8") -> None:
        """The camera took this picture."""
        with self._arrived:
            self._frames.append((pixels, pixel_format))
            self._arrived.notify_all()

    def _raise(self, what: str) -> None:
        once = self.fail_once.pop(what, None)
        if once is not None:
            raise once
        if what in self.fail:
            raise self.fail[what]

    # ── The backend's calls ───────────────────────────────────────────────────

    def list_devices(self) -> List[Dict[str, str]]:
        return [dict(d) for d in self.devices]

    def open(self, serial: str) -> Dict[str, str]:
        self.calls.append(("open", serial))
        self._raise("open")
        for device in self.devices:
            if device.get("serial") == serial:
                self.opened = serial
                return dict(device)
        raise LookupError(serial)

    def close(self) -> None:
        self.calls.append(("close",))
        self.opened, self.acquiring = None, False

    def has(self, feature: str) -> bool:
        return feature not in self.missing and (feature in self.features or feature in self.commands)

    def get(self, feature: str) -> Any:
        return self.features[feature]

    def set(self, feature: str, value: Any) -> None:
        self.calls.append(("set", feature, value))
        if not self.has(feature) or feature in self.refuses:
            raise ValueError(f"{feature} = {value}: the camera refused it")
        choices = self.choices_of.get(feature)
        if choices is not None and value not in choices:
            raise ValueError(f"{feature} = {value}: the camera offers {', '.join(choices)}")
        self.features[feature] = value

    def choices(self, feature: str) -> List[str]:
        return [] if feature in self.missing else list(self.choices_of.get(feature, []))

    def execute(self, feature: str) -> None:
        self.calls.append(("execute", feature))
        self._raise("execute")
        if not self.has(feature):
            raise ValueError(f"{feature}: the camera does not have it")
        if self.on_trigger is not None:
            self.on_trigger()
        elif self.auto and feature == "TriggerSoftware":
            self.push(*self._picture())

    def ping(self) -> None:
        self.calls.append(("ping",))
        self._raise("ping")

    def stream_drivers(self) -> List[str]:
        return list(self.stream_drivers_offered)

    def use_stream_driver(self, name: str) -> None:
        self.calls.append(("stream_driver", name))
        if self.acquiring:
            raise ValueError(f"Stream driver {name}: it cannot be changed while pictures are coming")
        if name not in self.stream_drivers_offered:
            raise ValueError(f"Stream driver {name}: the producer offers "
                             f"{', '.join(self.stream_drivers_offered) or 'no choice of stream driver'}")

    def start(self) -> None:
        self.calls.append(("start",))
        self.acquiring = True
        self._next_auto = time.monotonic() + self._auto_period()

    def stop(self) -> None:
        self.calls.append(("stop",))
        self.acquiring = False

    def fetch(self, timeout: float) -> Optional[Tuple[np.ndarray, str]]:
        self._raise("fetch")
        deadline = time.monotonic() + timeout
        with self._arrived:
            while not self._frames:
                now = time.monotonic()
                if self.auto and self.acquiring and self._auto_period() and now >= self._next_auto:
                    self._next_auto = now + self._auto_period()
                    return self._picture()
                wait = deadline - now
                if wait <= 0:
                    return None
                if self.auto and self._auto_period():
                    wait = min(wait, max(0.0, self._next_auto - now) or 0.001)
                self._arrived.wait(wait)
            return self._frames.popleft()

    # ── Pictures of the auto mode ─────────────────────────────────────────────

    def _auto_period(self) -> float:
        """Seconds between the pictures the camera takes on its own; 0 when it waits for a command."""
        if self.features.get("TriggerMode") != "On":
            return FREE_RUN_PERIOD_S
        return 0.0 if self.features.get("TriggerSource") == "Software" else HARDWARE_PULSE_S

    def _picture(self) -> Tuple[np.ndarray, str]:
        """A 640 x 480 picture with a block that moves one step per picture and the picture's number."""
        self._count += 1
        pixel_format = str(self.features.get("PixelFormat") or "Mono8")
        image = np.full((480, 640), 60, np.uint8)
        x = 40 + (self._count * 37) % 480
        image[180:300, x:x + 120] = 210
        try:
            import cv2
            cv2.putText(image, f"{self.opened or 'SIM'}  #{self._count}", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 255, 2)
        except Exception:
            pass
        if pixel_format in ("RGB8", "BGR8"):
            return np.dstack([image, image, image]), pixel_format
        return image, pixel_format if pixel_format.startswith(("Mono", "Bayer")) else "Mono8"
