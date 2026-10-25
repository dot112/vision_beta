"""System usage readings: CPU, RAM, disks and this server from psutil; GPUs from NVIDIA's library, Jetson and AMD system files."""
from __future__ import annotations

from collections import namedtuple
from types import SimpleNamespace

import pytest

from app.services.system_usage import (
    amd_gpus,
    cpu_reading,
    disk_readings,
    gpu_readings,
    jetson_gpus,
    nvidia_gpus,
    take_reading,
)

Freq = namedtuple("Freq", "current min max")
Vmem = namedtuple("Vmem", "total available percent used free")
Swap = namedtuple("Swap", "total used free percent sin sout")
Part = namedtuple("Part", "device mountpoint fstype opts")
Usage = namedtuple("Usage", "total used free percent")
Temp = namedtuple("Temp", "label current high critical")
GB = 1024 ** 3


def _ps(**over):
    """A psutil stand-in: 4 logical / 2 physical cores, 16 GB, one disk C:\\ (Windows by default)."""
    parts = over.pop("parts", [Part("C:\\", "C:\\", "NTFS", "rw,fixed")])
    usage = over.pop("usage", lambda path: Usage(100 * GB, 71 * GB, 29 * GB, 71.0))
    ps = SimpleNamespace(
        WINDOWS=True,
        cpu_percent=lambda interval=None, percpu=False: [10.0, 20.0, 30.04, 40.0] if percpu else 23.46,
        cpu_count=lambda logical=True: 4 if logical else 2,
        cpu_freq=lambda: Freq(2496.0, 0.0, 3000.0),
        virtual_memory=lambda: Vmem(16 * GB, 3 * GB, 81.24, 13 * GB, 3 * GB),
        swap_memory=lambda: Swap(4 * GB, 1 * GB, 3 * GB, 25.0, 0, 0),
        disk_partitions=lambda all=False: parts,
        disk_usage=usage,
    )
    for key, value in over.items():
        setattr(ps, key, value)
    return ps


def _process():
    return SimpleNamespace(cpu_percent=lambda interval=None: 16.4, memory_info=lambda: SimpleNamespace(rss=900 * 1024 ** 2),
                           memory_percent=lambda: 5.62)


def _nvml(fail_util=False, fail_init=False):
    def init():
        if fail_init:
            raise RuntimeError("NVML Shared Library Not Found")

    def util(handle):
        if fail_util:
            raise RuntimeError("GPU is lost")
        return SimpleNamespace(gpu=12, memory=3)

    return SimpleNamespace(
        nvmlInit=init, NVML_TEMPERATURE_GPU=0,
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda i: f"h{i}",
        nvmlDeviceGetName=lambda h: b"NVIDIA GeForce GTX 1650",
        nvmlDeviceGetUtilizationRates=util,
        nvmlDeviceGetMemoryInfo=lambda h: SimpleNamespace(total=4096 * 1024 ** 2, used=127 * 1024 ** 2, free=0),
        nvmlDeviceGetTemperature=lambda h, sensor: 49,
    )


def _write(root, rel, text):
    path = root.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _jetson(root):
    _write(root, "sys/devices/gpu.0/load", "412\n")
    _write(root, "proc/device-tree/model", "NVIDIA Jetson Orin Nano\x00")


def test_reading_has_every_key(tmp_path):
    reading = take_reading(ps=_ps(), nvml=_nvml(), root=str(tmp_path), data_dir="C:\\data", process=_process())
    assert set(reading) == {"taken_at", "cpu", "memory", "disks", "gpus", "server"}
    assert set(reading["cpu"]) == {"percent", "per_core", "cores_logical", "cores_physical", "freq_mhz", "freq_max_mhz",
                                   "load_avg", "temp_c"}
    assert set(reading["memory"]) == {"percent", "used", "total", "available", "swap_percent", "swap_used", "swap_total"}
    assert set(reading["server"]) == {"cpu_percent", "memory_rss", "memory_percent"}
    assert reading["cpu"]["percent"] == 23.5 and reading["cpu"]["per_core"] == [10.0, 20.0, 30.0, 40.0]
    assert reading["cpu"]["cores_logical"] == 4 and reading["cpu"]["cores_physical"] == 2
    assert reading["cpu"]["freq_mhz"] == 2496.0 and reading["cpu"]["freq_max_mhz"] == 3000.0
    assert reading["memory"]["percent"] == 81.2 and reading["memory"]["total"] == 16 * GB
    assert reading["server"] == {"cpu_percent": 4.1, "memory_rss": 900 * 1024 ** 2, "memory_percent": 5.6}
    assert reading["taken_at"].endswith("+00:00")


