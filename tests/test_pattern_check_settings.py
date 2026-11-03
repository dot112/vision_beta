"""A pattern check (role "match") on a line: its settings, what the save refuses (no trained reference set,
a set with another number of checks, a search area smaller than the reference), and the station and
trigger rules it shares with a text reader on One picture per trigger."""
from __future__ import annotations

import json

import pytest

from app.services import match_references
from app.services.line_config import (REJECT_REASON_CODES, REJECT_REASONS, camera_station, is_pattern_check,
                                      normalize_camera, picture_per_trigger)
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_joined_cameras import lines  # noqa: F401  (a fixture)
from tests.test_trigger_numbers import _take

SET_ID = "ref-0a1b2c3d"
GOOD = {"camera_id": "m1", "role": "match", "match": {"reference_id": SET_ID, "checks": [{"name": "Label"}]}}


def _match(**change):
    return {**GOOD, "match": {**GOOD["match"], **change}}


def test_defaults():
    cam = normalize_camera(GOOD, 0)
    assert cam["match"] == {"reference_id": SET_ID, "search_area": None, "angle": 15,
                            "checks": [{"name": "Label", "min_score": 80, "max_defect_px": 30, "colour": False,
                                        "sensitivity": "normal"}]}
    assert cam["role"] == "match" and is_pattern_check(cam)
    assert "count_mode" not in cam and camera_station(cam) is None and not picture_per_trigger(cam)


def test_all_settings_are_kept():
    cam = normalize_camera(_match(search_area=[0.1, 0.05, 0.9, 0.95], angle=30, checks=[
        {"name": "Label", "min_score": 85, "max_defect_px": 40, "colour": False, "sensitivity": "low"},
        {"name": "Cap", "min_score": 70, "max_defect_px": 0, "colour": True, "sensitivity": "high"}]), 0)
    assert cam["match"]["search_area"] == [0.1, 0.05, 0.9, 0.95] and cam["match"]["angle"] == 30
    assert cam["match"]["checks"][1] == {"name": "Cap", "min_score": 70, "max_defect_px": 0, "colour": True, "sensitivity": "high"}


@pytest.mark.parametrize("change, message", [
    ({"reference_id": "x"}, "train its reference on Pattern and checks"),
    ({"reference_id": None}, "train its reference on Pattern and checks"),
    ({"angle": 46}, "0 to 45"), ({"angle": 2.5}, "0 to 45"),
    ({"checks": []}, "1 to 4 checks"), ({"checks": [{"name": f"C{i}"} for i in range(5)]}, "1 to 4 checks"),
    ({"checks": [{"name": "score"}]}, "cannot be named 'score'"),
    ({"checks": [{"name": "A"}, {"name": "a"}]}, "two checks are named"),
    ({"checks": [{"name": "1st"}]}, "must start with a letter"),
    ({"checks": [{"name": "A", "min_score": 101}]}, "0 to 100"),
    ({"checks": [{"name": "A", "max_defect_px": -1}]}, "0 to 1000000"),
    ({"checks": [{"name": "A", "sensitivity": "max"}]}, "'low', 'normal' or 'high'"),
    ({"search_area": [0.5, 0, 0.51, 1]}, "at least 0.02"),
    ({"search_area": [0.5, 0.5, 0.2, 0.9]}, "at least 0.02")])
def test_refusals(change, message):
    with pytest.raises(ValueError, match=message):
        normalize_camera(_match(**change), 0)


def test_the_role_list_names_the_pattern_check():
    with pytest.raises(ValueError, match="'vision', 'qr', 'ocr' or 'match'"):
        normalize_camera({"camera_id": "x", "role": "colour"}, 0)


def test_one_picture_per_trigger_own_and_join():
    cam = normalize_camera({**GOOD, "count_mode": "trigger", "triggers": [2]}, 0)
    assert picture_per_trigger(cam) and camera_station(cam) == "own" and cam["triggers"] == [2]
    assert cam["picture_wait_ms"] == 1000 and cam["picture_delay_ms"] == 0
    joined = normalize_camera({**GOOD, "count_mode": "trigger", "station": "join", "join_offset_ms": 300}, 0)
    assert camera_station(joined) == "join" and joined["join_offset_ms"] == 300 and joined["join_missing"] == "ignore"


