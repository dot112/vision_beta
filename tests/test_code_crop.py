"""The upright crop of a code, with a margin: the code reader's corner picture."""
from __future__ import annotations

import cv2
import numpy as np

from app.engines.code_crop import upright


def _noise(w, h, seed=1):
    return np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)


def _turned_rect(cx, cy, w, h, angle):
    """TL, TR, BR, BL of a w x h code as printed, turned ``angle`` degrees clockwise about (cx, cy)."""
    t = np.deg2rad(angle)
    c, s = np.cos(t), np.sin(t)
    return [[cx + x * c - y * s, cy + x * s + y * c] for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]


def test_upright_sizes():
    img = _noise(800, 600)
    crop, matrix = upright(img, _turned_rect(400, 300, 200, 100, 30), 16)
    h, w = crop.shape[:2]
    assert abs(w - 232) <= 3 and abs(h - 132) <= 3
    assert matrix.shape == (3, 3)
    thin = [[250, 300], [550, 300], [550, 304], [250, 304]]     # a 1D code's outline: a scan line
    h, w = upright(img, thin, 16)[0].shape[:2]
    assert abs(w - 332) <= 3 and abs(h - 182) <= 3
    h, w = upright(img, [[100, 200], [500, 200], [500, 300], [100, 300]], 16, max_long=240)[0].shape[:2]
    assert max(w, h) == 240


def test_upright_matrix_maps_the_polygon():
    img = _noise(800, 600)
    poly = _turned_rect(400, 300, 200, 100, 30)
    _, matrix = upright(img, poly, 16)
    moved = cv2.perspectiveTransform(np.asarray(poly, np.float64).reshape(-1, 1, 2), matrix).reshape(-1, 2)
    assert np.allclose(moved, [[16, 16], [216, 16], [216, 116], [16, 116]], atol=3)


def test_upright_at_the_picture_edge():
    crop, _ = upright(_noise(100, 100), [[60, 60], [150, 60], [150, 150], [60, 150]], 16)
    assert crop.size > 0


def test_upright_on_a_grey_or_tiny_picture():
    assert upright(np.zeros((80, 120), np.uint8), [[10, 10], [50, 10], [50, 40], [10, 40]], 16)[0].ndim == 2
    assert upright(_noise(1, 1), [[0, 0], [0, 0], [0, 0], [0, 0]], 4)[0].size > 0
