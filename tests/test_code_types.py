"""The code types a reader can be limited to (all, 2D, 1D or one type), and a camera's
connection state as the Cameras page shows it."""
from __future__ import annotations

import warnings

import cv2
import numpy as np
import pytest

from app.engines import qr_engine
from app.engines.qr_engine import QREngine, code_type_options, is_code_type
from app.services.line_config import normalize_line
from app.state.application_state import app_state


def _picture_with_codes() -> np.ndarray:
    """A QR code, a Data Matrix, an EAN-13 and a Code 128 side by side on white."""
    zxingcpp = pytest.importorskip("zxingcpp")
    formats = zxingcpp.BarcodeFormat
    wanted = [(formats.QRCode, "HELLO-QR"), (formats.DataMatrix, "HELLO-DM"),
              (formats.EAN13, "5901234123457"), (formats.Code128, "LOT-128")]
    with warnings.catch_warnings():
        # write_barcode makes codes in both zxing-cpp 2 and 3; version 3 calls it deprecated.
        warnings.simplefilter("ignore")
        tiles = [np.array(zxingcpp.write_barcode(fmt, text, 300, 300)) for fmt, text in wanted]
    height = max(t.shape[0] for t in tiles) + 40
    canvas = np.full((height, sum(t.shape[1] + 40 for t in tiles) + 40, 3), 255, np.uint8)
    x = 40
    for tile in tiles:
        canvas[20:20 + tile.shape[0], x:x + tile.shape[1]] = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
        x += tile.shape[1] + 40
    return canvas


def _read(engine: QREngine, picture: np.ndarray, code_type: str) -> set:
    return {c.data for c in engine.decode(picture, code_type=code_type).codes}


def test_reader_ignores_codes_of_other_types():
    picture = _picture_with_codes()
    engine = QREngine()
    assert _read(engine, picture, "all") == {"HELLO-QR", "HELLO-DM", "5901234123457", "LOT-128"}
    assert _read(engine, picture, "2d") == {"HELLO-QR", "HELLO-DM"}
    assert _read(engine, picture, "1d") == {"5901234123457", "LOT-128"}
    assert _read(engine, picture, "QRCODE") == {"HELLO-QR"}
    assert _read(engine, picture, "DATAMATRIX") == {"HELLO-DM"}
    assert _read(engine, picture, "EAN13") == {"5901234123457"}
    assert _read(engine, picture, "CODE128") == {"LOT-128"}
    assert _read(engine, picture, "AZTEC") == set()
    # However it is written, a type is the same type.
    assert _read(engine, picture, "QR Code") == _read(engine, picture, "QR_CODE") == {"HELLO-QR"}


def test_second_pass_keeps_to_the_chosen_type():
    """The slower pass for blurred codes must not bring back a type the reader is not set to."""
    from tests.test_camera_roi_and_qr import _blurred_code_frame

    engine = QREngine()
    frame = _blurred_code_frame()
    assert [c.data for c in engine.decode(frame, hard_budget_ms=500, code_type="2d").codes] == ["LOT-2026-000123"]
    assert engine.decode(frame, hard_budget_ms=150, code_type="1d").codes == []


def test_options_are_the_three_groups_then_every_type_the_reader_supports():
    options = code_type_options()
    assert [o["value"] for o in options[:3]] == ["all", "2d", "1d"]
    singles = options[3:]
    assert {"QRCODE", "EAN13"} <= {o["value"] for o in singles}
    assert {o["kind"] for o in singles} == {"2d", "1d"}
    kinds = [o["kind"] for o in singles]
    assert kinds == sorted(kinds, reverse=True)  # the 2D types, then the 1D ones
    assert all(is_code_type(o["value"]) for o in options)
    assert not is_code_type("HOLOGRAM")
    by_value = {o["value"]: o for o in singles}
    assert by_value["QRCODE"]["kind"] == "2d" and by_value["EAN13"]["kind"] == "1d"


def test_unknown_saved_type_reads_everything_rather_than_nothing(caplog):
    qr_engine.code_filter.cache_clear()
    with caplog.at_level("WARNING", logger="app.engines.qr_engine"):
        assert qr_engine.code_filter("HOLOGRAM") == qr_engine.CodeFilter()
    assert "HOLOGRAM" in caplog.text


