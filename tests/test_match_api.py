"""The pattern check's setup API on Line setup: a held picture, the draft (boxes, mask, good samples), Train,
Edit training, Start over, and the preview of unsaved settings. Supervisor and up; API keys cannot call it;
while the configuration is locked only the picture and the preview work (they save nothing)."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.security.api_key_scopes import required_api_key_scopes
from app.services import held_pictures, match_references
from tests.pattern_pictures import good_pictures, label, scene
from tests.test_config_lock_api import operator, supervisor, unlocked  # noqa: F401  (fixtures)

API = "/api/v1/match"
LABEL = [0.359375, 0.375, 0.640625, 0.625]          # the label's box on the training picture (1280 x 960)
CHECKS = [{"colour": False, "sensitivity": "normal"}]
PICTURES = good_pictures()


@pytest.fixture(autouse=True)
def _nothing_held():
    held_pictures._held.clear()
    match_references._cache.clear()
    yield
    held_pictures._held.clear()


def _show(cam, picture):
    cam._frame = picture


def _draft_with_samples(client, headers, cam, count=3):
    _show(cam, PICTURES[0])
    assert client.post(f"{API}/picture/camera/c1", headers=headers).status_code == 200
    started = client.put(f"{API}/draft/camera/c1", headers=headers, json={"reference": LABEL, "checks": [], "search_area": None})
    assert started.status_code == 200, started.text
    for picture in PICTURES[1:count]:
        _show(cam, picture)
        client.post(f"{API}/picture/camera/c1", headers=headers)
        added = client.post(f"{API}/draft/camera/c1/samples", headers=headers, json={"angle": 15})
        assert added.status_code == 200, added.text
    return started.json()


def test_train_from_the_page_flow(client, supervisor, fake_camera, data_dir):  # noqa: F811
    cam = fake_camera("c1")
    started = _draft_with_samples(client, supervisor, cam, count=4)
    assert started["reference"] == [0.3594, 0.375, 0.6406, 0.625] and started["samples"] == [{"n": 1, "score": 100}]
    draft = client.get(f"{API}/draft/camera/c1", headers=supervisor).json()
    assert [s["n"] for s in draft["samples"]] == [1, 2, 3, 4] and all(s["score"] >= 90 for s in draft["samples"])
    trained = client.post(f"{API}/draft/camera/c1/train", headers=supervisor, json={"angle": 15, "checks": CHECKS})
    assert trained.status_code == 200, trained.text
    body = trained.json()
    assert match_references.ID_RE.fullmatch(body["reference_id"]) and len(body["suggested"]) == 1 and body["note"] is None
    assert len(body["samples"]) == 4 and client.get(f"{API}/draft/camera/c1", headers=supervisor).json()["trained"] == body["reference_id"]
    assert (data_dir / "match-references" / body["reference_id"] / "set.json").is_file()
    settings = {"count_mode": "trigger", "match": {"reference_id": body["reference_id"], "checks": [{"name": "Label"}]}}
    _show(cam, scene(label(), 7, 700, 450, seed=40))
    preview = client.post(f"{API}/preview/camera/c1", headers=supervisor, json={"settings": settings, "held": False})
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result["result"] == "good" and result["checks"][0]["score"] >= 90 and (result["width"], result["height"]) == (1280, 960)
    _show(cam, scene(label(text="APPLE"), 3, 640, 480, seed=41))
    assert client.post(f"{API}/preview/camera/c1", headers=supervisor,
                       json={"settings": settings, "held": False}).json()["reason"] == "pattern_not_found"


def test_the_draft_files(client, supervisor, fake_camera, data_dir):  # noqa: F811
    cam = fake_camera("c1")
    _draft_with_samples(client, supervisor, cam, count=2)
    picture = client.get(f"{API}/draft/camera/c1/picture", headers=supervisor)
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/jpeg"
    assert client.get(f"{API}/draft/camera/c1/mask", headers=supervisor).status_code == 404       # none painted yet
    mask = np.zeros((960, 1280), np.uint8)
    mask[553:585, 485:660] = 255
    saved = client.put(f"{API}/draft/camera/c1/mask", headers={**supervisor, "Content-Type": "image/png"},
                       content=cv2.imencode(".png", mask)[1].tobytes())
    assert saved.status_code == 200, saved.text
    got = client.get(f"{API}/draft/camera/c1/mask", headers=supervisor)
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    small = client.put(f"{API}/draft/camera/c1/mask", headers={**supervisor, "Content-Type": "image/png"},
                       content=cv2.imencode(".png", np.zeros((10, 10), np.uint8))[1].tobytes())
    assert small.status_code == 400 and "1280 × 960" in small.json()["detail"]
    assert client.get(f"{API}/draft/camera/c1/samples/2", headers=supervisor).status_code == 200
    assert client.get(f"{API}/draft/camera/c1/samples/9", headers=supervisor).status_code == 404
    removed = client.delete(f"{API}/draft/camera/c1/samples/2", headers=supervisor)
    assert removed.status_code == 200 and [s["n"] for s in removed.json()["samples"]] == [1]
    assert client.delete(f"{API}/draft/camera/c1/samples/1", headers=supervisor).status_code == 400


def test_a_draft_needs_a_held_picture_and_a_reference(client, supervisor, fake_camera, data_dir):  # noqa: F811
    fake_camera("c1")
    none = client.put(f"{API}/draft/camera/c1", headers=supervisor, json={"reference": LABEL, "checks": [], "search_area": None})
    assert none.status_code == 404 and none.json()["detail"] == "No picture is held for this camera: take a picture first"
    assert client.get(f"{API}/draft/camera/c1", headers=supervisor).status_code == 404
    client.post(f"{API}/picture/camera/c1", headers=supervisor)
    bad = client.put(f"{API}/draft/camera/c1", headers=supervisor, json={"reference": [0.5, 0.5, 0.2, 0.9], "checks": [], "search_area": None})
    assert bad.status_code == 400 and "x1 < x2" in bad.json()["detail"]


def test_a_sample_without_the_reference_is_refused(client, supervisor, fake_camera, data_dir):  # noqa: F811
    cam = fake_camera("c1")
    _draft_with_samples(client, supervisor, cam, count=1)
    _show(cam, scene(label(text="APPLE"), 2, 640, 480, seed=42))
    client.post(f"{API}/picture/camera/c1", headers=supervisor)
    refused = client.post(f"{API}/draft/camera/c1/samples", headers=supervisor, json={"angle": 15})
    assert refused.status_code == 400 and refused.json()["detail"] == "Reference not found in this picture"


def test_training_refusals(client, supervisor, fake_camera, data_dir):  # noqa: F811
    cam = fake_camera("c1")
    _show(cam, PICTURES[0])
    client.post(f"{API}/picture/camera/c1", headers=supervisor)
    client.put(f"{API}/draft/camera/c1", headers=supervisor, json={"reference": [0.01, 0.01, 0.15, 0.15], "checks": [], "search_area": None})
    plain = client.post(f"{API}/draft/camera/c1/train", headers=supervisor, json={"angle": 15, "checks": CHECKS})
    assert plain.status_code == 400 and "Draw the reference around printing or a shape" in plain.json()["detail"]
    assert client.post(f"{API}/draft/camera/c9/train", headers=supervisor, json={"angle": 15, "checks": CHECKS}).status_code == 404


def test_a_preview_with_bad_settings_says_what_a_save_would(client, supervisor, fake_camera, data_dir):  # noqa: F811
    fake_camera("c1")
    bad = client.post(f"{API}/preview/camera/c1", headers=supervisor,
                      json={"settings": {"match": {"reference_id": "ref-0a1b2c3d", "checks": []}}, "held": False})
    assert bad.status_code == 400 and "1 to 4 checks" in bad.json()["detail"]
    assert client.post(f"{API}/preview/camera/c1", headers=supervisor, json={"settings": "x"}).status_code == 400
    missing = client.post(f"{API}/preview/camera/c1", headers=supervisor, json={"held": False, "settings": {
        "match": {"reference_id": "ref-99999999", "checks": [{"name": "Label"}]}}})
    assert missing.status_code == 200 and missing.json()["reason"] == "pattern_error" and "missing" in missing.json()["error"]


def test_edit_and_start_over(client, supervisor, fake_camera, data_dir):  # noqa: F811
    cam = fake_camera("c1")
    _draft_with_samples(client, supervisor, cam, count=3)
    set_id = client.post(f"{API}/draft/camera/c1/train", headers=supervisor, json={"angle": 15, "checks": CHECKS}).json()["reference_id"]
    assert client.delete(f"{API}/draft/camera/c1", headers=supervisor).status_code == 200
    assert client.get(f"{API}/draft/camera/c1", headers=supervisor).status_code == 404
    edited = client.post(f"{API}/draft/camera/c1/edit", headers=supervisor, json={"reference_id": set_id})
    assert edited.status_code == 200, edited.text
    assert edited.json()["trained"] == set_id and len(edited.json()["samples"]) == 3
    assert client.post(f"{API}/draft/camera/c1/edit", headers=supervisor, json={"reference_id": "ref-99999999"}).status_code == 404


def test_train_and_add_as_good_are_refused_without_room(client, supervisor, fake_camera, data_dir, monkeypatch):  # noqa: F811
    cam = fake_camera("c1")
    _draft_with_samples(client, supervisor, cam, count=1)
    monkeypatch.setattr(match_references, "room_ok", lambda: False)
    full = client.post(f"{API}/draft/camera/c1/train", headers=supervisor, json={"angle": 15, "checks": CHECKS})
    assert full.status_code == 507 and "free-space floor" in full.json()["detail"]
    assert client.post(f"{API}/draft/camera/c1/samples", headers=supervisor, json={"angle": 15}).status_code == 507


def test_operators_api_keys_and_the_lock(client, admin_headers, operator, supervisor, fake_camera, data_dir):  # noqa: F811
    cam = fake_camera("c1")
    _show(cam, PICTURES[0])
    assert client.post(f"{API}/picture/camera/c1", headers=operator).status_code == 403
    assert client.get(f"{API}/draft/camera/c1", headers=operator).status_code == 403
    for method, path in (("POST", "/api/v1/match/preview/camera/c1"), ("PUT", "/api/v1/match/draft/camera/c1"),
                         ("POST", "/api/v1/match/draft/camera/c1/train")):
        assert required_api_key_scopes(method, path) is None
    settings = {"match": {"reference_id": "ref-99999999", "checks": [{"name": "Label"}]}}
    try:
        assert client.post("/api/v1/system/lock", headers=supervisor).json()["locked"] is True
        assert client.post(f"{API}/picture/camera/c1", headers=supervisor).status_code == 200
        assert client.post(f"{API}/preview/camera/c1", headers=supervisor, json={"settings": settings, "held": True}).status_code == 200
        assert client.put(f"{API}/draft/camera/c1", headers=supervisor,
                          json={"reference": LABEL, "checks": [], "search_area": None}).status_code == 423
        assert client.post(f"{API}/draft/camera/c1/samples", headers=supervisor, json={"angle": 15}).status_code == 423
        assert client.post(f"{API}/draft/camera/c1/train", headers=supervisor, json={"angle": 15, "checks": CHECKS}).status_code == 423
        assert client.delete(f"{API}/draft/camera/c1", headers=supervisor).status_code == 423
    finally:
        client.post("/api/v1/system/unlock", headers=admin_headers)


def test_the_training_picture_shows_from_a_data_folder_with_other_letters(client, supervisor, fake_camera, data_dir, monkeypatch):  # noqa: F811
    """Review: cv2.imread cannot open a path with non-ASCII letters on Windows, so the training picture never showed."""
    from app.services import settings_persistence_service

    folder = data_dir / "بيانات données"
    folder.mkdir()
    monkeypatch.setattr(settings_persistence_service, "DATA_DIR", str(folder))
    cam = fake_camera("c1")
    _draft_with_samples(client, supervisor, cam, count=1)
    picture = client.get(f"{API}/draft/camera/c1/picture", headers=supervisor)
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/jpeg", picture.text[:200]
