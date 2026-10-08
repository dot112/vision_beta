"""One switch, "Connect cameras when the server starts", connects the cameras of the running lines."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

import app.services.line_service as line_module
import app.services.settings_persistence_service as persistence_module
from app.services import camera_service
from app.services.counting_service import counting_service
from app.services.line_config import PRIMARY_LINE_ID, SCHEMA_VERSION, normalize_line, upgrade_state
from app.services.line_service import LineManager, pick_default_camera
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService
from app.state.application_state import app_state


@pytest.fixture
def lines(monkeypatch, tmp_path):
    """Throwaway settings file and line manager; camera connects are recorded, not made."""
    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(tmp_path / "system_state.json"))
    state = copy.deepcopy(DEFAULT_STATE)
    upgrade_state(state)
    monkeypatch.setattr(SettingsPersistenceService, "_state", state)
    monkeypatch.setattr(SettingsPersistenceService, "_recent_changes", [])
    manager = LineManager()
    monkeypatch.setattr(line_module, "line_manager", manager)
    monkeypatch.setattr(app_state, "cameras", {})
    monkeypatch.setattr(camera_service.CameraReconnector, "_pending", {})
    saved_config = counting_service.config

    calls = []
    offline = set()

    async def connect_camera(db, camera_id, _background=False):
        calls.append(camera_id)
        if camera_id in offline:
            return False, "no route to host"
        app_state.cameras[camera_id] = SimpleNamespace(is_connected=True)
        return True, None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    import app.db.session as session_module
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(camera_service.CameraService, "connect_camera", staticmethod(connect_camera))
    manager.calls, manager.offline = calls, offline
    yield manager
    counting_service.event_sink = None
    counting_service.update_config(saved_config)


def _line(name, cameras, enabled=True):
    line = SettingsPersistenceService.save_line(
        {"name": name, "cameras": [{"camera_id": c, "model_id": "model-test"} for c in cameras], "enabled": enabled})
    line_module.line_manager.apply_state()
    return line


def _switch(on: bool) -> None:
    SettingsPersistenceService.update_settings({"camera_auto_connect": on})


# ── The settings upgrade ──────────────────────────────────────────────────────

def _v2_state(cameras_switch, line_switches):
    """A version 2 file: the Cameras page switch and one (cameras, switch) pair per line."""
    lines = []
    for index, (cameras, own_switch) in enumerate(line_switches):
        line = {"id": PRIMARY_LINE_ID if index == 0 else f"line-{index + 1}", "name": f"Line {index + 1}",
                "enabled": True, "cameras": [{"camera_id": c, "role": "vision"} for c in cameras]}
        if own_switch is not None:
            line["auto_connect"] = own_switch
        lines.append(line)
    return {"schema_version": 2, "camera_auto_connect": cameras_switch, "lines": lines}


@pytest.mark.parametrize("cameras_switch, line_switches, expected", [
    (False, [(["a"], False), (["b"], False)], False),
    (True, [(["a"], False)], True),                    # the old Cameras page switch was on
    (False, [(["a"], False), (["b"], True)], True),    # one line had its own switch on
    (False, [([], True), ([], True)], False),          # switches of lines without cameras connected nothing
    (False, [(["a"], None)], True),                    # a missing line switch counted as on
])
def test_the_one_switch_starts_on_when_either_old_switch_was_on(cameras_switch, line_switches, expected):
    state = _v2_state(cameras_switch, line_switches)
    assert upgrade_state(state) is True
    assert state["schema_version"] == SCHEMA_VERSION
    assert state["camera_auto_connect"] is expected
    assert all("auto_connect" not in line for line in state["lines"])
    # A second load changes nothing.
    assert upgrade_state(state) is False


def test_a_line_no_longer_has_its_own_switch():
    assert "auto_connect" not in normalize_line({"name": "A", "auto_connect": True, "cameras": [{"camera_id": "a"}]})


# ── Startup ───────────────────────────────────────────────────────────────────

def test_switch_off_connects_nothing(lines):
    _line("Line 2", ["cam-2"])
    _switch(False)
    asyncio.run(lines.connect_on_startup())
    assert lines.calls == []


def test_switch_on_connects_every_camera_of_the_running_lines_once(lines):
    SettingsPersistenceService.save_line({"id": PRIMARY_LINE_ID, "cameras": [
        {"camera_id": "cam-1a", "model_id": "model-test"}, {"camera_id": "cam-1b", "role": "qr"}]})
    _line("Line 2", ["cam-2"])
    _line("Stopped", ["cam-3"], enabled=False)
    _line("Empty", [])
    _switch(True)
    asyncio.run(lines.connect_on_startup())
    # Line 1's two cameras and Line 2's one; not the stopped line's, each asked once.
    assert sorted(lines.calls) == ["cam-1a", "cam-1b", "cam-2"]


def test_a_camera_that_is_offline_at_startup_is_retried_in_the_background(lines):
    _line("Line 2", ["cam-2", "cam-down"])
    SettingsPersistenceService.save_line({"id": PRIMARY_LINE_ID, "cameras": [{"camera_id": "cam-1", "model_id": "model-test"}]})
    line_module.line_manager.apply_state()
    lines.offline.add("cam-down")
    _switch(True)
    asyncio.run(lines.connect_on_startup())
    assert sorted(lines.calls) == ["cam-1", "cam-2", "cam-down"]
    assert camera_service.CameraReconnector.pending() == ["cam-down"]


def test_line1_without_cameras_connects_its_default_camera(lines, monkeypatch):
    asked = []

    async def default_camera():
        asked.append(True)

    monkeypatch.setattr(lines, "_connect_last_active_camera", default_camera)
    _line("Line 2", ["cam-2"])
    _switch(True)
    asyncio.run(lines.connect_on_startup())
    assert asked == [True] and lines.calls == ["cam-2"]

    # With a camera of its own, Line 1 no longer falls back to another one.
    asked.clear()
    SettingsPersistenceService.save_line({"id": PRIMARY_LINE_ID, "cameras": [{"camera_id": "cam-1", "model_id": "model-test"}]})
    line_module.line_manager.apply_state()
    asyncio.run(lines.connect_on_startup())
    assert asked == []

    # A stopped Line 1 connects nothing.
    SettingsPersistenceService.save_line({"id": PRIMARY_LINE_ID, "cameras": [], "enabled": False})
    line_module.line_manager.apply_state()
    asyncio.run(lines.connect_on_startup())
    assert asked == []


def test_default_camera_is_never_another_lines_camera():
    cam = lambda cid, active=False: SimpleNamespace(id=cid, is_active=active)  # noqa: E731
    cameras = [cam("newest"), cam("was-live", active=True), cam("selected"), cam("line2-cam", active=True)]
    owned = {"line2-cam"}
    assert pick_default_camera(cameras, owned, "selected").id == "selected"
    # The selected camera belongs to Line 2: the last connected free camera instead.
    assert pick_default_camera(cameras, owned, "line2-cam").id == "was-live"
    assert pick_default_camera([cam("newest"), cam("older")], set(), None).id == "newest"
    assert pick_default_camera([cam("line2-cam")], owned, None) is None


def test_startup_connects_cameras_before_the_endpoint_checks(monkeypatch):
    import main
    from app.services.line_service import line_manager

    order = []

    async def cameras():
        order.append("cameras")

    async def comms(cls):
        order.append("communication")

    monkeypatch.setattr(line_manager, "connect_on_startup", cameras)
    monkeypatch.setattr(SettingsPersistenceService, "connect_on_startup", classmethod(comms))
    asyncio.run(main._connect_saved_hardware())
    assert order == ["cameras", "communication"]


def test_deleting_the_selected_camera_leaves_the_switch_alone(lines):
    SettingsPersistenceService._state["ip_cameras"] = [{"id": "ip-1", "name": "Dock", "source": "rtsp://10.0.0.9/1"}]
    SettingsPersistenceService._state["active_camera_id"] = "ip-1"
    _switch(True)
    assert SettingsPersistenceService.delete_ip_camera("ip-1") is True
    assert SettingsPersistenceService.camera_auto_connect() is True
