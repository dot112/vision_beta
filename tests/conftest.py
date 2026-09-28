"""Shared pytest setup.

Every test runs against a throwaway SQLite database and settings file, and no
test touches real cameras, ONNX models or PLCs: those are replaced with fakes.
"""
from __future__ import annotations

import os
import sys
import tempfile
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
    from app.state.application_state import app_state

    engine = FakeInferenceEngine()
    monkeypatch.setattr(app_state, "active_model", {
        "id": None, "name": "fake-model", "classes": ["bottle"], "engine": engine,
    })
    return engine


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

    from app.services import discovery_service, vision_service
    import main

    patches = [
        (discovery_service.ServerDiscoveryService, "start", staticmethod(_async_noop)),
        (vision_service.ContinuousVisionRunner, "start", staticmethod(lambda *a, **k: None)),
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


@pytest.fixture(scope="session")
def admin_headers(client):
    res = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD, "force": True},
    )
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}
