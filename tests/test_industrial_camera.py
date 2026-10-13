"""Industrial cameras (GigE Vision, USB3 Vision) through GenICam: the driver, on a stand-in camera.

No hardware and no GenICam library are needed: ``FakeBackend`` stands in for the camera.
"""
import threading
import time

import numpy as np
import pytest

from app.hardware.camera import genicam_backend
from app.hardware.camera.genicam_backend import GenICamUnavailable, cti_files
from app.hardware.camera.genicam_sim import SimulatedBackend
from app.hardware.camera.gige_camera import GigECamera, to_bgr

DEVICE = {"serial": "A1", "model": "X", "vendor": "V", "interface": "GigE"}


class FakeBackend(SimulatedBackend):
    """A camera that gives the pictures a test pushes, and remembers every call."""

    def __init__(self, devices=None, **kwargs):
        super().__init__(devices=[dict(DEVICE)] if devices is None else devices, **kwargs)


def _wait_until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return bool(condition())


@pytest.fixture
def connected():
    """``connected(settings=..., backend=...)`` gives a connected camera and its backend; all are disconnected afterwards."""
    cameras = []

    def make(settings=None, backend=None):
        backend = backend or FakeBackend()
        cam = GigECamera("c1", "Top", "A1", settings=settings, backend=backend)
        cameras.append(cam)
        assert cam.connect() is True, cam.last_error
        return cam, backend

    yield make
    for cam in cameras:
        cam.disconnect()


def _sets(backend, prefix):
    return [c for c in backend.calls if c[0] == "set" and c[1].startswith(prefix)]


# ── Connecting and reading pictures (free running) ───────────────────────────


def test_a_camera_connects_by_serial_and_gives_bgr_pictures(connected):
    cam, backend = connected()
    assert cam.is_connected and ("open", "A1") in backend.calls
    backend.push(np.full((4, 6), 200, np.uint8), "Mono8")
    assert _wait_until(lambda: cam.get_latest_frame_id() == 1)
    ok, mat, frame_id = cam.get_latest_raw_mat()
    assert ok and mat.shape == (4, 6, 3) and mat.dtype == np.uint8 and frame_id == 1
    assert int(mat[0, 0, 0]) == 200
    ok, jpeg = cam.grab_frame()
    assert ok and jpeg[:2] == b"\xff\xd8"


@pytest.mark.parametrize("pixel_format,shape", [
    ("Mono8", (4, 6)), ("BayerRG8", (4, 6)), ("BayerGB8", (4, 6)), ("BayerGR8", (4, 6)), ("BayerBG8", (4, 6)),
    ("RGB8", (4, 6, 3)), ("BGR8", (4, 6, 3)), ("RGBa8", (4, 6, 4)), ("YUV422_8", (4, 6, 2)),
])
def test_every_common_pixel_format_becomes_bgr(pixel_format, shape):
    out = to_bgr(np.full(shape, 90, np.uint8), pixel_format)
    assert out.shape == (4, 6, 3) and out.dtype == np.uint8


def test_a_bayer_picture_keeps_its_colours():
    # A red scene behind an RGGB filter: only the R sites are lit.
    mosaic = np.zeros((8, 8), np.uint8)
    mosaic[0::2, 0::2] = 255
    out = to_bgr(mosaic, "BayerRG8")
    assert out[2:6, 2:6, 2].mean() > 200 and out[2:6, 2:6, 0].mean() < 60
    blue = np.zeros((8, 8), np.uint8)
    blue[0::2, 0::2] = 255          # BGGR: the first site is blue
    out = to_bgr(blue, "BayerBG8")
    assert out[2:6, 2:6, 0].mean() > 200 and out[2:6, 2:6, 2].mean() < 60


def test_a_rgb_picture_is_turned_to_bgr():
    rgb = np.zeros((2, 2, 3), np.uint8)
    rgb[:, :, 0] = 250
    assert int(to_bgr(rgb, "RGB8")[0, 0, 2]) == 250


def test_a_12_bit_picture_is_scaled_to_8_bits():
    out = to_bgr(np.full((2, 2), 4095, np.uint16), "Mono12")
    assert out.dtype == np.uint8 and int(out[0, 0, 0]) == 255


def test_an_unknown_pixel_format_says_which_ones_work():
    with pytest.raises(ValueError, match="Mono8"):
        to_bgr(np.zeros((2, 2), np.uint8), "Mono12p")


