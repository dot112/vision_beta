"""The code reader's image processing steps: the settings check, the steps, point mapping and the upright crop."""
import copy

import cv2
import numpy as np
import pytest

from app.engines.code_processing import (
    DEFAULT_PROCESSING,
    is_active,
    map_points,
    normalize_processing,
    prepare,
    upright,
)


def _on(**steps):
    """Normalized settings with processing on and the given steps."""
    raw = {"enabled": True}
    for name, values in steps.items():
        raw[name] = {"on": True, **values}
    return normalize_processing(raw)


def _noise(w, h, seed=1):
    return np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)


# ── Settings ──────────────────────────────────────────────────────────────────

def test_defaults_and_unknown_keys():
    assert normalize_processing({}) == DEFAULT_PROCESSING
    assert normalize_processing(None) == DEFAULT_PROCESSING
    out = normalize_processing({"scale": {"on": True}, "x": 1})
    assert out["scale"] == {"on": True, "factor": 1.0}
    assert "x" not in out
    assert DEFAULT_PROCESSING["perspective"]["corners"] == [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
    assert DEFAULT_PROCESSING["crop_margin_px"] == 16


@pytest.mark.parametrize("raw", [
    {"scale": {"factor": 5}},
    {"rotate": {"angle": 181}},
    {"denoise": {"strength": 6}},
    {"contrast": {"strength": 0}},
    {"sharpen": {"strength": 11}},
    {"binarize": {"method": "otsu"}},
    {"binarize": {"level": 256}},
    {"binarize": {"block": 2}},
    {"crop_margin_px": 201},
    {"enabled": "yes"},
    {"scale": {"factor": "2"}},
    {"grayscale": {"on": 1}},
    {"rotate": "on"},
    "everything",
])
def test_ranges_are_checked(raw):
    with pytest.raises(ValueError):
        normalize_processing(raw)


def test_scale_message():
    with pytest.raises(ValueError, match=r"^the scale factor must be 0\.25 to 4$"):
        normalize_processing({"scale": {"factor": 5}})


@pytest.mark.parametrize("corners", [
    [[0, 0], [1, 0], [1, 1]],
    [[0, 0], [1.2, 0], [1, 1], [0, 1]],
    [[0, 0], [1, 1], [1, 0], [0, 1]],
    [[0.1, 0.1], [0.15, 0.1], [0.15, 0.15], [0.1, 0.15]],
    [[0, 1], [1, 1], [1, 0], [0, 0]],
    "corners",
])
def test_bad_corners(corners):
    with pytest.raises(ValueError, match="perspective corners"):
        normalize_processing({"perspective": {"corners": corners}})


def test_even_block_is_made_odd():
    assert normalize_processing({"binarize": {"block": 30}})["binarize"]["block"] == 31
    assert normalize_processing({"binarize": {"block": 31}})["binarize"]["block"] == 31


def test_is_active():
    assert not is_active(None)
    assert not is_active(normalize_processing({}))
    assert not is_active(normalize_processing({"enabled": True}))
    assert not is_active(normalize_processing({"grayscale": {"on": True}}))
    assert is_active(_on(grayscale={}))


# ── Steps ─────────────────────────────────────────────────────────────────────

def test_pixel_steps():
    img = _noise(160, 120)
    grey, _ = prepare(img, _on(grayscale={}))
    assert grey.ndim == 2 and grey.shape == (120, 160)
    black_white, _ = prepare(img, _on(binarize={"method": "auto"}))
    assert set(np.unique(black_white)) <= {0, 255}
    inverted, _ = prepare(img, _on(binarize={"method": "auto", "invert": True}))
    assert int((inverted == 255).sum()) == int((black_white == 0).sum())
    for method, extra in (("adaptive", {"block": 15}), ("fixed", {"level": 200})):
        out, _ = prepare(img, _on(binarize={"method": method, **extra}))
        assert set(np.unique(out)) <= {0, 255}
    for step in ("denoise", "contrast", "sharpen"):
        out, _ = prepare(img, _on(**{step: {}}))
        assert out.ndim == 2, step               # steps 5-8 work on a grey picture


def test_geometric_sizes():
    img = _noise(640, 480)
    assert prepare(img, _on(scale={"factor": 2}))[0].shape[:2] == (960, 1280)
    h, w = prepare(img, _on(rotate={"angle": 90}))[0].shape[:2]
    assert abs(w - 480) <= 1 and abs(h - 640) <= 1
    big = np.zeros((1080, 1920, 3), np.uint8)
    h, w = prepare(big, _on(scale={"factor": 4}))[0].shape[:2]
    assert max(w, h) == 4096 and abs(h - 2304) <= 1
    corners = [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]
    h, w = prepare(img, _on(perspective={"corners": corners}))[0].shape[:2]
    assert abs(w - 320) <= 1 and abs(h - 240) <= 1


def test_no_geometric_step_gives_the_identity():
    _, matrix = prepare(_noise(64, 48), _on(grayscale={}))
    assert np.allclose(matrix, np.eye(3))


def test_points_map_back():
    """A white dot in the camera picture is where map_points says in the processed one, and back."""
    img = np.zeros((480, 640, 3), np.uint8)
    cv2.circle(img, (300, 260), 6, (255, 255, 255), -1)
    corners = [[0.15, 0.1], [0.85, 0.15], [0.9, 0.9], [0.1, 0.85]]
    out, matrix = prepare(img, _on(perspective={"corners": corners}, rotate={"angle": 30}, scale={"factor": 1.5}))
    ys, xs = np.nonzero(out[..., 0] > 127 if out.ndim == 3 else out > 127)
    (px, py), = map_points([[300, 260]], matrix)
    assert abs(xs.mean() - px) <= 2 and abs(ys.mean() - py) <= 2
    back = map_points(map_points([[300, 260], [10, 20]], matrix), np.linalg.inv(matrix))
    assert np.allclose(back, [[300, 260], [10, 20]], atol=2)


# ── Upright crop ──────────────────────────────────────────────────────────────

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
    h, w = upright(img, [[100, 100], [160, 100], [160, 140], [100, 140]], 0, min_short=120)[0].shape[:2]
    assert min(w, h) >= 119 and max(w, h) <= 60 * 4 + 1


def test_upright_matrix_maps_the_polygon():
    img = _noise(800, 600)
    poly = _turned_rect(400, 300, 200, 100, 30)
    _, matrix = upright(img, poly, 16)
    assert np.allclose(map_points(poly, matrix), [[16, 16], [216, 16], [216, 116], [16, 116]], atol=3)


# ── Review Focus ──────────────────────────────────────────────────────────────

def test_upright_at_the_picture_edge():
    img = _noise(100, 100)
    crop, _ = upright(img, [[60, 60], [150, 60], [150, 150], [60, 150]], 16)
    assert crop.size > 0


def test_tiny_and_grey_pictures():
    everything = _on(perspective={}, rotate={"angle": 12}, scale={"factor": 0.25}, grayscale={}, denoise={"strength": 5},
                     contrast={}, sharpen={}, binarize={"method": "adaptive", "block": 255, "invert": True})
    for img in (_noise(4, 4), np.zeros((80, 120), np.uint8), _noise(1, 1)):
        out, matrix = prepare(img, copy.deepcopy(everything))
        assert isinstance(out, np.ndarray) and out.size > 0 and matrix.shape == (3, 3)


def test_corners_follow_the_picture_size():
    corners = [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]
    for (w, h), (ew, eh) in (((640, 480), (320, 240)), ((1280, 720), (640, 360))):
        oh, ow = prepare(_noise(w, h), _on(perspective={"corners": corners}))[0].shape[:2]
        assert abs(ow - ew) <= 1 and abs(oh - eh) <= 1
