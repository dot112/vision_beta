"""Checking one picture with a reference set: the main reference is found by its edges and lined up,
each check gets its Match % (against the good samples' typical score) and its biggest defect (pixels
further from the average good picture than the samples varied), and the picture is good or a reject
with the first problem: not found, a mismatch, a defect, or an error.
"""
from __future__ import annotations

import statistics

import cv2
import numpy as np
import pytest

import app.engines.pattern_check as pattern_check
from app.engines.pattern_check import (CheckModel, ReferenceSet, check_picture, level_light, line_up,
                                       match_percent, match_values)
from app.engines.pattern_find import Found, find, refine, to_grey
from tests.pattern_pictures import label, scene

# Good samples: angle, centre, gain, offset, noise seed. Whole-number centres put the label exactly there.
SAMPLES = [(0.0, 640.0, 480.0, 1.0, 0, 1), (6.0, 600.0, 430.0, 0.9, 10, 2), (-8.0, 700.0, 520.0, 1.1, -10, 3),
           (12.0, 560.0, 500.0, 0.8, 20, 4), (-12.0, 720.0, 440.0, 1.15, -20, 5), (3.0, 650.0, 470.0, 1.05, 5, 6),
           (-4.0, 610.0, 510.0, 0.95, -5, 7), (9.0, 680.0, 450.0, 1.0, 0, 8)]
LABEL_BOX = (-180, -120, 180, 120)
CAP_BOX = (84, 34, 156, 106)            # the circle (the "cap"), from the label's centre
EDGE_BOX = (150, -20, 230, 20)          # over the label's right edge, out onto the background
BLUE, GREEN = (180, 90, 60), (60, 106, 60)   # the same lightness (Lab L 103), other colours


def _check(name, **extra):
    return {"name": name, "min_score": 80, "max_defect_px": 40, "colour": False, "sensitivity": "normal", **extra}


MATCH = {"reference_id": "ref-0000test", "search_area": None, "angle": 15, "checks": [_check("Label"), _check("Cap")]}


def _centre(box, angle, cx, cy):
    """Where a box's centre (from the label's centre) lands for a label at (cx, cy) turned ``angle``."""
    dx, dy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    a = np.radians(angle)
    return cx + np.cos(a) * dx + np.sin(a) * dy, cy - np.sin(a) * dx + np.cos(a) * dy


