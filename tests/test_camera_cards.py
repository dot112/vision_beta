"""Camera cards on Line setup: up to 8 cameras per line, each with its own counting
settings, confidence threshold and station, and the version 8 step that copies a
line's count lines into its cameras."""
from __future__ import annotations

import copy
import json
import time

import numpy as np
import pytest

import app.services.line_service as line_module
import app.services.settings_persistence_service as persistence_module
from app.services.counting_service import counting_service
from app.services.line_config import (
    MAX_CAMERAS_PER_LINE,
    PRIMARY_LINE_ID,
    SCHEMA_VERSION,
    camera_station,
    normalize_line,
    upgrade_state,
)
from app.services.line_service import LineManager
from app.services.settings_persistence_service import (
    DEFAULT_STATE,
    SettingsPersistenceService,
    counting_config_from_dict,
)
from app.state.application_state import app_state
from tests.conftest import FakeInferenceEngine

API = "/api/v1"


@pytest.fixture
def lines(monkeypatch, tmp_path):
    """Throwaway settings file and line manager."""
    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(tmp_path / "system_state.json"))
    state = copy.deepcopy(DEFAULT_STATE)
    upgrade_state(state)
    monkeypatch.setattr(SettingsPersistenceService, "_state", state)
    monkeypatch.setattr(SettingsPersistenceService, "_recent_changes", [])
    manager = LineManager()
    monkeypatch.setattr(line_module, "line_manager", manager)
    saved_config = counting_service.config
    counting_service.reset_counts()
    yield manager
    counting_service.event_sink = None
    counting_service.line_crossing_sink = None
    counting_service.update_config(saved_config)
    counting_service.reset_counts()


def _save(**line):
    saved = SettingsPersistenceService.save_line(line)
    line_module.line_manager.apply_state()
    return saved


def _vision(camera_id, **extra):
    return {"camera_id": camera_id, "role": "vision", "model_id": "m", "expected_classes": ["bottle"], **extra}


class _Det:
    """One detection, as the model gives it, centred on (cx, cy)."""

    def __init__(self, cx, cy, class_name="bottle"):
        class Box:
            pass

        self.bbox = Box()
        self.bbox.x1, self.bbox.y1, self.bbox.x2, self.bbox.y2 = cx - 20, cy - 20, cx + 20, cy + 20
        self.class_name, self.confidence, self.class_id, self.polygon = class_name, 0.9, 0, None


