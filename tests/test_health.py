"""Tests for component health checks, the watchdog alarms, and the health/metrics/alarm routes."""
from __future__ import annotations

import os

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production-use-0123456789")

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.dependencies import require_operator
from app.events.alarm_events import AlarmSeverity, alarm_manager
from app.hardware.plc.factory import _driver_pool
from app.routes.v1 import health as health_routes
from app.security.api_key_scopes import required_api_key_scopes
from app.services import health_service as hs
from app.services.health_service import HealthAlarmCode, HealthService, to_prometheus
from app.state.application_state import app_state


@pytest.fixture(autouse=True)
def _reset_alarms():
    alarm_manager.reset()
    yield
    alarm_manager.reset()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeCamera:
    def __init__(self, name="Line cam", connected=True):
        self.name = name
        self.is_connected = connected
        self.last_error = None if connected else "USB device unplugged"
        self.frame_id = 1

    def get_latest_frame_id(self) -> int:
        return self.frame_id


class FakeEngine:
    def __init__(self, loaded=True):
        self.is_loaded = loaded


class FakeThread:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


class FakePLCDriver:
    def __init__(self, connected, error=""):
        self._ep = {"name": "Line 1 PLC"}
        self.protocol = "s7"
        self.is_connected = connected
        self.last_connect_error = error


@pytest.fixture
def runtime(monkeypatch):
    """Isolate app_state, the PLC pool, the lines and the vision runner for each test.

    Line 1 has one vision camera, "cam1", that runs the model "yolo-test".
    """
    monkeypatch.setattr(app_state, "cameras", {})
    monkeypatch.setattr(app_state, "processed_frames", 0)
    monkeypatch.setattr(app_state, "detection_count", 0)
    monkeypatch.setattr(app_state, "db_ready", True)
    monkeypatch.setattr(app_state, "mqtt_connected", False)
    saved_pool = dict(_driver_pool)
    _driver_pool.clear()

    from app.services import line_service, vision_service
    engine = FakeEngine(loaded=True)
    manager = line_service.LineManager()
    manager.primary().cameras = [{"camera_id": "cam1", "role": "vision", "counting": True, "model_id": "m1"}]
    manager._camera_map = {"cam1": (line_service.PRIMARY_LINE_ID, "vision")}
    manager._engines = {"m1": {"engine": engine, "name": "yolo-test"}}
    monkeypatch.setattr(line_service, "line_manager", manager)
    monkeypatch.setattr(vision_service.ContinuousVisionRunner, "_running", True)
    monkeypatch.setattr(vision_service.ContinuousVisionRunner, "_thread", FakeThread(alive=True))

    clock = FakeClock()
    service = HealthService(clock=clock)
    yield service, clock, engine
    _driver_pool.clear()
    _driver_pool.update(saved_pool)


# ── Overall status ────────────────────────────────────────────────────────────

def test_idle_system_is_ok(runtime):
    service, _, _ = runtime
    report = service.snapshot()
    assert report["status"] == "ok"
    assert report["components"]["cameras"]["status"] == "idle"
    assert report["components"]["inference"]["status"] == "idle"
    assert report["components"]["plc"]["status"] == "idle"
    assert alarm_manager.active() == []


def test_database_down_makes_service_down(runtime, monkeypatch):
    service, _, _ = runtime
    monkeypatch.setattr(app_state, "db_ready", False)
    assert service.snapshot()["status"] == "down"


def test_active_critical_alarm_degrades_status(runtime):
    service, _, _ = runtime
    alarm_manager.raise_alarm("plc.action_failed", "plc_card:x", "failed", AlarmSeverity.CRITICAL)
    report = service.snapshot()
    assert report["status"] == "degraded"
    assert report["alarms"]["critical"] == 1


# ── Cameras ───────────────────────────────────────────────────────────────────

def test_camera_with_fresh_frames_is_ok(runtime):
    service, clock, _ = runtime
    cam = FakeCamera()
    app_state.cameras["cam1"] = cam
    service.check_cameras()
    clock.now += 3
    cam.frame_id += 90
    result = service.check_cameras()
    assert result["status"] == "ok"
    assert result["cameras"][0]["seconds_since_new_frame"] == 0


