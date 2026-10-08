#!/usr/bin/env python3
"""Detect this machine's hardware and write docker-compose.override.yml for it.

A container only gets the hardware it is given when it is created, so the
detection runs on the host, before `docker compose up`:

    python docker/detect_hardware.py          # write docker-compose.override.yml
    python docker/detect_hardware.py --up     # ... then build and start
    python docker/detect_hardware.py --dry-run

Docker Compose reads docker-compose.override.yml together with
docker-compose.yml, so afterwards the usual `docker compose` commands use the
hardware. Run it again after adding or removing hardware.

What it looks for, and what it does with it:
  NVIDIA GPU (x86-64 Linux, or Windows with Docker Desktop)
      builds the image with ONNX Runtime's CUDA build (ACCEL=nvidia) and
      gives the container the GPU. Needs the NVIDIA runtime in Docker (the
      NVIDIA Container Toolkit on Linux; built into Docker Desktop).
  USB / V4L2 cameras (/dev/video*) and serial adapters (/dev/ttyUSB*, /dev/ttyACM*)
      passed into the container, with the groups that own them. Linux hosts
      only: Docker on Windows and macOS has no access to USB devices.
  Jetson, Raspberry Pi and other arm64 machines
      the image builds for the machine's own architecture and runs inference
      on the CPU; cameras and serial adapters are passed in as above.

Standard library only, and no syntax newer than Python 3.6, so it also runs
with the Python that ships with older Jetson and Raspberry Pi systems.
"""
import argparse
import glob
import json
import os
import platform
import re
import stat
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE_FILE = os.path.join(ROOT, "docker-compose.yml")
OVERRIDE_FILE = os.path.join(ROOT, "docker-compose.override.yml")

CAMERA_PATTERNS = ("video[0-9]*",)
SERIAL_PATTERNS = ("ttyUSB[0-9]*", "ttyACM[0-9]*")
# Value of INFERENCE_DEVICE for each image build (see app/engines/inference_engine.py).
INFERENCE_DEVICE = {"cpu": "cpu", "nvidia": "cuda"}


def run(cmd, timeout=30):
    """Run a command; (exit status, stdout). Status None when it could not be run."""
    try:
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout,
        )
        return res.returncode, res.stdout
    except (OSError, subprocess.SubprocessError):
        return None, ""


def normalize_arch(arch):
    arch = (arch or "").strip().lower()
    if arch in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if arch in ("aarch64", "arm64"):
        return "arm64"
    return arch


def docker_facts():
    """What the Docker engine reports: its architecture, system and runtimes."""
    facts = {"available": False, "arch": "", "os": "", "runtimes": []}
    status, out = run(["docker", "info", "--format", "{{json .}}"])
    try:
        info = json.loads(out) if out.strip() else {}
    except ValueError:
        info = {}
    facts["available"] = status == 0 and not info.get("ServerErrors")
    facts["arch"] = normalize_arch(info.get("Architecture"))
    facts["os"] = info.get("OperatingSystem") or ""
    facts["runtimes"] = sorted((info.get("Runtimes") or {}).keys())
    return facts


def nvidia_gpus():
    """NVIDIA GPUs the host's driver reports, as "name (memory)" strings."""
    status, out = run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    if status != 0:
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if parts and parts[0]:
            gpus.append("%s (%s)" % (parts[0], parts[1]) if len(parts) > 1 and parts[1] else parts[0])
    return gpus


def jetson_model(root="/"):
    """The board's name on an NVIDIA Jetson, else ""."""
    model = ""
    try:
        with open(os.path.join(root, "proc/device-tree/model"), "r", errors="ignore") as fh:
            model = fh.read().replace("\x00", "").strip()
    except OSError:
        pass
    if "jetson" in model.lower():
        return model
    if os.path.exists(os.path.join(root, "etc/nv_tegra_release")):
        return model or "NVIDIA Jetson"
    return ""


def _natural(path):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", path)]


