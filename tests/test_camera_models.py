"""Each vision camera runs its own model and counts its own classes (settings version 6)."""
from __future__ import annotations

import asyncio
import copy
import io

import pytest

import app.services.line_service as line_module
import app.services.settings_persistence_service as persistence_module
from app.events.alarm_events import AlarmCode, alarm_manager
from app.services.counting_service import counting_service
from app.services.line_config import (
    PRIMARY_LINE_ID,
    SCHEMA_VERSION,
    cameras_using_model,
    model_warnings,
    normalize_line,
    upgrade_state,
)
from app.services.line_service import LineManager
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService
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
    alarm_manager.reset()
    yield manager
    alarm_manager.reset()
    counting_service.event_sink = None
    counting_service.update_config(saved_config)


def _save(**line):
    saved = SettingsPersistenceService.save_line(line)
    line_module.line_manager.apply_state()
    return saved


# ── The settings upgrade ──────────────────────────────────────────────────────

def _v5_state():
    return {
        "schema_version": 5, "camera_auto_connect": True, "active_model_id": "model-active", "active_camera_id": "cam-1",
        "action_trigger": {"line1_position": 0.3, "expected_classes": ["orange", "apple"], "defect_classes": ["bruised"]},
        "lines": [
            {"id": PRIMARY_LINE_ID, "name": "Line 1", "enabled": True, "model_id": "model-own", "cameras": [
                {"camera_id": "cam-1", "role": "vision", "counting": True, "qr_hold_ms": 1500},
                {"camera_id": "cam-qr", "role": "qr", "counting": False, "qr_hold_ms": 1500, "product_list_id": "list-1"},
            ]},
            # This line followed the server's active model and has two vision cameras.
            {"id": "line-2", "name": "Line 2", "enabled": True, "model_id": None,
             "action_trigger": {"line1_position": 0.4, "expected_classes": ["box"]}, "cameras": [
                 {"camera_id": "cam-2", "role": "vision", "counting": True, "qr_hold_ms": 1500},
                 {"camera_id": "cam-3", "role": "vision", "counting": False, "qr_hold_ms": 1500, "line1_position": 0.1},
             ]},
            {"id": "line-3", "name": "Line 3", "enabled": False, "model_id": "model-own", "cameras": [],
             "action_trigger": {"line1_position": 0.5, "expected_classes": ["cup"], "defect_classes": []}},
        ],
    }


def test_upgrade_gives_each_vision_camera_its_lines_model_and_class_lists():
    state = _v5_state()
    assert upgrade_state(state) is True and state["schema_version"] == SCHEMA_VERSION
    line1, line2, line3 = state["lines"]

    own, reader = line1["cameras"]
    assert (own["model_id"], own["expected_classes"], own["defect_classes"]) == ("model-own", ["orange", "apple"], ["bruised"])
    assert not any(key in reader for key in ("model_id", "expected_classes", "defect_classes"))
    # A line that named no model ran the server's active one; a list it did not have was the old default.
    for camera in line2["cameras"]:
        assert (camera["model_id"], camera["expected_classes"]) == ("model-active", ["box"])
        assert camera["defect_classes"] == ["defect", "scratch", "broken"]
    assert line2["cameras"][1]["line1_position"] == 0.1

    # Nothing of it is left on the lines or in the settings.
    assert "active_model_id" not in state and not any("model_id" in line for line in state["lines"])
    assert state["action_trigger"] == {"line1_position": 0.3}
    assert line2["action_trigger"] == {"line1_position": 0.4} and line3["action_trigger"] == {"line1_position": 0.5}
    assert line3["cameras"] == []
    # A second load changes nothing.
    again = copy.deepcopy(state)
    assert upgrade_state(state) is False and state == again


def test_upgraded_lines_detect_and_count_as_before(lines):
    state = _v5_state()
    upgrade_state(state)
    SettingsPersistenceService._state.update({key: state[key] for key in ("lines", "action_trigger")})
    lines.apply_state()
    assert lines.model_for_camera("cam-1") == "model-own"
    assert lines.model_for_camera("cam-2") == lines.model_for_camera("cam-3") == "model-active"
    assert lines.model_for_camera("cam-qr") is None
    assert counting_service.config.expected_classes == ["orange", "apple"]
    assert counting_service.config.defect_classes == ["bruised"]
    second = lines.counter_for_camera("cam-3").config
    assert (second.expected_classes, second.line1_position) == (["box"], 0.1)


