"""Fixes to the counting logic: what a defect is, the count direction and tracking
settings per camera, the version 8 settings upgrade, and consistent counters."""
from __future__ import annotations

import copy

import pytest

from app.engines.tracker import WirelineTracker, is_defect_class
from app.services.line_config import SCHEMA_VERSION, normalize_camera, normalize_line, upgrade_state


def _det(class_name, cy, cx=320):
    class Box:
        pass

    class Det:
        pass

    box = Box()
    box.x1, box.y1, box.x2, box.y2 = cx - 20, cy - 30, cx + 20, cy + 30
    det = Det()
    det.class_name, det.confidence, det.class_id, det.bbox, det.polygon = class_name, 0.9, 0, box, None
    return det


DOWN = tuple(range(40, 450, 25))
UP = tuple(reversed(DOWN))


def _cross(class_name, defect_classes, name_based, direction="forward", path=DOWN):
    """One product moving down through count lines A (0.35) and B (0.65); returns the tracker's count events."""
    tracker = WirelineTracker(min_hits=1)
    events = []
    for cy in path:
        events += tracker.update(
            [_det(class_name, cy)], 640, 480, 0.35, 0.65, "horizontal", direction,
            [], list(defect_classes), name_based_defects=name_based,
        )
    return events


# ── What a defect is ──────────────────────────────────────────────────────────

def test_a_class_named_like_a_defect_rejects_only_with_the_name_rule_on():
    assert is_defect_class("Scratch", [], name_based=True)
    assert not is_defect_class("Scratch", [], name_based=False)
    assert is_defect_class("dent", ["Dent"], name_based=False)

    [event] = _cross("scratch_free_label", [], name_based=True)
    assert event[2] is True  # rejected by its name
    [event] = _cross("scratch_free_label", [], name_based=False)
    assert event[2] is False  # only the ticked defect classes reject
    [event] = _cross("dent", ["dent"], name_based=False)
    assert event[2] is True


def test_products_moving_backward_or_either_way_are_counted_when_set():
    assert _cross("bottle", [], False, direction="forward", path=UP) == []
    assert len(_cross("bottle", [], False, direction="backward", path=UP)) == 1
    assert len(_cross("bottle", [], False, direction="both", path=UP)) == 1
    assert len(_cross("bottle", [], False, direction="both")) == 1


# ── Counting settings per camera ──────────────────────────────────────────────

def test_a_camera_has_its_own_direction_and_tracking_else_the_lines():
    from app.services.settings_persistence_service import counting_config_from_dict

    line = {"line1_position": 0.2, "line2_position": 0.8, "orientation": "vertical", "tracking": {"min_hits": 4}}
    config = counting_config_from_dict(line, {"direction": "both", "tracking": {"max_speed_pixels": 300}})
    assert (config.line1_position, config.line2_position, config.orientation) == (0.2, 0.8, "vertical")
    assert config.direction == "both"
    assert (config.min_hits, config.max_speed_pixels) == (4, 300)
    # A camera saved before the switch keeps the old name rule; a camera can switch it off.
    assert config.name_based_defects is True
    assert counting_config_from_dict({}, {"name_based_defects": False}).name_based_defects is False
    camera_lines = counting_config_from_dict(line, {"line1_position": 0.4, "orientation": "horizontal"})
    assert (camera_lines.line1_position, camera_lines.line2_position, camera_lines.orientation) == (0.4, 0.8, "horizontal")


def test_camera_counting_settings_are_checked_when_saved():
    camera = normalize_camera({
        "camera_id": "c", "role": "vision", "model_id": "m", "direction": "backward",
        "tracking": {"min_hits": 3, "max_speed_pixels": "250", "track_low_thresh": ""},
        "name_based_defects": False,
    }, 0)
    assert camera["direction"] == "backward"
    assert camera["tracking"] == {"min_hits": 3, "max_speed_pixels": 250.0}
    assert camera["name_based_defects"] is False
    # Left out, the switch is not written: the camera keeps the old name rule.
    assert "name_based_defects" not in normalize_camera({"camera_id": "c", "role": "vision"}, 0)

    bad = [
        ({"direction": "sideways"}, "count direction"),
        ({"tracking": {"speed": 1}}, "is not a tracking setting"),
        ({"tracking": {"min_hits": 99}}, "between"),
        ({"tracking": {"min_hits": 2.5}}, "whole number"),
        ({"tracking": "fast"}, "must be an object"),
    ]
    for extra, message in bad:
        with pytest.raises(ValueError, match=message):
            normalize_camera({"camera_id": "c", "role": "vision", **extra}, 0)


def test_a_client_that_does_not_send_the_new_settings_leaves_them_as_saved():
    saved = normalize_line({"id": "line-x", "name": "X", "cameras": [{
        "camera_id": "c", "role": "vision", "model_id": "m", "direction": "both",
        "tracking": {"min_hits": 3}, "name_based_defects": False, "line1_position": 0.3,
    }]})
    # A dashboard written before these settings sends the camera without them.
    again = normalize_line({"cameras": [{"camera_id": "c", "role": "vision", "model_id": "m"}]}, saved)
    camera = again["cameras"][0]
    assert (camera["direction"], camera["tracking"], camera["name_based_defects"], camera["line1_position"]) == (
        "both", {"min_hits": 3}, False, 0.3)


def test_version_8_keeps_the_name_rule_on_every_existing_vision_camera():
    state = {"schema_version": 7, "camera_auto_connect": False, "lines": [
        {"id": "line-1", "name": "Line 1", "enabled": True, "cameras": [
            {"camera_id": "vis", "role": "vision", "counting": True, "model_id": "m"},
            {"camera_id": "qr", "role": "qr", "qr_trigger": "continuous", "product_list_id": "list-1"},
        ]},
        {"id": "line-2", "name": "Line 2", "enabled": True, "cameras": [
            {"camera_id": "off", "role": "vision", "counting": True, "model_id": "m", "name_based_defects": False},
        ]},
    ]}
    before = copy.deepcopy(state)
    assert upgrade_state(state) is True and state["schema_version"] == SCHEMA_VERSION == 8
    assert state["lines"][0]["cameras"][0]["name_based_defects"] is True
    assert "name_based_defects" not in state["lines"][0]["cameras"][1]
    assert state["lines"][1]["cameras"][0]["name_based_defects"] is False  # an explicit choice is kept
    state["lines"][0]["cameras"][0].pop("name_based_defects")
    assert state["lines"] == before["lines"]


# ── Counters ──────────────────────────────────────────────────────────────────

def test_resetting_one_class_keeps_good_plus_rejected_equal_to_the_total():
    from app.services.counting_service import CountingService

    counter = CountingService(dispatch_telemetry=False)
    for name, reject in (("bottle", False), ("bottle", True), ("can", False), ("can", True), ("can", True)):
        counter.count_product(name, reject)
    assert (counter.total_inspected, counter.good_count, counter.rejected_count) == (5, 2, 3)

    counter.reset_counts(reset_all=False, classes_to_reset=["CAN"])
    assert (counter.total_inspected, counter.good_count, counter.rejected_count) == (2, 1, 1)
    assert counter.counts_by_class == {"bottle": 2}
    assert counter.products_per_minute > 0

    counter.reset_counts()
    assert (counter.total_inspected, counter.good_count, counter.rejected_count) == (0, 0, 0)
    assert counter.rejects_by_class == {} and counter.products_per_minute == 0.0
