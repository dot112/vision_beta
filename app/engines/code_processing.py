"""Image processing for the code reader: the steps a copy of the picture goes through before codes are read.

The steps run in a fixed order: perspective, rotate and scale (one warp, so the
picture is resampled once; see prepare), then grayscale, denoise, contrast,
sharpen and binarize. The matrix of the warp maps a code's outline in the
processed picture back to the camera picture (map_points with its inverse).
upright cuts a code out of a picture, straight, with a margin around it.

Settings are checked once, when they are saved (normalize_processing); the
reader then trusts them.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

# Steps that work on a grey picture: when one is on, the picture is turned grey first.
GREY_STEPS = ("denoise", "contrast", "sharpen", "binarize")
STEPS = ("perspective", "rotate", "scale", "grayscale") + GREY_STEPS
BINARIZE_METHODS = ("auto", "adaptive", "fixed")
# The processed picture's long side is at most this many pixels: a larger scale factor is reduced.
MAX_SIDE = 4096
# Adaptive binarization: subtracted from each area's weighted mean.
ADAPTIVE_C = 5

DEFAULT_PROCESSING: Dict[str, Any] = {
    "enabled": False,
    "perspective": {"on": False, "corners": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]},
    "rotate": {"on": False, "angle": 0.0},
    "scale": {"on": False, "factor": 1.0},
    "grayscale": {"on": False},
    "denoise": {"on": False, "strength": 2},
    "contrast": {"on": False, "strength": 3},
    "sharpen": {"on": False, "strength": 3},
    "binarize": {"on": False, "method": "auto", "level": 128, "block": 31, "invert": False},
    "crop_margin_px": 16,
}

CORNERS_FORMAT = "the four perspective corners must be four [x, y] pairs from 0 to 1"
CORNERS_SHAPE = "the four perspective corners must make a convex area of at least 1% of the picture"


# ── Settings ──────────────────────────────────────────────────────────────────

def _flag(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _number(value: Any, default: float, low: float, high: float, message: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not low <= value <= high:
        raise ValueError(message)
    return value


def _corners(value: Any) -> List[List[float]]:
    if value is None:
        return copy.deepcopy(DEFAULT_PROCESSING["perspective"]["corners"])
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(CORNERS_FORMAT)
    pts: List[List[float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in point):
            raise ValueError(CORNERS_FORMAT)
        pts.append([float(point[0]), float(point[1])])
    # Top-left, top-right, bottom-right, bottom-left turn the same way (clockwise on the picture) at
    # every corner: a crossed or mirrored order is refused, as is a sliver.
    for i in range(4):
        a, b, c = pts[i], pts[(i + 1) % 4], pts[(i + 2) % 4]
        if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) <= 0:
            raise ValueError(CORNERS_SHAPE)
    area = abs(sum(pts[i][0] * pts[(i + 1) % 4][1] - pts[(i + 1) % 4][0] * pts[i][1] for i in range(4))) / 2
    if area < 0.01:
        raise ValueError(CORNERS_SHAPE)
    return pts


def normalize_processing(raw: Any) -> Dict[str, Any]:
    """The settings with every key filled in (defaults for the missing ones, unknown keys dropped).

    Raises ValueError with a message that names the setting, without the camera
    (the caller says which camera or screen it is)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("image processing must be an object")
    out = copy.deepcopy(DEFAULT_PROCESSING)
    out["enabled"] = _flag(raw.get("enabled"), "enabled", False)
    parts: Dict[str, Dict[str, Any]] = {}
    for step in STEPS:
        part = raw.get(step)
        if part is None:
            part = {}
        if not isinstance(part, dict):
            raise ValueError(f"{step} must be an object")
        out[step]["on"] = _flag(part.get("on"), f"{step} on", False)
        parts[step] = part
    out["perspective"]["corners"] = _corners(parts["perspective"].get("corners"))
    out["rotate"]["angle"] = float(_number(parts["rotate"].get("angle"), 0.0, -180, 180, "the angle must be -180 to 180"))
    out["scale"]["factor"] = float(_number(parts["scale"].get("factor"), 1.0, 0.25, 4, "the scale factor must be 0.25 to 4"))
    for step, high in (("denoise", 5), ("contrast", 10), ("sharpen", 10)):
        out[step]["strength"] = int(_number(parts[step].get("strength"), DEFAULT_PROCESSING[step]["strength"], 1, high,
                                            f"{step} strength must be 1 to {high}"))
    binarize = parts["binarize"]
    method = binarize.get("method", "auto")
    if method not in BINARIZE_METHODS:
        raise ValueError("the binarize method must be 'auto', 'adaptive' or 'fixed'")
    out["binarize"]["method"] = method
    out["binarize"]["level"] = int(_number(binarize.get("level"), 128, 0, 255, "the binarize level must be 0 to 255"))
    block = int(_number(binarize.get("block"), 31, 3, 255, "the adaptive block size must be 3 to 255"))
    out["binarize"]["block"] = block if block % 2 else block + 1
    out["binarize"]["invert"] = _flag(binarize.get("invert"), "invert", False)
    out["crop_margin_px"] = int(_number(raw.get("crop_margin_px"), 16, 0, 200, "the crop margin must be 0 to 200 px"))
    return out


