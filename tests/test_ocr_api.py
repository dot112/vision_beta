"""Line setup's text reader API: a held picture, the preview of unsaved settings, the models on disk.
Supervisor only, JWT-only, and allowed while the configuration is locked (they save nothing)."""
from __future__ import annotations

import pytest

from app.engines import ocr_engine, ocr_models
from app.engines.ocr_engine import OcrLine
from app.routes.v1 import ocr as ocr_route
from app.security.api_key_scopes import required_api_key_scopes
from tests.test_config_lock_api import operator, supervisor, unlocked  # noqa: F401  (fixtures)

API = "/api/v1"
BOX = [[2, 2], [40, 2], [40, 12], [2, 12]]
SETTINGS = {"count_mode": "trigger", "ocr": {"fields": [{"name": "Lot", "find": "all", "format": "LOT A99999"}]}}


class _Engine:
    ready = True
    error = None

    def __init__(self):
        self.reads = []

    def read(self, img, *, upside_down=False, allowed=None):
        self.reads.append(img.shape)
        return [OcrLine("LOT A12345", 0.93, BOX), OcrLine("EXP 12/03/2027", 0.8, BOX)]


@pytest.fixture
def engine(monkeypatch):
    made = _Engine()
    monkeypatch.setattr(ocr_engine, "engine_for", lambda model: made)
    ocr_route._held.clear()
    yield made
    ocr_route._held.clear()


def test_take_a_picture_then_preview_it(client, supervisor, fake_camera, engine):  # noqa: F811
    fake_camera("c1")
    taken = client.post(f"{API}/ocr/picture/camera/c1", headers=supervisor)
    assert taken.status_code == 200, taken.text
    assert taken.headers["content-type"] == "image/jpeg" and taken.content[:2] == b"\xff\xd8"
    held = client.get(f"{API}/ocr/picture/camera/c1?held=1", headers=supervisor)
    assert held.status_code == 200 and held.content[:2] == b"\xff\xd8"
    answer = client.post(f"{API}/ocr/preview/camera/c1", headers=supervisor, json={"settings": SETTINGS, "held": True})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["width"], body["height"], body["result"], body["reason"], body["error"]) == (64, 48, "good", None, None)
    assert body["fields"] == [{"name": "Lot", "text": "LOT A12345", "raw": "LOT A12345", "fits": True,
                               "box": BOX, "area": [0, 0, 64, 48]}]
    assert [line["text"] for line in body["lines"]] == ["LOT A12345", "EXP 12/03/2027"]
    assert body["lines"][0] == {"text": "LOT A12345", "confidence": 0.93, "box": BOX} and body["ms"] >= 0
    live = client.post(f"{API}/ocr/preview/camera/c1", headers=supervisor, json={"settings": SETTINGS, "held": False})
    assert live.status_code == 200 and engine.reads == [(48, 64, 3), (48, 64, 3)]


def test_a_preview_with_bad_settings_says_what_a_save_would(client, supervisor, fake_camera, engine):  # noqa: F811
    fake_camera("c1")
    bad = client.post(f"{API}/ocr/preview/camera/c1", headers=supervisor,
                      json={"settings": {"ocr": {"fields": []}}, "held": False})
    assert bad.status_code == 400 and "needs 1 to 4 text fields" in bad.json()["detail"]
    assert client.post(f"{API}/ocr/preview/camera/c1", headers=supervisor, json={"settings": "x"}).status_code == 400


def test_no_held_picture_is_404_and_it_expires(client, supervisor, fake_camera, engine, monkeypatch):  # noqa: F811
    assert client.post(f"{API}/ocr/picture/camera/nope", headers=supervisor).status_code == 404
    fake_camera("c1")
    assert client.get(f"{API}/ocr/picture/camera/c1?held=1", headers=supervisor).status_code == 404
    assert client.post(f"{API}/ocr/preview/camera/c1", headers=supervisor,
                       json={"settings": SETTINGS, "held": True}).status_code == 404
    clock = [1000.0]
    monkeypatch.setattr(ocr_route, "_now", lambda: clock[0])
    client.post(f"{API}/ocr/picture/camera/c1", headers=supervisor)
    clock[0] += ocr_route.HELD_TTL_S - 1
    assert client.get(f"{API}/ocr/picture/camera/c1?held=1", headers=supervisor).status_code == 200     # used: kept
    clock[0] += ocr_route.HELD_TTL_S + 1
    assert client.get(f"{API}/ocr/picture/camera/c1?held=1", headers=supervisor).status_code == 404
    assert client.get(f"{API}/ocr/picture/camera/c1?held=0", headers=supervisor).status_code == 200      # a new one


def test_a_camera_that_is_not_connected_is_connected_for_its_picture(client, supervisor, engine, monkeypatch):  # noqa: F811
    """As for the count lines' picture: setting up a reader on a stopped line connects its camera."""
    import numpy as np

    from app.services.camera_service import CameraService

    asked = []

    async def grab(db, camera_id):
        asked.append(camera_id)
        if camera_id == "c9":
            return True, np.zeros((48, 64, 3), np.uint8), 1, None
        if camera_id == "c7":
            return False, None, 0, "Camera is offline or not found"
        return False, None, 0, "Camera connection failed: no answer"

    monkeypatch.setattr(CameraService, "grab_raw_frame", staticmethod(grab))
    assert client.post(f"{API}/ocr/picture/camera/c9", headers=supervisor).status_code == 200
    assert client.post(f"{API}/ocr/preview/camera/c9", headers=supervisor, json={"settings": SETTINGS, "held": False}).status_code == 200
    refused = client.get(f"{API}/ocr/picture/camera/c8", headers=supervisor)
    assert refused.status_code == 404 and refused.json()["detail"] == "Camera connection failed: no answer"
    # A camera switched off (not connected, not kept connected) is not connected for it: its card says how.
    off = client.post(f"{API}/ocr/picture/camera/c7", headers=supervisor)
    assert off.status_code == 404 and off.json()["detail"] == "The camera is not connected: switch on Connect under Camera"
    assert asked == ["c9", "c9", "c8", "c7"]


def test_models(client, supervisor, engine, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ocr_models, "available", lambda: ["tiny", "small"])
    monkeypatch.setattr(ocr_models, "new_reader_model", lambda: "tiny")
    answer = client.get(f"{API}/ocr/models", headers=supervisor)
    assert answer.status_code == 200 and answer.json() == {"models": ["tiny", "small"], "new": "tiny", "ready": True, "error": None}


def test_operators_api_keys_and_the_lock(client, admin_headers, operator, supervisor, fake_camera, engine):  # noqa: F811
    fake_camera("c1")
    assert client.post(f"{API}/ocr/picture/camera/c1", headers=operator).status_code == 403
    assert client.get(f"{API}/ocr/models", headers=operator).status_code == 403
    assert required_api_key_scopes("POST", "/api/v1/ocr/preview/camera/c1") is None
    assert required_api_key_scopes("GET", "/api/v1/ocr/models") is None
    try:
        assert client.post(f"{API}/system/lock", headers=supervisor).json()["locked"] is True
        assert client.post(f"{API}/ocr/picture/camera/c1", headers=supervisor).status_code == 200
        assert client.post(f"{API}/ocr/preview/camera/c1", headers=supervisor,
                           json={"settings": SETTINGS, "held": True}).status_code == 200
    finally:
        client.post(f"{API}/system/unlock", headers=admin_headers)
