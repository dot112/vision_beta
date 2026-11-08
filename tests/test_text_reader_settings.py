"""A text reader (role "ocr") on a line: its settings, what the save refuses, and the station and
trigger rules it shares with a vision camera on One picture per trigger."""
from __future__ import annotations

import re

import pytest

from app.services.line_config import (
    REJECT_REASON_CODES,
    REJECT_REASONS,
    camera_station,
    cameras_using_model,
    joined_cameras,
    locks_trigger_mode,
    normalize_camera,
    picture_per_trigger,
    trigger_mode_for_card,
    trigger_mode_to_set,
    vision_models,
)
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_joined_cameras import lines  # noqa: F401  (a fixture)
from tests.test_trigger_numbers import _take

READER = {"camera_id": "c1", "role": "ocr", "count_mode": "trigger", "triggers": [1],
          "ocr": {"fields": [{"name": "Lot", "find": "region", "region": [0.1, 0.2, 0.6, 0.4], "format": "LOT A99999"}]}}


def _field(name, find="region", region=(0.1, 0.2, 0.6, 0.4), format="LOT A99999", **extra):
    field = {"name": name, "find": find, "format": format, **extra}
    if find == "region":
        field["region"] = list(region)
    return field


def test_a_text_reader_saves_its_settings():
    cam = normalize_camera(READER, 0)
    assert cam["role"] == "ocr" and cam["count_mode"] == "trigger" and cam["station"] == "own"
    assert cam["picture_delay_ms"] == 0 and cam["picture_wait_ms"] == 1000 and cam["triggers"] == [1]
    assert cam["ocr"] == {"model": "small", "locator_model_id": None, "upside_down": False, "join_dots": 0,
                          "ink": "dark", "min_confidence": 0.5,
                          "fields": [{"name": "Lot", "find": "region", "region": [0.1, 0.2, 0.6, 0.4], "format": "LOT A99999"}]}
    assert "no_product" not in cam and "model_id" not in cam and cam["counting"] is False
    assert picture_per_trigger(cam) and camera_station(cam) == "own"


def test_a_continuous_text_reader_has_no_station_and_no_picture_settings():
    cam = normalize_camera({**READER, "count_mode": "track", "station": "join"}, 0)
    assert "count_mode" not in cam and "station" not in cam and "picture_source" not in cam and cam.get("triggers", []) == []
    assert camera_station(cam) is None and picture_per_trigger(cam) is False
    assert normalize_camera({**READER, "count_mode": None}, 0).get("count_mode") is None


def test_the_full_settings_are_kept():
    ocr = {"model": "tiny", "locator_model_id": " m1 ", "upside_down": True, "join_dots": 7, "ink": "light",
           "min_confidence": 0.734, "fields": [_field("Lot", find="yolo", yolo_class="dateBox"),
                                               _field("Exp", find="all", format=" EXP 99/99/9999 "),
                                               _field("Batch", region=(0.123456, 0.2, 0.5, 0.6))]}
    cam = normalize_camera({**READER, "ocr": ocr}, 0)["ocr"]
    assert (cam["model"], cam["locator_model_id"], cam["upside_down"], cam["join_dots"], cam["ink"], cam["min_confidence"]) == \
        ("tiny", "m1", True, 7, "light", 0.73)
    assert cam["fields"] == [{"name": "Lot", "find": "yolo", "yolo_class": "dateBox", "format": "LOT A99999"},
                             {"name": "Exp", "find": "all", "format": "EXP 99/99/9999"},
                             {"name": "Batch", "find": "region", "region": [0.1235, 0.2, 0.5, 0.6], "format": "LOT A99999"}]


