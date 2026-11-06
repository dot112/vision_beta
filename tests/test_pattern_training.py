"""Training a pattern check: the reference and check boxes drawn on the training picture, a mask, and
the good samples become a reference set (each box's average good picture and how much it varied, the
typical edge score) with suggested thresholds worked out by leaving each sample out in turn.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.engines.pattern_check import check_picture
from app.engines.pattern_find import to_grey
from app.engines.pattern_training import DraftData, TrainingRefused, find_sample, train
from tests.pattern_pictures import GOOD_SAMPLES, good_pictures, label, label_box, scene

LABEL_BOX = (-180, -120, 180, 120)
CAP_BOX = (84, 34, 156, 106)
CHECKS = [{"colour": False, "sensitivity": "normal"}] * 2
PICTURES = good_pictures()


def _draft(pictures=PICTURES, boxes=(LABEL_BOX, CAP_BOX)):
    return DraftData(pictures[0], label_box(boxes[0]), [label_box(b) for b in boxes[1:]],
                     np.zeros(pictures[0].shape[:2], bool), list(pictures))


def _settings(info, names=("Label", "Cap")):
    return {"reference_id": "ref-0000000a", "search_area": None, "angle": 15,
            "checks": [{"name": n, "colour": False, "sensitivity": "normal", **s} for n, s in zip(names, info["suggested"])]}


@pytest.fixture(scope="module")
def trained():
    return train(_draft(), 15, CHECKS, "ref-0000000a")


def test_eight_samples_give_a_set_and_suggestions(trained):
    ref, info = trained
    assert ref.id == "ref-0000000a" and ref.samples == 8 and len(ref.checks) == 2 and ref.picture_size == (1280, 960)
    assert ref.checks[0].mean.shape == (240, 360, 3) and ref.checks[1].mean.shape == (72, 72, 3)
    assert tuple(ref.checks[1].box) == CAP_BOX and ref.checks[0].typical > 0.5
    first = info["suggested"][0]
    assert 50 <= first["min_score"] <= 95 and first["max_defect_px"] >= 10 and info["note"] is None
    assert len(info["samples"]) == 8 and len(info["samples"][0]["scores"]) == 2
    settings = _settings(info)
    for a, cx, cy, g, o in ((9.46, 757.9, 456.1, 1.03, -29), (-10.61, 749.8, 518.0, 1.19, -2), (12.74, 575.4, 495.2, 1.1, -8)):
        result = check_picture(ref, settings, scene(label(), a, cx, cy, g, o, seed=21))
        assert result.result == "good", result.to_dict()
    assert check_picture(ref, settings, scene(label(), -9, 654, 342, 1.19, -21, seed=22, defect=True)).reason == "pattern_defect"
    assert check_picture(ref, settings, scene(label(text="APPLE"), 5, 600, 500, seed=23)).reason == "pattern_not_found"
    assert check_picture(ref, settings, scene(label(circle=False), 4, 640, 480, seed=24)).reason == "pattern_mismatch"


def test_suggestions_come_from_leave_one_out():
    pictures = list(PICTURES)
    a, cx, cy, g, o, s = GOOD_SAMPLES[4]
    pictures[4] = scene(label(text_at=(32, 93)), a, cx, cy, g, o, seed=s)     # its big word printed 3 px off
    ref, info = train(_draft(pictures), 15, CHECKS, "ref-0000000b")
    scores = [sample["scores"][0] for sample in info["samples"]]
    # Left out, it is judged by the others only: lower than against a set that holds it too.
    defects = [sample["defects"][0] for sample in info["samples"]]
    against_all = check_picture(ref, _settings(info), pictures[4]).checks[0]
    assert defects[4] > against_all.defect_px
    assert scores[4] == min(scores) and scores[4] < max(scores)
    assert info["suggested"][0]["min_score"] == max(50, min(95, scores[4] - 5))
    assert info["suggested"][0]["max_defect_px"] == max(10, int(np.ceil(1.5 * max(defects) + 5)))


def test_fewer_than_three_samples_give_the_defaults_and_a_note():
    ref, info = train(_draft(PICTURES[:1]), 15, CHECKS, "ref-0000000c")
    assert info["suggested"] == [{"min_score": 80, "max_defect_px": 30}] * 2 and "add" in info["note"].lower()
    assert ref.samples == 1 and 0.3 < ref.checks[0].typical <= 1.0
    result = check_picture(ref, _settings(info), scene(label(), 7, 700, 450, seed=25))
    assert result.checks[0].score >= 85


def _plain():
    draft = _draft()
    draft.reference = [30, 30, 230, 160]          # the blurred background: no print, no shape
    draft.checks = []
    return draft


def _small_box():
    draft = _draft()
    draft.checks = [[700, 500, 710, 560]]
    return draft


@pytest.mark.parametrize("make, message", [
    (_small_box, "Check 2's box must be at least 16 px wide and high"),
    (_plain, "Draw the reference around printing or a shape")])
def test_refusals(make, message):
    with pytest.raises(TrainingRefused, match=message):
        train(make(), 15, CHECKS, "ref-0000000d")


def test_a_sample_without_the_reference_is_named():
    pictures = list(PICTURES)
    pictures[2] = scene(label(text="APPLE"), 2, 640, 480, seed=26)
    with pytest.raises(TrainingRefused, match="sample 3"):
        train(_draft(pictures), 15, CHECKS, "ref-0000000e")


def test_find_sample():
    draft = _draft(PICTURES[:1])
    found = find_sample(draft, scene(label(), -11, 700, 430, 0.8, 15, seed=27), 15)
    assert found is not None and abs(found.x - 700) <= 2 and abs(found.angle + 11) <= 0.5 and 0.8 <= found.score <= 1.0
    assert find_sample(draft, scene(label(text="APPLE"), 3, 640, 480, seed=28), 15) is None
    assert find_sample(draft, scene(np.full((240, 360), 225, np.uint8), 0, 640, 480, seed=29), 15) is None


def test_training_from_grey_pictures():
    greys = [to_grey(p) for p in PICTURES]
    ref, info = train(_draft(greys), 15, CHECKS, "ref-0000000f")
    result = check_picture(ref, _settings(info), to_grey(scene(label(), 5, 620, 470, seed=30)))
    assert result.result == "good", result.to_dict()
    assert ref.checks[0].mean.shape == (240, 360, 3)



def test_a_check_without_edges_is_trained_as_an_area_check():
    ref, info = train(_draft(boxes=(LABEL_BOX, (125, -20, 165, 30))), 15, CHECKS, "ref-00000010")
    assert [c.area for c in ref.checks] == [False, True] and info["area"] == [False, True]
    assert all(sample["scores"][1] == 100 for sample in info["samples"]) and info["suggested"][1]["min_score"] == 95