def test_opencv_reader_honours_the_type_without_zxing(monkeypatch):
    """Without zxing-cpp the QR detector and the barcode detector are switched by the filter."""
    monkeypatch.setattr(qr_engine, "zxingcpp", None)
    for cached in (qr_engine._code_type_table, qr_engine.code_filter):
        cached.cache_clear()
    try:
        assert [o["value"] for o in code_type_options()] == ["all", "2d", "1d", "QRCODE", "EAN13", "EAN8", "UPCA", "UPCE"]
        picture = np.full((300, 300, 3), 255, np.uint8)
        code = cv2.resize(cv2.QRCodeEncoder.create().encode("PROD-0042"), None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
        picture[40:40 + code.shape[0], 40:40 + code.shape[1]] = cv2.cvtColor(code, cv2.COLOR_GRAY2BGR)
        engine = QREngine()
        assert _read(engine, picture, "all") == _read(engine, picture, "2d") == _read(engine, picture, "QRCODE") == {"PROD-0042"}
        assert _read(engine, picture, "1d") == _read(engine, picture, "EAN13") == set()
    finally:
        monkeypatch.undo()
        for cached in (qr_engine._code_type_table, qr_engine.code_filter):
            cached.cache_clear()


# ── Line settings ─────────────────────────────────────────────────────────────

def test_code_type_is_kept_for_cameras_that_read_codes_only():
    line = normalize_line({"name": "L", "cameras": [
        {"camera_id": "vision", "code_type": "QRCODE"},
        {"camera_id": "reader", "role": "qr", "code_type": "Data Matrix"},
    ]})
    vision, reader = line["cameras"]
    assert "code_type" not in vision
    assert reader["code_type"] == "DATAMATRIX"
    both = normalize_line({"name": "L", "cameras": [{"camera_id": "one", "read_codes": True, "code_type": "2D"}]})
    assert both["cameras"][0]["code_type"] == "2d"
    default = normalize_line({"name": "L", "cameras": [{"camera_id": "q", "role": "qr"}]})
    assert default["cameras"][0]["code_type"] == "all"
    with pytest.raises(ValueError, match="code type"):
        normalize_line({"name": "L", "cameras": [{"camera_id": "q", "role": "qr", "code_type": "--"}]})


def test_reader_worker_reads_only_its_cameras_code_type(monkeypatch):
    from app.schemas.qr import BarcodeDecodeResponse
    from app.services import qr_service
    from app.services.line_service import line_manager

    class Line:
        def camera_entry(self, camera_id):
            return {"camera_id": camera_id, "role": "qr", "qr_hold_ms": 1500, "code_type": "EAN13"}

        def reads_codes(self, camera_id):
            return False

    asked = []

    def decode(mat, hard_budget_ms=0.0, code_type="all"):
        asked.append(code_type)
        return BarcodeDecodeResponse(total_found=0, codes=[], decode_time_ms=1.0, image_width=10, image_height=10)

    monkeypatch.setattr(line_manager, "route", lambda camera_id: (Line(), "qr"))
    worker = qr_service._QRReaderWorker("cam-typed")
    try:
        monkeypatch.setattr(worker._engine, "decode", decode)
        worker.process(np.zeros((10, 10, 3), np.uint8))
    finally:
        worker.stop()
    assert asked == ["EAN13"]


def test_api_lists_code_types_and_refuses_one_the_reader_lacks(client, admin_headers):
    listed = client.get("/api/v1/qr/code-types", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    assert [t["value"] for t in listed.json()["types"][:3]] == ["all", "2d", "1d"]

    made = client.post("/api/v1/lines", headers=admin_headers,
                       json={"name": "Typed reader", "cameras": [{"camera_id": "typed-qr", "role": "qr", "code_type": "QRCODE"}]})
    assert made.status_code == 201, made.text
    line = made.json()
    try:
        assert line["cameras"][0]["code_type"] == "QRCODE"
        assert line["status"]["cameras"][0]["code_type"] == "QRCODE"
        bad = client.put(f"/api/v1/lines/{line['id']}", headers=admin_headers,
                         json={"cameras": [{"camera_id": "typed-qr", "role": "qr", "code_type": "HOLOGRAM"}]})
        assert bad.status_code == 422 and "HOLOGRAM" in bad.json()["detail"]
    finally:
        client.delete(f"/api/v1/lines/{line['id']}", headers=admin_headers)


# ── Connection state on the Cameras page ──────────────────────────────────────

class _Row:
    def __init__(self, camera_id: str, last_error=None):
        self.id = camera_id
        self.last_error = last_error


class _Driver:
    def __init__(self, connected=True, reconnecting=False):
        self.is_connected = connected
        self.reconnecting = reconnecting

    def get_properties(self):
        return {}


def test_connection_state_tells_a_lost_camera_from_a_disconnected_one(monkeypatch):
    from app.services.camera_service import CameraReconnector, CameraService

    monkeypatch.setattr(app_state, "cameras", {})
    monkeypatch.setattr(CameraReconnector, "_pending", {})
    monkeypatch.setattr(CameraService, "_restarting", {})
    state = CameraService.connection_state

    assert state(_Row("cam")) == "disconnected"               # a user disconnected it, or it was never connected
    assert state(_Row("cam", last_error="no route")) == "failed"

    app_state.cameras["cam"] = _Driver()
    assert state(_Row("cam")) == "connected"
    app_state.cameras["cam"].reconnecting = True               # its driver lost the stream and is reopening it
    assert state(_Row("cam")) == "reconnecting"

    del app_state.cameras["cam"]
    CameraReconnector.want("cam")                               # the server retries it in the background
    assert state(_Row("cam", last_error="no route")) == "reconnecting"
    CameraReconnector.forget("cam")                             # what a user's Disconnect does
    assert state(_Row("cam")) == "disconnected"

    CameraService._restarting["cam"] = object()                 # being restarted on a new address
    assert state(_Row("cam")) == "reconnecting"


def test_usb_camera_is_reconnecting_while_its_device_is_gone(monkeypatch):
    import threading
    import time

    from app.hardware.camera.usb_camera import USBCamera

    class Capture:
        def __init__(self, good_reads=None):
            self.good_reads = good_reads
            self.released = False

        def isOpened(self):
            return not self.released

        def read(self):
            time.sleep(0.001)
            if self.good_reads is not None:
                if self.good_reads <= 0:
                    return False, None
                self.good_reads -= 1
            return True, np.zeros((4, 4, 3), np.uint8)

        def release(self):
            self.released = True

    plugged_in = threading.Event()
    opened = []

    def open_device(self):
        if not opened:
            opened.append(Capture(good_reads=3))  # delivers three frames, then the cable is pulled
            return opened[0]
        if not plugged_in.is_set():
            self.last_error = "device not found"
            return None
        opened.append(Capture())
        return opened[-1]

    def wait_for(condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            time.sleep(0.01)
        return condition()

    monkeypatch.setattr(USBCamera, "REOPEN_AFTER_FAILED_READS", 5)
    monkeypatch.setattr(USBCamera, "_open_device", open_device)
    camera = USBCamera("cam-usb-state", "Belt", "0")
    assert camera.connect() and camera.reconnecting is False
    try:
        assert wait_for(lambda: camera.reconnecting), "a lost device was not reported"
        assert camera.is_connected
        plugged_in.set()
        assert wait_for(lambda: not camera.reconnecting), "the device coming back was not noticed"
    finally:
        camera.disconnect()
    assert camera.reconnecting is False


def test_camera_listing_carries_the_connection_state(client, admin_headers, monkeypatch):
    saved = client.post("/api/v1/system/endpoints", headers=admin_headers,
                        json={"name": "State camera", "protocol": "ipcam", "source": "http://127.0.0.1:9/state"})
    camera_id = saved.json()["id"]
    try:
        def listed_state():
            cams = client.get("/api/v1/cameras", headers=admin_headers).json()
            return next(c for c in cams if c["id"] == camera_id)["connection_state"]

        assert listed_state() == "disconnected"
        driver = _Driver(reconnecting=True)
        monkeypatch.setitem(app_state.cameras, camera_id, driver)
        assert listed_state() == "reconnecting"
        status = client.get(f"/api/v1/cameras/{camera_id}/status", headers=admin_headers).json()
        assert status["connection_state"] == "reconnecting"
        driver.reconnecting = False
        assert listed_state() == "connected"
    finally:
        monkeypatch.delitem(app_state.cameras, camera_id, raising=False)
        client.delete(f"/api/v1/system/endpoints/{camera_id}", headers=admin_headers)