def test_stalled_camera_raises_alarm_and_recovers(runtime):
    service, clock, _ = runtime
    cam = FakeCamera()
    app_state.cameras["cam1"] = cam
    service.check_cameras()
    clock.now += hs.settings.HEALTH_CAMERA_STALE_SECONDS + 1

    result = service.check_cameras()
    assert result["status"] == "down"
    alarm = alarm_manager.get(HealthAlarmCode.CAMERA_STALLED, "camera:cam1")
    assert alarm is not None and alarm.severity == AlarmSeverity.CRITICAL

    cam.frame_id += 1
    assert service.check_cameras()["status"] == "ok"
    assert alarm_manager.get(HealthAlarmCode.CAMERA_STALLED, "camera:cam1") is None


def test_disconnected_camera_raises_alarm(runtime):
    service, _, _ = runtime
    app_state.cameras["cam1"] = FakeCamera(connected=False)
    result = service.check_cameras()
    assert result["status"] == "down"
    assert result["cameras"][0]["error"] == "USB device unplugged"
    assert alarm_manager.get(HealthAlarmCode.CAMERA_DISCONNECTED, "camera:cam1") is not None


def test_one_of_two_cameras_down_is_degraded(runtime):
    service, _, _ = runtime
    app_state.cameras["cam1"] = FakeCamera()
    app_state.cameras["cam2"] = FakeCamera(connected=False)
    assert service.check_cameras()["status"] == "degraded"


def test_removed_camera_alarms_are_cleared(runtime):
    service, _, _ = runtime
    app_state.cameras["cam1"] = FakeCamera(connected=False)
    service.check_cameras()
    assert alarm_manager.active("camera:")
    app_state.cameras.clear()
    service.check_cameras()
    assert alarm_manager.active("camera:") == []


# ── Inference ─────────────────────────────────────────────────────────────────

def test_inference_ok_while_frames_are_processed(runtime, monkeypatch):
    service, clock, _ = runtime
    app_state.cameras["cam1"] = cam = FakeCamera()
    service.snapshot()
    clock.now += 2
    cam.frame_id += 60
    monkeypatch.setattr(app_state, "processed_frames", 50)
    report = service.snapshot()
    assert report["components"]["inference"]["status"] == "ok"
    assert report["components"]["inference"]["model"] == "yolo-test"


def test_inference_stall_raises_alarm(runtime):
    service, clock, _ = runtime
    app_state.cameras["cam1"] = cam = FakeCamera()
    service.snapshot()
    # Frames keep arriving, but processed_frames never moves.
    for _ in range(int(hs.settings.HEALTH_INFERENCE_STALE_SECONDS) + 2):
        clock.now += 1
        cam.frame_id += 30
        report = service.snapshot()
    assert report["components"]["inference"]["status"] == "down"
    assert alarm_manager.get(HealthAlarmCode.INFERENCE_STALLED, "inference") is not None


def test_model_not_loaded_only_alarms_when_cameras_stream(runtime):
    service, _, engine = runtime
    engine.is_loaded = False
    assert service.snapshot()["components"]["inference"]["status"] == "idle"
    assert alarm_manager.active() == []

    app_state.cameras["cam1"] = FakeCamera()
    report = service.snapshot()
    assert report["components"]["inference"]["status"] == "down"
    assert alarm_manager.get(HealthAlarmCode.INFERENCE_MODEL_NOT_LOADED, "inference") is not None

    engine.is_loaded = True
    service.snapshot()
    assert alarm_manager.get(HealthAlarmCode.INFERENCE_MODEL_NOT_LOADED, "inference") is None


def test_a_server_that_only_reads_codes_needs_no_model(runtime):
    service, _, _ = runtime
    from app.services import line_service
    manager = line_service.line_manager
    manager.primary().cameras = [{"camera_id": "cam1", "role": "qr"}]
    manager._camera_map = {"cam1": (line_service.PRIMARY_LINE_ID, "qr")}
    manager._engines = {}
    app_state.cameras["cam1"] = FakeCamera()
    report = service.snapshot()
    assert report["components"]["inference"]["status"] == "idle"
    assert report["components"]["inference"]["model_loaded"] is False
    assert alarm_manager.active() == []