def is_active(settings: Optional[Dict[str, Any]]) -> bool:
    """True when processing is switched on and at least one step is on."""
    return bool(settings and settings.get("enabled") and any((settings.get(step) or {}).get("on") for step in STEPS))


# ── Steps ─────────────────────────────────────────────────────────────────────

def _distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.hypot(*(np.asarray(b, np.float64) - np.asarray(a, np.float64))))


def _geometry(w: int, h: int, settings: Dict[str, Any]) -> Tuple[np.ndarray, float, float, float]:
    """(perspective then rotation matrix, the width and height it gives, the scale factor after the size cap)."""
    matrix = np.eye(3)
    cw, ch = float(w), float(h)
    if settings["perspective"]["on"]:
        src = np.float32([[x * w, y * h] for x, y in settings["perspective"]["corners"]])
        cw = max(1.0, (_distance(src[0], src[1]) + _distance(src[3], src[2])) / 2)
        ch = max(1.0, (_distance(src[0], src[3]) + _distance(src[1], src[2])) / 2)
        dst = np.float32([[0, 0], [cw, 0], [cw, ch], [0, ch]])
        matrix = cv2.getPerspectiveTransform(src, dst).astype(np.float64)
    angle = settings["rotate"]["angle"] if settings["rotate"]["on"] else 0.0
    if angle % 360:
        # Clockwise, as the Cameras page turns a picture; the canvas grows to hold all of it.
        turn = cv2.getRotationMatrix2D((cw / 2, ch / 2), -angle, 1.0)
        cos, sin = abs(turn[0, 0]), abs(turn[0, 1])
        nw, nh = ch * sin + cw * cos, ch * cos + cw * sin
        turn[0, 2] += nw / 2 - cw / 2
        turn[1, 2] += nh / 2 - ch / 2
        matrix = np.vstack([turn, [0.0, 0.0, 1.0]]) @ matrix
        cw, ch = nw, nh
    factor = settings["scale"]["factor"] if settings["scale"]["on"] else 1.0
    return matrix, cw, ch, min(factor, MAX_SIDE / max(cw, ch))


