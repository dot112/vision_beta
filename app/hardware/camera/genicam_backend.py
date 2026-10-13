"""
GenICam access for industrial cameras (GigE Vision, USB3 Vision)
================================================================
``GenICamBackend`` is the handful of calls the camera driver needs.
``HarvestersBackend`` is the one that talks to real cameras, through the
``harvesters`` library and the camera makers' GenTL producer files (``.cti``).
This is the only file that imports ``harvesters``: the server starts and runs
without it, and says what to install when an industrial camera is asked for.

Every producer file found is loaded into ONE library handle that all
industrial cameras share (a producer can be loaded once per process). The
library's own scan closes every open camera, so it is used only while no
camera is open; with a camera open, the device lists of the open network and
USB ports are read again instead, which leaves the open cameras alone.

Written from the library's documentation and source. No camera was available
when it was written: see docs/real-life-tests/triggered-cameras.md.
"""
from __future__ import annotations

import glob
import os
import threading
import time
from typing import Any, Dict, List, Optional, Protocol, Tuple

import numpy as np

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

INSTALL_HINT = (
    "Industrial cameras need the GenICam library, which is not installed: "
    "run 'pip install -r requirements-industrial.txt' and restart the server"
)
NO_CTI_HINT = (
    "No GenTL producer file (.cti) was found. Install the camera maker's SDK (it sets GENICAM_GENTL64_PATH), "
    "or set GENTL_CTI_PATH to the folder or the .cti file, and restart the server"
)
SIMULATOR = "simulator"


class GenICamUnavailable(RuntimeError):
    """The library or a producer file is missing. ``str()`` says what to install."""


class GenICamBackend(Protocol):
    """One camera. ``open`` picks it by serial number; the other calls are for the open camera."""

    def list_devices(self) -> List[Dict[str, str]]: ...     # [{"serial", "model", "vendor", "interface"}]
    def open(self, serial: str) -> Dict[str, str]: ...      # its row of list_devices; LookupError when not found
    def close(self) -> None: ...
    def has(self, feature: str) -> bool: ...
    def get(self, feature: str) -> Any: ...
    def set(self, feature: str, value: Any) -> None: ...    # ValueError naming the feature when the camera refuses
    def choices(self, feature: str) -> List[str]: ...       # what a list feature offers; [] when it has none
    def execute(self, feature: str) -> None: ...
    def ping(self) -> None: ...                             # raises when the camera does not answer
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def fetch(self, timeout: float) -> Optional[Tuple[np.ndarray, str]]: ...   # (pixels, pixel format); None on timeout


def cti_files(configured: Optional[str] = None) -> List[str]:
    """Every ``.cti`` file of GENTL_CTI_PATH, else of GENICAM_GENTL64_PATH.

    Each may name folders and files, several of them, separated like PATH.
    """
    raw = (settings.GENTL_CTI_PATH if configured is None else configured) or ""
    raw = raw.strip() or os.environ.get("GENICAM_GENTL64_PATH", "")
    found: List[str] = []
    seen = set()
    for part in raw.split(os.pathsep):
        part = part.strip().strip('"')
        if not part:
            continue
        if os.path.isdir(part):
            files = sorted(glob.glob(os.path.join(part, "*.cti")))
        elif os.path.isfile(part) and part.lower().endswith(".cti"):
            files = [part]
        else:
            files = []
        for path in files:
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen:
                seen.add(key)
                found.append(path)
    return found


def _device_ids(info: Any) -> List[str]:
    """What a camera can be asked for by: its serial number, else the producer's own id."""
    return [str(v) for v in (getattr(info, "serial_number", None), getattr(info, "id_", None)) if v]


def _device_row(info: Any) -> Dict[str, str]:
    def text(name: str) -> str:
        try:
            return str(getattr(info, name, None) or "")
        except Exception:
            return ""

    ids = _device_ids(info)
    kind = text("tl_type")
    interface = {"GEV": "GigE", "U3V": "USB3"}.get(kind.upper(), kind)
    return {"serial": ids[0] if ids else "", "model": text("model") or text("display_name"),
            "vendor": text("vendor"), "interface": interface, "name": text("user_defined_name")}