def _set_file(data_dir, set_id=SET_ID, checks=1):
    folder = data_dir / "match-references" / set_id
    folder.mkdir(parents=True)
    (folder / "set.json").write_text(json.dumps({
        "format": 1, "id": set_id, "reference": [460, 360, 820, 600], "picture_size": [1280, 960], "samples": 8,
        "checks": [{"box": [-180, -120, 180, 120], "typical": 0.9}] * checks}), encoding="utf-8")


def test_a_client_that_leaves_match_out_keeps_it(lines, data_dir):  # noqa: F811
    _set_file(data_dir)
    saved = SettingsPersistenceService.save_line({"name": "Pattern", "cameras": [
        {**_match(angle=20), "count_mode": "trigger", "station": "join", "join_offset_ms": 250, "triggers": [1]}]})
    again = SettingsPersistenceService.save_line({"id": saved["id"], "name": "Pattern", "cameras": [
        {"camera_id": "m1", "role": "match"}]})
    cam = again["cameras"][0]
    assert cam["match"] == saved["cameras"][0]["match"] and cam["match"]["angle"] == 20
    assert (cam["count_mode"], cam["station"], cam["join_offset_ms"], cam["triggers"]) == ("trigger", "join", 250, [1])


def test_the_save_checks_the_reference_set(lines, data_dir):  # noqa: F811
    with pytest.raises(ValueError, match="Camera 1: train its reference on Pattern and checks"):
        SettingsPersistenceService.save_line({"name": "P1", "cameras": [GOOD]})
    _set_file(data_dir)
    two = _match(checks=[{"name": "Label"}, {"name": "Cap"}])
    with pytest.raises(ValueError, match="has 1 checks, not 2: train it again"):
        SettingsPersistenceService.save_line({"name": "P2", "cameras": [two]})
    with pytest.raises(ValueError, match="its search area is smaller than its reference"):
        SettingsPersistenceService.save_line({"name": "P3", "cameras": [_match(search_area=[0.4, 0.4, 0.5, 0.5])]})
    line = SettingsPersistenceService.save_line({"name": "P4", "cameras": [_match(search_area=[0.3, 0.3, 0.7, 0.7])]})
    assert line["cameras"][0]["match"]["reference_id"] == SET_ID


def test_a_capture_input_may_name_a_triggered_pattern_check(lines, data_dir):  # noqa: F811
    _set_file(data_dir)
    line = SettingsPersistenceService.save_line({"name": "L", "cameras": [{**GOOD, "count_mode": "trigger"}],
                                                 "plc_inputs": [_take(4, camera_id="m1")]})
    assert line["cameras"][0]["triggers"] == [4]
    with pytest.raises(ValueError, match="a pattern check"):
        SettingsPersistenceService.save_line({"name": "M", "cameras": [GOOD], "plc_inputs": [_take(1, camera_id="m1")]})


def test_the_save_cleans_up_unnamed_sets(lines, data_dir, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(match_references, "clean_up", lambda all_lines, now=None: seen.append(
        [c.get("camera_id") for line in all_lines for c in line.get("cameras") or []]) or 0)
    monkeypatch.setattr(SettingsPersistenceService, "_clean_up_later", staticmethod(lambda run: run()))
    _set_file(data_dir)
    SettingsPersistenceService.save_line({"name": "C", "cameras": [GOOD]})
    assert seen and "m1" in seen[-1]


def test_reason_codes():
    names = ("pattern_not_found", "pattern_mismatch", "pattern_defect", "pattern_error")
    assert [REJECT_REASON_CODES[r] for r in names] == [14, 15, 16, 17] and all(r in REJECT_REASONS for r in names)


def test_the_server_start_cleans_up(monkeypatch):
    import asyncio

    import main
    ran = []
    monkeypatch.setattr(SettingsPersistenceService, "clean_up_references", classmethod(lambda cls: ran.append(True)))
    asyncio.run(main._clean_up_references())
    assert ran == [True]