def _warp(img: np.ndarray, settings: Dict[str, Any]) -> Tuple[np.ndarray, np.ndarray]:
    matrix, cw, ch, factor = _geometry(img.shape[1], img.shape[0], settings)
    out_w, out_h = max(1, round(cw * factor)), max(1, round(ch * factor))
    turned = not np.allclose(matrix, np.eye(3))
    if factor >= 1 or not turned:
        full = np.diag([out_w / cw, out_h / ch, 1.0]) @ matrix
        if np.allclose(full, np.eye(3)) and (out_w, out_h) == (img.shape[1], img.shape[0]):
            return img, np.eye(3)
        if not turned:
            return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_CUBIC if factor > 1 else cv2.INTER_AREA), full
        # Cubic only where the picture grows; turning it at its own size is linear (cheaper, as sharp).
        flags = cv2.INTER_CUBIC if factor > 1 else cv2.INTER_LINEAR
        return cv2.warpPerspective(img, full, (out_w, out_h), flags=flags, borderMode=cv2.BORDER_REPLICATE), full
    # A warp cannot average pixels as it shrinks: it turns at full size, then shrinks by area.
    mid_w, mid_h = max(1, round(cw)), max(1, round(ch))
    mid = cv2.warpPerspective(img, matrix, (mid_w, mid_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return cv2.resize(mid, (out_w, out_h), interpolation=cv2.INTER_AREA), np.diag([out_w / mid_w, out_h / mid_h, 1.0]) @ matrix


def prepare(img: np.ndarray, settings: Dict[str, Any]) -> Tuple[np.ndarray, np.ndarray]:
    """The processed picture and the 3 x 3 matrix from the camera picture to it.

    ``settings`` are normalized (normalize_processing). The matrix is the identity
    when no geometric step changes the picture. A step the picture is too small
    for is skipped."""
    # Turned grey before the warp, not after: a third of the pixels to move, the same picture.
    if img.ndim == 3 and (settings["grayscale"]["on"] or any(settings[step]["on"] for step in GREY_STEPS)):
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    out, matrix = _warp(img, settings)
    small = min(out.shape[:2])
    if settings["denoise"]["on"]:
        kernel = 2 * settings["denoise"]["strength"] + 1
        if kernel <= small:
            out = cv2.medianBlur(out, kernel)
    if settings["contrast"]["on"]:
        out = cv2.createCLAHE(clipLimit=float(settings["contrast"]["strength"]), tileGridSize=(8, 8)).apply(out)
    if settings["sharpen"]["on"]:
        amount = 0.3 * settings["sharpen"]["strength"]
        out = cv2.addWeighted(out, 1 + amount, cv2.GaussianBlur(out, (0, 0), 2), -amount, 0)
    binarize = settings["binarize"]
    if binarize["on"]:
        if binarize["method"] == "fixed":
            _, out = cv2.threshold(out, binarize["level"], 255, cv2.THRESH_BINARY)
        elif binarize["method"] == "adaptive" and binarize["block"] <= small:
            out = cv2.adaptiveThreshold(out, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
                                        binarize["block"], ADAPTIVE_C)
        else:   # automatic, or a picture smaller than the adaptive block
            _, out = cv2.threshold(out, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        if binarize["invert"]:
            out = cv2.bitwise_not(out)
    return out, matrix


def map_points(points: Sequence[Sequence[float]], matrix: np.ndarray) -> List[List[int]]:
    """Points moved by a 3 x 3 matrix (prepare's, upright's, or an inverse of one)."""
    if not len(points):
        return []
    moved = cv2.perspectiveTransform(np.asarray(points, np.float64).reshape(-1, 1, 2), np.asarray(matrix, np.float64))
    return [[int(round(x)), int(round(y))] for x, y in moved.reshape(-1, 2)]


# ── Upright crop ──────────────────────────────────────────────────────────────

def upright(img: np.ndarray, polygon: Sequence[Sequence[float]], margin_px: int, *, min_short: int = 0,
            max_scale: float = 4.0, max_long: Optional[int] = None) -> Tuple[np.ndarray, np.ndarray]:
    """A code cut out of ``img`` from its four corners (TL, TR, BR, BL as printed), warped upright,
    with ``margin_px`` around it, and the 3 x 3 matrix from ``img`` to the crop.

    A polygon thinner than a third of its length (a 1D code's scan line) is first
    made half its length tall, centred, so the bars show. ``min_short`` enlarges
    the crop until its short side is that long (at most ``max_scale`` times);
    ``max_long`` shrinks it until its long side fits. Outside the picture the
    edge's pixels are repeated."""
    tl, tr, br, bl = np.asarray(polygon, np.float64).reshape(4, 2)
    w = (_distance(tl, tr) + _distance(bl, br)) / 2
    h = (_distance(tl, bl) + _distance(tr, br)) / 2
    if w < 2:
        # No length to go by: a small square around the points.
        centre, half = (tl + tr + br + bl) / 4, max(w, h, 8.0) / 2
        tl, tr, br, bl = (centre + np.array(d) * half for d in ((-1, -1), (1, -1), (1, 1), (-1, 1)))
        w = h = 2 * half
    elif h < w / 3:
        along = (tr - tl) + (br - bl)
        along /= max(np.hypot(*along), 1e-9)
        across = np.array([-along[1], along[0]])
        left, right = (tl + bl) / 2, (tr + br) / 2
        h = w / 2
        tl, tr, br, bl = left - across * h / 2, right - across * h / 2, right + across * h / 2, left + across * h / 2
    m = float(margin_px)
    full_w, full_h = w + 2 * m, h + 2 * m
    scale = 1.0
    if min_short and min(full_w, full_h) < min_short:
        scale = min(max_scale, min_short / min(full_w, full_h))
    if max_long and max(full_w, full_h) * scale > max_long:
        scale = max_long / max(full_w, full_h)
    dst = np.float32([[m, m], [m + w, m], [m + w, m + h], [m, m + h]]) * scale
    matrix = cv2.getPerspectiveTransform(np.float32([tl, tr, br, bl]), dst).astype(np.float64)
    size = (max(1, round(full_w * scale)), max(1, round(full_h * scale)))
    crop = cv2.warpPerspective(img, matrix, size, flags=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)
    return crop, matrix
