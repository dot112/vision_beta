"""The Line setup camera card's Connect switch: a camera the user switched on is kept connected.

The switch is remembered in the settings file (``cameras_kept_connected``). A camera on the list that
does not connect is retried in the background until it is switched off; it is connected when the
server starts, and a line's Stop leaves it connected.
"""
from __future__ import annotations

import asyncio
import copy
import threading
import time
from types import SimpleNamespace

import pytest

import app.services.line_service as line_module
import app.services.settings_persistence_service as persistence_module
from app.services import camera_service
from app.services.camera_service import CameraReconnector, CameraService
from app.services.counting_service import counting_service
from app.services.line_config import upgrade_state
from app.services.line_service import LineManager
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService as S
from app.state.application_state import app_state

API = "/api/v1/cameras"
RUNNING_LINE = "The running line keeps this camera connected. Stop the line first."


class FakeDriver:
    """A camera driver whose stream answers when its source is in ``up``; ``gate`` holds a connect."""

    up: set = set()
    gate: dict = {}
    opened: list = []

    def __init__(self, source):
        self.source = source.rsplit("/", 1)[-1]          # the name the test gave the camera
        self.is_connected = False
        self.last_error = None

    def connect(self):
        gate = FakeDriver.gate.get(self.source)
        if gate is not None:
            gate["started"].set()
            gate["release"].wait(10)
        self.is_connected = self.source in FakeDriver.up
        self.last_error = None if self.is_connected else "no route to host"
        if self.is_connected:
            FakeDriver.opened.append(self)
        return self.is_connected

    def disconnect(self):
        self.is_connected = False

    def get_properties(self):
        return {}


@pytest.fixture
def cams(client, admin_headers, monkeypatch):
    """``cams(source, up=True)`` saves an IP camera whose fake stream answers when ``up``; all are deleted afterwards."""
    monkeypatch.setattr(camera_service, "_instantiate_driver", lambda camera: FakeDriver(camera.source))
    monkeypatch.setattr(FakeDriver, "up", set())
    monkeypatch.setattr(FakeDriver, "gate", {})
    monkeypatch.setattr(FakeDriver, "opened", [])
    monkeypatch.setattr(CameraReconnector, "_pending", {})
    made = []

    def make(source, up=True):
        if up:
            FakeDriver.up.add(source)
        res = client.post(API, json={"name": f"Kept {source}", "type": "ip", "source": f"rtsp://10.9.9.9/{source}"},
                          headers=admin_headers)
        assert res.status_code == 201, res.text
        made.append(res.json()["id"])
        return res.json()["id"]

    make.headers = admin_headers
    yield make
    for camera_id in made:
        client.post(f"{API}/{camera_id}/disconnect", headers=admin_headers)
        client.delete(f"{API}/{camera_id}", headers=admin_headers)
    for camera_id in made:
        S.set_camera_kept(camera_id, False)


def _connected(camera_id) -> bool:
    return getattr(app_state.cameras.get(camera_id), "is_connected", False)


def _row(client, headers, camera_id):
    return next(c for c in client.get(API, headers=headers).json() if c["id"] == camera_id)


# ── Connect and Disconnect ────────────────────────────────────────────────────

def test_connect_saves_the_camera_and_connects_it(client, cams):
    cam = cams("a")
    r = client.post(f"{API}/{cam}/connect", headers=cams.headers)
    assert r.status_code == 200 and r.json() == {"status": "connected", "camera_id": cam}
    assert cam in S.kept_cameras() and _connected(cam)


def test_connect_saves_the_camera_and_retries_when_it_fails(client, cams):
    cam = cams("dead", up=False)
    r = client.post(f"{API}/{cam}/connect", headers=cams.headers)
    assert r.status_code == 200 and r.json() == {"status": "retrying", "camera_id": cam}
    assert cam in S.kept_cameras() and cam in CameraReconnector.pending()
    assert _row(client, cams.headers, cam)["connection_state"] == "reconnecting"


def test_retries_do_not_give_up(client, cams, monkeypatch):     # Review Focus 1
    cam = cams("dead", up=False)
    client.post(f"{API}/{cam}/connect", headers=cams.headers)
    monkeypatch.setattr(app_state, "shutting_down", False)
    for _ in range(10):
        asyncio.run(CameraReconnector.tick(now=time.monotonic() + 10 ** 6))
    assert cam in CameraReconnector.pending()
    assert _row(client, cams.headers, cam)["connection_state"] == "reconnecting"
    FakeDriver.up.add("dead")                                     # the camera comes back
    asyncio.run(CameraReconnector.tick(now=time.monotonic() + 10 ** 6))
    assert _connected(cam) and CameraReconnector.pending() == []