def test_a_camera_on_an_unsupported_pixel_format_is_switched_to_one_that_works(connected):
    backend = FakeBackend(features={"PixelFormat": "Mono12p"})
    backend.choices_of["PixelFormat"] = ["Mono12p", "BayerRG8", "Mono8"]
    connected(backend=backend)
    assert ("set", "PixelFormat", "BayerRG8") in backend.calls


def test_a_picture_that_cannot_be_read_is_skipped_and_said(connected):
    cam, backend = connected()
    backend.push(np.zeros((2, 2), np.uint8), "Mono12p")
    assert _wait_until(lambda: "Mono12p" in (cam.get_properties()["picture_error"] or ""))
    assert cam.get_latest_frame_id() == 0
    backend.push(np.full((2, 2), 7, np.uint8), "Mono8")
    assert _wait_until(lambda: cam.get_latest_frame_id() == 1)
    assert cam.get_properties()["picture_error"] is None


def test_an_unknown_serial_does_not_connect():
    cam = GigECamera("c1", "Top", "NOPE", backend=FakeBackend(devices=[]))
    assert cam.connect() is False and "NOPE" in cam.last_error and not cam.is_connected


def test_a_missing_library_says_what_to_install(monkeypatch):           # Review Focus 5
    def missing():
        raise GenICamUnavailable("Install requirements-industrial.txt")

    monkeypatch.setattr(genicam_backend, "default_backend", missing)
    cam = GigECamera("c1", "Top", "A1")
    assert cam.connect() is False and "requirements-industrial.txt" in cam.last_error


def test_without_a_producer_file_the_real_backend_says_what_is_missing(monkeypatch, tmp_path):   # Review Focus 5
    monkeypatch.setattr(genicam_backend.settings, "GENTL_CTI_PATH", str(tmp_path))
    with pytest.raises(GenICamUnavailable) as raised:
        genicam_backend.default_backend()
    assert "requirements-industrial.txt" in str(raised.value) or ".cti" in str(raised.value)


def test_cti_files_are_found_in_folders_and_as_files(tmp_path, monkeypatch):
    import os

    one, two = tmp_path / "a", tmp_path / "b"
    one.mkdir()
    two.mkdir()
    (one / "maker1.cti").write_bytes(b"")
    (one / "readme.txt").write_bytes(b"")
    (two / "maker2.cti").write_bytes(b"")
    found = cti_files(os.pathsep.join([str(one), str(two / "maker2.cti"), str(one)]))
    assert [os.path.basename(f) for f in found] == ["maker1.cti", "maker2.cti"]
    monkeypatch.setenv("GENICAM_GENTL64_PATH", str(two))
    assert [os.path.basename(f) for f in cti_files("")] == ["maker2.cti"]


def test_the_simulator_setting_gives_stand_in_cameras(monkeypatch):
    monkeypatch.setattr(genicam_backend.settings, "GENTL_CTI_PATH", "simulator")
    backend = genicam_backend.default_backend()
    serials = [d["serial"] for d in backend.list_devices()]
    assert serials == ["SIM-0001", "SIM-0002"]
    cam = GigECamera("c1", "Top", "SIM-0001", settings={"trigger_mode": "software"})
    try:
        assert cam.connect() is True, cam.last_error
        shot = cam.take_picture(timeout=2.0)
        assert shot is not None and shot[0].shape[2] == 3
    finally:
        cam.disconnect()


def test_exposure_and_gain_are_set_on_connect(connected):
    _, backend = connected(settings={"exposure_us": 800, "gain_db": 6})
    assert ("set", "ExposureTime", 800.0) in backend.calls and ("set", "Gain", 6.0) in backend.calls
    assert ("set", "ExposureAuto", "Off") in backend.calls and ("set", "GainAuto", "Off") in backend.calls


def test_exposure_is_left_alone_when_it_is_not_set(connected):
    _, backend = connected()
    assert not _sets(backend, "Exposure") and not _sets(backend, "Gain")


def test_an_older_camera_takes_its_exposure_under_the_older_name(connected):
    backend = FakeBackend()
    backend.missing.add("ExposureTime")
    backend.features["ExposureTimeAbs"] = 1000.0
    connected(settings={"exposure_us": 800}, backend=backend)
    assert ("set", "ExposureTimeAbs", 800.0) in backend.calls