class _Library:
    """The one ``Harvester`` of this process."""

    lock = threading.RLock()
    RESCAN_MIN_S = 3.0
    _harvester: Any = None
    _files: Tuple[str, ...] = ()
    _open = 0
    _scanned_at = 0.0

    @classmethod
    def harvester(cls) -> Any:
        """The loaded library. Raises GenICamUnavailable when it or every producer file is missing."""
        with cls.lock:
            try:
                from harvesters.core import Harvester
            except Exception as exc:
                raise GenICamUnavailable(f"{INSTALL_HINT} ({type(exc).__name__}: {exc})") from exc
            files = tuple(cti_files())
            if not files:
                raise GenICamUnavailable(NO_CTI_HINT)
            if cls._harvester is not None and (files == cls._files or cls._open):
                return cls._harvester
            if cls._harvester is not None:
                try:
                    cls._harvester.reset()
                except Exception:
                    logger.warning("The GenICam library could not be reset cleanly", exc_info=True)
            harvester = Harvester()
            for path in files:
                try:
                    harvester.add_file(path, check_existence=True)
                except Exception as exc:
                    logger.warning("GenTL producer file %s was not loaded: %s: %s", path, type(exc).__name__, exc)
            cls._harvester, cls._files, cls._scanned_at = harvester, files, 0.0
            logger.info("GenICam library loaded with %d producer file(s): %s", len(files), ", ".join(files))
            return harvester

    @classmethod
    def scan(cls, force: bool = False) -> List[Any]:
        """The cameras the producers see. Looked for again when the last look is older than RESCAN_MIN_S."""
        with cls.lock:
            harvester = cls.harvester()
            if force or cls._scanned_at == 0.0 or time.monotonic() - cls._scanned_at >= cls.RESCAN_MIN_S:
                if cls._open == 0:
                    harvester.update()      # closes every open camera: only while none is open
                else:
                    cls._scan_open_ports(harvester)
                cls._scanned_at = time.monotonic()
            return list(harvester.device_info_list)

    @staticmethod
    def _scan_open_ports(harvester: Any) -> None:
        """Read the device lists of the ports that are already open, without touching an open camera.

        A network or USB port that was added since the last full scan is not
        seen here; it is found by the next scan made while no camera is open.
        """
        try:
            from harvesters.core import DeviceInfo

            fresh = []
            for port in list(harvester._ifaces):
                port.module.update_device_info_list(harvester.timeout_for_update)
                fresh.extend(DeviceInfo(module=info, parent=port) for info in port.module.device_info_list)
            harvester._device_info_list[:] = fresh
        except Exception as exc:
            logger.warning("Industrial cameras could not be looked for while one is open; the last list is kept: %s: %s",
                           type(exc).__name__, exc)

    @classmethod
    def opened(cls, change: int) -> None:
        with cls.lock:
            cls._open = max(0, cls._open + change)


def _first_sentence(exc: BaseException) -> str:
    """The library's messages end in a long 'thrown in node ... (file ..., line ...)' part."""
    return str(exc).split(" : ")[0].strip() or type(exc).__name__


def _read_live(node: Any) -> Any:
    """A feature's value read from the camera itself, not from the library's cache."""
    for name in ("get_value", "get_int_value"):
        reader = getattr(node, name, None)
        if reader is None:
            continue
        try:
            return reader(False, True)      # verify=False, ignore_cache=True
        except TypeError:
            continue
    return node.value