def test_disconnect_forgets_and_stops_retries(client, cams):
    cam = cams("dead", up=False)
    client.post(f"{API}/{cam}/connect", headers=cams.headers)
    r = client.post(f"{API}/{cam}/disconnect", headers=cams.headers)
    assert r.status_code == 200 and r.json() == {"status": "disconnected", "camera_id": cam}
    assert cam not in S.kept_cameras() and cam not in CameraReconnector.pending()


def test_switch_off_during_a_retry_is_not_undone(client, cams, monkeypatch):     # Review Focus 2
    cam = cams("slow", up=False)
    client.post(f"{API}/{cam}/connect", headers=cams.headers)
    assert cam in CameraReconnector.pending()
    FakeDriver.up.add("slow")
    gate = {"started": threading.Event(), "release": threading.Event()}
    FakeDriver.gate["slow"] = gate
    monkeypatch.setattr(app_state, "shutting_down", False)
    retry = threading.Thread(target=lambda: asyncio.run(CameraReconnector.tick(now=time.monotonic() + 10 ** 6)))
    retry.start()
    assert gate["started"].wait(5)                                # the retry is opening the camera
    client.post(f"{API}/{cam}/disconnect", headers=cams.headers)  # the user switches it off meanwhile
    gate["release"].set()
    retry.join(10)
    assert not _connected(cam) and cam not in S.kept_cameras()
    assert all(not d.is_connected for d in FakeDriver.opened)     # the retry's driver was closed again


def test_quick_connect_disconnect_ends_in_the_last_choice(client, cams):     # Review Focus 5
    cam = cams("slow")
    gate = {"started": threading.Event(), "release": threading.Event()}
    FakeDriver.gate["slow"] = gate
    answers = {}
    connect = threading.Thread(target=lambda: answers.update(
        connect=client.post(f"{API}/{cam}/connect", headers=cams.headers)))
    connect.start()
    assert gate["started"].wait(5)                                # Connect is opening the camera
    off = client.post(f"{API}/{cam}/disconnect", headers=cams.headers)   # then Disconnect
    gate["release"].set()
    connect.join(10)
    assert off.status_code == 200 and answers["connect"].status_code == 200
    assert answers["connect"].json()["status"] == "disconnected"
    assert not _connected(cam) and cam not in S.kept_cameras()
    assert all(not d.is_connected for d in FakeDriver.opened)

    # And the other way round: Disconnect, then Connect: connected and kept.
    FakeDriver.gate.clear()
    client.post(f"{API}/{cam}/disconnect", headers=cams.headers)
    assert client.post(f"{API}/{cam}/connect", headers=cams.headers).json()["status"] == "connected"
    assert _connected(cam) and cam in S.kept_cameras()


def test_disconnect_on_a_running_line_is_refused(client, cams):
    cam = cams("a")
    line = S.save_line({"name": "Kept test line", "enabled": True,
                        "cameras": [{"camera_id": cam, "model_id": "model-test"}]})
    line_module.line_manager.apply_state()
    try:
        client.post(f"{API}/{cam}/connect", headers=cams.headers)
        r = client.post(f"{API}/{cam}/disconnect", headers=cams.headers)
        assert r.status_code == 409 and r.json()["detail"] == RUNNING_LINE
        assert cam in S.kept_cameras() and _connected(cam)
    finally:
        S.delete_line(line["id"])
        line_module.line_manager.apply_state()


def test_over_the_camera_limit_is_refused_and_not_saved(client, cams, monkeypatch):
    from app.config import settings as app_settings

    first, second = cams("a"), cams("b")
    monkeypatch.setattr(app_settings, "MAX_CONNECTED_CAMERAS", 1)
    assert client.post(f"{API}/{first}/connect", headers=cams.headers).status_code == 200
    r = client.post(f"{API}/{second}/connect", headers=cams.headers)
    assert r.status_code == 409 and "MAX_CONNECTED_CAMERAS" in r.json()["detail"]
    assert second not in S.kept_cameras() and not _connected(second)
    assert second not in CameraReconnector.pending()


def test_deleting_a_camera_removes_it_from_the_list(client, cams):     # Review Focus 4 (second half)
    cam = cams("a")
    client.post(f"{API}/{cam}/connect", headers=cams.headers)
    assert client.delete(f"{API}/{cam}", headers=cams.headers).status_code == 200
    assert cam not in S.kept_cameras()


def test_camera_list_says_kept_connected(client, cams):
    kept, other = cams("a"), cams("b")
    client.post(f"{API}/{kept}/connect", headers=cams.headers)
    assert _row(client, cams.headers, kept)["kept_connected"] is True
    assert _row(client, cams.headers, other)["kept_connected"] is False
    status = client.get(f"{API}/{kept}/status", headers=cams.headers)
    assert status.status_code == 200 and status.json()["kept_connected"] is True


# ── The settings ──────────────────────────────────────────────────────────────