def test_a_setting_the_camera_refuses_is_said_and_the_camera_still_connects(connected):
    backend = FakeBackend()
    backend.refuses.add("ExposureTime")
    cam, _ = connected(settings={"exposure_us": 5}, backend=backend)
    problems = cam.get_properties()["setting_problems"]
    assert cam.is_connected and len(problems) == 1 and "ExposureTime" in problems[0]


def test_disconnect_stops_the_thread_and_closes_the_camera():
    backend = FakeBackend()
    cam = GigECamera("c1", "Top", "A1", backend=backend)
    assert cam.connect()
    cam.disconnect()
    assert not cam.is_connected
    assert backend.calls.index(("stop",)) < backend.calls.index(("close",))
    assert not [t for t in threading.enumerate() if t.name.startswith("GenICam-")]
    assert cam.get_latest_raw_mat() == (False, None, 0)


# ── Trigger modes ─────────────────────────────────────────────────────────────


def test_hardware_trigger_sets_the_cameras_features_in_order(connected):
    cam, backend = connected(settings={"trigger_mode": "hardware", "trigger_line": "Line1",
                                       "trigger_edge": "falling", "trigger_delay_us": 250})
    # The trigger is switched off while its source is changed: some cameras refuse the change otherwise.
    assert _sets(backend, "Trigger") == [
        ("set", "TriggerSelector", "FrameStart"), ("set", "TriggerMode", "Off"), ("set", "TriggerSource", "Line1"),
        ("set", "TriggerActivation", "FallingEdge"), ("set", "TriggerDelay", 250.0), ("set", "TriggerMode", "On"),
    ]
    assert cam.trigger_mode == "hardware"
    assert ("set", "AcquisitionMode", "Continuous") in backend.calls


def test_free_running_switches_the_trigger_off(connected):
    cam, backend = connected()
    assert _sets(backend, "TriggerMode") == [("set", "TriggerMode", "Off")] and cam.trigger_mode == "off"


def test_a_camera_that_refuses_the_trigger_runs_freely_and_says_so(connected):
    # Else every picture of a free running camera would be counted as a product.
    backend = FakeBackend()
    backend.refuses.add("TriggerSource")
    cam, _ = connected(settings={"trigger_mode": "hardware"}, backend=backend)
    assert cam.trigger_mode == "off" and _sets(backend, "TriggerMode")[-1] == ("set", "TriggerMode", "Off")
    assert any("TriggerSource" in p for p in cam.get_properties()["setting_problems"])


def test_a_camera_without_a_trigger_says_so(connected):
    backend = FakeBackend()
    backend.missing.update({"TriggerMode", "TriggerSource", "TriggerSelector", "TriggerActivation", "TriggerDelay"})
    cam, _ = connected(settings={"trigger_mode": "software"}, backend=backend)
    assert cam.trigger_mode == "off" and "trigger" in cam.get_properties()["setting_problems"][0].lower()


def test_a_strobe_line_follows_the_exposure(connected):
    _, backend = connected(settings={"strobe_line": "Line2"})
    wanted = [("set", "LineSelector", "Line2"), ("set", "LineMode", "Output"), ("set", "LineSource", "ExposureActive")]
    assert [c for c in backend.calls if c[0] == "set" and c[1].startswith("Line")] == wanted


def test_every_triggered_picture_is_kept_in_order(connected):        # Review Focus 1
    cam, backend = connected(settings={"trigger_mode": "hardware"})
    for value in (10, 20, 30):
        backend.push(np.full((2, 2), value, np.uint8))
    assert _wait_until(lambda: cam.get_latest_frame_id() == 3)
    assert cam.pending_triggered == 3
    got = [cam.next_triggered_frame() for _ in range(3)]
    assert [g[1] for g in got] == [1, 2, 3] and [int(g[0][0, 0, 0]) for g in got] == [10, 20, 30]
    assert got[0][2] <= got[1][2] <= got[2][2] <= time.time() and cam.next_triggered_frame() is None


def test_a_free_running_camera_keeps_no_queue(connected):
    cam, backend = connected()
    backend.push(np.zeros((2, 2), np.uint8))
    assert _wait_until(lambda: cam.get_latest_frame_id() == 1)
    assert cam.pending_triggered == 0 and cam.next_triggered_frame() is None


def test_a_full_queue_drops_the_oldest_and_counts_it(connected):      # Review Focus 2
    cam, backend = connected(settings={"trigger_mode": "hardware"})
    for _ in range(GigECamera.QUEUE_SIZE + 1):
        backend.push(np.zeros((2, 2), np.uint8))
    assert _wait_until(lambda: cam.get_latest_frame_id() == GigECamera.QUEUE_SIZE + 1)
    assert cam.dropped_frames == 1 and cam.pending_triggered == GigECamera.QUEUE_SIZE
    assert cam.next_triggered_frame()[1] == 2