@pytest.mark.parametrize("ocr, message", [
    ({"fields": []}, "needs 1 to 4 text fields"),
    ({"fields": [_field("Lot")] * 5}, "needs 1 to 4 text fields"),
    ({"fields": [_field("1lot")]}, "the name must start with a letter"),
    ({"fields": [_field("Lot"), _field("LOT")]}, "two text fields are named 'LOT'"),
    ({"fields": [_field("Lot", region=[0.5, 0.2, 0.4, 0.4])]}, "the region must be [x1, y1, x2, y2]"),
    ({"fields": [_field("Lot", region=[0.1, 0.2, 0.11, 0.4])]}, "at least 0.02 wide and high"),
    ({"fields": [_field("Lot", region=[0.1, 0.2, 1.2, 0.4])]}, "inside the picture (0 to 1)"),
    ({"fields": [_field("Lot", find="nowhere")]}, "must find its text in a 'region', by 'yolo' class or in 'all'"),
    ({"fields": [_field("Lot", find="yolo")]}, "choose the YOLO class it reads in"),
    ({"fields": [_field("Lot", find="yolo", yolo_class="dateBox")]}, "choose the YOLO model whose boxes its fields read in"),
    ({"fields": [_field("Lot", format="")]}, "the format must be 1 to 64 characters"),
    ({"model": "huge", "fields": [_field("Lot")]}, "reader model must be 'tiny', 'small' or 'medium'"),
    ({"join_dots": 4, "fields": [_field("Lot")]}, "join dots must be 0 (off) or an odd size from 3 to 15 px"),
    ({"join_dots": 17, "fields": [_field("Lot")]}, "join dots must be 0 (off) or an odd size from 3 to 15 px"),
    ({"ink": "red", "fields": [_field("Lot")]}, "ink must be 'dark' or 'light'"),
    ({"min_confidence": 0.2, "fields": [_field("Lot")]}, "lowest confidence"),
    ("nope", "text reader settings must be an object"),
])
def test_bad_reader_settings_say_what_is_wrong(ocr, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        normalize_camera({**READER, "ocr": ocr}, 0)


def test_a_bad_reading_mode_or_station_is_refused():
    with pytest.raises(ValueError, match="count mode"):
        normalize_camera({**READER, "count_mode": "sometimes"}, 0)
    with pytest.raises(ValueError, match="station must be 'own'"):
        normalize_camera({**READER, "station": "elsewhere"}, 0)


def test_a_yolo_field_keeps_its_model_and_a_region_field_drops_it():
    cam = normalize_camera({**READER, "ocr": {"locator_model_id": "m1", "fields": [_field("Lot", find="yolo", yolo_class="dateBox")]}}, 0)
    assert cam["ocr"]["locator_model_id"] == "m1"
    assert cam["ocr"]["fields"][0] == {"name": "Lot", "find": "yolo", "yolo_class": "dateBox", "format": "LOT A99999"}
    assert normalize_camera({**READER, "ocr": {"locator_model_id": "m1", "fields": [_field("Lot")]}}, 0)["ocr"]["locator_model_id"] is None


def test_the_text_reasons_have_plc_numbers():
    assert (REJECT_REASON_CODES["text_wrong"], REJECT_REASON_CODES["no_text"], REJECT_REASON_CODES["reader_error"]) == (11, 12, 13)
    assert {"text_wrong", "no_text", "reader_error"} <= set(REJECT_REASONS)


def test_a_joined_text_reader_is_a_joined_station():
    cam = normalize_camera({**READER, "station": "join", "join_offset_ms": 300, "join_missing": "reject"}, 0)
    assert (cam["join_offset_ms"], cam["join_window_ms"], cam["join_missing"]) == (300, 500, "reject")
    assert joined_cameras([cam]) == [cam] and locks_trigger_mode(cam) and trigger_mode_for_card(cam) == "software"
    camera = normalize_camera({**READER, "picture_source": "camera"}, 0)
    assert trigger_mode_for_card(camera) == "hardware" and trigger_mode_to_set(camera, "off") == "hardware"
    continuous = normalize_camera({**READER, "count_mode": "track"}, 0)
    assert not locks_trigger_mode(continuous) and trigger_mode_to_set(continuous, "software") is None


def test_the_locator_model_is_in_use_and_loaded(lines):  # noqa: F811
    line = SettingsPersistenceService.save_line({"name": "OCR", "cameras": [{**READER, "ocr": {"locator_model_id": "m1",
        "fields": [_field("Lot", find="yolo", yolo_class="dateBox")]}}]})
    assert vision_models(line) == ["m1"]
    assert [cam["camera_id"] for _, cam in cameras_using_model([line], "m1")] == ["c1"]


def test_a_running_line_loads_its_text_readers_yolo_model(lines, monkeypatch):  # noqa: F811
    """The line loads a reader's YOLO model, used by no vision camera, and warms it for a triggered reader
    (review C1: a YOLO-class field failed with "the YOLO model of the text reader is not loaded")."""
    import asyncio

    from tests.conftest import FakeInferenceEngine

    loads, warmed = [], []

    async def load(model_id):
        loads.append(model_id)
        return {"engine": FakeInferenceEngine(), "name": model_id}, None

    monkeypatch.setattr(lines, "_load_engine", load)
    monkeypatch.setattr("app.services.line_service._warm_up", lambda engine, name: warmed.append(name))
    line = SettingsPersistenceService.save_line({"name": "OCR", "enabled": True, "cameras": [{**READER, "ocr": {
        "locator_model_id": "m1", "fields": [_field("Lot", find="yolo", yolo_class="dateBox")]}}]})
    lines.apply_state()

    async def refresh():
        await lines.refresh_models()
        await asyncio.sleep(0.05)                       # the warm-up runs off the event loop

    asyncio.run(refresh())
    assert loads == ["m1"] and lines.engine_for_camera("c1")[0].is_loaded
    assert warmed == ["m1"]
    SettingsPersistenceService.save_line({"id": line["id"], "cameras": []})
    lines.apply_state()
    asyncio.run(lines.refresh_models())
    assert lines.loaded_models() == []


def test_a_client_that_leaves_the_reader_settings_out_keeps_them(lines):  # noqa: F811
    saved = SettingsPersistenceService.save_line({"name": "OCR", "cameras": [{**READER, "station": "join", "join_offset_ms": 250}]})
    again = SettingsPersistenceService.save_line({"id": saved["id"], "name": "OCR", "cameras": [{"camera_id": "c1", "role": "ocr"}]})
    cam = again["cameras"][0]
    assert cam["ocr"] == saved["cameras"][0]["ocr"] and cam["count_mode"] == "trigger"
    assert (cam["station"], cam["join_offset_ms"], cam["triggers"]) == ("join", 250, [1])


def test_a_capture_input_may_name_a_triggered_text_reader(lines):  # noqa: F811
    line = SettingsPersistenceService.save_line({"name": "L", "cameras": [{**READER, "triggers": []}],
                                                 "plc_inputs": [_take(4, camera_id="c1")]})
    assert line["cameras"][0]["triggers"] == [4]
    with pytest.raises(ValueError, match="a text reader"):
        SettingsPersistenceService.save_line({"name": "M", "cameras": [{**READER, "count_mode": "track"}],
                                             "plc_inputs": [_take(1, camera_id="c1")]})