def test_the_list_is_saved_once_per_camera(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(tmp_path / "system_state.json"))
    state = copy.deepcopy(DEFAULT_STATE)
    upgrade_state(state)
    monkeypatch.setattr(S, "_state", state)
    assert S.kept_cameras() == []
    S.set_camera_kept("cam-1", True)
    S.set_camera_kept("cam-1", True)
    S.set_camera_kept("cam-2", True)
    assert S.kept_cameras() == ["cam-1", "cam-2"]
    S.set_camera_kept("cam-1", False)
    S.set_camera_kept("cam-1", False)
    assert S.kept_cameras() == ["cam-2"]
    S._state = {}
    assert S.kept_cameras() == ["cam-2"]                          # read back from the file


# ── Server start and line Stop ────────────────────────────────────────────────

@pytest.fixture
def lines(monkeypatch, tmp_path):
    """Throwaway settings file and line manager; camera connects and disconnects are recorded, not made."""
    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(tmp_path / "system_state.json"))
    state = copy.deepcopy(DEFAULT_STATE)
    upgrade_state(state)
    monkeypatch.setattr(S, "_state", state)
    monkeypatch.setattr(S, "_recent_changes", [])
    manager = LineManager()
    monkeypatch.setattr(line_module, "line_manager", manager)
    monkeypatch.setattr(app_state, "cameras", {})
    monkeypatch.setattr(CameraReconnector, "_pending", {})
    saved_config = counting_service.config
    offline = set()

    async def connect_camera(db, camera_id, _background=False):
        if camera_id in offline:
            return False, "no route to host"
        app_state.cameras[camera_id] = SimpleNamespace(is_connected=True)
        return True, None

    async def disconnect_camera(db, camera_id):
        CameraReconnector.forget(camera_id)
        app_state.cameras.pop(camera_id, None)
        return True

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    import app.db.session as session_module
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(CameraService, "connect_camera", staticmethod(connect_camera))
    monkeypatch.setattr(CameraService, "disconnect_camera", staticmethod(disconnect_camera))
    manager.offline = offline
    yield manager
    counting_service.event_sink = None
    counting_service.update_config(saved_config)


def _line(name, cameras, enabled=True):
    line = S.save_line(
        {"name": name, "cameras": [{"camera_id": c, "model_id": "model-test"} for c in cameras], "enabled": enabled})
    line_module.line_manager.apply_state()
    return line


def test_startup_connects_kept_cameras_even_with_auto_connect_off(lines):
    S.update_settings({"camera_auto_connect": False})
    S.set_camera_kept("cam-kept", True)
    S.set_camera_kept("cam-down", True)
    lines.offline.add("cam-down")
    _line("Line 2", ["cam-line"])
    asyncio.run(lines.connect_on_startup())
    assert _connected("cam-kept")
    assert not _connected("cam-line")                             # the switch is off: lines connect nothing
    assert CameraReconnector.pending() == ["cam-down"]            # retried in the background


def test_startup_with_auto_connect_on_still_connects_the_running_lines(lines):
    S.update_settings({"camera_auto_connect": True})
    S.set_camera_kept("cam-kept", True)
    _line("Line 2", ["cam-line"])
    asyncio.run(lines.connect_on_startup())
    assert _connected("cam-kept") and _connected("cam-line")


def test_line_stop_leaves_kept_cameras_connected(lines):
    line = _line("Line 2", ["cam-kept", "cam-other"])
    asyncio.run(lines.connect_line(line["id"]))
    S.set_camera_kept("cam-kept", True)
    asyncio.run(lines.disconnect_line(line["id"]))
    assert _connected("cam-kept") and not _connected("cam-other")


def test_camera_off_a_line_but_kept_stays_connected(lines):     # Review Focus 4
    line = _line("Line 2", ["cam-kept", "cam-other"])
    asyncio.run(lines.connect_line(line["id"]))
    S.set_camera_kept("cam-kept", True)
    S.save_line({"id": line["id"], "cameras": [{"camera_id": "cam-other", "model_id": "model-test"}]})
    lines.apply_state()
    asyncio.run(lines.disconnect_line(line["id"]))                # and the line is stopped later
    assert _connected("cam-kept")


def test_a_retry_that_lands_after_connect_was_pressed_again_stays_connected(client, cams, monkeypatch):
    cam = cams("slow", up=False)
    client.post(f"{API}/{cam}/connect", headers=cams.headers)
    FakeDriver.up.add("slow")
    gate = {"started": threading.Event(), "release": threading.Event()}
    FakeDriver.gate["slow"] = gate
    monkeypatch.setattr(app_state, "shutting_down", False)
    retry = threading.Thread(target=lambda: asyncio.run(CameraReconnector.tick(now=time.monotonic() + 10 ** 6)))
    retry.start()
    assert gate["started"].wait(5)
    CameraReconnector.forget(cam)              # what a second Connect does first; the switch stays on
    gate["release"].set()
    retry.join(10)
    assert _connected(cam) and cam in S.kept_cameras()
