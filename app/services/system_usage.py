"""How busy the machine is: CPU, RAM, disks, GPUs and this server's own share, for the dashboard's top bar.

One reading (take_reading) is a plain dict. CPU, RAM, disks and this server come
from psutil, the same on Windows and Linux. GPUs come from NVIDIA's management
library (nvidia-ml-py: Windows, Linux, Docker with a GPU), a Jetson board's
system files, and an AMD card's system files on Linux; a machine with none of
them lists no GPU. Anything a system does not give is None.
"""
from __future__ import annotations

import glob
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psutil

from app.services.settings_persistence_service import DATA_DIR
from app.utils.logger import LogThrottle, get_logger

logger = get_logger(__name__)
_source_errors = LogThrottle(60.0)

# The CPU's own temperature sensors, in the order they are preferred (Linux).
CPU_SENSORS = ("coretemp", "k10temp", "cpu_thermal", "soc_thermal")
# Read-only loop mounts of packages (Linux), not disks.
SKIPPED_FSTYPES = ("squashfs",)
JETSON_LOAD_FILES = ("sys/devices/gpu.0/load", "sys/devices/platform/gpu.0/load")
JETSON_GPU_ZONES = ("GPU-therm", "gpu-thermal")

_process: Optional[Any] = None


def _round(value: Any) -> Optional[float]:
    return None if value is None else round(float(value), 1)


def _this_process() -> Any:
    """This server's process, the same object every time: its cpu_percent compares with the previous call."""
    global _process
    if _process is None:
        _process = psutil.Process()
    return _process


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip().strip("\x00").strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    text = _read(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


# ── CPU, RAM, disks, this server ──────────────────────────────────────────────

def _cpu_temperature(ps: Any) -> Optional[float]:
    if not hasattr(ps, "sensors_temperatures"):
        return None
    try:
        sensors = ps.sensors_temperatures() or {}
    except Exception:
        return None
    for name in CPU_SENSORS:
        if sensors.get(name):
            return _round(sensors[name][0].current)
    readings = [t.current for entries in sensors.values() for t in entries if t.current is not None]
    return _round(max(readings)) if readings else None


def cpu_reading(ps: Any) -> Dict[str, Any]:
    freq = None
    try:
        freq = ps.cpu_freq()
    except Exception:
        pass
    load = None
    # psutil only imitates a load average on Windows: it is shown on Linux only.
    if not getattr(ps, "WINDOWS", False) and hasattr(ps, "getloadavg"):
        try:
            load = [_round(v) for v in ps.getloadavg()]
        except Exception:
            load = None
    return {
        "percent": _round(ps.cpu_percent(interval=None)),
        "per_core": [_round(v) for v in ps.cpu_percent(interval=None, percpu=True)],
        "cores_logical": ps.cpu_count(logical=True),
        "cores_physical": ps.cpu_count(logical=False),
        "freq_mhz": _round(freq.current) if freq and freq.current else None,
        "freq_max_mhz": _round(freq.max) if freq and freq.max else None,
        "load_avg": load,
        "temp_c": _cpu_temperature(ps),
    }


def memory_reading(ps: Any) -> Dict[str, Any]:
    ram = ps.virtual_memory()
    swap = ps.swap_memory()
    return {
        "percent": _round(ram.percent),
        "used": int(ram.used),
        "total": int(ram.total),
        "available": int(ram.available),
        "swap_percent": _round(swap.percent),
        "swap_used": int(swap.used),
        "swap_total": int(swap.total),
    }


def disk_readings(ps: Any, data_dir: str) -> List[Dict[str, Any]]:
    """Each disk that answers; the one holding ``data_dir`` (longest mount point containing it) is marked."""
    windows = bool(getattr(ps, "WINDOWS", False))

    def norm(path: str) -> str:
        path = path.replace("/", "\\") if windows else path
        return path.lower() if windows else path

    data = norm(data_dir)
    disks: List[Dict[str, Any]] = []
    for part in ps.disk_partitions(all=False):
        if part.fstype in SKIPPED_FSTYPES or part.mountpoint.startswith("/snap/"):
            continue
        try:
            usage = ps.disk_usage(part.mountpoint)
        except Exception:
            continue   # an empty card reader, a disconnected network drive
        disks.append({
            "mount": part.mountpoint, "device": part.device, "fstype": part.fstype, "percent": _round(usage.percent),
            "used": int(usage.used), "free": int(usage.free), "total": int(usage.total), "server_data": False,
        })
    sep = "\\" if windows else "/"
    holding = [d for d in disks if data == norm(d["mount"]).rstrip(sep) or data.startswith(norm(d["mount"]).rstrip(sep) + sep)
               or norm(d["mount"]) == sep]
    if holding:
        max(holding, key=lambda d: len(d["mount"]))["server_data"] = True
    return disks


def server_reading(ps: Any, process: Any) -> Dict[str, Any]:
    cores = ps.cpu_count(logical=True) or 1
    return {
        "cpu_percent": _round(process.cpu_percent(interval=None) / cores),
        "memory_rss": int(process.memory_info().rss),
        "memory_percent": _round(process.memory_percent()),
    }


# ── GPUs ──────────────────────────────────────────────────────────────────────

def nvidia_gpus(nvml: Any = None) -> List[Dict[str, Any]]:
    """NVIDIA GPUs through NVIDIA's management library; [] without the library or the driver."""
    if nvml is None:
        try:
            import pynvml as nvml
        except Exception:
            return []
    started = getattr(nvml, "_vision_started", None)
    if started is None:
        try:
            nvml.nvmlInit()
            started = True
        except Exception:
            started = False
        nvml._vision_started = started     # started once per process; no driver: not tried again
    if not started:
        return []
    gpus = []
    for index in range(nvml.nvmlDeviceGetCount()):
        handle = nvml.nvmlDeviceGetHandleByIndex(index)
        name = nvml.nvmlDeviceGetName(handle)
        memory = nvml.nvmlDeviceGetMemoryInfo(handle)
        try:
            temp = _round(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))
        except Exception:
            temp = None
        gpus.append({
            "name": name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name),
            "source": "nvidia",
            "percent": _round(nvml.nvmlDeviceGetUtilizationRates(handle).gpu),
            "memory_used": int(memory.used),
            "memory_total": int(memory.total),
            "temp_c": temp,
        })
    return gpus