class HarvestersBackend:
    """A real camera, through ``harvesters``."""

    # Read every few seconds to see that the camera still answers; the first one it can read is used.
    PING_FEATURES = ("DeviceTemperature", "TriggerMode", "PixelFormat", "Width")
    BUFFERS = 16

    def __init__(self) -> None:
        self._acquirer: Any = None
        self._nodes: Any = None
        self._ping_feature: Optional[str] = None

    def list_devices(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        seen = set()
        for info in _Library.scan():
            row = _device_row(info)
            # Two makers' producers can both see the same network camera.
            if row["serial"] and row["serial"] not in seen:
                seen.add(row["serial"])
                rows.append(row)
        return rows

    def open(self, serial: str) -> Dict[str, str]:
        with _Library.lock:
            found = [info for info in _Library.scan() if serial in _device_ids(info)]
            if not found:
                found = [info for info in _Library.scan(force=True) if serial in _device_ids(info)]
            if not found:
                raise LookupError(serial)
            harvester = _Library.harvester()
            problem: Optional[BaseException] = None
            for info in found:      # the same camera seen by two producers: the first that opens it
                try:
                    acquirer = harvester.create(info)
                except Exception as exc:
                    problem = exc
                    continue
                self._acquirer = acquirer
                self._nodes = acquirer.remote_device.node_map
                _Library.opened(+1)
                self._ping_feature = self._pick_ping_feature()
                return _device_row(info)
            raise RuntimeError(f"it was found but could not be opened ({_first_sentence(problem)}). "
                               "Is another program using it, or is its network address in another range?")

    def close(self) -> None:
        with _Library.lock:
            acquirer, self._acquirer, self._nodes = self._acquirer, None, None
            if acquirer is None:
                return
            try:
                acquirer.destroy()
            finally:
                _Library.opened(-1)

    def _node(self, feature: str) -> Any:
        nodes = self._nodes
        if nodes is None:
            raise RuntimeError("the camera is not open")
        try:
            if not nodes.has_node(feature):
                return None
        except Exception:
            return None
        return getattr(nodes, feature, None)

    def has(self, feature: str) -> bool:
        node = self._node(feature)
        if node is None:
            return False
        try:
            from genicam import genapi
            return bool(genapi.is_implemented(node) and genapi.is_available(node))
        except Exception:
            return True

    def get(self, feature: str) -> Any:
        node = self._node(feature)
        if node is None:
            raise ValueError(f"{feature}: the camera does not have it")
        return node.value

    def set(self, feature: str, value: Any) -> None:
        node = self._node(feature)
        if node is None:
            raise ValueError(f"{feature}: the camera does not have it")
        try:
            if hasattr(node, "symbolics"):
                node.value = str(value)
                return
            current = node.value
            if isinstance(current, bool):
                node.value = bool(value)
            elif isinstance(current, int):
                node.value = int(round(float(value)))
            elif isinstance(current, float):
                node.value = float(value)
            else:
                node.value = value
        except Exception as exc:
            raise ValueError(f"{feature} = {value}: {_first_sentence(exc)}") from exc

    def choices(self, feature: str) -> List[str]:
        node = self._node(feature)
        try:
            return [str(name) for name in getattr(node, "symbolics", None) or []]
        except Exception:
            return []

    def execute(self, feature: str) -> None:
        node = self._node(feature)
        if node is None:
            raise ValueError(f"{feature}: the camera does not have it")
        node.execute()

    def _pick_ping_feature(self) -> Optional[str]:
        for feature in self.PING_FEATURES:
            try:
                node = self._node(feature)
                if node is not None:
                    _read_live(node)
                    return feature
            except Exception:
                continue
        return None

    def ping(self) -> None:
        if self._ping_feature is not None:
            _read_live(self._node(self._ping_feature))

    def start(self) -> None:
        try:
            self._acquirer.num_buffers = self.BUFFERS      # room for a burst of triggers
        except Exception:
            pass
        self._acquirer.start()

    def stop(self) -> None:
        if self._acquirer is not None:
            self._acquirer.stop()

    def fetch(self, timeout: float) -> Optional[Tuple[np.ndarray, str]]:
        buffer = self._acquirer.try_fetch(timeout=timeout)
        if buffer is None:
            return None
        try:
            component = buffer.payload.components[0]
            data = component.data
            if data is None:
                return None
            height, width = int(component.height), int(component.width)
            pixel_format = str(component.data_format)
            per_pixel, rest = divmod(int(data.size), max(1, height * width))
            if per_pixel < 1 or rest:
                raise ValueError(f"a picture of {data.size} values does not fit {width} x {height} ({pixel_format})")
            shape = (height, width) if per_pixel == 1 else (height, width, per_pixel)
            # The buffer goes back to the camera's driver below: keep our own copy.
            return np.array(data.reshape(shape), copy=True), pixel_format
        finally:
            buffer.queue()


def default_backend() -> GenICamBackend:
    """A backend for one camera: the stand-in when GENTL_CTI_PATH is "simulator", else the real one.

    Raises GenICamUnavailable when the library or every producer file is missing.
    """
    if (settings.GENTL_CTI_PATH or "").strip().lower() == SIMULATOR:
        from app.hardware.camera.genicam_sim import SimulatedBackend
        return SimulatedBackend(auto=True)
    _Library.harvester()
    return HarvestersBackend()
