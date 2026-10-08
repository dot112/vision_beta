"""docker/detect_hardware.py: the plan and Compose override for each kind of host."""
import importlib.util
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "detect_hardware.py"
spec = importlib.util.spec_from_file_location("detect_hardware", SCRIPT)
detect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detect)

GTX = ["NVIDIA GeForce GTX 1650 (4096 MiB)"]
CAMERA = {"path": "/dev/video0", "gid": 44, "mode": 0o660, "name": "HD Webcam"}
SERIAL = {"path": "/dev/ttyUSB0", "gid": 20, "mode": 0o660, "name": "usb-FTDI_RS485"}


def facts(system="Linux", machine="x86_64", arch="x86_64", runtimes=("runc",), gpus=(),
          jetson="", cameras=(), serial=(), docker_os="Ubuntu 24.04", available=True):
    return {
        "system": system,
        "machine": machine,
        "docker": {"available": available, "arch": arch, "os": docker_os, "runtimes": list(runtimes)},
        "gpus": list(gpus),
        "jetson": jetson,
        "cameras": list(cameras),
        "serial": list(serial),
    }


def service(plan, host, image="vision:1"):
    return yaml.safe_load(detect.render_override(plan, host, image))["services"]["vision"]


def test_linux_pc_with_nvidia_gpu_camera_and_serial_adapter():
    host = facts(runtimes=("nvidia", "runc"), gpus=GTX, cameras=[CAMERA], serial=[SERIAL])
    plan = detect.build_plan(host)

    assert plan["accel"] == "nvidia" and plan["gpu"]
    assert plan["devices"] == ["/dev/video0", "/dev/ttyUSB0"]
    assert plan["groups"] == ["44", "20"]
    assert plan["notes"] == []

    svc = service(plan, host)
    assert svc["image"] == "vision:1-nvidia"
    assert svc["build"]["args"]["ACCEL"] == "nvidia"
    assert svc["environment"]["INFERENCE_DEVICE"] == "cuda"
    gpu = svc["deploy"]["resources"]["reservations"]["devices"][0]
    assert gpu == {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}
    assert svc["devices"] == ["/dev/video0:/dev/video0", "/dev/ttyUSB0:/dev/ttyUSB0"]
    assert svc["group_add"] == ["44", "20"]


def test_windows_pc_gets_the_gpu_but_no_usb_devices():
    host = facts(system="Windows", machine="AMD64", runtimes=("nvidia", "runc"), gpus=GTX,
                 docker_os="Docker Desktop")
    plan = detect.build_plan(host)

    assert plan["accel"] == "nvidia"
    assert plan["devices"] == [] and plan["groups"] == []
    assert any("cannot pass USB cameras" in note for note in plan["notes"])
    svc = service(plan, host)
    assert "devices" not in svc and "group_add" not in svc


def test_gpu_without_the_nvidia_runtime_falls_back_to_cpu_and_says_why():
    host = facts(gpus=GTX)
    plan = detect.build_plan(host)

    assert plan["accel"] == "cpu" and not plan["gpu"]
    assert any("NVIDIA Container Toolkit" in note for note in plan["notes"])
    svc = service(plan, host)
    assert "deploy" not in svc and "image" not in svc
    assert svc["build"]["args"]["ACCEL"] == "cpu"
    assert svc["environment"]["INFERENCE_DEVICE"] == "cpu"


def test_jetson_runs_on_cpu_and_still_gets_its_devices():
    host = facts(machine="aarch64", arch="aarch64", runtimes=("nvidia", "runc"),
                 jetson="NVIDIA Jetson Nano Developer Kit", cameras=[CAMERA])
    plan = detect.build_plan(host)

    assert plan["accel"] == "cpu"
    assert plan["devices"] == ["/dev/video0"]
    assert any("Jetson" in note for note in plan["notes"])


def test_arm_board_without_a_gpu_is_plain_cpu():
    plan = detect.build_plan(facts(machine="aarch64", arch="aarch64"))
    assert plan["accel"] == "cpu" and plan["notes"] == []


def test_forced_build_is_kept_and_warns_when_no_gpu_was_found():
    plan = detect.build_plan(facts(), accel="nvidia")
    assert plan["accel"] == "nvidia"
    assert any("forced" in note for note in plan["notes"])

    assert detect.build_plan(facts(runtimes=("nvidia",), gpus=GTX), accel="cpu")["accel"] == "cpu"


def test_device_groups_follow_the_device_permissions():
    everyone = dict(CAMERA, path="/dev/video1", mode=0o666)
    root_only = dict(SERIAL, path="/dev/ttyACM0", gid=0, mode=0o600)
    plan = detect.build_plan(facts(cameras=[CAMERA, everyone], serial=[root_only]))

    assert plan["devices"] == ["/dev/video0", "/dev/video1", "/dev/ttyACM0"]
    assert plan["groups"] == ["44"]
    assert any("/dev/ttyACM0" in note and "root" in note for note in plan["notes"])


def test_docker_desktop_on_linux_passes_no_devices():
    plan = detect.build_plan(facts(cameras=[CAMERA], docker_os="Docker Desktop"))
    assert plan["devices"] == []
    assert any("Docker Desktop" in note for note in plan["notes"])


def test_devices_are_found_in_numeric_order_with_their_names(tmp_path):
    dev, sysfs = tmp_path / "dev", tmp_path / "sys"
    for node in ("video10", "video0", "video2", "ttyUSB0", "videotape", "null"):
        dev.mkdir(exist_ok=True)
        (dev / node).write_text("")
    name = sysfs / "class" / "video4linux" / "video2" / "name"
    name.parent.mkdir(parents=True)
    name.write_text("Integrated Camera\n")

    cameras = detect.list_devices(detect.CAMERA_PATTERNS, str(dev), str(sysfs), require_char=False)
    assert [c["path"] for c in cameras] == ["/dev/video0", "/dev/video2", "/dev/video10"]
    assert cameras[1]["name"] == "Integrated Camera"

    serial = detect.list_devices(detect.SERIAL_PATTERNS, str(dev), str(sysfs), require_char=False)
    assert [s["path"] for s in serial] == ["/dev/ttyUSB0"]
    # Regular files are not devices.
    assert detect.list_devices(detect.CAMERA_PATTERNS, str(dev), str(sysfs)) == []


def test_jetson_is_recognised_from_the_device_tree(tmp_path):
    assert detect.jetson_model(str(tmp_path)) == ""
    model = tmp_path / "proc" / "device-tree" / "model"
    model.parent.mkdir(parents=True)
    model.write_text("Raspberry Pi 5 Model B\x00")
    assert detect.jetson_model(str(tmp_path)) == ""
    model.write_text("NVIDIA Jetson Orin Nano Developer Kit\x00")
    assert detect.jetson_model(str(tmp_path)) == "NVIDIA Jetson Orin Nano Developer Kit"


@pytest.mark.parametrize("raw, expected", [("AMD64", "x86_64"), ("x86_64", "x86_64"),
                                           ("aarch64", "arm64"), ("arm64", "arm64"), ("", "")])
def test_architecture_names_are_normalised(raw, expected):
    assert detect.normalize_arch(raw) == expected


def test_image_name_comes_from_the_compose_file():
    assert detect.base_image_name().startswith("fastapi-vision-server:")