def test_upgrade_makes_the_camera_that_fed_line1_its_counting_camera():
    """Line 1 without cameras was fed by the selected camera, with the active model."""
    def state(**changes):
        base = {"schema_version": 5, "active_model_id": "model-active", "active_camera_id": "cam-9",
                "action_trigger": {"expected_classes": ["can"], "defect_classes": []},
                "lines": [{"id": PRIMARY_LINE_ID, "name": "Line 1", "enabled": True, "model_id": None, "cameras": []}]}
        base.update(changes)
        upgrade_state(base)
        return base

    assert state()["lines"][0]["cameras"] == [{
        "camera_id": "cam-9", "role": "vision", "counting": True, "qr_hold_ms": 1500,
        "model_id": "model-active", "expected_classes": ["can"], "defect_classes": [],
    }]
    # Without an active model nothing was detected, so there is nothing to carry over.
    assert state(active_model_id=None)["lines"][0]["cameras"] == []
    # A camera that belongs to another line stays there.
    other = {"id": "line-2", "name": "Line 2", "enabled": True, "cameras": [{"camera_id": "cam-9", "role": "qr"}]}
    taken = state(lines=[{"id": PRIMARY_LINE_ID, "name": "Line 1", "enabled": True, "cameras": []}, other])
    assert taken["lines"][0]["cameras"] == []


# ── Saving a line ─────────────────────────────────────────────────────────────

def test_a_line_is_not_saved_with_a_vision_camera_that_has_no_model(lines):
    with pytest.raises(ValueError, match="Camera 1: choose the vision model"):
        SettingsPersistenceService.save_line({"name": "Packing", "cameras": [{"camera_id": "cam-a"}]})
    with pytest.raises(ValueError, match="Camera 2: choose the vision model"):
        SettingsPersistenceService.save_line({"name": "Packing", "cameras": [
            {"camera_id": "cam-a", "model_id": "m-a"}, {"camera_id": "cam-b", "read_codes": True}]})
    # A reader needs none, and keeps no model or class lists.
    line = _save(name="Readers", cameras=[{"camera_id": "cam-q", "role": "qr", "model_id": "m-a", "expected_classes": ["x"]}])
    assert "model_id" not in line["cameras"][0] and "expected_classes" not in line["cameras"][0]
    line = _save(name="Packing", cameras=[{"camera_id": "cam-a", "model_id": " m-a ", "expected_classes": ["Bottle", "bottle ", "can"],
                                          "defect_classes": ["dent"]}])
    camera = line["cameras"][0]
    assert (camera["model_id"], camera["expected_classes"], camera["defect_classes"]) == ("m-a", ["Bottle", "can"], ["dent"])
    with pytest.raises(ValueError, match="products to count"):
        normalize_line({"name": "A", "cameras": [{"camera_id": "a", "model_id": "m", "expected_classes": "bottle, can"}]})


def test_a_line_from_before_per_camera_models_can_still_be_started_and_renamed(lines):
    """An upgraded line may have a camera without a model (no model was active): only saving its cameras needs one."""
    SettingsPersistenceService._state["lines"].append(
        {"id": "line-old", "name": "Old", "enabled": False, "cameras": [{"camera_id": "cam-old", "role": "vision", "counting": True}]})
    started = SettingsPersistenceService.save_line({"id": "line-old", "enabled": True, "name": "Old line"})
    assert started["enabled"] is True and started["cameras"][0]["model_id"] is None
    with pytest.raises(ValueError, match="choose the vision model"):
        SettingsPersistenceService.save_line({"id": "line-old", "cameras": [{"camera_id": "cam-old"}]})