def test_a_camera_whose_own_model_is_missing_is_not_reported_as_stalled_inference(runtime):
    """That camera has its own alarm (camera.model_unavailable); inference as a whole is not stalled."""
    service, clock, engine = runtime
    from app.services import line_service
    line_service.line_manager._engines = {"m-other": {"engine": engine, "name": "another camera's model"}}
    app_state.cameras["cam1"] = cam = FakeCamera()
    for _ in range(int(hs.settings.HEALTH_INFERENCE_STALE_SECONDS) + 2):
        clock.now += 1
        cam.frame_id += 30
        report = service.snapshot()
    assert report["components"]["inference"]["model"] == "another camera's model"
    assert alarm_manager.get(HealthAlarmCode.INFERENCE_STALLED, "inference") is None
    assert alarm_manager.get(HealthAlarmCode.INFERENCE_MODEL_NOT_LOADED, "inference") is None


def test_stopped_runner_raises_alarm(runtime, monkeypatch):
    service, _, _ = runtime
    from app.services import vision_service
    monkeypatch.setattr(vision_service.ContinuousVisionRunner, "_thread", FakeThread(alive=False))
    app_state.cameras["cam1"] = FakeCamera()
    report = service.snapshot()
    assert report["components"]["inference"]["runner_alive"] is False
    assert alarm_manager.get(HealthAlarmCode.INFERENCE_RUNNER_STOPPED, "inference") is not None


# ── PLC ───────────────────────────────────────────────────────────────────────

def test_plc_status_rolls_up_pool(runtime):
    service, _, _ = runtime
    _driver_pool["ep1"] = FakePLCDriver(connected=True)
    assert service.check_plc()["status"] == "ok"
    _driver_pool["ep2"] = FakePLCDriver(connected=False, error="connect timed out")
    result = service.check_plc()
    assert result["status"] == "degraded"
    down = next(e for e in result["endpoints"] if e["id"] == "ep2")
    assert down["error"] == "connect timed out"
    assert down["protocol"] == "s7"


# ── Metrics ───────────────────────────────────────────────────────────────────

def test_metrics_and_prometheus_output(runtime, monkeypatch):
    service, _, _ = runtime
    monkeypatch.setattr(app_state, "processed_frames", 1234)
    app_state.cameras["cam1"] = FakeCamera()
    _driver_pool["ep1"] = FakePLCDriver(connected=False)
    alarm_manager.raise_alarm("x", "s", "m", AlarmSeverity.CRITICAL)

    data = service.metrics()
    assert data["processed_frames_total"] == 1234
    assert data["cameras_connected"] == 1
    assert data["plc_endpoints_connected"] == 0
    assert data["plc_endpoints_count"] == 1
    assert data["alarms_active_critical"] == 1

    text = to_prometheus(data)
    assert "# TYPE vision_server_processed_frames_total counter" in text
    assert "vision_server_processed_frames_total 1234" in text
    assert "# TYPE vision_server_cameras_connected gauge" in text


# ── Routes ────────────────────────────────────────────────────────────────────

class FakeUser:
    username = "operator1"
    clearance_level = 1


@pytest.fixture
def client(runtime, monkeypatch):
    service, _, _ = runtime
    monkeypatch.setattr(health_routes, "health_service", service)
    app = FastAPI()
    app.include_router(health_routes.public_router)
    api = APIRouter(prefix="/api/v1")
    api.include_router(health_routes.router)
    app.include_router(api)
    app.dependency_overrides[require_operator] = lambda: FakeUser()
    return TestClient(app)


def test_public_health_hides_details(client):
    app_state.cameras["cam1"] = FakeCamera(connected=False)
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "degraded"
    assert body["components"]["cameras"] == "down"
    assert "USB device unplugged" not in res.text


def test_public_health_503_when_database_down(client, monkeypatch):
    monkeypatch.setattr(app_state, "db_ready", False)
    assert client.get("/health").status_code == 503


def test_detailed_health_includes_instances(client):
    app_state.cameras["cam1"] = FakeCamera(connected=False)
    body = client.get("/api/v1/telemetry/health").json()
    assert body["components"]["cameras"]["cameras"][0]["error"] == "USB device unplugged"


def test_metrics_route_formats(client):
    assert "processed_frames_total" in client.get("/api/v1/telemetry/metrics").json()
    res = client.get("/api/v1/telemetry/metrics?format=prometheus")
    assert res.headers["content-type"].startswith("text/plain")
    assert "vision_server_uptime_seconds" in res.text
    assert client.get("/api/v1/telemetry/metrics?format=xml").status_code == 422