def test_software_trigger_sends_the_command(connected):
    cam, backend = connected(settings={"trigger_mode": "software"})
    assert cam.trigger_mode == "software" and ("set", "TriggerSource", "Software") in backend.calls
    assert cam.trigger_software() is True and ("execute", "TriggerSoftware") in backend.calls


def test_a_refused_software_trigger_says_why(connected):
    cam, backend = connected(settings={"trigger_mode": "software"})
    backend.fail["execute"] = OSError("the camera is busy")
    assert cam.trigger_software() is False and "busy" in cam.last_error


def test_a_free_running_camera_has_no_software_trigger(connected):
    cam, backend = connected()
    assert cam.trigger_software() is False and ("execute", "TriggerSoftware") not in backend.calls


def test_take_picture_gives_the_picture_its_own_trigger_made(connected):
    cam, backend = connected(settings={"trigger_mode": "software"})
    backend.on_trigger = lambda: backend.push(np.full((2, 2), 55, np.uint8))
    mat, frame_id, arrived = cam.take_picture(timeout=1.0)
    assert int(mat[0, 0, 0]) == 55 and frame_id == 1 and arrived <= time.time()
    assert cam.pending_triggered == 0


def test_a_late_picture_is_not_taken_for_the_next_trigger(connected):   # Review Focus 4, D9
    cam, backend = connected(settings={"trigger_mode": "software"})
    assert cam.take_picture(timeout=0.05) is None                  # trigger 1: the camera does not answer in time
    backend.push(np.full((2, 2), 11, np.uint8))                    # ... its picture comes late
    assert _wait_until(lambda: cam.pending_triggered == 1)
    backend.on_trigger = lambda: backend.push(np.full((2, 2), 99, np.uint8))
    mat, _, _ = cam.take_picture(timeout=1.0)                      # trigger 2 gets its own picture
    assert int(mat[0, 0, 0]) == 99


def test_take_picture_says_why_a_camera_cannot_be_triggered(connected):
    cam, _ = connected(settings={"trigger_mode": "hardware"})
    with pytest.raises(RuntimeError, match="its own input"):
        cam.take_picture(timeout=0.05)


def test_no_picture_for_a_long_time_is_not_a_lost_camera(connected, monkeypatch):     # Review Focus 3
    monkeypatch.setattr(GigECamera, "LINK_CHECK_S", 0.05)
    monkeypatch.setattr(GigECamera, "FETCH_S", 0.02)
    cam, backend = connected(settings={"trigger_mode": "hardware"})
    time.sleep(0.4)
    assert cam.is_connected and not cam.reconnecting and cam.last_error is None
    assert backend.calls.count(("ping",)) >= 2 and backend.calls.count(("open", "A1")) == 1


def test_a_camera_that_stops_answering_is_opened_again_with_its_settings(connected, monkeypatch):   # Review Focus 3
    monkeypatch.setattr(GigECamera, "LINK_CHECK_S", 0.03)
    monkeypatch.setattr(GigECamera, "REOPEN_FIRST_S", 0.03)
    monkeypatch.setattr(GigECamera, "REOPEN_MAX_S", 0.05)
    monkeypatch.setattr(GigECamera, "FETCH_S", 0.02)
    cam, backend = connected(settings={"trigger_mode": "hardware"})
    backend.fail["ping"] = OSError("no answer")
    backend.fail["open"] = OSError("no answer")
    assert _wait_until(lambda: cam.reconnecting)
    assert "not answering" in cam.last_error and ("close",) in backend.calls
    backend.fail.clear()
    assert _wait_until(lambda: not cam.reconnecting)
    assert cam.is_connected and cam.last_error is None and cam.trigger_mode == "hardware"
    assert backend.calls.count(("set", "TriggerMode", "On")) == 2


def test_one_failed_check_is_not_a_lost_camera(connected, monkeypatch):
    monkeypatch.setattr(GigECamera, "LINK_CHECK_S", 0.03)
    monkeypatch.setattr(GigECamera, "FETCH_S", 0.02)
    cam, backend = connected(settings={"trigger_mode": "hardware"})
    backend.fail_once["ping"] = OSError("one lost packet")
    time.sleep(0.3)
    assert not cam.reconnecting and backend.calls.count(("open", "A1")) == 1