def test_a_client_from_before_per_camera_models_keeps_working(lines):
    line = _save(name="Packing", cameras=[{"camera_id": "cam-a", "model_id": "m-a", "expected_classes": ["bottle"], "defect_classes": ["dent"]}])
    # It sends the cameras without a model or class lists: they stay as saved.
    resent = _save(id=line["id"], cameras=[{"camera_id": "cam-a", "counting": True}, {"camera_id": "cam-q", "role": "qr"}])
    assert resent["cameras"][0]["model_id"] == "m-a"
    assert (resent["cameras"][0]["expected_classes"], resent["cameras"][0]["defect_classes"]) == (["bottle"], ["dent"])
    # It names one model for the line: the vision cameras it sends without a model run it,
    # one it sends with a model keeps that, and nothing is kept on the line itself.
    moved = _save(id=line["id"], model_id="m-line", cameras=[
        {"camera_id": "cam-a"}, {"camera_id": "cam-new", "model_id": "m-own"}])
    assert [cam["model_id"] for cam in moved["cameras"]] == ["m-line", "m-own"]
    assert moved["cameras"][0]["expected_classes"] == ["bottle"] and "model_id" not in moved
    # The same when it changes only the line's model, or sends none (the old "server active model").
    assert [cam["model_id"] for cam in _save(id=line["id"], model_id="m-all")["cameras"]] == ["m-all", "m-all"]
    assert [cam["model_id"] for cam in _save(id=line["id"], model_id=None, name="Packing A")["cameras"]] == ["m-all", "m-all"]
    # The class lists it still sends with the count lines are not kept there.
    trigger = _save(id=line["id"], action_trigger={"line1_position": 0.2, "expected_classes": ["typed"], "defect_classes": []})["action_trigger"]
    assert trigger["line1_position"] == 0.2 and "expected_classes" not in trigger and "defect_classes" not in trigger
    SettingsPersistenceService.update_settings({"action_trigger": {"line2_position": 0.9, "expected_classes": ["typed"]}})
    assert "expected_classes" not in SettingsPersistenceService.get_state()["action_trigger"]
    assert lines.get(line["id"]).counter.config.expected_classes == ["bottle"]


# ── Models at runtime ─────────────────────────────────────────────────────────

def test_each_camera_runs_its_own_model_and_counts_its_own_classes(lines):
    line = _save(name="Two cameras", cameras=[
        {"camera_id": "top", "counting": True, "model_id": "m-top", "expected_classes": ["bottle"], "defect_classes": ["dent"]},
        {"camera_id": "side", "model_id": "m-side", "expected_classes": ["label"], "defect_classes": []},
    ])
    top, side = FakeInferenceEngine(), FakeInferenceEngine()
    lines._engines = {"m-top": {"engine": top, "name": "Top model"}, "m-side": {"engine": side, "name": "Side model"}}
    assert lines.engine_for_camera("top") == (top, "Top model", "m-top")
    assert lines.engine_for_camera("side") == (side, "Side model", "m-side")
    runtime = lines.get(line["id"])
    assert (runtime.counter.config.expected_classes, runtime.counter.config.defect_classes) == (["bottle"], ["dent"])
    aux = lines.counter_for_camera("side").config
    assert (aux.expected_classes, aux.defect_classes) == (["label"], [])

    row = lines.summary(runtime)
    assert row["model_name"] == "Top model, Side model"
    assert [(c["model_id"], c["model_name"], c["model_loaded"]) for c in row["cameras"]] == [
        ("m-top", "Top model", True), ("m-side", "Side model", True)]
    assert row["cameras"][1]["expected_classes"] == ["label"]
    # Line 1's counting camera is what version 1 called the server's one camera.
    assert lines.default_model_id() is None
    _save(id=PRIMARY_LINE_ID, cameras=[{"camera_id": "first", "model_id": "m-side"}])
    assert lines.default_model_id() == "m-side"


def test_a_counter_changed_while_running_keeps_that_until_its_settings_change(lines):
    line = _save(name="Packing", cameras=[{"camera_id": "cam-a", "model_id": "m-a", "expected_classes": ["bottle"]}])
    counter = lines.get(line["id"]).counter
    counter.update_config(counter.config.model_copy(update={"min_hits": 5}))
    lines.apply_state()  # another line was saved: this one's settings are the same
    assert counter.config.min_hits == 5
    _save(id=line["id"], cameras=[{"camera_id": "cam-a", "model_id": "m-a", "expected_classes": ["can"]}])
    assert counter.config.expected_classes == ["can"] and counter.config.min_hits == 2


