"""Check an industrial camera (GigE Vision or USB3 Vision) without starting the server.

It uses the server's own camera driver, so what works here works in the server.
Run it from the repository folder, with the server stopped (a camera can be open
in one program only):

    python "test codes/industrial_camera_check.py"                    list the cameras that are found
    python "test codes/industrial_camera_check.py" SERIAL             open one: what it offers, 3 pictures
    python "test codes/industrial_camera_check.py" SERIAL software    3 pictures by software trigger, with their times
    python "test codes/industrial_camera_check.py" SERIAL hardware Line1 20
                                                                      wait 20 s for pictures a signal on Line1 takes

Needs:  pip install -r requirements-industrial.txt   and the camera maker's SDK
(its GenTL producer file, .cti). To try the script without a camera:
    set GENTL_CTI_PATH=simulator        (Linux: export GENTL_CTI_PATH=simulator)

When a step of docs/real-life-tests/triggered-cameras.md fails, send this script's
whole output with the report.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.hardware.camera import genicam_backend  # noqa: E402
from app.hardware.camera.genicam_backend import GenICamUnavailable, cti_files  # noqa: E402
from app.hardware.camera.gige_camera import GigECamera  # noqa: E402

FEATURES = ("DeviceVendorName", "DeviceModelName", "DeviceFirmwareVersion", "Width", "Height", "PixelFormat",
            "AcquisitionMode", "TriggerSelector", "TriggerMode", "TriggerSource", "TriggerActivation", "TriggerDelay",
            "ExposureMode", "ExposureAuto", "ExposureTime", "ExposureTimeAbs", "GainAuto", "Gain", "GainRaw",
            "LineSelector", "LineMode", "LineSource", "DeviceTemperature", "GevSCPSPacketSize", "DeviceLinkThroughputLimit")
CHOICES = ("PixelFormat", "TriggerSelector", "TriggerSource", "TriggerActivation", "LineSelector", "LineSource")


def main() -> int:
    args = sys.argv[1:]
    simulated = (os.environ.get("GENTL_CTI_PATH") or genicam_backend.settings.GENTL_CTI_PATH or "").strip().lower() == "simulator"
    print("Producer files (.cti):", "the stand-in cameras (simulator)" if simulated else (cti_files() or "NONE FOUND"))
    try:
        backend = genicam_backend.default_backend()
        started = time.monotonic()
        devices = backend.list_devices()
    except GenICamUnavailable as exc:
        print("NOT READY:", exc)
        return 2
    print(f"Cameras found ({time.monotonic() - started:.1f} s):")
    for device in devices:
        print("  serial", device.get("serial"), "|", device.get("vendor"), device.get("model"), "|", device.get("interface"))
    if not devices:
        print("  none. Is the camera powered, on the same network as this computer, and not open in another program?")
    if not args:
        return 0 if devices else 1

    serial, mode = args[0], (args[1] if len(args) > 1 else "off")
    settings = {"trigger_mode": mode}
    if mode == "hardware" and len(args) > 2:
        settings["trigger_line"] = args[2]
    wait_s = float(args[3]) if len(args) > 3 else 20.0
    cam = GigECamera("check", "check", serial, settings=settings)
    if not cam.connect():
        print("COULD NOT CONNECT:", cam.last_error)
        return 3
    try:
        props = cam.get_properties()
        print("\nConnected:", props["vendor"], props["model"], "|", props["interface"])
        print("Trigger the camera accepted:", props["trigger_mode"], "(asked for:", mode + ")")
        print("Trigger inputs it offers:", props["trigger_inputs"])
        print("Its lines:", props["lines"])
        print("Settings it refused:", props["setting_problems"] or "none")
        live = cam._backend
        print("\nFeatures:")
        for name in FEATURES:
            if live.has(name):
                try:
                    print(f"  {name} = {live.get(name)}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {name}: could not be read ({type(exc).__name__})")
        for name in CHOICES:
            offered = live.choices(name)
            if offered:
                print(f"  {name} offers: {', '.join(offered)}")
        try:
            started = time.monotonic()
            live.ping()
            print(f"Link check: answered in {(time.monotonic() - started) * 1000:.0f} ms")
        except Exception as exc:  # noqa: BLE001
            print("Link check FAILED:", type(exc).__name__, exc)

        print()
        if cam.trigger_mode == "software":
            for number in range(1, 4):
                started = time.monotonic()
                try:
                    shot = cam.take_picture(timeout=3.0)
                except RuntimeError as exc:
                    print(f"  trigger {number}: REFUSED: {exc}")
                    continue
                took = (time.monotonic() - started) * 1000
                print(f"  trigger {number}:", f"picture {shot[0].shape[1]} x {shot[0].shape[0]} after {took:.0f} ms" if shot else "NO PICTURE in 3 s")
        elif cam.trigger_mode == "hardware":
            print(f"Waiting {wait_s:.0f} s for pictures. Give the camera's input ({settings.get('trigger_line', 'Line0')}) some pulses now ...")
            end = time.monotonic() + wait_s
            count = 0
            while time.monotonic() < end:
                shot = cam.next_triggered_frame()
                if shot is None:
                    time.sleep(0.01)
                    continue
                count += 1
                print(f"  picture {count}: {shot[0].shape[1]} x {shot[0].shape[0]} at {time.strftime('%H:%M:%S', time.localtime(shot[2]))}")
            print(f"Pictures: {count}. Lost in a burst: {cam.dropped_frames}. One picture per pulse is right.")
        else:
            end = time.monotonic() + 5.0
            while cam.get_latest_frame_id() < 3 and time.monotonic() < end:
                time.sleep(0.05)
            ok, mat, frame_id = cam.get_latest_raw_mat()
            print(f"Free running: {frame_id} picture(s) in up to 5 s;", f"size {mat.shape[1]} x {mat.shape[0]}" if ok else "NO PICTURE")
        props = cam.get_properties()
        print("Pixel format of the pictures:", props["pixel_format"], "| picture error:", props["picture_error"] or "none")
        return 0
    finally:
        cam.disconnect()


if __name__ == "__main__":
    sys.exit(main())
