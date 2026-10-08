"""IP cameras that send JPEG pictures over HTTP (MJPEG streams, slide shows, snapshot
URLs), a camera address changed while the camera runs, and still pictures in the live feed."""
from __future__ import annotations

import base64
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest

from app.config import settings
from app.hardware.camera.base import BaseCamera
from app.hardware.camera.http_capture import HttpPictureCapture, NotAPictureSource
from app.hardware.camera.ip_camera import IPCamera
from app.state.application_state import app_state

SIZES = [(64, 48), (96, 72), (128, 96)]
LOGIN = "operator:s3cret-pw"


def _wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def _picture(number: int) -> bytes:
    """Picture `number`: flat grey of brightness 20 + 40 * number, in a size that changes with it."""
    w, h = SIZES[number % len(SIZES)]
    img = np.full((h, w, 3), 20 + 40 * number, np.uint8)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()


def _number(mat) -> int:
    return int(round((int(mat[2, 2, 0]) - 20) / 40.0))


def _showing(camera):
    """(picture number, (width, height)) of the camera's latest picture, or None."""
    if camera is None:
        return None
    ok, mat, _ = camera.get_latest_raw_mat(copy=False)
    return (_number(mat), (mat.shape[1], mat.shape[0])) if ok else None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _part(self, jpeg: bytes, with_length: bool, chunked: bool) -> None:
        head = b"--frame\r\nContent-Type: image/jpeg\r\n"
        if with_length:
            head += b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n"
        data = head + b"\r\n" + jpeg + b"\r\n"
        if chunked:
            data = f"{len(data):X}\r\n".encode() + data + b"\r\n"
        self.wfile.write(data)
        self.wfile.flush()

    def do_GET(self):
        camera: FakeCameraServer = self.server.camera
        path = self.path.split("?")[0]
        camera.requests.append((path, dict(self.headers)))
        if path == "/text":
            body = b"not a camera"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/secure" and self.headers.get("Authorization") != "Basic " + base64.b64encode(LOGIN.encode()).decode():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="camera"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/shot.jpg":
            jpeg = camera.jpeg
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.end_headers()
            self.wfile.write(jpeg)
            return
        if path not in ("/video", "/once", "/nolength", "/chunked", "/secure"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        chunked = path == "/chunked"
        if chunked:
            self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        sent = None
        try:
            while not camera.closing:
                with camera.changed:
                    jpeg = camera.jpeg
                    if jpeg is sent or (path == "/once" and sent is not None):
                        camera.changed.wait(0.05)
                        continue
                sent = jpeg
                self._part(jpeg, with_length=path != "/nolength", chunked=chunked)
        except OSError:
            pass


class FakeCameraServer:
    """A camera on a loopback port. show(n) changes its picture.

    /video     MJPEG: the current picture on connect, then each new picture once
               (how a slide show streamer and many triggered cameras send)
    /once      MJPEG: the current picture on connect and nothing after it
    /nolength  /video without Content-Length in its parts
    /chunked   /video sent with chunked transfer encoding
    /secure    /video behind a Basic login
    /shot.jpg  the current picture as one JPEG
    /text      a page that is not a picture
    """

    def __init__(self, number: int = 0, port: int = 0):
        self.changed = threading.Condition()
        self.closing = False
        self.requests = []
        self.jpeg = _picture(number)
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._server.daemon_threads = True
        self._server.camera = self
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def url(self, path: str, login: str = "") -> str:
        return f"http://{login + '@' if login else ''}127.0.0.1:{self.port}{path}"

    def show(self, number: int) -> None:
        with self.changed:
            self.jpeg = _picture(number)
            self.changed.notify_all()

    def close(self) -> None:
        self.closing = True
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def camera_server():
    server = FakeCameraServer()
    yield server
    server.close()


def _connected(url: str) -> IPCamera:
    camera = IPCamera("cam-http", "Slides", url, {"roi_enabled": False})
    assert camera.connect(), camera.last_error
    return camera


# ── The driver ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/video", "/nolength", "/chunked"])
def test_every_picture_arrives_when_the_pictures_change_size(camera_server, path):
    camera = _connected(camera_server.url(path))
    try:
        assert _showing(camera) == (0, SIZES[0])
        for number in (1, 2, 3, 4):
            camera_server.show(number)
            assert _wait_for(lambda: _showing(camera) == (number, SIZES[number % 3])), \
                f"picture {number} did not arrive, the camera shows {_showing(camera)}"
        assert camera.get_properties()["backend"] == HttpPictureCapture.backend
    finally:
        camera.disconnect()


def test_quiet_stream_is_reopened_without_a_warning_and_its_picture_is_kept(camera_server, monkeypatch, caplog):
    """/once sends a picture only to a new connection, so a new picture can
    arrive only as the first one of the reopened stream."""
    monkeypatch.setattr(settings, "CAMERA_READ_TIMEOUT_SECONDS", 0.5)
    with caplog.at_level(logging.INFO, logger="app.hardware.camera.ip_camera"):
        camera = _connected(camera_server.url("/once"))
        try:
            camera_server.show(1)
            assert _wait_for(lambda: _showing(camera) == (1, SIZES[1])), "the reopened stream's picture was dropped"
            camera_server.show(2)
            assert _wait_for(lambda: _showing(camera) == (2, SIZES[2]))
        finally:
            camera.disconnect()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text
    assert "reconnected" not in caplog.text


def test_camera_that_loses_its_stream_is_reconnecting_until_it_is_back():
    server = FakeCameraServer(number=1)
    port = server.port
    camera = _connected(server.url("/video"))
    try:
        assert camera.reconnecting is False
        server.close()  # the camera goes away; nobody disconnected it
        assert _wait_for(lambda: camera.reconnecting), "a lost stream was not reported"
        assert camera.is_connected  # still the server's camera: it keeps trying

        server = FakeCameraServer(number=2, port=port)
        assert _wait_for(lambda: not camera.reconnecting, timeout=20), "the camera did not come back"
        assert _showing(camera) == (2, SIZES[2])
    finally:
        camera.disconnect()
        server.close()
    assert camera.reconnecting is False


def test_snapshot_address_is_asked_again_for_new_pictures(camera_server):
    camera = _connected(camera_server.url("/shot.jpg"))
    try:
        first_id = camera.get_latest_frame_id()
        assert _showing(camera) == (0, SIZES[0])
        camera_server.show(1)
        assert _wait_for(lambda: _showing(camera) == (1, SIZES[1]))
        assert camera.get_latest_frame_id() > first_id
    finally:
        camera.disconnect()


def test_disconnect_does_not_wait_for_the_next_picture(camera_server):
    camera = _connected(camera_server.url("/video"))
    reader = camera._thread
    time.sleep(0.3)  # the reader is now waiting for a picture that will not come
    started = time.monotonic()
    camera.disconnect()
    reader.join(2)
    assert not reader.is_alive() and time.monotonic() - started < 1.0


def test_password_is_sent_only_after_the_camera_asks_for_it(camera_server):
    camera = _connected(camera_server.url("/secure", login=LOGIN))
    try:
        assert _showing(camera) == (0, SIZES[0])
    finally:
        camera.disconnect()
    first, second = [headers for path, headers in camera_server.requests if path == "/secure"][:2]
    assert "Authorization" not in first
    assert second["Authorization"].startswith("Basic ")


def test_other_http_sources_are_left_to_ffmpeg_and_refusals_are_not(camera_server):
    with pytest.raises(NotAPictureSource):
        HttpPictureCapture.open(camera_server.url("/text"), 2.0, 2.0)
    with pytest.raises(OSError):  # the camera answered and said no: FFmpeg would be told the same
        HttpPictureCapture.open(camera_server.url("/missing"), 2.0, 2.0)


# ── A camera address changed while the camera runs ────────────────────────────

@pytest.fixture
def keep_active_camera():
    """connect_camera records the camera it connects as the active one; put that back."""
    from app.services.settings_persistence_service import SettingsPersistenceService

    state = SettingsPersistenceService.get_state()
    previous = state.get("active_camera_id")
    yield
    state["active_camera_id"] = previous
    SettingsPersistenceService.save()


def test_saved_address_moves_the_running_camera(client, admin_headers, keep_active_camera):
    old, new = FakeCameraServer(number=1), FakeCameraServer(number=3)
    camera_id = None
    try:
        saved = client.post("/api/v1/system/endpoints", headers=admin_headers,
                            json={"name": "Moving camera", "protocol": "ipcam", "source": old.url("/video")})
        assert saved.status_code == 200, saved.text
        camera_id = saved.json()["id"]
        assert client.post(f"/api/v1/cameras/{camera_id}/connect", headers=admin_headers).status_code == 200
        assert _showing(app_state.cameras[camera_id])[0] == 1

        changed = client.put(f"/api/v1/system/endpoints/{camera_id}", headers=admin_headers,
                             json={"name": "Moving camera", "protocol": "ipcam", "source": new.url("/video")})
        assert changed.status_code == 200, changed.text
        assert _wait_for(lambda: (_showing(app_state.cameras.get(camera_id)) or [None])[0] == 3), \
            "the camera still reads its old address"
        assert app_state.cameras[camera_id].source == new.url("/video")
        listed = client.get("/api/v1/cameras", headers=admin_headers).json()
        assert next(c for c in listed if c["id"] == camera_id)["source"] == new.url("/video")
    finally:
        if camera_id:
            client.post(f"/api/v1/cameras/{camera_id}/disconnect", headers=admin_headers)
            client.delete(f"/api/v1/system/endpoints/{camera_id}", headers=admin_headers)
        old.close()
        new.close()


def test_address_changed_on_the_cameras_api_is_not_undone_by_the_next_listing(client, admin_headers):
    saved = client.post("/api/v1/system/endpoints", headers=admin_headers,
                        json={"name": "Renamed camera", "protocol": "ipcam", "source": "http://127.0.0.1:9/a"})
    assert saved.status_code == 200, saved.text
    camera_id = saved.json()["id"]
    try:
        res = client.put(f"/api/v1/cameras/{camera_id}", headers=admin_headers, json={"source": "http://127.0.0.1:9/b"})
        assert res.status_code == 200, res.text
        listed = client.get("/api/v1/cameras", headers=admin_headers).json()
        assert next(c for c in listed if c["id"] == camera_id)["source"] == "http://127.0.0.1:9/b"
        endpoints = client.get("/api/v1/system/endpoints", headers=admin_headers).json()
        assert next(e for e in endpoints if e["id"] == camera_id)["source"] == "http://127.0.0.1:9/b"
    finally:
        client.delete(f"/api/v1/system/endpoints/{camera_id}", headers=admin_headers)


def test_connection_test_passes_on_a_running_camera_only_while_it_delivers(client, admin_headers, monkeypatch):
    class NoStream:
        def __init__(self, *args):
            pass

        def set(self, *args):
            return False

        def isOpened(self):
            return False

        def release(self):
            pass

    class Running:
        is_connected = True
        name = "Probe camera"
        source = "http://127.0.0.1:9/video"
        _latest_timestamp = 0.0

    monkeypatch.setattr(cv2, "VideoCapture", NoStream)  # the address itself does not answer
    saved = client.post("/api/v1/system/endpoints", headers=admin_headers,
                        json={"name": "Probe camera", "protocol": "ipcam", "source": Running.source})
    camera_id = saved.json()["id"]
    driver = Running()
    monkeypatch.setitem(app_state.cameras, camera_id, driver)

    def test_result():
        return client.post(f"/api/v1/system/endpoints/{camera_id}/test", headers=admin_headers).json()["success"]

    try:
        driver._latest_timestamp = time.monotonic()
        assert test_result() is True
        # Still marked connected, but the stream has gone: the address is tried instead.
        driver._latest_timestamp = time.monotonic() - 60
        assert test_result() is False
        # Delivering, but from another address than the saved one.
        driver._latest_timestamp = time.monotonic()
        driver.source = "http://127.0.0.1:9/old"
        assert test_result() is False
    finally:
        monkeypatch.delitem(app_state.cameras, camera_id)
        client.delete(f"/api/v1/system/endpoints/{camera_id}", headers=admin_headers)


# ── Still pictures in the live feed and the 24/7 runner ───────────────────────

class _Still(BaseCamera):
    """A connected camera whose one picture never changes (frame id stays 1)."""

    def __init__(self, camera_id: str, brightness: int):
        super().__init__(camera_id, "Still", "0", {"roi_enabled": False})
        self.is_connected = True
        self._frame = np.full((48, 64, 3), brightness, np.uint8)
        self._latest_frame_id = 1

    def connect(self):
        return True

    def disconnect(self):
        self.is_connected = False

    def grab_frame(self):
        return True, cv2.imencode(".jpg", self._frame)[1].tobytes()

    def grab_raw_frame(self):
        return True, self._frame, self._latest_frame_id


class _DrawEngine:
    def draw_annotations_mat(self, mat, detections, **kwargs):
        if detections:
            mat[:8, :8] = 255
        return mat


def test_still_picture_is_redrawn_when_its_detections_arrive(monkeypatch):
    from app.services.line_service import line_manager
    from app.services.vision_service import CameraStreamPipeline

    monkeypatch.setattr(line_manager, "engine_for_camera", lambda camera_id: (_DrawEngine(), "fake", None))
    monkeypatch.setitem(app_state.cameras, "cam-still-feed", _Still("cam-still-feed", 90))
    publisher = CameraStreamPipeline.get_publisher("cam-still-feed")
    publisher.add_subscriber()
    try:
        assert _wait_for(lambda: publisher.get_latest_frame_id() == 1)
        first = publisher.get_latest_annotated()
        time.sleep(0.7)  # redraws that come out the same are not sent again
        assert publisher.get_latest_frame_id() == 1

        worker = CameraStreamPipeline.get_worker("cam-still-feed")
        with worker._lock:
            worker._latest_detections = ["found after the picture was first drawn"]
        assert _wait_for(lambda: publisher.get_latest_frame_id() == 2), "the still picture was not redrawn"
        assert publisher.get_latest_annotated() != first

        # With nobody watching, the picture is not kept for the next viewer.
        publisher.remove_subscriber()
        with publisher._lock:
            publisher._last_active_time = 0.0
        assert _wait_for(lambda: publisher._latest_annotated_jpeg is None)
        publisher.add_subscriber()
        assert _wait_for(lambda: publisher.get_latest_frame_id() == 3)
    finally:
        publisher.remove_subscriber()
        CameraStreamPipeline.remove_camera("cam-still-feed")


def test_first_picture_of_a_reconnected_camera_is_processed(monkeypatch):
    from app.schemas.vision import DetectionResponse
    from app.services import counting_service as counting_module
    from app.services import vision_service

    class Engine:
        is_loaded = True
        task = "detect"

        def __init__(self):
            self.seen = []

        def predict_mat(self, mat, conf_threshold=None, nms_threshold=None):
            self.seen.append(int(mat[0, 0, 0]))
            return DetectionResponse(model_name="fake", total_detections=0, detections=[], inference_time_ms=1.0,
                                     image_width=mat.shape[1], image_height=mat.shape[0])

    engine = Engine()
    from app.services.line_service import line_manager
    monkeypatch.setattr(line_manager, "engine_for_camera", lambda camera_id: (engine, "fake", None))
    monkeypatch.setattr(counting_module.counting_service, "process_frame", lambda *a, **k: [])
    monkeypatch.setitem(app_state.cameras, "cam-still-runner", _Still("cam-still-runner", 10))
    vision_service.ContinuousVisionRunner.start()
    try:
        assert _wait_for(lambda: engine.seen == [10])
        # A reconnect replaces the driver and its workers; the new driver numbers its frames from 1 again.
        vision_service.CameraStreamPipeline.remove_camera("cam-still-runner")
        monkeypatch.setitem(app_state.cameras, "cam-still-runner", _Still("cam-still-runner", 200))
        assert _wait_for(lambda: engine.seen == [10, 200]), "the reconnected camera's picture was skipped"
        time.sleep(0.2)
        assert engine.seen == [10, 200]  # and each picture is processed once
    finally:
        vision_service.ContinuousVisionRunner.stop()
        vision_service.CameraStreamPipeline.remove_camera("cam-still-runner")