def test_models_are_loaded_once_each_and_a_camera_that_cannot_run_its_model_raises_an_alarm(lines, monkeypatch):
    loads = []

    async def load(model_id):
        loads.append(model_id)
        lines._model_names[model_id] = f"Model {model_id}"
        if model_id == "m-broken":
            return None, "the model's file is missing"
        return {"engine": FakeInferenceEngine(), "name": f"Model {model_id}"}, None

    monkeypatch.setattr(lines, "_load_engine", load)
    _save(id=PRIMARY_LINE_ID, cameras=[{"camera_id": "cam-1", "model_id": "m-shared"}])
    two = _save(name="Line 2", enabled=True, cameras=[
        {"camera_id": "cam-2", "counting": True, "model_id": "m-shared"}, {"camera_id": "cam-3", "model_id": "m-broken"}])
    stopped = _save(name="Line 3", enabled=False, cameras=[{"camera_id": "cam-4", "model_id": "m-broken"}])

    assert asyncio.run(lines.refresh_models()) == {"m-broken": "the model's file is missing"}
    assert sorted(loads) == ["m-broken", "m-shared"]  # one copy of the model two cameras share
    assert lines.engine_for_camera("cam-1")[0] is lines.engine_for_camera("cam-2")[0]
    assert lines.engine_for_camera("cam-3")[0].is_loaded is False
    assert lines.loaded_models() == [{"id": "m-shared", "name": "Model m-shared"}]
    assert lines.model_state("m-broken") == {"loaded": False, "error": "the model's file is missing"}

    # Only the running line's camera raises the alarm, and it names the line and the reason.
    alarms = alarm_manager.active("camera_model:")
    assert [a.source for a in alarms] == ["camera_model:cam-3"]
    assert alarms[0].code == AlarmCode.CAMERA_MODEL_UNAVAILABLE and alarms[0].details["line_id"] == two["id"]
    assert "Line 2" in alarms[0].message and "the model's file is missing" in alarms[0].message
    row = lines.summary(lines.get(two["id"]))["cameras"][1]
    assert (row["model_loaded"], row["model_error"]) == (False, "the model's file is missing")

    # Picking a model that loads clears it; a model no camera runs any more is dropped.
    _save(id=two["id"], cameras=[{"camera_id": "cam-2", "model_id": "m-shared"}, {"camera_id": "cam-3", "model_id": "m-shared"}])
    _save(id=stopped["id"], cameras=[])
    assert asyncio.run(lines.refresh_models()) == {}
    assert alarm_manager.active("camera_model:") == []
    _save(id=PRIMARY_LINE_ID, cameras=[])
    _save(id=two["id"], cameras=[])
    asyncio.run(lines.refresh_models())
    assert lines.loaded_models() == []


def test_a_running_vision_camera_without_a_model_raises_an_alarm(lines):
    SettingsPersistenceService._state["lines"].append(
        {"id": "line-old", "name": "Old", "enabled": True, "cameras": [{"camera_id": "cam-old", "role": "vision", "counting": True}]})
    lines.apply_state()
    asyncio.run(lines.refresh_models())
    (alarm,) = alarm_manager.active("camera_model:")
    assert "has no vision model" in alarm.message and alarm.details == {"line_id": "line-old", "camera_id": "cam-old", "model_id": None}
    SettingsPersistenceService.save_line({"id": "line-old", "enabled": False})
    lines.apply_state()
    asyncio.run(lines.refresh_models())
    assert alarm_manager.active() == []