def test_a_free_running_camera_that_gives_no_pictures_is_opened_again(connected, monkeypatch):
    monkeypatch.setattr(GigECamera, "STALL_S", 0.1)
    monkeypatch.setattr(GigECamera, "REOPEN_FIRST_S", 0.02)
    monkeypatch.setattr(GigECamera, "FETCH_S", 0.02)
    cam, backend = connected()
    assert _wait_until(lambda: backend.calls.count(("open", "A1")) >= 2)


def test_clear_triggered_frames_empties_the_queue(connected):
    cam, backend = connected(settings={"trigger_mode": "hardware"})
    backend.push(np.zeros((2, 2), np.uint8))
    backend.push(np.zeros((2, 2), np.uint8))
    assert _wait_until(lambda: cam.pending_triggered == 2)
    assert cam.clear_triggered_frames() == 2 and cam.pending_triggered == 0


def test_a_changed_setting_is_applied_without_a_reconnect(connected):
    cam, backend = connected(settings={"trigger_mode": "hardware"})
    backend.push(np.zeros((2, 2), np.uint8))
    assert _wait_until(lambda: cam.pending_triggered == 1)
    cam.set_properties({"trigger_delay_us": 500, "trigger_mode": "software"})
    assert _wait_until(lambda: ("set", "TriggerDelay", 500.0) in backend.calls and cam.trigger_mode == "software")
    assert backend.calls.count(("open", "A1")) == 1
    assert cam.pending_triggered == 0          # pictures of the old mode are not kept


def test_a_setting_that_is_not_the_cameras_does_not_touch_the_camera(connected):
    cam, backend = connected()
    before = len(backend.calls)
    cam.set_properties({"rotation": 90})
    time.sleep(0.3)
    assert not [c for c in backend.calls[before:] if c[0] in ("set", "stop", "start")]


def test_the_cameras_own_inputs_are_listed(connected):
    cam, _ = connected()
    props = cam.get_properties()
    assert props["trigger_inputs"] == ["Line0", "Line1"] and props["lines"] == ["Line0", "Line1", "Line2"]
    assert props["model"] == "X" and props["type"] == "gige"


# ── The API: scan, add, settings ──────────────────────────────────────────────

API = "/api/v1/cameras"
SCANNED = [{"serial": "A1", "model": "X", "vendor": "V", "interface": "GigE"},
           {"serial": "B2", "model": "Y", "vendor": "W", "interface": "USB3"}]


@pytest.fixture
def scanner(monkeypatch):
    """The scan sees the two cameras of SCANNED."""
    monkeypatch.setattr(genicam_backend, "default_backend", lambda: FakeBackend(devices=[dict(d) for d in SCANNED]))


@pytest.fixture
def camera_row(client, admin_headers):
    """``camera_row(type="gige", source="A1", **settings)`` saves a camera and returns it; all are deleted afterwards."""
    made = []

    def make(type="gige", source="A1", **settings):
        res = client.post(API, json={"name": f"Test {type} {len(made)}", "type": type, "source": source,
                                     "settings": settings}, headers=admin_headers)
        assert res.status_code == 201, res.text
        made.append(res.json()["id"])
        return res.json()

    yield make
    for camera_id in made:
        client.delete(f"{API}/{camera_id}", headers=admin_headers)


def test_the_scan_lists_industrial_cameras_and_marks_the_ones_in_use(client, admin_headers, scanner, camera_row):
    camera_row(source="A1")
    res = client.get(f"{API}/discover/gige", headers=admin_headers)
    assert res.status_code == 200, res.text
    rows = res.json()
    assert [(r["serial"], r["model"], r["vendor"], r["interface"], r["in_use"]) for r in rows] == [
        ("A1", "X", "V", "GigE", True), ("B2", "Y", "W", "USB3", False)]


def test_the_scan_says_what_to_install_when_the_library_is_missing(client, admin_headers, monkeypatch):   # Review Focus 5
    def missing():
        raise GenICamUnavailable("Industrial cameras need the GenICam library: pip install -r requirements-industrial.txt")

    monkeypatch.setattr(genicam_backend, "default_backend", missing)
    res = client.get(f"{API}/discover/gige", headers=admin_headers)
    assert res.status_code == 503 and "requirements-industrial.txt" in res.json()["detail"]


def test_the_scan_needs_a_signed_in_user(client):
    assert client.get(f"{API}/discover/gige").status_code == 401