def _read(path):
    try:
        with open(path, "r", errors="ignore") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def list_devices(patterns, dev_root="/dev", sys_root="/sys", require_char=True):
    """Device nodes matching the patterns: [{"path", "gid", "mode", "name"}]."""
    by_id = {}
    for link in glob.glob(os.path.join(dev_root, "serial", "by-id", "*")):
        by_id[os.path.realpath(link)] = os.path.basename(link)

    found = []
    for pattern in patterns:
        for path in glob.glob(os.path.join(dev_root, pattern)):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if require_char and not stat.S_ISCHR(st.st_mode):
                continue
            node = os.path.basename(path)
            name = (
                _read(os.path.join(sys_root, "class", "video4linux", node, "name"))
                or by_id.get(os.path.realpath(path), "")
            )
            found.append({
                # The path inside the container is the same as on the host.
                "path": "/dev/" + node,
                "gid": st.st_gid,
                "mode": stat.S_IMODE(st.st_mode),
                "name": name,
            })
    return sorted(found, key=lambda d: _natural(d["path"]))


def collect_facts():
    system = platform.system()
    linux = system == "Linux"
    return {
        "system": system,
        "machine": platform.machine(),
        "docker": docker_facts(),
        "gpus": nvidia_gpus(),
        "jetson": jetson_model() if linux else "",
        "cameras": list_devices(CAMERA_PATTERNS) if linux else [],
        "serial": list_devices(SERIAL_PATTERNS) if linux else [],
    }


def build_plan(facts, accel="auto"):
    """Decide the image build, the devices and the groups from the facts."""
    docker = facts.get("docker") or {}
    system = facts.get("system") or ""
    arch = normalize_arch(docker.get("arch") or facts.get("machine"))
    gpus = facts.get("gpus") or []
    jetson = facts.get("jetson") or ""
    has_runtime = "nvidia" in (docker.get("runtimes") or [])
    notes = []

    if not docker.get("available"):
        notes.append("Docker is not installed or not running, so its GPU support could not be checked.")

    chosen = "cpu"
    if accel in INFERENCE_DEVICE:
        chosen = accel
        if accel == "nvidia" and not (gpus and has_runtime and arch == "x86_64"):
            notes.append(
                "--accel nvidia was forced, but no usable NVIDIA GPU was detected; "
                "the container will not start without one."
            )
    elif jetson:
        notes.append(
            "%s: inference runs on the CPU. ONNX Runtime's GPU build for Jetson is tied "
            "to the JetPack version and is not part of this image." % jetson
        )
    elif gpus and arch != "x86_64":
        notes.append("NVIDIA GPU found, but the CUDA image is built for x86-64 only; inference runs on the CPU.")
    elif gpus and not has_runtime:
        notes.append(
            "NVIDIA GPU found, but Docker has no NVIDIA runtime, so inference runs on the CPU. "
            "Install the NVIDIA Container Toolkit, restart Docker and run this again."
        )
    elif gpus:
        chosen = "nvidia"

    devices, groups = [], []
    found = (facts.get("cameras") or []) + (facts.get("serial") or [])
    desktop = "docker desktop" in (docker.get("os") or "").lower()
    if system == "Linux" and not desktop:
        for dev in found:
            devices.append(dev["path"])
            mode, gid = dev.get("mode", 0), dev.get("gid", 0)
            if mode & 0o006 == 0o006:
                continue  # readable and writable by everyone
            if mode & 0o060 == 0o060 and gid != 0:
                if str(gid) not in groups:
                    groups.append(str(gid))
            else:
                notes.append(
                    "%s can only be opened by root; the server runs as an unprivileged user "
                    "and will not be able to use it. Give a group access with a udev rule." % dev["path"]
                )
    else:
        where = "Docker Desktop" if desktop else "Docker on %s" % ("macOS" if system == "Darwin" else system or "this system")
        notes.append(
            "%s cannot pass USB cameras or serial adapters into a container. Use IP cameras "
            "and network PLCs, or run the server natively (start_server.bat) for USB devices." % where
        )

    return {
        "accel": chosen,
        "inference_device": INFERENCE_DEVICE[chosen],
        "gpu": chosen == "nvidia",
        "devices": devices,
        "groups": groups,
        "notes": notes,
    }


def base_image_name(compose_file=COMPOSE_FILE):
    """The image name docker-compose.yml gives the service, else ""."""
    match = re.search(r"^\s*image:\s*([^\s#]+)", _read(compose_file), re.MULTILINE)
    return match.group(1).strip("'\"") if match else ""