def test_a_detect_call_names_a_model_or_gets_line1s(lines, monkeypatch):
    loads = []

    async def load(model_id):
        loads.append(model_id)
        if model_id == "m-gone":
            return None, "the model no longer exists"
        return {"engine": FakeInferenceEngine(), "name": f"Model {model_id}"}, None

    monkeypatch.setattr(lines, "_load_engine", load)
    with pytest.raises(ValueError, match="pick a model for Line 1's vision camera"):
        asyncio.run(lines.api_engine())
    with pytest.raises(ValueError, match="the model no longer exists"):
        asyncio.run(lines.api_engine("m-gone"))

    _save(id=PRIMARY_LINE_ID, cameras=[{"camera_id": "cam-1", "model_id": "m-1"}])
    _save(name="Line 2", cameras=[{"camera_id": "cam-2", "model_id": "m-2"}])
    asyncio.run(lines.refresh_models())
    loads.clear()
    assert asyncio.run(lines.api_engine())[1:] == ("Model m-1", "m-1")                    # Line 1's counting camera
    assert asyncio.run(lines.api_engine(camera_id="cam-2"))[1:] == ("Model m-2", "m-2")   # the camera's own
    assert asyncio.run(lines.api_engine("m-1", "cam-2"))[1:] == ("Model m-1", "m-1")      # the one that is named
    assert asyncio.run(lines.api_engine(camera_id="cam-unassigned"))[2] == "m-1"
    assert loads == []
    # A model no camera runs is loaded for the call and kept for the next one, one at a time.
    first = asyncio.run(lines.api_engine("m-extra"))[0]
    assert asyncio.run(lines.api_engine("m-extra"))[0] is first and loads == ["m-extra"]
    asyncio.run(lines.api_engine("m-other"))
    assert lines._api_model[0] == "m-other" and loads == ["m-extra", "m-other"]
    # A camera that then picks it takes over the loaded copy.
    other = lines._api_model[1]
    _save(name="Line 3", cameras=[{"camera_id": "cam-3", "model_id": "m-other"}])
    asyncio.run(lines.refresh_models())
    assert lines._engines["m-other"] is other and lines._api_model is None and loads == ["m-extra", "m-other"]


def test_a_model_that_is_dropped_is_freed_at_once(lines, monkeypatch):
    # Left to the garbage collector, a GPU session could be freed while another model runs.
    class Engine(FakeInferenceEngine):
        closed = False

        def close(self):
            self.closed = True

    made = {}

    async def load(model_id):
        made[model_id] = Engine()
        return {"engine": made[model_id], "name": f"Model {model_id}"}, None

    monkeypatch.setattr(lines, "_load_engine", load)
    _save(id=PRIMARY_LINE_ID, cameras=[{"camera_id": "cam-1", "model_id": "m-1"}])
    two = _save(name="Line 2", cameras=[{"camera_id": "cam-2", "model_id": "m-2"}])
    asyncio.run(lines.refresh_models())

    # No camera runs it any more.
    _save(id=two["id"], cameras=[])
    asyncio.run(lines.refresh_models())
    assert made["m-2"].closed and not made["m-1"].closed
    # A detect call names another model than the one loaded for the call before.
    asyncio.run(lines.api_engine("m-extra"))
    asyncio.run(lines.api_engine("m-other"))
    assert made["m-extra"].closed and not made["m-other"].closed
    # A camera takes over the copy a detect call loaded: it stays loaded.
    _save(id=two["id"], cameras=[{"camera_id": "cam-2", "model_id": "m-other"}])
    asyncio.run(lines.refresh_models())
    assert not made["m-other"].closed
    # The model a detect call loaded is deleted.
    asyncio.run(lines.api_engine("m-last"))
    lines.forget_model("m-last")
    assert made["m-last"].closed and lines._api_model is None


def test_inference_worker_skips_a_camera_without_a_model(lines):
    import time

    import numpy as np

    from app.services.vision_service import _CameraInferenceWorker
    from app.state.application_state import app_state

    before = app_state.processed_frames
    worker = _CameraInferenceWorker("cam-nomodel")
    try:
        assert worker.submit_frame_if_idle(np.zeros((48, 64, 3), dtype=np.uint8), 1)
        deadline = time.time() + 2
        while worker._processed_frame_id != 1 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()
    assert worker._processed_frame_id == 1 and worker.get_latest_inference() == ([], 0.0)
    assert app_state.processed_frames == before and counting_service.total_inspected == 0


# ── Warnings on Line setup ────────────────────────────────────────────────────

