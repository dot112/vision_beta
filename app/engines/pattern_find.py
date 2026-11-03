"""The pattern check's edge search: where a reference is in a picture, and how far it is turned.

A reference is known by its edges: at each point where its brightness changes sharply, the direction of
that change. A place and angle in the picture scores the mean, over the reference's edge points, of how
well the picture's edge direction agrees there (the cosine of the angle between the two; 0 where the
picture has no edge), so light and contrast changes hardly move it, and light-on-dark is not
dark-on-light. All places are scored at once with two correlations of the unit gradient pictures.

The search runs coarse to fine: on a pyramid of halved pictures, every angle within +-angle at the top,
then at each finer level only the angles next to the best one, in a small window around it. The
templates of the finer levels are made when first needed and a few are kept.

No runtime imports: OpenCV and numpy only.
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from typing import List, NamedTuple, Optional, Tuple

import cv2
import numpy as np

# A reference point is an edge point where its gradient (Sobel 3 x 3 on grey 0-255) is at least this;
# a picture point counts where its gradient is at least EDGE_PIC_MIN.
EDGE_REF_MIN = 20
EDGE_PIC_MIN = 10
# The pyramid goes down until the reference's shorter side is about TOP_SIDE px, at most MAX_LEVELS halvings.
TOP_SIDE = 24
MAX_LEVELS = 4
# Angle steps move the reference's far corner this far; each finer level searches +-WINDOW_PX.
STEP_PX = 1.5
WINDOW_PX = 4
# Templates of the two finest levels kept per model (they are large; the others are all made at once).
_KEEP = {0: 16, 1: 32}
_RIM = np.ones((5, 5), np.uint8)


class Found(NamedTuple):
    """The reference's centre in the searched picture's pixels, its angle (degrees, counter-clockwise as
    cv2.getRotationMatrix2D) and its raw score (0-1)."""
    x: float
    y: float
    angle: float
    score: float


class _Template(NamedTuple):
    angle: float
    gx: np.ndarray
    gy: np.ndarray
    n: int


def to_grey(img: np.ndarray) -> np.ndarray:
    """A BGR, BGRA or one-channel picture as grey uint8."""
    if img.ndim == 3:
        if img.shape[2] == 4:
            return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        if img.shape[2] == 3:
            return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img = img[:, :, 0]
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(img)


def unit_gradients(grey: np.ndarray, floor: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The gradient's direction as unit vectors (gx, gy), 0 where it is under ``floor``, and that mask."""
    gx = cv2.Sobel(grey, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(grey, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    strong = mag >= floor
    inv = np.zeros_like(mag)
    np.divide(1.0, mag, out=inv, where=strong)
    return gx * inv, gy * inv, strong


def turned(img: np.ndarray, angle: float, interpolation: int = cv2.INTER_LINEAR) -> np.ndarray:
    """``img`` turned ``angle`` degrees about its centre, in a box that holds all of it (border 0); the
    centre lands on the box's centre."""
    h, w = img.shape[:2]
    turn = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(turn[0, 0]), abs(turn[0, 1])
    nw, nh = int(math.ceil(h * sin + w * cos - 1e-9)), int(math.ceil(h * cos + w * sin - 1e-9))
    turn[0, 2] += nw / 2 - w / 2
    turn[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(img, turn, (nw, nh), flags=interpolation, borderValue=0)


def _make_template(ref: np.ndarray, ignore: Optional[np.ndarray], angle: float) -> _Template:
    image = turned(ref, angle)
    # The turned box's corners are outside the reference: their rim is not an edge of it.
    inside = cv2.erode(turned(np.full(ref.shape[:2], 255, np.uint8), angle, cv2.INTER_NEAREST), _RIM) > 0
    gx, gy, keep = unit_gradients(image, EDGE_REF_MIN)
    keep &= inside
    if ignore is not None and ignore.any():
        masked = turned(ignore.astype(np.uint8) * 255, angle, cv2.INTER_NEAREST)
        keep &= cv2.dilate(masked, np.ones((3, 3), np.uint8)) == 0
    gx[~keep] = 0
    gy[~keep] = 0
    return _Template(float(angle), gx, gy, int(keep.sum()))


class EdgeModel:
    """A reference's edges per pyramid level and angle, for find() and refine(). Safe to share between
    threads."""

    def __init__(self, ref: np.ndarray, ignore: Optional[np.ndarray], angle: int) -> None:
        self.angle = int(angle)
        self.size = (int(ref.shape[1]), int(ref.shape[0]))
        levels = 0
        while levels < MAX_LEVELS and min(self.size) / 2 ** (levels + 1) >= TOP_SIDE:
            levels += 1
        self.levels = levels
        self._refs: List[np.ndarray] = [ref]
        self._ignores: List[Optional[np.ndarray]] = [ignore if ignore is not None and ignore.any() else None]
        for _ in range(levels):
            smaller = cv2.pyrDown(self._refs[-1])
            last = self._ignores[-1]
            self._refs.append(smaller)
            self._ignores.append(None if last is None else cv2.resize(
                last.astype(np.float32), (smaller.shape[1], smaller.shape[0]), interpolation=cv2.INTER_AREA) > 0)
        self._angles: List[np.ndarray] = []
        for level_ref in self._refs:
            radius = math.hypot(*level_ref.shape[:2]) / 2
            step = math.degrees(math.atan(STEP_PX / max(radius, 1.0)))
            count = int(math.ceil(self.angle / step)) if self.angle else 0
            self._angles.append(np.linspace(-self.angle, self.angle, 2 * count + 1) if count else np.zeros(1))
        self._steps = [float(a[1] - a[0]) if len(a) > 1 else 0.0 for a in self._angles]
        self._lock = threading.Lock()
        self._kept = {level: OrderedDict() for level in _KEEP}
        self._made = {level: {float(a): _make_template(self._refs[level], self._ignores[level], a)
                              for a in self._angles[level]}
                      for level in range(levels + 1) if level not in _KEEP or level == levels}
        self.points = self._template(0, 0.0).n
        self.top_points = self._template(levels, 0.0).n

    def angles(self, level: int) -> np.ndarray:
        return self._angles[level]

    def step(self, level: int) -> float:
        return self._steps[level]

    def _template(self, level: int, angle: float) -> _Template:
        made = self._made.get(level)
        if made is not None and float(angle) in made:
            return made[float(angle)]
        kept = self._kept.get(level)
        key = round(float(angle), 6)
        with self._lock:
            if kept is not None and key in kept:
                kept.move_to_end(key)
                return kept[key]
        template = _make_template(self._refs[level], self._ignores[level], angle)
        if kept is not None:
            with self._lock:
                kept[key] = template
                while len(kept) > _KEEP[level]:
                    kept.popitem(last=False)
        return template


def build_edge_model(ref: np.ndarray, ignore: Optional[np.ndarray], angle: int) -> EdgeModel:
    """The edge model of a grey reference (``ignore``: True where masked), searched within +-``angle``."""
    return EdgeModel(to_grey(ref), ignore, angle)


def _scores(px: np.ndarray, py: np.ndarray, template: _Template) -> np.ndarray:
    return (cv2.matchTemplate(px, template.gx, cv2.TM_CCORR)
            + cv2.matchTemplate(py, template.gy, cv2.TM_CCORR)) / max(template.n, 1)


def _best_near(model: EdgeModel, level: int, img: np.ndarray, cx: float, cy: float,
               angles: List[float], window: int) -> Optional[Tuple[float, float, float, float]]:
    """(score, angle, x, y) of the best of these angles with the centre within +-window of (cx, cy)."""
    templates = [model._template(level, a) for a in angles]
    half_w = max(t.gx.shape[1] for t in templates) / 2 + window + 2
    half_h = max(t.gx.shape[0] for t in templates) / 2 + window + 2
    rx0, ry0 = max(int(math.floor(cx - half_w)), 0), max(int(math.floor(cy - half_h)), 0)
    rx1, ry1 = min(int(math.ceil(cx + half_w)), img.shape[1]), min(int(math.ceil(cy + half_h)), img.shape[0])
    if rx1 - rx0 < 3 or ry1 - ry0 < 3:
        return None
    px, py, _ = unit_gradients(img[ry0:ry1, rx0:rx1], EDGE_PIC_MIN)
    best = None
    for template in templates:
        th, tw = template.gx.shape
        wx0 = max(int(round(cx - tw / 2)) - window - rx0, 0)
        wy0 = max(int(round(cy - th / 2)) - window - ry0, 0)
        wx1 = min(wx0 + tw + 2 * window, px.shape[1])
        wy1 = min(wy0 + th + 2 * window, px.shape[0])
        if wx1 - wx0 < tw or wy1 - wy0 < th:
            continue
        _, value, _, loc = cv2.minMaxLoc(_scores(px[wy0:wy1, wx0:wx1], py[wy0:wy1, wx0:wx1], template))
        if best is None or value > best[0]:
            best = (float(value), template.angle, rx0 + wx0 + loc[0] + tw / 2, ry0 + wy0 + loc[1] + th / 2)
    return best


def _near(model: EdgeModel, level: int, angle: float, within: float) -> List[float]:
    angles = model.angles(level)
    chosen = [float(a) for a in angles if abs(a - angle) <= within + 1e-9]
    return chosen or [float(angles[int(np.argmin(np.abs(angles - angle)))])]


def find(model: EdgeModel, grey: np.ndarray, area: Optional[Tuple[int, int, int, int]] = None) -> Optional[Found]:
    """The best place and angle of the reference in ``grey`` (within ``area``: x1, y1, x2, y2 pixels).
    None when the area is smaller than the reference."""
    height, width = grey.shape[:2]
    x1, y1, x2, y2 = (0, 0, width, height) if area is None else (
        max(int(area[0]), 0), max(int(area[1]), 0), min(int(area[2]), width), min(int(area[3]), height))
    if x2 - x1 < model.size[0] or y2 - y1 < model.size[1]:
        return None
    pyramid = [grey[y1:y2, x1:x2]]
    for _ in range(model.levels):
        pyramid.append(cv2.pyrDown(pyramid[-1]))
    top = model.levels
    px, py, _ = unit_gradients(pyramid[top], EDGE_PIC_MIN)
    best = None
    for angle in model.angles(top):
        template = model._template(top, angle)
        th, tw = template.gx.shape
        if tw > px.shape[1] or th > px.shape[0]:
            continue
        _, value, _, loc = cv2.minMaxLoc(_scores(px, py, template))
        if best is None or value > best[0]:
            best = (float(value), template.angle, loc[0] + tw / 2, loc[1] + th / 2)
    if best is None:
        return None
    for level in range(top - 1, -1, -1):
        score, angle, cx, cy = best
        finer = _best_near(model, level, pyramid[level], cx * 2, cy * 2,
                           _near(model, level, angle, 2 * model.step(level)), WINDOW_PX)
        best = finer if finer is not None else (score, angle, cx * 2, cy * 2)
    score, angle, cx, cy = best
    return Found(cx + x1, cy + y1, angle, score)


def refine(model: EdgeModel, grey: np.ndarray, near: Found, shift_px: int = 6, turn_deg: float = 1.0) -> Found:
    """The best place and angle at full size within +-shift_px and +-turn_deg of ``near``."""
    best = _best_near(model, 0, grey, near.x, near.y, _near(model, 0, near.angle, turn_deg), shift_px)
    if best is None:
        return Found(near.x, near.y, near.angle, 0.0)
    score, angle, x, y = best
    return Found(x, y, angle, score)