def summary(plan, facts):
    """The report printed on screen and written as the override's header."""
    docker = facts.get("docker") or {}
    host = "%s %s" % (facts.get("system") or "?", facts.get("machine") or "?")
    if facts.get("jetson"):
        host += ", " + facts["jetson"]
    if docker.get("available"):
        host += "; %s (linux/%s)" % (docker.get("os") or "Docker", normalize_arch(docker.get("arch")) or "?")

    gpus = facts.get("gpus") or []
    if plan["gpu"]:
        inference = "NVIDIA CUDA on " + (", ".join(gpus) or "the GPU")
    elif gpus:
        inference = "CPU (%s not used)" % ", ".join(gpus)
    else:
        inference = "CPU (no NVIDIA GPU found)"

    def listed(devs):
        used = [d for d in devs if d["path"] in plan["devices"]]
        if not used:
            return "none passed in"
        return ", ".join("%s (%s)" % (d["path"], d["name"]) if d.get("name") else d["path"] for d in used)

    return [
        ("Host", host),
        ("Inference", inference),
        ("Cameras", listed(facts.get("cameras") or [])),
        ("Serial", listed(facts.get("serial") or [])),
    ]


def render_override(plan, facts, base_image="", generated=""):
    """The text of docker-compose.override.yml for the plan."""
    out = [
        "# Written by docker/detect_hardware.py for this machine%s." % (" (%s)" % generated if generated else ""),
        "# Docker Compose reads it together with docker-compose.yml. Run the script again",
        "# after adding or removing hardware; delete this file for the plain CPU setup.",
    ]
    if plan["gpu"] or plan["devices"]:
        out.append("# The container does not start while a GPU or device listed here is missing.")
    out.append("#")
    for label, value in summary(plan, facts):
        out.append("#   %-10s %s" % (label + ":", value))
    out += ["", "services:", "  vision:"]
    if plan["accel"] != "cpu" and base_image:
        out.append("    image: %s-%s" % (base_image, plan["accel"]))
    out += [
        "    build:",
        "      args:",
        "        ACCEL: %s" % plan["accel"],
        "    environment:",
        "      INFERENCE_DEVICE: %s" % plan["inference_device"],
    ]
    if plan["gpu"]:
        out += [
            "    deploy:",
            "      resources:",
            "        reservations:",
            "          devices:",
            "            - driver: nvidia",
            "              count: all",
            "              capabilities: [gpu]",
        ]
    if plan["devices"]:
        out.append("    devices:")
        out += ["      - %s:%s" % (path, path) for path in plan["devices"]]
    if plan["groups"]:
        out.append("    group_add:")
        out += ['      - "%s"' % gid for gid in plan["groups"]]
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Detect this machine's hardware for the vision server's container.")
    parser.add_argument("--accel", choices=["auto", "cpu", "nvidia"], default="auto",
                        help="image build: auto (default) picks nvidia when a usable NVIDIA GPU is found")
    parser.add_argument("--output", default=OVERRIDE_FILE, help="file to write (default: docker-compose.override.yml)")
    parser.add_argument("--dry-run", action="store_true", help="print the file instead of writing it")
    parser.add_argument("--up", action="store_true", help="then run `docker compose up -d --build`")
    args = parser.parse_args(argv)

    facts = collect_facts()
    plan = build_plan(facts, args.accel)
    text = render_override(plan, facts, base_image_name(), time.strftime("%Y-%m-%d %H:%M"))

    for label, value in summary(plan, facts):
        print("%-10s %s" % (label + ":", value))
    for note in plan["notes"]:
        print("Note:      " + note)

    if args.dry_run:
        print("\n" + text, end="")
        return 0
    with open(args.output, "w", newline="\n") as fh:
        fh.write(text)
    print("Wrote " + args.output)

    if args.up:
        try:
            return subprocess.call(["docker", "compose", "up", "-d", "--build"], cwd=ROOT)
        except OSError as exc:
            print("Could not run docker compose: %s" % exc, file=sys.stderr)
            return 1
    print("Start with: docker compose up -d --build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