def test_line_setup_warns_about_a_model_that_is_missing_not_loaded_or_lacks_a_class():
    line = {"cameras": [
        {"camera_id": "a", "role": "vision", "model_id": None},
        {"camera_id": "b", "role": "vision", "model_id": "m-gone"},
        {"camera_id": "q", "role": "qr"},
    ]}
    none, gone = model_warnings(line, {})
    assert none.startswith("Camera 1 has no vision model") and "Camera 2: its vision model no longer exists" in gone

    line = {"cameras": [{"camera_id": "a", "role": "vision", "model_id": "m", "expected_classes": ["Bottle", "botle"],
                         "defect_classes": ["scratch", "dent"]}]}
    models = {"m": {"name": "Bottles", "classes": ["bottle", "dent"], "loaded": False, "error": "out of GPU memory"}}
    unloaded, unknown = model_warnings(line, models)
    assert "'Bottles' is not loaded (out of GPU memory)" in unloaded
    assert "'botle', 'scratch' are not classes of its model 'Bottles'" in unknown
    models["m"]["loaded"] = True
    line["cameras"][0].update(expected_classes=["bottle"], defect_classes=["dent"])
    assert model_warnings(line, models) == []
    assert [cam["camera_id"] for _, cam in cameras_using_model([line], "m")] == ["a"]


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def line1_restored(client, admin_headers):
    """Tests that change Line 1 through the API put its cameras back."""
    before = client.get(f"{API}/lines/{PRIMARY_LINE_ID}", headers=admin_headers).json()["cameras"]
    yield
    res = client.put(f"{API}/lines/{PRIMARY_LINE_ID}", json={"cameras": before}, headers=admin_headers)
    assert res.status_code == 200, res.text


def _model(client, admin_headers, model_id):
    return next(m for m in client.get(f"{API}/models", headers=admin_headers).json() if m["id"] == model_id)


def test_a_model_is_active_while_a_camera_runs_it_and_cannot_be_deleted_then(client, admin_headers, vision_model):
    assert _model(client, admin_headers, vision_model)["is_active"] is False
    camera = client.post(f"{API}/cameras", json={"name": "Model cam", "type": "usb", "source": "96", "settings": {}}, headers=admin_headers).json()

    # A vision camera needs a model, and one that exists.
    none = client.post(f"{API}/lines", json={"name": "Model line", "cameras": [{"camera_id": camera["id"]}]}, headers=admin_headers)
    assert none.status_code == 422 and "choose the vision model" in none.json()["detail"]
    gone = client.post(f"{API}/lines", json={"name": "Model line", "cameras": [{"camera_id": camera["id"], "model_id": "no-such-model"}]},
                       headers=admin_headers)
    assert gone.status_code == 422 and "was not found" in gone.json()["detail"]

    made = client.post(f"{API}/lines", json={"name": "Model line", "cameras": [
        {"camera_id": camera["id"], "model_id": vision_model, "expected_classes": ["bottle"], "defect_classes": ["scratch"]}]},
        headers=admin_headers)
    assert made.status_code == 201, made.text
    line = made.json()
    try:
        row = line["status"]["cameras"][0]
        assert (row["model_id"], row["model_name"], row["model_loaded"]) == (vision_model, "fake-model", True)
        # 'scratch' is not one of the model's classes: said once after the save and kept on Line setup.
        assert "'scratch' is not a class of its model 'fake-model'" in line["warning"]
        assert line["warnings"] == [line["warning"]]

        model = _model(client, admin_headers, vision_model)
        assert model["is_active"] is True and model["loaded"] is True
        assert model["used_by"] == [{"line_id": line["id"], "line_name": "Model line", "camera_id": camera["id"], "camera_name": "Model cam"}]
        refused = client.delete(f"{API}/models/{vision_model}", headers=admin_headers)
        assert refused.status_code == 409 and "Model line (Model cam)" in refused.json()["detail"]
    finally:
        client.delete(f"{API}/lines/{line['id']}", headers=admin_headers)
        client.delete(f"{API}/cameras/{camera['id']}", headers=admin_headers)
    model = _model(client, admin_headers, vision_model)
    assert (model["is_active"], model["used_by"], model["loaded"]) == (False, [], False)
    assert client.delete(f"{API}/models/{vision_model}", headers=admin_headers).status_code == 204


