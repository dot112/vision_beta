"""Deployment hardening: secrets kept out of logs, log rotation, SQLite settings,
self-restarting worker threads, camera and MQTT reconnects, startup that does
not wait on offline devices, and a shutdown that ends streams and makes every
PLC safe at once. Docker itself is exercised by the CI docker job."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import signal
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from app.config import Settings, settings
from app.state.application_state import app_state
from app.utils import threads as thread_utils
from app.utils.logger import LogThrottle, RedactFilter, redact

ROOT = Path(__file__).resolve().parents[1]


def _wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# ── Secrets in logs ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, secret", [
    ("Connecting via rtsp://admin:S3cr3t!@10.0.0.5:554/stream1", "S3cr3t!"),
    ("WebSocket /api/v1/ws/live/cam?token=eyJhbGciOi.payload.sig&fps=5", "eyJhbGciOi.payload.sig"),
    ('{"username": "op", "password": "hunter22"}', "hunter22"),
    ("{'password': 'hunter22', 'port': 1883}", "hunter22"),
    ("broker rejected api_key=abc123def, retrying", "abc123def"),
])
def test_redact_masks_credentials(raw, secret):
    masked = redact(raw)
    assert secret not in masked
    assert "***" in masked


@pytest.mark.parametrize("text", [
    "rtsp://10.0.0.5:554/stream1",
    "http://viewer@10.0.0.5/video",
    "Token boot_id mismatch (server was restarted)",
    "tokens=5 keys=3",
])
def test_redact_leaves_ordinary_text_alone(text):
    assert redact(text) == text


def test_log_records_are_masked_before_any_handler_writes_them():
    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = Capture()
    handler.addFilter(RedactFilter())
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("test.redact")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("Opening %s for camera %s", "rtsp://admin:pw123@cam/1", "Line 1")
    finally:
        log.removeHandler(handler)
    assert lines == ["Opening rtsp://admin:***@cam/1 for camera Line 1"]


def test_setup_logging_rotates_app_log_and_can_run_twice(monkeypatch, tmp_path):
    from logging.handlers import RotatingFileHandler

    from app.utils import logger as logger_mod

    fake_root = logging.Logger("fake-root")
    real_get_logger = logging.getLogger
    monkeypatch.setattr(logging, "getLogger", lambda name=None: fake_root if name is None else real_get_logger(name))
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "LOG_FILE_MAX_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(settings, "LOG_FILE_BACKUP_COUNT", 3)

    logger_mod.setup_logging()
    logger_mod.setup_logging()
    try:
        ours = [h for h in fake_root.handlers if getattr(h, "_vision_handler", False)]
        files = [h for h in ours if isinstance(h, RotatingFileHandler)]
        assert len(ours) == 2 and len(files) == 1  # stdout + one file, not doubled
        assert files[0].maxBytes == 2 * 1024 * 1024
        assert files[0].backupCount == 3
        assert Path(files[0].baseFilename) == tmp_path / "app.log"
    finally:
        for handler in list(fake_root.handlers):
            fake_root.removeHandler(handler)
            handler.close()


def test_log_throttle_reports_a_repeating_problem_once_per_interval(caplog):
    throttle = LogThrottle(interval=60)
    log = logging.getLogger("test.throttle")
    with caplog.at_level(logging.WARNING, logger="test.throttle"):
        assert throttle.log(log, logging.WARNING, "cam1", "camera %s down", "cam1") is True
        for _ in range(5):
            assert throttle.log(log, logging.WARNING, "cam1", "camera %s down", "cam1") is False
        assert throttle.log(log, logging.WARNING, "cam2", "camera %s down", "cam2") is True
        throttle._state["cam1"] = (time.monotonic() - 61, throttle._state["cam1"][1])
        assert throttle.log(log, logging.WARNING, "cam1", "camera %s down", "cam1") is True
    messages = [r.getMessage() for r in caplog.records]
    assert messages[:2] == ["camera cam1 down", "camera cam2 down"]
    assert "repeated 5 more time(s)" in messages[2]


# ── SQLite ────────────────────────────────────────────────────────────────────

def test_sqlite_connections_use_wal_normal_sync_and_a_busy_timeout(tmp_path):
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import session

    assert event.contains(session.engine.sync_engine, "connect", session._sqlite_pragmas)
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'wal.db').as_posix()}")
    event.listen(engine.sync_engine, "connect", session._sqlite_pragmas)

    async def read():
        try:
            async with engine.connect() as conn:
                return [
                    (await conn.execute(text(f"PRAGMA {name}"))).scalar()
                    for name in ("journal_mode", "synchronous", "busy_timeout")
                ]
        finally:
            await engine.dispose()

    assert asyncio.run(read()) == ["wal", 1, 5000]


def test_sqlite_settings_are_validated():
    with pytest.raises(ValueError):
        Settings(SECRET_KEY="k" * 40, SQLITE_JOURNAL_MODE="OFF")
    assert Settings(SECRET_KEY="k" * 40, SQLITE_SYNCHRONOUS="full").SQLITE_SYNCHRONOUS == "FULL"


def test_env_example_documents_every_setting():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, flags=re.MULTILINE))
    assert [name for name in Settings.model_fields if name not in documented] == []


# ── Worker threads ────────────────────────────────────────────────────────────

def test_crashed_worker_loop_is_logged_and_restarted(caplog):
    stop = threading.Event()
    calls = []

    def loop():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")

    before = thread_utils.restart_counts().get("test_kind", 0)
    with caplog.at_level(logging.ERROR, logger="app.utils.threads"):
        thread_utils.run_supervised("TestWorker", loop, stop, kind="test_kind", first_delay=0.01, max_delay=0.02)
    assert len(calls) == 3
    assert thread_utils.restart_counts()["test_kind"] == before + 2
    crashes = [r for r in caplog.records if "TestWorker crashed" in r.getMessage()]
    assert len(crashes) == 2 and all(r.exc_info for r in crashes)


def test_supervised_loop_is_not_restarted_while_stopping():
    stop = threading.Event()
    calls = []

    def loop():
        calls.append(1)
        stop.set()
        raise RuntimeError("raised while stopping")

    started = time.monotonic()
    thread_utils.run_supervised("Stopping", loop, stop, first_delay=5)
    assert calls == [1] and time.monotonic() - started < 1


# ── Cameras ───────────────────────────────────────────────────────────────────

class FakeCapture:
    """cv2.VideoCapture stand-in. grab() fails for good after `frames` frames (None = never)."""

    def __init__(self, frames=None):
        self.frames = frames
        self.released = False
        self.frame = np.zeros((4, 4, 3), dtype=np.uint8)

    def isOpened(self):
        return not self.released

    def set(self, *args):
        return True

    def read(self):
        return True, self.frame.copy()

    def grab(self):
        time.sleep(0.001)
        if self.frames is None:
            return True
        if self.frames <= 0:
            return False
        self.frames -= 1
        return True

    def retrieve(self):
        return True, self.frame.copy()

    def release(self):
        self.released = True


def test_ip_camera_reopens_a_stalled_stream_without_logging_its_password(monkeypatch, caplog):
    from app.hardware.camera.ip_camera import IPCamera

    captures = [FakeCapture(frames=3), FakeCapture()]
    opened = []

    def fake_open(self, url):
        opened.append(url)
        return captures[min(len(opened), len(captures)) - 1]

    monkeypatch.setattr(IPCamera, "_open_capture", fake_open)
    camera = IPCamera("cam-ip-stall", "Gate", "rtsp://admin:pw-123@10.0.0.9:554/live")
    with caplog.at_level(logging.INFO, logger="app.hardware.camera.ip_camera"):
        assert camera.connect()
        try:
            assert _wait_for(lambda: len(opened) >= 2), "the stalled stream was not reopened"
            frame_id = camera.get_latest_frame_id()
            assert _wait_for(lambda: camera.get_latest_frame_id() > frame_id), "frames did not resume"
        finally:
            camera.disconnect()
    assert captures[0].released
    assert _wait_for(lambda: captures[1].released)
    assert "pw-123" not in caplog.text
    assert "reconnected" in caplog.text


def test_ip_camera_waits_longer_between_attempts_while_unreachable(monkeypatch):
    from app.hardware.camera.ip_camera import IPCamera

    opened = []

    def fake_open(self, url):
        opened.append(time.monotonic())
        return FakeCapture(frames=0) if len(opened) == 1 else None

    monkeypatch.setattr(IPCamera, "_open_capture", fake_open)
    camera = IPCamera("cam-ip-down", "Gate", "rtsp://admin:pw-123@10.0.0.9:554/live")
    # The first frame is read in connect(); the stream then stalls and stays down.
    monkeypatch.setattr(FakeCapture, "read", lambda self: (True, self.frame.copy()))
    assert camera.connect()
    try:
        time.sleep(2.0)
    finally:
        camera.disconnect()
    retries = opened[1:]
    assert 1 <= len(retries) <= 4, f"{len(retries)} reopen attempts in 2 s"
    gaps = [b - a for a, b in zip(retries, retries[1:])]
    assert all(later >= earlier for earlier, later in zip(gaps, gaps[1:]))
    assert "pw-123" not in (camera.last_error or "")


def test_ip_camera_disconnect_leaves_a_blocked_read_to_release_its_stream(monkeypatch):
    from app.hardware.camera.ip_camera import IPCamera

    unblock = threading.Event()
    in_read = threading.Event()

    class BlockingCapture(FakeCapture):
        def grab(self):
            in_read.set()
            unblock.wait(5)
            return False

    capture = BlockingCapture()
    monkeypatch.setattr(IPCamera, "_open_capture", lambda self, url: capture)
    camera = IPCamera("cam-ip-block", "Gate", "rtsp://10.0.0.9:554/live")
    assert camera.connect()
    reader = camera._thread
    assert in_read.wait(2)
    camera.disconnect()
    assert reader.is_alive() and not capture.released  # not released under the running read
    unblock.set()
    reader.join(5)
    assert capture.released


def test_usb_camera_reopens_the_device_after_it_stops_delivering(monkeypatch):
    from app.hardware.camera.usb_camera import USBCamera

    class UsbCapture(FakeCapture):
        def __init__(self, good_reads=None):
            super().__init__()
            self.good_reads = good_reads

        def read(self):
            time.sleep(0.001)
            if self.good_reads is not None:
                if self.good_reads <= 0:
                    return False, None
                self.good_reads -= 1
            return True, self.frame.copy()

    captures = [UsbCapture(good_reads=3), UsbCapture()]
    opened = []

    def fake_open_device(self):
        capture = captures[min(len(opened), len(captures) - 1)]
        opened.append(capture)
        ok, _ = capture.read()
        return capture if ok else None

    monkeypatch.setattr(USBCamera, "REOPEN_AFTER_FAILED_READS", 5)
    monkeypatch.setattr(USBCamera, "_open_device", fake_open_device)
    camera = USBCamera("cam-usb-unplug", "Belt", "0")
    assert camera.connect()
    try:
        assert _wait_for(lambda: len(opened) >= 2), "the device was not reopened"
        frame_id = camera.get_latest_frame_id()
        assert _wait_for(lambda: camera.get_latest_frame_id() > frame_id), "frames did not resume"
    finally:
        camera.disconnect()
    assert captures[0].released
    assert _wait_for(lambda: captures[1].released)


def test_camera_reconnector_backs_off_then_forgets_a_connected_camera(monkeypatch):
    from app.services import camera_service as cs

    results = iter([(False, "no route to host"), (False, "no route to host"), (True, None)])
    attempts = []

    async def fake_connect(db, camera_id, _background=False):
        attempts.append((camera_id, _background))
        return next(results)

    monkeypatch.setattr(cs.CameraService, "connect_camera", staticmethod(fake_connect))
    monkeypatch.setattr(cs.CameraReconnector, "_pending", {})
    monkeypatch.setattr(app_state, "cameras", {})
    monkeypatch.setattr(app_state, "shutting_down", False)
    cs.CameraReconnector.want("cam-late")
    entry = cs.CameraReconnector._pending["cam-late"]

    async def run():
        await cs.CameraReconnector.tick(now=entry["next"] - 1)
        assert attempts == []  # not due yet
        delays = []
        for _ in range(3):
            await cs.CameraReconnector.tick(now=entry["next"])
            delays.append(entry["delay"])
        return delays

    delays = asyncio.run(run())
    assert attempts == [("cam-late", True)] * 3
    assert delays[1] == 2 * delays[0]
    assert cs.CameraReconnector.pending() == []


def test_camera_disconnected_during_a_retry_is_not_left_connected(monkeypatch):
    from app.services import camera_service as cs

    disconnected = []

    async def fake_connect(db, camera_id, _background=False):
        cs.CameraReconnector.forget(camera_id)  # an operator pressed Disconnect meanwhile
        return True, None

    async def fake_disconnect(db, camera_id):
        disconnected.append(camera_id)
        return True

    monkeypatch.setattr(cs.CameraService, "connect_camera", staticmethod(fake_connect))
    monkeypatch.setattr(cs.CameraService, "disconnect_camera", staticmethod(fake_disconnect))
    monkeypatch.setattr(cs.CameraReconnector, "_pending", {})
    monkeypatch.setattr(app_state, "cameras", {})
    monkeypatch.setattr(app_state, "shutting_down", False)
    cs.CameraReconnector.want("cam-race")
    asyncio.run(cs.CameraReconnector.tick(now=time.monotonic() + 3600))
    assert disconnected == ["cam-race"]


def test_video_streams_end_when_shutdown_starts(monkeypatch, fake_camera):
    from app.services.vision_service import CameraStreamPipeline

    fake_camera("cam-stream-end")
    monkeypatch.setattr(app_state, "shutting_down", True)

    async def drain(stream):
        return [chunk async for chunk in stream]

    try:
        for stream in (CameraStreamPipeline.stream_annotated_mjpeg, CameraStreamPipeline.stream_raw_mjpeg):
            assert asyncio.run(asyncio.wait_for(drain(stream("cam-stream-end")), 2)) == []
    finally:
        CameraStreamPipeline.remove_camera("cam-stream-end")


# ── MQTT ──────────────────────────────────────────────────────────────────────

def test_mqtt_keeps_retrying_in_the_background_after_a_failed_first_connect(monkeypatch):
    from app.hardware.mqtt.client import MQTTClient

    built = []

    class FakePaho:
        def __init__(self):
            self.loop_running = False
            self.delays = None

        def reconnect_delay_set(self, min_delay, max_delay):
            self.delays = (min_delay, max_delay)

        def connect(self, host, port, keepalive):
            raise ConnectionRefusedError("refused")

        def loop_start(self):
            self.loop_running = True

        def loop_stop(self):
            self.loop_running = False

        def disconnect(self):
            pass

    def fake_build(self):
        built.append(FakePaho())
        return built[-1]

    monkeypatch.setattr(MQTTClient, "_build_client", fake_build)
    client = MQTTClient(host="192.0.2.10")

    async def scenario():
        first = await client.connect()
        again = await client.connect()  # e.g. from the publish path
        client.reconfigure(host="192.0.2.11")
        after_reconfigure = await client.connect()
        return first, again, after_reconfigure

    assert asyncio.run(scenario()) == (False, False, False)
    assert len(built) == 2  # paho was left to retry; only new settings built a new client
    assert built[0].loop_running is False
    assert built[1].loop_running is True and built[1].delays == (1, 60)


def test_mqtt_reports_connection_changes():
    from app.hardware.mqtt.client import MQTTClient

    client = MQTTClient(host="192.0.2.10")
    seen = []
    client.on_state_change = seen.append
    client._on_connect(None, None, {}, 0)
    client._on_disconnect(None, None, 1)
    assert seen == [True, False]


# ── Startup and shutdown ──────────────────────────────────────────────────────

def test_startup_serves_while_slow_devices_keep_connecting(monkeypatch):
    import main

    finished = []

    async def slow_devices():
        await asyncio.sleep(0.5)
        finished.append(True)

    monkeypatch.setattr(main, "_connect_saved_hardware", slow_devices)
    monkeypatch.setattr(settings, "STARTUP_CONNECT_WAIT_SECONDS", 0.05)

    async def run():
        started = time.monotonic()
        task = await main._start_saved_connections()
        waited = time.monotonic() - started
        still_running = not task.done()
        await task
        return waited, still_running

    waited, still_running = asyncio.run(run())
    assert waited < 0.4 and still_running
    assert finished == [True]


def test_saved_connection_errors_are_logged_not_raised(monkeypatch, caplog):
    import main
    from app.services.line_service import line_manager
    from app.services.settings_persistence_service import SettingsPersistenceService

    async def boom(*args, **kwargs):
        raise RuntimeError("camera exploded")

    monkeypatch.setattr(SettingsPersistenceService, "connect_on_startup", classmethod(lambda cls: boom()))
    monkeypatch.setattr(line_manager, "connect_on_startup", boom)
    with caplog.at_level(logging.ERROR, logger="main"):
        asyncio.run(main._connect_saved_hardware())
    errors = [r for r in caplog.records if r.name == "main" and r.exc_info]
    assert [str(r.exc_info[1]) for r in errors] == ["camera exploded", "camera exploded"]


def test_shutdown_releases_every_camera(monkeypatch):
    import main

    class Camera:
        def __init__(self, fail=False):
            self.fail = fail
            self.released = False

        def disconnect(self):
            self.released = True
            if self.fail:
                raise RuntimeError("stuck")

    cameras = {"a": Camera(), "b": Camera(fail=True)}
    monkeypatch.setattr(app_state, "cameras", dict(cameras))
    main._release_cameras()
    assert all(c.released for c in cameras.values())
    assert app_state.cameras == {}


def test_sigterm_marks_shutdown_and_still_reaches_uvicorns_handler(monkeypatch):
    import main

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    received = []
    try:
        signal.signal(signal.SIGTERM, lambda signum, frame: received.append(signum))
        monkeypatch.setattr(app_state, "shutting_down", False)
        main._flag_shutdown_on_signal()
        main._flag_shutdown_on_signal()  # a second startup does not wrap the handler twice
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert app_state.shutting_down is True
        assert received == [signal.SIGTERM]
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def test_failsafe_shutdown_makes_every_endpoint_safe_at_once(monkeypatch):
    from app.services.plc_failsafe_service import PLCFailsafeService

    endpoints = {"ep1": {"id": "ep1", "timeout": 3}, "ep2": {"id": "ep2", "timeout": 3}}
    applied = []

    async def slow_apply(cls, endpoint, reason):
        await asyncio.sleep(0.3)
        applied.append((endpoint["id"], reason))
        return True, "ok"

    monkeypatch.setattr(PLCFailsafeService, "_task", None)
    monkeypatch.setattr(PLCFailsafeService, "_endpoints", classmethod(lambda cls: endpoints))
    monkeypatch.setattr(PLCFailsafeService, "safe_targets", classmethod(lambda cls, ep_id: [("%Q0.0", "RESET", 0.0)]))
    monkeypatch.setattr(PLCFailsafeService, "apply", classmethod(slow_apply))
    started = time.monotonic()
    asyncio.run(PLCFailsafeService.shutdown())
    assert sorted(applied) == [("ep1", "shutdown"), ("ep2", "shutdown")]
    assert time.monotonic() - started < 0.55


# ── Container supervisor (docker/supervise.py) ────────────────────────────────

def _load_supervisor():
    spec = importlib.util.spec_from_file_location("vision_supervisor", ROOT / "docker" / "supervise.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeChild:
    def poll(self):
        return None


def test_supervisor_waits_for_a_slow_start_without_restarting(monkeypatch):
    sup = _load_supervisor()
    answers = iter([(False, False, "ConnectionRefusedError")] * 5 + [(True, True, "HTTP 200")])
    stops = []
    monkeypatch.setattr(sup, "probe", lambda: next(answers))
    monkeypatch.setattr(sup, "request_stop", lambda child, reason: stops.append(reason))
    monkeypatch.setattr(sup, "START_PERIOD", 0)
    monkeypatch.setattr(sup, "INTERVAL", 0.01)
    monkeypatch.setattr(sup, "STARTUP_TIMEOUT", 30)
    assert sup.wait_until_started(FakeChild()) is True
    assert stops == []


def test_supervisor_counts_a_hang_right_after_the_server_started(monkeypatch):
    # The port accepts connections but /health never answers: that is a hang,
    # not a slow start, so failures must count straight away.
    sup = _load_supervisor()
    stops = []
    monkeypatch.setattr(sup, "probe", lambda: (True, False, "TimeoutError: timed out"))
    monkeypatch.setattr(sup, "request_stop", lambda child, reason: stops.append(reason) or sup.stopping.set())
    monkeypatch.setattr(sup, "START_PERIOD", 0)
    monkeypatch.setattr(sup, "INTERVAL", 0.01)
    monkeypatch.setattr(sup, "FAILURES", 2)
    monkeypatch.setattr(sup, "STARTUP_TIMEOUT", 300)
    started = time.monotonic()
    sup.watchdog(FakeChild())
    assert stops == ["watchdog"]
    assert time.monotonic() - started < 5


def test_supervisor_restarts_a_server_that_never_opens_its_port(monkeypatch):
    sup = _load_supervisor()
    stops = []
    monkeypatch.setattr(sup, "probe", lambda: (False, False, "ConnectionRefusedError"))
    monkeypatch.setattr(sup, "request_stop", lambda child, reason: stops.append(reason))
    monkeypatch.setattr(sup, "START_PERIOD", 0)
    monkeypatch.setattr(sup, "INTERVAL", 0.01)
    monkeypatch.setattr(sup, "STARTUP_TIMEOUT", 0.05)
    assert sup.wait_until_started(FakeChild()) is False
    assert stops == ["watchdog"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows retries refused loopback connects for seconds; the supervisor runs on Linux")
def test_supervisor_probe_tells_a_closed_port_from_a_silent_one(monkeypatch):
    import socket

    sup = _load_supervisor()
    monkeypatch.setattr(sup, "TIMEOUT", 0.3)

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    monkeypatch.setattr(sup, "PORT", closed.getsockname()[1])
    closed.close()
    assert sup.probe()[:2] == (False, False)

    silent = socket.socket()  # accepts connections, never answers
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)
    try:
        monkeypatch.setattr(sup, "PORT", silent.getsockname()[1])
        assert sup.probe()[:2] == (True, False)
    finally:
        silent.close()


def test_supervisor_empties_tmpdir_at_start(monkeypatch, tmp_path):
    sup = _load_supervisor()
    tmpdir = tmp_path / "tmp"
    (tmpdir / "leftover-dir").mkdir(parents=True)
    (tmpdir / "upload.part").write_bytes(b"half an upload")
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    sup.prepare_tmpdir()
    assert tmpdir.is_dir() and list(tmpdir.iterdir()) == []