def test_an_industrial_camera_is_saved_with_its_trigger_settings(client, admin_headers, camera_row):
    row = camera_row(source="A1", trigger_mode="Hardware", trigger_line="Line1", trigger_edge="falling",
                     trigger_delay_us="250", exposure_us=800, gain_db="", strobe_line="Line2")
    assert row["type"] == "gige" and row["source"] == "A1"
    assert row["settings"] == {"trigger_mode": "hardware", "trigger_line": "Line1", "trigger_edge": "falling",
                               "trigger_delay_us": 250, "exposure_us": 800, "gain_db": None, "strobe_line": "Line2"}


@pytest.mark.parametrize("bad, field", [
    ({"trigger_mode": "laser"}, "trigger_mode"),
    ({"trigger_edge": "both"}, "trigger_edge"),
    ({"trigger_delay_us": -1}, "trigger_delay_us"),
    ({"trigger_delay_us": 10000001}, "trigger_delay_us"),
    ({"trigger_delay_us": "soon"}, "trigger_delay_us"),
    ({"exposure_us": 5}, "exposure_us"),
    ({"gain_db": 99}, "gain_db"),
    ({"trigger_line": "L" * 33}, "trigger_line"),
    ({"trigger_line": "Line 1; drop"}, "trigger_line"),
    ({"strobe_line": "Line1", "trigger_line": "Line1", "trigger_mode": "hardware"}, "strobe_line"),
])
def test_a_wrong_trigger_setting_is_refused_and_named(client, admin_headers, camera_row, bad, field):
    row = camera_row(source="A1", trigger_mode="software")
    res = client.patch(f"{API}/{row['id']}", json={"settings": bad}, headers=admin_headers)
    assert res.status_code == 422 and field in res.json()["detail"], res.text
    kept = client.get(f"{API}/{row['id']}", headers=admin_headers).json()["settings"]
    assert kept == {"trigger_mode": "software"}
    res = client.post(API, json={"name": "Bad", "type": "gige", "source": "Z9", "settings": bad}, headers=admin_headers)
    assert res.status_code == 422 and field in res.json()["detail"]


def test_a_trigger_setting_for_another_kind_of_camera_is_refused(client, admin_headers, camera_row):
    row = camera_row(type="usb", source="7", width=640)
    res = client.patch(f"{API}/{row['id']}", json={"settings": {"trigger_mode": "hardware"}}, headers=admin_headers)
    assert res.status_code == 422 and "industrial" in res.json()["detail"]
    assert client.patch(f"{API}/{row['id']}", json={"settings": {"rotation": 90}}, headers=admin_headers).status_code == 200


def test_a_saved_setting_reaches_the_connected_camera_without_a_reconnect(client, admin_headers, camera_row, monkeypatch):
    from app.state.application_state import app_state

    row = camera_row(source="A1", trigger_mode="hardware")
    backend = FakeBackend()
    cam = GigECamera(row["id"], row["name"], "A1", settings=dict(row["settings"]), backend=backend)
    assert cam.connect()
    monkeypatch.setitem(app_state.cameras, row["id"], cam)
    try:
        res = client.patch(f"{API}/{row['id']}", json={"settings": {"trigger_delay_us": 500}}, headers=admin_headers)
        assert res.status_code == 200 and res.json()["settings"]["trigger_delay_us"] == 500
        assert _wait_until(lambda: ("set", "TriggerDelay", 500.0) in backend.calls)
        assert backend.calls.count(("open", "A1")) == 1
        status = client.get(f"{API}/{row['id']}/status", headers=admin_headers).json()
        assert status["properties"]["trigger_mode"] == "hardware" and status["properties"]["trigger_inputs"] == ["Line0", "Line1"]
    finally:
        cam.disconnect()


def test_a_line_warns_about_a_software_camera_nothing_triggers(client, admin_headers, camera_row):
    row = camera_row(source="A1", trigger_mode="software")
    res = client.post("/api/v1/lines", json={"name": "Reader line", "cameras": [{"camera_id": row["id"], "role": "qr"}]},
                      headers=admin_headers)
    assert res.status_code in (200, 201), res.text
    line_id = res.json()["id"]
    try:
        warnings = client.get(f"/api/v1/lines/{line_id}", headers=admin_headers).json()["warnings"]
        assert any("Software trigger" in w and "Read codes" in w for w in warnings), warnings
    finally:
        client.delete(f"/api/v1/lines/{line_id}", headers=admin_headers)