def test_activate_picks_the_model_for_line1s_counting_camera(client, admin_headers, vision_model, line1_restored):
    """The calls of version 1: its one camera is Line 1's counting camera now."""
    camera = client.post(f"{API}/cameras", json={"name": "Line 1 cam", "type": "usb", "source": "95", "settings": {}}, headers=admin_headers).json()
    try:
        # Line 1 with only a reader has no camera to run a model on.
        client.put(f"{API}/lines/{PRIMARY_LINE_ID}", json={"cameras": [{"camera_id": camera["id"], "role": "qr"}]}, headers=admin_headers)
        assert client.get(f"{API}/models/active", headers=admin_headers).status_code == 404
        refused = client.post(f"{API}/models/{vision_model}/activate", headers=admin_headers)
        assert refused.status_code == 409 and "no vision camera" in refused.json()["detail"]
        assert client.post(f"{API}/models/no-such-model/activate", headers=admin_headers).status_code == 404

        # Line 1 without cameras: the camera that feeds it becomes its counting camera, with the model.
        client.put(f"{API}/lines/{PRIMARY_LINE_ID}", json={"cameras": []}, headers=admin_headers)
        client.post(f"{API}/system/settings", json={"active_camera_id": camera["id"]}, headers=admin_headers)
        activated = client.post(f"{API}/models/{vision_model}/activate", headers=admin_headers)
        assert activated.status_code == 200, activated.text
        assert activated.json()["is_active"] is True and activated.json()["used_by"][0]["line_id"] == PRIMARY_LINE_ID
        (only,) = client.get(f"{API}/lines/{PRIMARY_LINE_ID}", headers=admin_headers).json()["cameras"]
        assert (only["camera_id"], only["role"], only["counting"], only["model_id"]) == (camera["id"], "vision", True, vision_model)
        assert client.get(f"{API}/models/active", headers=admin_headers).json()["id"] == vision_model

        # With a counting camera, activate changes that camera's model and nothing else of it.
        other = client.post(f"{API}/models/register", headers=admin_headers, json={
            "name": "second-model", "file_path": _model(client, admin_headers, vision_model)["file_path"], "classes": ["can"]}).json()
        client.put(f"{API}/lines/{PRIMARY_LINE_ID}", json={"cameras": [
            {"camera_id": camera["id"], "model_id": vision_model, "expected_classes": ["bottle"]}]}, headers=admin_headers)
        assert client.post(f"{API}/models/{other['id']}/activate", headers=admin_headers).status_code == 200
        (only,) = client.get(f"{API}/lines/{PRIMARY_LINE_ID}", headers=admin_headers).json()["cameras"]
        assert (only["model_id"], only["expected_classes"]) == (other["id"], ["bottle"])
        assert _model(client, admin_headers, vision_model)["is_active"] is False
        client.put(f"{API}/lines/{PRIMARY_LINE_ID}", json={"cameras": []}, headers=admin_headers)
        assert client.delete(f"{API}/models/{other['id']}", headers=admin_headers).status_code == 204
    finally:
        client.put(f"{API}/lines/{PRIMARY_LINE_ID}", json={"cameras": []}, headers=admin_headers)
        client.delete(f"{API}/cameras/{camera['id']}", headers=admin_headers)


def test_detect_runs_the_named_model_and_says_when_there_is_none(client, admin_headers, vision_model, line1_restored):
    import cv2
    import numpy as np

    ok, png = cv2.imencode(".png", np.zeros((48, 64, 3), dtype=np.uint8))
    assert ok

    def detect(**params):
        return client.post(f"{API}/vision/detect", headers=admin_headers, params=params,
                           files={"file": ("part.png", io.BytesIO(png.tobytes()), "image/png")})

    client.put(f"{API}/lines/{PRIMARY_LINE_ID}", json={"cameras": []}, headers=admin_headers)
    none = detect()
    assert none.status_code == 400 and "pick a model for Line 1's vision camera" in none.json()["detail"]
    named = detect(model_id=vision_model)
    assert named.status_code == 200, named.text
    assert named.json()["detections"][0]["class_name"] == "bottle"