STEPS = tuple(range(30, 460, 20))
# Where a product passes through a 640 x 480 picture, for each flow direction.
FLOWS = {
    "down": [(320, y) for y in STEPS],
    "up": [(320, y) for y in reversed(STEPS)],
    "right": [(x * 4 // 3, 240) for x in STEPS],
    "left": [(x * 4 // 3, 240) for x in reversed(STEPS)],
}


def _pass(counter, camera_id, flow):
    """One product moving through the camera's picture; returns how many the counter counted."""
    before = counter.total_inspected
    for cx, cy in FLOWS[flow]:
        counter.process_frame([_Det(cx, cy)], 640, 480, camera_id=camera_id)
    # Gone long enough for its track to end (max missed frames), so the next product is a new one.
    for _ in range(20):
        counter.process_frame([], 640, 480, camera_id=camera_id)
    return counter.total_inspected - before


# ── Up to 8 cameras ───────────────────────────────────────────────────────────

def test_a_line_with_three_then_eight_cameras_saves_and_runs(lines):
    three = _save(name="Three", enabled=True, cameras=[
        _vision("v1", counting=True), _vision("v2", station="own"), {"camera_id": "q1", "role": "qr"},
    ])
    runtime = lines.get(three["id"])
    assert [lines.route(cid)[0] is runtime for cid in ("v1", "v2", "q1")] == [True, True, True]
    assert set(runtime.aux_counters) == {"v2"}

    eight = [_vision(f"v{i}") for i in range(1, 7)] + [{"camera_id": "q1", "role": "qr"}, {"camera_id": "q2", "role": "qr"}]
    assert len(eight) == MAX_CAMERAS_PER_LINE
    saved = _save(id=three["id"], cameras=eight)
    assert [c["camera_id"] for c in saved["cameras"]] == [c["camera_id"] for c in eight]
    assert saved["cameras"][0]["counting"] and not any(c["counting"] for c in saved["cameras"][1:])
    # One counter per vision camera: the line's, and one of its own for each of the others.
    assert set(runtime.aux_counters) == {"v2", "v3", "v4", "v5", "v6"}
    assert all(lines.route(c["camera_id"])[0] is runtime for c in eight)
    assert lines.counter_for_camera("v1") is runtime.counter
    assert len({id(lines.counter_for_camera(f"v{i}")) for i in range(1, 7)}) == 6
    # Every camera's frames are counted by its own counter.
    for i in range(1, 7):
        assert _pass(lines.counter_for_camera(f"v{i}"), f"v{i}", "down") == 1

    with pytest.raises(ValueError, match="at most 8 cameras"):
        SettingsPersistenceService.save_line({"id": three["id"], "cameras": eight + [_vision("v9")]})


def test_more_cameras_than_the_server_connects_are_refused(client, admin_headers, vision_model, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MAX_CONNECTED_CAMERAS", 3)
    line = client.post(f"{API}/lines", json={"name": "Too many cameras"}, headers=admin_headers).json()
    try:
        cameras = [{"camera_id": f"tm-{i}", "role": "vision", "model_id": vision_model} for i in range(4)]
        res = client.put(f"{API}/lines/{line['id']}", json={"cameras": cameras}, headers=admin_headers)
        assert res.status_code == 422
        assert "4 cameras" in res.json()["detail"] and "at most 3" in res.json()["detail"]
        assert "MAX_CONNECTED_CAMERAS" in res.json()["detail"]
        res = client.put(f"{API}/lines/{line['id']}", json={"cameras": cameras[:3]}, headers=admin_headers)
        assert res.status_code == 200, res.text
    finally:
        client.put(f"{API}/lines/{line['id']}", json={"cameras": []}, headers=admin_headers)
        client.delete(f"{API}/lines/{line['id']}", headers=admin_headers)


# ── Counting settings per camera ──────────────────────────────────────────────

def test_each_vision_camera_counts_with_its_own_count_lines_and_direction(lines):
    line = _save(name="Two flows", enabled=True, cameras=[
        # Top -> bottom with lines across the picture.
        _vision("top", counting=True, orientation="horizontal", direction="forward", line1_position=0.3, line2_position=0.6),
        # Right -> left with lines down the picture.
        _vision("side", orientation="vertical", direction="backward", line1_position=0.4, line2_position=0.7),
    ])
    runtime = lines.get(line["id"])
    top, side = runtime.counter.config, runtime.aux_counters["side"].config
    assert (top.orientation, top.direction, top.line1_position, top.line2_position) == ("horizontal", "forward", 0.3, 0.6)
    assert (side.orientation, side.direction, side.line1_position, side.line2_position) == ("vertical", "backward", 0.4, 0.7)

    assert _pass(runtime.counter, "top", "down") == 1
    assert _pass(runtime.counter, "top", "left") == 0  # not this camera's flow
    assert _pass(runtime.aux_counters["side"], "side", "left") == 1
    assert _pass(runtime.aux_counters["side"], "side", "down") == 0
    assert _pass(runtime.aux_counters["side"], "side", "right") == 0
    # "Both ways" counts either way along the camera's own axis.
    _save(id=line["id"], cameras=[
        _vision("top", counting=True, orientation="horizontal", direction="forward", line1_position=0.3, line2_position=0.6),
        _vision("side", orientation="vertical", direction="both", line1_position=0.4, line2_position=0.7),
    ])
    side = runtime.aux_counters["side"]
    assert _pass(side, "side", "right") == 1 and _pass(side, "side", "left") == 1
    assert runtime.counter.total_inspected == 1  # the line's totals come from the counting camera only


def test_a_setting_sent_empty_goes_back_to_its_default(lines):
    line = _save(name="Defaults", cameras=[_vision(
        "cam", line1_position=0.2, line2_position=0.9, orientation="vertical", direction="both",
        tracking={"min_hits": 4}, confidence=0.6,
    )])
    camera = _save(id=line["id"], cameras=[_vision(
        "cam", line1_position="", line2_position=None, orientation="", direction="", tracking={}, confidence="",
    )])["cameras"][0]
    for key in ("line1_position", "line2_position", "orientation", "direction", "tracking", "confidence"):
        assert key not in camera
    config = lines.get(line["id"]).counter.config
    trigger = SettingsPersistenceService.get_line_action_trigger(line["id"])
    assert (config.line1_position, config.line2_position) == (trigger["line1_position"], trigger["line2_position"])
    assert (config.direction, config.min_hits) == ("forward", 2)


def test_confidence_and_station_are_checked_when_saved():
    line = normalize_line({"name": "A", "cameras": [
        _vision("a", confidence="0.45"), _vision("b", station="JOIN"), _vision("c"), {"camera_id": "q", "role": "qr", "station": "join"},
    ]})
    a, b, c, q = line["cameras"]
    assert a["confidence"] == 0.45 and "station" not in a  # the counting camera has no station
    assert b["station"] == "join" and camera_station(b) == "join"
    assert "station" not in c and camera_station(c) == "own"  # left out: its own station
    assert camera_station(a) is None and camera_station(q) is None and "station" not in q
    for extra, message in (({"confidence": 1.5}, "confidence threshold must be between 0.05 and 0.99"),
                           ({"confidence": "high"}, "confidence threshold must be a number"),
                           ({"station": "merge"}, "station must be")):
        with pytest.raises(ValueError, match=message):
            normalize_line({"name": "A", "cameras": [_vision("a"), _vision("b", **extra)]})


def test_choosing_a_second_counting_camera_is_refused_with_a_clear_message():
    with pytest.raises(ValueError) as refused:
        normalize_line({"name": "A", "cameras": [_vision("a", counting=True), {"camera_id": "q", "role": "qr"},
                                                  _vision("b", counting=True)]})
    message = str(refused.value)
    assert "Only one vision camera can be the counting camera" in message
    assert "Camera 1 and Camera 3" in message and "Inspection station" in message


def test_messages_name_the_camera_by_its_position_and_name(client, admin_headers, vision_model, fake_camera):
    first, second = fake_camera("named-1"), fake_camera("named-2")
    first.name, second.name = "Infeed top", "Infeed side"
    line = client.post(f"{API}/lines", json={"name": "Named cameras"}, headers=admin_headers).json()
    try:
        cameras = [{"camera_id": "named-1", "model_id": vision_model, "counting": True},
                   {"camera_id": "named-2", "model_id": vision_model, "counting": True}]
        res = client.put(f"{API}/lines/{line['id']}", json={"cameras": cameras}, headers=admin_headers)
        assert res.status_code == 422
        assert "Camera 1 ('Infeed top') and Camera 2 ('Infeed side')" in res.json()["detail"]
        res = client.put(f"{API}/lines/{line['id']}", json={"cameras": [cameras[0], {"camera_id": "named-2"}]},
                         headers=admin_headers)
        assert res.status_code == 422 and res.json()["detail"].startswith("Camera 2 ('Infeed side'): choose the vision model")
    finally:
        client.delete(f"{API}/lines/{line['id']}", headers=admin_headers)


def test_a_client_that_does_not_send_the_new_camera_fields_keeps_them():
    saved = normalize_line({"id": "line-x", "name": "X", "cameras": [
        _vision("a"), _vision("b", confidence=0.7, station="join", orientation="vertical", line2_position=0.8),
    ]})
    again = normalize_line({"cameras": [{"camera_id": "a", "role": "vision"}, {"camera_id": "b", "role": "vision"}]}, saved)
    b = again["cameras"][1]
    assert (b["confidence"], b["station"], b["orientation"], b["line2_position"]) == (0.7, "join", "vertical", 0.8)


# ── The confidence threshold reaches the model ────────────────────────────────

class _Feeding:
    """A connected camera that has a new picture on every look."""

    def __init__(self, camera_id):
        self.camera_id, self.name, self.is_connected = camera_id, "Feeding", True
        self._frame = np.zeros((48, 64, 3), dtype=np.uint8)
        self._fid = 0

    def get_latest_frame_id(self):
        self._fid += 1
        return self._fid

    def grab_raw_frame(self):
        return True, self._frame, self._fid

    def disconnect(self):
        self.is_connected = False


def test_the_cameras_confidence_threshold_reaches_the_model(lines, monkeypatch):
    from app.services import vision_service

    engines = {"conf-a": FakeInferenceEngine(), "conf-b": FakeInferenceEngine()}
    monkeypatch.setattr(lines, "engine_for_camera", lambda camera_id: (engines[camera_id], "fake", "m"))
    _save(name="Thresholds", enabled=True, cameras=[_vision("conf-a", confidence=0.42), _vision("conf-b")])
    for camera_id in engines:
        monkeypatch.setitem(app_state.cameras, camera_id, _Feeding(camera_id))
    vision_service.ContinuousVisionRunner.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and not all(e.conf_thresholds for e in engines.values()):
            time.sleep(0.02)
    finally:
        vision_service.ContinuousVisionRunner.stop()
        for camera_id in engines:
            vision_service.CameraStreamPipeline.remove_camera(camera_id)
    assert engines["conf-a"].conf_thresholds and set(engines["conf-a"].conf_thresholds) == {0.42}
    # Without its own threshold a camera uses the model's (None).
    assert engines["conf-b"].conf_thresholds and set(engines["conf-b"].conf_thresholds) == {None}


# ── The version 8 step copies the count lines into the cameras ────────────────

def _v7_state():
    return {
        "schema_version": 7, "camera_auto_connect": False,
        "action_trigger": {"line1_position": 0.25, "line2_position": 0.55, "orientation": "vertical"},
        "lines": [
            {"id": PRIMARY_LINE_ID, "name": "Line 1", "enabled": True, "cameras": [
                {"camera_id": "l1-main", "role": "vision", "counting": True, "qr_hold_ms": 1500, "model_id": "m",
                 "expected_classes": ["bottle"], "defect_classes": []},
                {"camera_id": "l1-qr", "role": "qr", "counting": False, "qr_hold_ms": 1500, "product_list_id": "list-1"},
            ]},
            {"id": "line-2", "name": "Line 2", "enabled": True,
             "action_trigger": {"line1_position": 0.6, "line2_position": 0.8, "orientation": "horizontal",
                                "direction": "backward", "tracking": {"min_hits": 3}},
             "cameras": [
                 {"camera_id": "l2-main", "role": "vision", "counting": True, "qr_hold_ms": 1500, "model_id": "m",
                  "expected_classes": ["bottle"], "defect_classes": []},
                 # This one had its own count line A and one tracking setting already.
                 {"camera_id": "l2-side", "role": "vision", "counting": False, "qr_hold_ms": 1500, "model_id": "m",
                  "expected_classes": ["bottle"], "defect_classes": [], "line1_position": 0.1,
                  "tracking": {"max_missed_frames": 9}},
             ]},
        ],
    }


def test_version_8_copies_the_lines_count_lines_into_its_cameras():
    state = _v7_state()
    before = copy.deepcopy(state)
    assert upgrade_state(state) is True and state["schema_version"] == SCHEMA_VERSION == 8
    main, reader = state["lines"][0]["cameras"]
    assert (main["line1_position"], main["line2_position"], main["orientation"]) == (0.25, 0.55, "vertical")
    assert not any(key in reader for key in ("line1_position", "line2_position", "orientation"))
    l2_main, l2_side = state["lines"][1]["cameras"]
    assert (l2_main["line1_position"], l2_main["line2_position"], l2_main["orientation"]) == (0.6, 0.8, "horizontal")
    assert (l2_main["direction"], l2_main["tracking"]) == ("backward", {"min_hits": 3})
    assert (l2_side["line1_position"], l2_side["line2_position"]) == (0.1, 0.8)  # its own is kept
    assert l2_side["tracking"] == {"min_hits": 3, "max_missed_frames": 9}
    # The line keeps its values: version 1 and older API clients read Line 1's top-level ones.
    assert state["action_trigger"] == before["action_trigger"]
    assert state["lines"][1]["action_trigger"] == before["lines"][1]["action_trigger"]
    # Each camera counts with exactly the settings it had.
    for line_before, line_after in zip(before["lines"], state["lines"]):
        trigger = (before if line_before["id"] == PRIMARY_LINE_ID else line_before)["action_trigger"]
        for cam_before, cam_after in zip(line_before["cameras"], line_after["cameras"]):
            if cam_before["role"] == "vision":
                assert counting_config_from_dict(trigger, cam_after) == counting_config_from_dict(trigger, cam_before)
    again = copy.deepcopy(state)
    assert upgrade_state(state) is False and state == again


def test_an_upgraded_version_7_file_counts_exactly_as_before(lines, tmp_path):
    v7 = _v7_state()
    # How each camera counted with the version 7 file (the runtime reads camera, then line).
    old = {}
    for line in v7["lines"]:
        trigger = (v7 if line["id"] == PRIMARY_LINE_ID else line)["action_trigger"]
        for cam in line["cameras"]:
            if cam["role"] == "vision":
                old[cam["camera_id"]] = counting_config_from_dict(trigger, {**cam, "name_based_defects": True})
    (tmp_path / "system_state.json").write_text(json.dumps(v7))
    SettingsPersistenceService._state = {}
    SettingsPersistenceService.load()
    assert SettingsPersistenceService.get_state()["schema_version"] == SCHEMA_VERSION
    on_disk = json.loads((tmp_path / "system_state.json").read_text())
    assert on_disk["lines"][0]["cameras"][0]["orientation"] == "vertical"
    assert on_disk["action_trigger"]["line1_position"] == 0.25
    lines.apply_state()
    for camera_id, config in old.items():
        assert lines.counter_for_camera(camera_id).config == config, camera_id
    # Line 1 (top-level count lines): products move left to right through vertical lines.
    line1 = lines.get(PRIMARY_LINE_ID)
    assert _pass(line1.counter, "l1-main", "right") == 1 and _pass(line1.counter, "l1-main", "down") == 0
    # Line 2 (its own count lines): bottom to top, lines across.
    line2 = lines.get("line-2")
    assert _pass(line2.counter, "l2-main", "up") == 1 and _pass(line2.counter, "l2-main", "down") == 0


# ── Cameras renamed while connected ───────────────────────────────────────────

def test_renaming_a_connected_camera_renames_its_driver(client, admin_headers, fake_camera):
    created = client.post(f"{API}/cameras", json={"name": "Old name", "type": "usb", "source": "7"}, headers=admin_headers)
    assert created.status_code == 201, created.text
    camera_id = created.json()["id"]
    driver = fake_camera(camera_id)
    try:
        res = client.patch(f"{API}/cameras/{camera_id}", json={"name": "Packing left"}, headers=admin_headers)
        assert res.status_code == 200 and res.json()["name"] == "Packing left"
        assert driver.name == "Packing left"
    finally:
        app_state.cameras.pop(camera_id, None)
        client.delete(f"{API}/cameras/{camera_id}", headers=admin_headers)


# ── The camera strip's figures ────────────────────────────────────────────────

def test_line_summary_gives_each_station_its_own_counts(lines):
    line = _save(name="Strip", enabled=True, cameras=[_vision("s-main", counting=True), _vision("s-side", station="own"),
                                                      {"camera_id": "s-qr", "role": "qr"}])
    runtime = lines.get(line["id"])
    _pass(runtime.aux_counters["s-side"], "s-side", "down")
    rows = {row["camera_id"]: row for row in lines.summary(runtime)["cameras"]}
    assert "station" not in rows["s-main"] and "counts" not in rows["s-main"]
    assert rows["s-side"]["station"] == "own" and rows["s-side"]["counts"]["total_inspected"] == 1
    assert "station" not in rows["s-qr"]