def test_alarm_list_and_acknowledge_routes(client):
    alarm = alarm_manager.raise_alarm("plc.connect_failed", "plc:ep1", "down", AlarmSeverity.CRITICAL)
    alarm_manager.raise_alarm("flow.action_failed", "flow:f/n", "failed", AlarmSeverity.WARNING)

    body = client.get("/api/v1/alarms").json()
    assert [a["code"] for a in body["alarms"]] == ["plc.connect_failed", "flow.action_failed"]
    assert body["counts"]["critical"] == 1
    assert len(client.get("/api/v1/alarms?severity=warning").json()["alarms"]) == 1
    assert len(client.get("/api/v1/alarms?source=plc:").json()["alarms"]) == 1

    res = client.post(f"/api/v1/alarms/{alarm.id}/acknowledge")
    assert res.status_code == 200
    assert res.json()["state"] == "acknowledged"
    assert res.json()["acknowledged_by"] == "operator1"
    assert client.post("/api/v1/alarms/nope/acknowledge").status_code == 404

    alarm_manager.clear_alarm("plc.connect_failed", "plc:ep1")
    history = client.get("/api/v1/alarms/history").json()
    assert history[0]["code"] == "plc.connect_failed"


def test_alarms_for_a_line_can_include_the_ones_every_line_shares(client):
    alarm_manager.raise_alarm("camera.stalled", "camera:c1", "stalled", AlarmSeverity.CRITICAL, {"line_id": "line-1"})
    alarm_manager.raise_alarm("camera.stalled", "camera:c2", "stalled", AlarmSeverity.CRITICAL, {"line_id": "line-2"})
    alarm_manager.raise_alarm("inference.stalled", "inference", "stalled", AlarmSeverity.CRITICAL)

    only_line = client.get("/api/v1/alarms?line_id=line-1").json()["alarms"]
    assert [a["source"] for a in only_line] == ["camera:c1"]
    shared = client.get("/api/v1/alarms?line_id=line-1&include_all_lines=true").json()["alarms"]
    assert sorted(a["source"] for a in shared) == ["camera:c1", "inference"]
    by_source = {a["source"]: a for a in shared}
    assert by_source["camera:c1"]["label"] == "Camera stalled"
    assert by_source["camera:c1"]["line_id"] == "line-1" and by_source["camera:c1"]["all_lines"] is False
    assert by_source["inference"]["line_id"] is None and by_source["inference"]["all_lines"] is True

    alarm_manager.clear_alarm("inference.stalled", "inference")
    alarm_manager.clear_alarm("camera.stalled", "camera:c2")
    history = client.get("/api/v1/alarms/history?line_id=line-1&include_all_lines=true").json()
    assert [a["source"] for a in history] == ["inference"]
    assert client.get("/api/v1/alarms/history?line_id=line-1").json() == []


def test_alarm_catalog_route(client):
    catalog = client.get("/api/v1/alarms/catalog").json()["alarms"]
    by_code = {entry["code"]: entry for entry in catalog}
    assert by_code["camera.stalled"]["label"] == "Camera stalled"
    assert by_code["camera.stalled"]["scope"] == "line"
    assert by_code["plc.connect_failed"]["scope"] == "server"


def test_main_app_serves_public_health(monkeypatch):
    import main
    monkeypatch.setattr(app_state, "db_ready", False)
    res = TestClient(main.app).get("/health")
    assert res.status_code == 503
    assert res.json()["components"]["database"] == "down"


def test_api_key_scopes_for_new_routes():
    assert required_api_key_scopes("GET", "/api/v1/telemetry/health") == {"monitor:read"}
    assert required_api_key_scopes("GET", "/api/v1/telemetry/metrics") == {"monitor:read"}
    assert required_api_key_scopes("GET", "/api/v1/alarms") == {"monitor:read"}
    assert required_api_key_scopes("GET", "/api/v1/alarms/history") == {"monitor:read"}
    assert required_api_key_scopes("GET", "/api/v1/alarms/catalog") == {"monitor:read"}
    assert required_api_key_scopes("POST", "/api/v1/alarms/abc/acknowledge") == {"alarms:acknowledge"}
    assert required_api_key_scopes("DELETE", "/api/v1/alarms/abc") is None