def test_linux_only_values_are_null_without_them():
    cpu = cpu_reading(_ps())
    assert cpu["load_avg"] is None and cpu["temp_c"] is None
    # psutil imitates a load average on Windows: it is not shown there.
    assert cpu_reading(_ps(getloadavg=lambda: (1.0, 1.0, 1.0)))["load_avg"] is None
    linux = cpu_reading(_ps(WINDOWS=False, getloadavg=lambda: (0.81, 0.62, 0.5)))
    assert linux["load_avg"] == [0.8, 0.6, 0.5]


def test_cpu_temperature_prefers_coretemp():
    sensors = {"acpitz": [Temp("", 80.0, None, None)], "coretemp": [Temp("Package id 0", 54.04, 90.0, 100.0)]}
    assert cpu_reading(_ps(WINDOWS=False, sensors_temperatures=lambda: sensors))["temp_c"] == 54.0
    assert cpu_reading(_ps(WINDOWS=False, sensors_temperatures=lambda: {"acpitz": [Temp("", 61.0, None, None)],
                                                                      "nvme": [Temp("", 40.0, None, None)]}))["temp_c"] == 61.0
    assert cpu_reading(_ps(WINDOWS=False, sensors_temperatures=lambda: {}))["temp_c"] is None


def test_a_partition_that_raises_is_left_out():
    def usage(path):
        if path == "E:\\":
            raise PermissionError("The device is not ready")
        return Usage(100 * GB, 50 * GB, 50 * GB, 50.0)

    parts = [Part("C:\\", "C:\\", "NTFS", "rw,fixed"), Part("E:\\", "E:\\", "", "cdrom")]
    assert [d["mount"] for d in disk_readings(_ps(parts=parts, usage=usage), "C:\\data")] == ["C:\\"]


def test_snap_loops_are_left_out():
    parts = [Part("/dev/sda1", "/", "ext4", "rw"), Part("/dev/loop3", "/snap/core/123", "squashfs", "ro")]
    assert [d["mount"] for d in disk_readings(_ps(WINDOWS=False, parts=parts), "/srv/data")] == ["/"]


def test_server_data_marks_the_longest_matching_mount():
    parts = [Part("/dev/sda1", "/", "ext4", "rw"), Part("/dev/sdb1", "/srv", "ext4", "rw")]
    disks = disk_readings(_ps(WINDOWS=False, parts=parts), "/srv/app/data")
    assert {d["mount"]: d["server_data"] for d in disks} == {"/": False, "/srv": True}
    assert set(disks[0]) == {"mount", "device", "fstype", "percent", "used", "free", "total", "server_data"}


def test_nvidia_gpus_from_the_library():
    assert nvidia_gpus(nvml=_nvml()) == [{
        "name": "NVIDIA GeForce GTX 1650", "source": "nvidia", "percent": 12.0,
        "memory_used": 127 * 1024 ** 2, "memory_total": 4096 * 1024 ** 2, "temp_c": 49.0,
    }]


def test_no_nvidia_library_or_driver():
    assert nvidia_gpus(nvml=_nvml(fail_init=True)) == []


def test_jetson_load_is_tenths_of_a_percent(tmp_path):
    _jetson(tmp_path)
    _write(tmp_path, "sys/class/thermal/thermal_zone1/type", "GPU-therm\n")
    _write(tmp_path, "sys/class/thermal/thermal_zone1/temp", "47500\n")
    assert jetson_gpus(str(tmp_path)) == [{"name": "NVIDIA Jetson Orin Nano", "source": "jetson", "percent": 41.2,
                                           "memory_used": None, "memory_total": None, "temp_c": 47.5}]


