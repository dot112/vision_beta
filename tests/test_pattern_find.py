"""The pattern check's edge search: a reference is found by the direction of its edges, turned within
+-angle and shifted anywhere in the search area, through light changes; a wrong, blank or missing
product scores low; masked edges are left out.
"""
from __future__ import annotations

import statistics
import time

import numpy as np
import pytest

from app.engines.pattern_find import Found, build_edge_model, find, refine, to_grey
from tests.pattern_pictures import WORD_BOX, crop, label, label_point, scene

REF = crop(scene(label(), 0, 640, 480), 640, 480, 360, 240)


@pytest.fixture(scope="module")
def model():
    return build_edge_model(to_grey(REF), None, 15)


@pytest.mark.parametrize("angle, cx, cy, gain, offset", [
    (9.46, 757.9, 456.1, 1.03, -29), (-10.61, 749.8, 518.0, 1.19, -2), (12.74, 575.4, 495.2, 1.10, -8),
    (-14.0, 420.0, 330.0, 0.6, 30), (0.0, 640.0, 480.0, 1.0, 0)])
def test_finds_a_turned_shifted_relit_label(model, angle, cx, cy, gain, offset):
    found = find(model, to_grey(scene(label(), angle, cx, cy, gain, offset, seed=3)))
    assert abs(found.x - cx) <= 2 and abs(found.y - cy) <= 2 and abs(found.angle - angle) <= 0.5
    assert found.score >= 0.6


def test_wrong_blank_and_no_product_score_low(model):
    good = find(model, to_grey(scene(label(), 5, 600, 500))).score
    # Another word on the same label: the frame, the small words and the circle still agree.
    assert find(model, to_grey(scene(label(text="APPLE"), 5, 600, 500))).score < 0.8 * good
    assert find(model, to_grey(scene(np.full((240, 360), 225, np.uint8), 0, 600, 500))).score < 0.1


def test_a_search_area_limits_the_search_and_a_small_one_gives_none(model):
    grey = to_grey(scene(label(), 4, 900, 480))
    outside = find(model, grey, (0, 0, 640, 960))
    assert outside.score < 0.35 and outside.x < 640
    inside = find(model, grey, (600, 200, 1280, 760))
    assert abs(inside.x - 900) <= 2 and abs(inside.y - 480) <= 2 and inside.score >= 0.6
    assert find(model, grey, (0, 0, 100, 100)) is None


def test_masked_edges_are_left_out():
    ignore = np.zeros(REF.shape[:2], bool)
    ignore[35:100, 20:340] = True          # the big word
    masked = build_edge_model(to_grey(REF), ignore, 15)
    assert masked.points < build_edge_model(to_grey(REF), None, 15).points
    assert find(masked, to_grey(scene(label(text="APPLE"), 5, 600, 500))).score >= 0.6


def test_refine_finds_a_check_close_by(model):
    x1, y1, x2, y2 = WORD_BOX
    word = build_edge_model(to_grey(REF)[y1:y2, x1:x2], None, 15)
    angle, cx, cy = 8.0, 700.0, 500.0
    tx, ty = label_point((x1 + x2) / 2, (y1 + y2) / 2, angle, cx, cy)
    found = refine(word, to_grey(scene(label(), angle, cx, cy, seed=5)), Found(tx + 5, ty - 4, angle + 0.8, 0.0))
    assert abs(found.x - tx) <= 1 and abs(found.y - ty) <= 1 and abs(found.angle - angle) <= 0.3


def test_angle_zero_searches_without_turning():
    still = build_edge_model(to_grey(REF), None, 0)
    assert find(still, to_grey(scene(label(), 10, 640, 480))).score < 0.6
    assert find(still, to_grey(scene(label(), 0, 600, 420))).score >= 0.6


def test_a_grey_one_channel_picture_is_the_same_as_its_colour_one():
    picture = scene(label(), 3, 640, 480)
    assert np.array_equal(to_grey(picture), to_grey(to_grey(picture))) and to_grey(picture[..., :1]).ndim == 2


def test_speed_is_measured(model, capsys):
    grey = to_grey(scene(label(), 7.3, 700, 450, 0.9, 10, seed=8))
    times = []
    for _ in range(5):
        started = time.perf_counter()
        find(model, grey)
        times.append((time.perf_counter() - started) * 1000)
    median = statistics.median(times)
    with capsys.disabled():
        print(f"\npattern_find: 1280 x 960, +-15 deg, whole picture: median {median:.1f} ms (min {min(times):.1f})")
    assert median < 300
