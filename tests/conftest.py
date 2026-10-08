"""Shared pytest setup.

Every test runs against a throwaway SQLite database and settings file, and no
test touches real cameras, ONNX models or PLCs: those are replaced with fakes.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Settings are read once at import time, so the environment must be set up
# before anything under app/ is imported.
_TMP = Path(tempfile.mkdtemp(prefix="vision-tests-"))
ADMIN_PASSWORD = "test-admin-password-0123456789"
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-0123456789")
os.environ["ADMIN_INITIAL_PASSWORD"] = ADMIN_PASSWORD
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["LOG_DIR"] = str(_TMP / "logs")
os.environ["MODEL_STORE_PATH"] = str(_TMP / "model_store")
os.environ["INFERENCE_DEVICE"] = "cpu"
os.environ["APP_ENV"] = "test"
# One test client sends every request from the same address; the limiter has
# its own tests.
os.environ["RATE_LIMIT_PER_SECOND"] = "0"

import app.services.settings_persistence_service as _persistence  # noqa: E402

_persistence.DATA_DIR = str(_TMP / "data")
_persistence.STATE_FILE = str(_TMP / "data" / "system_state.json")


# ── Hardware fakes ────────────────────────────────────────────────────────────

class FakeInferenceEngine:
    """Stands in for the ONNX InferenceEngine and returns one fixed detection."""

    is_loaded = True
    task = "detect"

    def __init__(self, class_name: str = "bottle", confidence: float = 0.91):
        self.class_name = class_name
        self.confidence = confidence
        self.calls = 0

    def _response(self, width: int, height: int):
        from app.schemas.vision import BoundingBox, DetectionItem, DetectionResponse

        self.calls += 1
        item = DetectionItem(
            class_id=0,
            class_name=self.class_name,
            confidence=self.confidence,
            bbox=BoundingBox(x1=10, y1=20, x2=50, y2=80, width=40, height=60),
        )
        return DetectionResponse(
            model_name="fake-model",
            total_detections=1,
            detections=[item],
            inference_time_ms=1.5,
            image_width=width,
            image_height=height,
        )

    def predict(self, image_bytes, conf_threshold=None, nms_threshold=None):
        return self._response(64, 48)

    def predict_mat(self, mat, conf_threshold=None, nms_threshold=None):
        height, width = mat.shape[:2]
        return self._response(width, height)


class FakeCamera:
    """A connected camera driver that always returns the same black frame."""

    def __init__(self, camera_id: str, name: str = "Fake camera"):
        import numpy as np

        self.camera_id = camera_id
        self.name = name
        self.is_connected = True
        self.last_error = None
        self._frame = np.zeros((48, 64, 3), dtype=np.uint8)
        self._frame_id = 0

    def grab_raw_frame(self):
        self._frame_id += 1
        return True, self._frame.copy(), self._frame_id

    def disconnect(self):
        self.is_connected = False


@pytest.fixture
def fake_engine(monkeypatch):
    """One loaded model for every camera and for every detect call, whatever model they name."""
    from app.services.line_service import line_manager

    engine = FakeInferenceEngine()

    async def api_engine(model_id=None, camera_id=None):
        return engine, "fake-model", None

    monkeypatch.setattr(line_manager, "engine_for_camera", lambda camera_id: (engine, "fake-model", None))
    monkeypatch.setattr(line_manager, "api_engine", api_engine)
    return engine


@pytest.fixture
def vision_model(client, admin_headers, monkeypatch):
    """A model on the AI models page that loads as a fake engine. Gives its id.

    A line is not saved with a vision camera that has no model, so API tests
    that put a vision camera on a line name this one.
    """
    from app.services.line_service import LineManager

    folder = Path(os.environ["MODEL_STORE_PATH"]) / "fake-model" / "v1.0"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex[:8]}.onnx"
    path.write_bytes(b"not a real model")
    res = client.post("/api/v1/models/register", headers=admin_headers,
                      json={"name": "fake-model", "file_path": str(path), "classes": ["bottle", "defect"]})
    assert res.status_code == 201, res.text
    model_id = res.json()["id"]

    async def load(self, wanted):
        return {"engine": FakeInferenceEngine(), "name": "fake-model"}, None

    monkeypatch.setattr(LineManager, "_load_engine", load)
    yield model_id
    # Refused while a line of the test still uses it; the test database is thrown away anyway.
    client.delete(f"/api/v1/models/{model_id}", headers=admin_headers)


@pytest.fixture
def fake_camera(monkeypatch):
    from app.state.application_state import app_state

    def _attach(camera_id: str) -> FakeCamera:
        cam = FakeCamera(camera_id)
        monkeypatch.setitem(app_state.cameras, camera_id, cam)
        return cam

    return _attach


# ── API client ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def client():
    """App with its real lifespan (migrations, admin seed) but no background hardware."""
    from fastapi.testclient import TestClient

    from app.services import camera_service, discovery_service, health_service, plc_failsafe_service, vision_service
    import main

    patches = [
        # The fail-safe watchdog would keep ticking in the client's event-loop
        # thread and write to whatever fake PLC a later test sets up.
        (plc_failsafe_service.PLCFailsafeService, "start", classmethod(lambda cls: None)),
        # Same for the health watchdog: it would raise camera and inference
        # alarms against whatever fakes later tests put in app_state.
        (health_service.HealthMonitor, "start", classmethod(lambda cls: None)),
        (discovery_service.ServerDiscoveryService, "start", staticmethod(_async_noop)),
        (vision_service.ContinuousVisionRunner, "start", staticmethod(lambda *a, **k: None)),
        # Background camera retries would reconnect whatever cameras tests leave behind.
        (camera_service.CameraReconnector, "start", classmethod(lambda cls: None)),
    ]
    originals = [(obj, name, obj.__dict__[name]) for obj, name, _ in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)
    test_client = TestClient(main.create_app())
    try:
        test_client.__enter__()  # runs startup with the patches in place
    finally:
        # Unit tests of these services must see the real methods again.
        for obj, name, value in originals:
            setattr(obj, name, value)
    try:
        yield test_client
    finally:
        test_client.__exit__(None, None, None)


async def _async_noop(*args, **kwargs):
    return None


_admin_session: dict = {}


@pytest.fixture
def admin_headers(client):
    """Headers of a signed-in admin, shared by the whole run.

    A test that starts the app again or resets the sessions ends every
    sign-in, so the shared one is checked and renewed when it is gone.
    """
    headers = _admin_session.get("headers")
    if headers is None or client.get("/api/v1/auth/me", headers=headers).status_code != 200:
        res = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD, "force": True},
        )
        assert res.status_code == 200, res.text
        headers = _admin_session["headers"] = {"Authorization": f"Bearer {res.json()['access_token']}"}
    return headers