def _set(make=label, boxes=(LABEL_BOX, CAP_BOX), ignores=None, colour=None):
    """A reference set made by hand from the good samples (Task 3's train makes it from a draft)."""
    pictures = [scene(make(), a, cx, cy, g, o, seed=s, colour=colour) for a, cx, cy, g, o, s in SAMPLES]
    checks = []
    for index, box in enumerate(boxes):
        half = ((box[0] - box[2]) / 2, (box[1] - box[3]) / 2, (box[2] - box[0]) / 2, (box[3] - box[1]) / 2)
        cuts = []
        for (a, cx, cy, *_), picture in zip(SAMPLES, pictures):
            x, y = _centre(box, a, cx, cy)
            cuts.append(line_up(picture, Found(x, y, a, 1.0), half)[0])
        ignore = np.zeros(cuts[0].shape[:2], bool) if ignores is None or ignores[index] is None else ignores[index]
        use = ~ignore
        first = cv2.cvtColor(cuts[0], cv2.COLOR_BGR2LAB).astype(np.float32)
        labs = np.stack([level_light(cv2.cvtColor(c, cv2.COLOR_BGR2LAB).astype(np.float32), first, use) for c in cuts])
        mean = cv2.cvtColor(labs.mean(0).clip(0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
        checks.append(CheckModel(box, mean, labs.std(0), ignore, 1.0))
    ref = ReferenceSet("ref-0000test", (1280, 960), checks, len(SAMPLES))
    for index, check in enumerate(checks):
        model = ref.edge_model(index, 15)
        if index == 0:
            scores = [find(model, to_grey(p)).score for p in pictures]
        else:
            scores = [refine(model, to_grey(p), Found(*_centre(check.box, a, cx, cy), a, 0.0)).score
                      for (a, cx, cy, *_), p in zip(SAMPLES, pictures)]
        check.typical = statistics.median(scores)
    return ref


@pytest.fixture(scope="module")
def ref():
    return _set()


def test_a_good_label_passes_and_a_defect_is_found(ref):
    good = check_picture(ref, MATCH, scene(label(), 7.8, 870, 475, 1.16, -30, seed=11))
    assert good.result == "good" and good.reason is None and good.status == "good"
    assert good.checks[0].score >= 95 and good.checks[0].defect_px == 0 and good.checks[1].score >= 90
    bad = check_picture(ref, MATCH, scene(label(), -9.0, 654, 342, 1.19, -21, seed=12, defect=True))
    assert bad.reason == "pattern_defect" and bad.status == "defect"
    assert bad.checks[0].defect_px >= 100 and bad.checks[0].spots and not bad.checks[0].fits


def test_a_wrong_label_is_not_found_and_stops_there(ref):
    r = check_picture(ref, MATCH, scene(label(text="APPLE"), 5, 600, 500))
    assert r.reason == "pattern_not_found" and len(r.checks) == 1 and r.status == "not_found" and r.result == "reject"


def test_a_missing_cap_is_a_mismatch(ref):
    # The label's own check sees the missing circle as a defect too: a check that does not match comes first.
    r = check_picture(ref, MATCH, scene(label(circle=False), 4, 640, 480, seed=13))
    assert r.reason == "pattern_mismatch" and r.checks[1].score < 80 and r.checks[0].score >= 80


def test_sensitivity_and_the_floor(ref):
    lab = label()
    lab[110:140, 315:345] -= 20             # a 30 x 30 patch of paper 20 grey levels darker
    picture = scene(lab, 2, 640, 480, seed=14)
    for sensitivity, found in (("high", True), ("normal", False), ("low", False)):
        match = {**MATCH, "checks": [_check("Label", sensitivity=sensitivity), _check("Cap")]}
        assert (check_picture(ref, match, picture).checks[0].defect_px > 0) is found, sensitivity


def test_colour_rejects_a_wrong_coloured_cap_that_grey_passes():
    ref = _set(colour=BLUE)
    picture = scene(label(), 3, 640, 480, seed=15, colour=GREEN)
    grey = check_picture(ref, MATCH, picture)
    assert grey.result == "good", grey.to_dict()
    colour = check_picture(ref, {**MATCH, "checks": [_check("Label"), _check("Cap", colour=True)]}, picture)
    assert colour.reason == "pattern_defect" and colour.checks[1].defect_px > 40 and colour.checks[0].fits


def test_a_grey_picture_is_checked_as_grey(ref):
    picture = scene(label(), 5, 620, 470, seed=16)
    flat = to_grey(picture)
    as_grey = check_picture(ref, MATCH, flat)
    colour_settings = {**MATCH, "checks": [_check("Label", colour=True), _check("Cap", colour=True)]}
    assert as_grey.result == "good" and check_picture(ref, colour_settings, flat).to_dict()["checks"] == as_grey.to_dict()["checks"]
    assert check_picture(ref, MATCH, flat[..., None]).result == "good"


def test_a_picture_of_another_size_is_an_error(ref):
    r = check_picture(ref, MATCH, cv2.resize(scene(label(), 0, 640, 480), (640, 480)))
    assert r.reason == "pattern_error" and r.status == "error" and r.checks == []
    assert "640 × 480" in r.error and "trained on 1280 × 960" in r.error and "train it again" in r.error


def test_a_check_box_partly_outside_the_picture():
    ref = _set(boxes=(LABEL_BOX, EDGE_BOX))
    match = {**MATCH, "checks": [_check("Label"), _check("Edge")]}
    # The label's right edge at x 1270: the Edge box (x 1230..1310) is 62 % inside, so it is checked.
    partly = check_picture(ref, match, scene(label(), 0, 1080, 480, seed=17))
    assert partly.checks[1].score > 0 and partly.checks[0].fits
    assert partly.checks[1].defect_px == 0          # its part outside the picture counts as masked
    # At x 1095 the box is 44 % inside: Match 0 %, a mismatch.
    mostly = check_picture(ref, match, scene(label(), 0, 1095, 480, seed=18))
    assert mostly.checks[1].score == 0 and mostly.reason == "pattern_mismatch"


def test_masked_pixels_are_not_defects():
    ignore = np.zeros((240, 360), bool)
    ignore[193:225, 25:200] = True           # the lot number
    masked = _set(make=label, boxes=(LABEL_BOX,), ignores=[ignore])
    plain = _set(make=label, boxes=(LABEL_BOX,))
    match = {**MATCH, "checks": [_check("Label")]}
    picture = scene(label(lot="LOT 98760"), 2, 640, 480, seed=19)
    assert check_picture(masked, match, picture).result == "good"
    assert check_picture(plain, match, picture).reason == "pattern_defect"


def test_the_edge_model_is_cached_per_check_and_angle(ref, monkeypatch):
    built = []
    real = pattern_check.build_edge_model
    monkeypatch.setattr(pattern_check, "build_edge_model", lambda *a: built.append(a[2]) or real(*a))
    fresh = ReferenceSet(ref.id, ref.picture_size, ref.checks, ref.samples)
    assert fresh.edge_model(0, 15) is fresh.edge_model(0, 15)
    fresh.edge_model(0, 5)
    assert built == [15, 5]


def test_match_values_and_percent(ref):
    assert match_percent(0.74, 0.74) == 100 and match_percent(0.45, 0.75) == 60 and match_percent(0.9, 0.75) == 100
    assert match_percent(0.5, 0.0) == 0
    good = check_picture(ref, MATCH, scene(label(), 1, 640, 480, seed=20))
    values = match_values(good)
    assert values["status"] == "good" and values["score"] == good.checks[0].score
    assert set(values["checks"]) == {"Label", "Cap"} and values["checks"]["Cap"] == {
        "score": good.checks[1].score, "defect_px": good.checks[1].defect_px}
    error = check_picture(ref, MATCH, np.zeros((10, 10, 3), np.uint8))
    assert match_values(error) == {"status": "error"}
    assert set(good.to_dict()) == {"width", "height", "ms", "found", "checks", "result", "reason", "error"}