def jetson_gpus(root: str = "/") -> List[Dict[str, Any]]:
    """A Jetson board's GPU: its load file counts tenths of a percent; its memory is the CPU's."""
    for rel in JETSON_LOAD_FILES:
        load = _read_int(os.path.join(root, rel))
        if load is None:
            continue
        temp = None
        for zone in sorted(glob.glob(os.path.join(root, "sys", "class", "thermal", "thermal_zone*"))):
            if _read(os.path.join(zone, "type")) in JETSON_GPU_ZONES:
                milli = _read_int(os.path.join(zone, "temp"))
                temp = _round(milli / 1000) if milli is not None else None
                break
        return [{
            "name": _read(os.path.join(root, "proc", "device-tree", "model")) or "Jetson GPU",
            "source": "jetson", "percent": _round(load / 10), "memory_used": None, "memory_total": None, "temp_c": temp,
        }]
    return []


def amd_gpus(root: str = "/") -> List[Dict[str, Any]]:
    """AMD cards on Linux (amdgpu driver): busy percent, VRAM and temperature from their system files."""
    gpus = []
    for card in sorted(glob.glob(os.path.join(root, "sys", "class", "drm", "card*"))):
        name = os.path.basename(card)
        if not re.fullmatch(r"card\d+", name):
            continue   # a connector (card0-DP-1), not a card
        device = os.path.join(card, "device")
        busy = _read_int(os.path.join(device, "gpu_busy_percent"))
        if busy is None:
            continue
        temps = sorted(glob.glob(os.path.join(device, "hwmon", "hwmon*", "temp1_input")))
        milli = _read_int(temps[0]) if temps else None
        gpus.append({
            "name": _read(os.path.join(device, "product_name")) or f"AMD GPU ({name})",
            "source": "amd",
            "percent": _round(busy),
            "memory_used": _read_int(os.path.join(device, "mem_info_vram_used")),
            "memory_total": _read_int(os.path.join(device, "mem_info_vram_total")),
            "temp_c": _round(milli / 1000) if milli is not None else None,
        })
    return gpus


def gpu_readings(nvml: Any = None, root: str = "/") -> List[Dict[str, Any]]:
    """Every GPU of every source; a source that fails is left out of this reading."""
    gpus: List[Dict[str, Any]] = []
    for name, read in (("nvidia", lambda: nvidia_gpus(nvml)), ("jetson", lambda: jetson_gpus(root)),
                       ("amd", lambda: amd_gpus(root))):
        try:
            gpus.extend(read())
        except Exception as exc:
            _source_errors.log(logger, logging.WARNING, name, "GPU usage from %s could not be read: %s: %s",
                               name, type(exc).__name__, exc)
    return gpus


# ── One reading ───────────────────────────────────────────────────────────────

def prime(ps: Any = psutil, process: Any = None) -> None:
    """The first cpu_percent calls (machine, per core, this process): they answer 0 and start the measurement."""
    ps.cpu_percent(interval=None)
    ps.cpu_percent(interval=None, percpu=True)
    (process or _this_process()).cpu_percent(interval=None)


def take_reading(ps: Any = psutil, nvml: Any = None, root: str = "/", data_dir: str = DATA_DIR,
                 process: Any = None) -> Dict[str, Any]:
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "cpu": cpu_reading(ps),
        "memory": memory_reading(ps),
        "disks": disk_readings(ps, data_dir),
        "gpus": gpu_readings(nvml, root),
        "server": server_reading(ps, process or _this_process()),
    }