def test_amd_busy_vram_and_temperature(tmp_path):
    dev = "sys/class/drm/card0/device"
    _write(tmp_path, f"{dev}/gpu_busy_percent", "37\n")
    _write(tmp_path, f"{dev}/mem_info_vram_used", str(2 * GB))
    _write(tmp_path, f"{dev}/mem_info_vram_total", str(8 * GB))
    _write(tmp_path, f"{dev}/product_name", "Radeon RX 6600\n")
    _write(tmp_path, f"{dev}/hwmon/hwmon0/temp1_input", "61000\n")
    _write(tmp_path, "sys/class/drm/card0-DP-1/status", "connected\n")     # a connector, not a card
    assert amd_gpus(str(tmp_path)) == [{"name": "Radeon RX 6600", "source": "amd", "percent": 37.0,
                                        "memory_used": 2 * GB, "memory_total": 8 * GB, "temp_c": 61.0}]


def test_amd_without_a_name(tmp_path):
    _write(tmp_path, "sys/class/drm/card1/device/gpu_busy_percent", "5\n")
    (gpu,) = amd_gpus(str(tmp_path))
    assert gpu["name"] == "AMD GPU (card1)" and gpu["memory_total"] is None and gpu["temp_c"] is None


# ── Final review findings ─────────────────────────────────────────────────────

def test_docker_shows_each_disk_once(tmp_path):
    """In a container, Docker's file mounts and every volume of the same host disk were listed as separate disks."""
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost", encoding="utf-8")
    parts = [Part("/dev/sda1", str(hosts), "ext4", "rw"),                 # /etc/hosts: a file, not a disk
             Part("/dev/sda1", "/app/logs", "ext4", "rw"),
             Part("/dev/sda1", "/app/data", "ext4", "rw"),
             Part("/dev/sda1", "/app/model_store", "ext4", "rw"),
             Part("/dev/sdb1", "/mnt/archive", "xfs", "rw")]
    disks = disk_readings(_ps(WINDOWS=False, parts=parts), "/app/data")
    assert [(d["mount"], d["server_data"]) for d in disks] == [("/app/data", True), ("/mnt/archive", False)]


def test_same_device_keeps_the_shortest_mount_without_the_data():
    parts = [Part("/dev/sda1", "/home", "btrfs", "rw"), Part("/dev/sda1", "/", "btrfs", "rw")]
    assert [d["mount"] for d in disk_readings(_ps(WINDOWS=False, parts=parts), "/srv/data")] == ["/"]


def test_jetson_orin_jetpack6_gpu_path(tmp_path):
    """JetPack 6 on Orin puts the GPU's load under /sys/devices/platform/bus@0/gpu.0."""
    _write(tmp_path, "sys/devices/platform/bus@0/gpu.0/load", "250\n")
    _write(tmp_path, "proc/device-tree/model", "NVIDIA Jetson AGX Orin Developer Kit\x00")
    (gpu,) = jetson_gpus(str(tmp_path))
    assert gpu["percent"] == 25.0 and gpu["name"] == "NVIDIA Jetson AGX Orin Developer Kit"


def test_jetson_cpu_temperature_is_not_the_power_chip():
    """On a Jetson Nano the highest zone is PMIC-Die, which always reads 100 °C."""
    nano = {"PMIC-Die": [Temp("", 100.0, None, None)], "CPU-therm": [Temp("", 41.5, None, None)],
            "GPU-therm": [Temp("", 40.0, None, None)]}
    orin = {"tj-thermal": [Temp("", 52.0, None, None)], "cpu-thermal": [Temp("", 47.25, None, None)]}
    assert cpu_reading(_ps(WINDOWS=False, sensors_temperatures=lambda: nano))["temp_c"] == 41.5
    assert cpu_reading(_ps(WINDOWS=False, sensors_temperatures=lambda: orin))["temp_c"] == 47.2


# ── Review Focus ──────────────────────────────────────────────────────────────

def test_a_source_failing_mid_reading_is_skipped(tmp_path):
    _jetson(tmp_path)
    gpus = gpu_readings(nvml=_nvml(fail_util=True), root=str(tmp_path))
    assert [g["source"] for g in gpus] == ["jetson"]


def test_no_gpu_anywhere(tmp_path):
    assert gpu_readings(nvml=_nvml(fail_init=True), root=str(tmp_path)) == []


def test_the_real_machine_reads():
    """On whatever machine runs the tests: a full reading with no error."""
    reading = take_reading()
    assert 0 <= reading["cpu"]["percent"] <= 100 and reading["memory"]["total"] > 0
    assert reading["disks"] and any(d["server_data"] for d in reading["disks"])
