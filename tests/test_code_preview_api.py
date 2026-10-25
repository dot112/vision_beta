"""The live preview of a code reader's image processing: POST /api/v1/qr/preview/camera/{camera_id}."""
from __future__ import annotations

import copy

import cv2
import numpy as np
import pytest

from app.security.config_lock import LOCK_FREE_ROUTES, blocks_when_locked
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService
from app.state.application_state import app_state
from tests.test_qr_trigger import FrameCamera

API = "/api/v1/qr/preview/camera"


def _qr_picture(w=320, h=240):
    picture = np.full((h, w, 3), 255, np.uint8)
    code = cv2.resize(cv2.QRCodeEncoder.create().encode("PROD-0042"), None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
    y, x = (h - code.shape[0]) // 2, (w - code.shape[1]) // 2
    picture[y:y + code.shape[0], x:x + code.shape[1]] = cv2.cvtColor(code, cv2.COLOR_GRAY2BGR)
    return picture


@pytest.fixture
def camera(monkeypatch):
    def put(picture):
        monkeypatch.setitem(app_state.cameras, "cam-preview", FrameCamera(picture))
    put(_qr_picture())
    return put


def _jpeg(res):
    return cv2.imdecode(np.frombuffer(res.content, np.uint8), cv2.IMREAD_COLOR)


def test_preview_answers_a_jpeg_with_headers(client, admin_headers, camera):
    res = client.post(f"{API}/cam-preview", headers=admin_headers,
                      json={"processing": {"enabled": True, "grayscale": {"on": True}}})
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "image/jpeg"
    assert res.headers["X-Codes"] == "1"
    assert res.headers["X-Read-By"] in ("processed", "straightened", "original", "none")
    assert res.headers["X-Read-By"] == "processed"
    assert float(res.headers["X-Read-Ms"]) >= 0
    assert res.headers["Cache-Control"] == "no-store"
    assert _jpeg(res).shape[:2] == (240, 320)


def test_processed_view_follows_scale(client, admin_headers, camera):
    body = {"processing": {"enabled": True, "scale": {"on": True, "factor": 2}}}
    processed = client.post(f"{API}/cam-preview", headers=admin_headers, json=body)
    original = client.post(f"{API}/cam-preview", headers=admin_headers, json={**body, "view": "original"})
    assert _jpeg(processed).shape[:2] == (480, 640)
    assert _jpeg(original).shape[:2] == (240, 320)


def test_processing_off_shows_the_camera_picture(client, admin_headers, camera):
    res = client.post(f"{API}/cam-preview", headers=admin_headers, json={"processing": {"scale": {"on": True, "factor": 2}}})
    assert res.status_code == 200 and _jpeg(res).shape[:2] == (240, 320)
    assert res.headers["X-Read-By"] == "original"


def test_wide_pictures_are_capped_at_960(client, admin_headers, camera):
    camera(_qr_picture(1600, 1200))
    res = client.post(f"{API}/cam-preview", headers=admin_headers, json={"processing": {}})
    assert _jpeg(res).shape[:2] == (720, 960)


def test_not_connected_is_409(client, admin_headers):
    res = client.post(f"{API}/cam-nowhere", headers=admin_headers, json={"processing": {}})
    assert res.status_code == 409
    assert res.json()["detail"] == "Camera cam-nowhere is not connected"


def test_bad_settings_are_422(client, admin_headers, camera):
    res = client.post(f"{API}/cam-preview", headers=admin_headers, json={"processing": {"scale": {"factor": 9}}})
    assert res.status_code == 422
    assert res.json()["detail"] == "Image processing: the scale factor must be 0.25 to 4"
    res = client.post(f"{API}/cam-preview", headers=admin_headers, json={"processing": {}, "view": "sideways"})
    assert res.status_code == 422
    assert res.json()["detail"] == "view must be 'processed' or 'original'"


def test_preview_works_while_locked(client, admin_headers, camera):
    try:
        assert client.post("/api/v1/system/lock", headers=admin_headers).status_code == 200
        res = client.post(f"{API}/cam-preview", headers=admin_headers, json={"processing": {}})
        assert res.status_code == 200, res.text
    finally:
        SettingsPersistenceService.get_state()
        SettingsPersistenceService._state["config_lock"] = copy.deepcopy(DEFAULT_STATE["config_lock"])
        SettingsPersistenceService.save()


def test_preview_is_on_the_lock_free_list():
    assert ("POST", "/api/v1/qr/preview/camera/{camera_id}") in LOCK_FREE_ROUTES
    assert not blocks_when_locked("POST", "/api/v1/qr/preview/camera/{camera_id}")
