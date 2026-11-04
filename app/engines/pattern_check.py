"""Checking one picture with a pattern check's reference set: the main reference is found by its edges
(pattern_find), every check's box is cut out lined up with it, and each check gets:

- its Match %: its edge score / the good samples' typical score x 100, at most 100;
- its biggest defect: the largest spot of pixels further from the average good picture than the good
  samples varied there (x k of its Sensitivity, at least a floor), after the light is levelled.

The picture is good when every check reaches its lowest Match % and its biggest defect is within its
limit. Otherwise it is a reject with the first problem: the main reference not found, then a check that
does not match, then a defect (in check order). A picture of another size than the training picture, or
one that cannot be checked, is an error.

No thread, no line: the reference set and the camera's ``match`` settings come in, a result goes out.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import cv2
import numpy as np

from app.engines.pattern_find import EdgeModel, Found, build_edge_model, find, refine, to_grey
from app.services.line_config import (REASON_PATTERN_DEFECT, REASON_PATTERN_ERROR, REASON_PATTERN_MISMATCH,
                                      REASON_PATTERN_NOT_FOUND)

# How different a pixel must be to count: (x the samples' spread, at least this many levels).
SENSITIVITY = {"low": (5, 35), "normal": (4, 25), "high": (3, 15)}
BORDER_PX = 2
MAX_SPOTS = 5
# Checks 2-4 are searched this close to where the main reference puts them.
CHECK_SHIFT_PX = 6
CHECK_TURN_DEG = 1.0
STATUSES = {None: "good", REASON_PATTERN_NOT_FOUND: "not_found", REASON_PATTERN_MISMATCH: "mismatch",
            REASON_PATTERN_DEFECT: "defect", REASON_PATTERN_ERROR: "error"}
Box = Tuple[float, float, float, float]


@dataclass
class CheckModel:
    """One check of a reference set. ``box``: x1, y1, x2, y2 in pixels from the reference box's centre
    (check 1's is the reference box). ``mean``: the average good picture of the box, lined up (BGR);
    ``spread``: how much each pixel varied across the good samples (Lab); ``ignore``: masked pixels;
    ``typical``: the good samples' typical edge score."""
    box: Box
    mean: np.ndarray
    spread: np.ndarray
    ignore: np.ndarray
    typical: float
    mean_lab: np.ndarray = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.mean_lab = cv2.cvtColor(self.mean, cv2.COLOR_BGR2LAB).astype(np.float32)


@dataclass
class ReferenceSet:
    """What training made: the training picture's size and the checks. Its edge models are made once per
    check and angle, and shared by every camera that uses the set."""
    id: str
    picture_size: Tuple[int, int]
    checks: List[CheckModel]
    samples: int
    _models: Dict[Tuple[int, int], EdgeModel] = field(default_factory=dict, init=False, repr=False, compare=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def edge_model(self, index: int, angle: int) -> EdgeModel:
        key = (index, int(angle))
        with self._lock:
            model = self._models.get(key)
        if model is None:
            check = self.checks[index]
            model = build_edge_model(to_grey(check.mean), check.ignore, int(angle))
            with self._lock:
                model = self._models.setdefault(key, model)
        return model


class CheckResult(NamedTuple):
    name: str
    score: int
    defect_px: int
    spots: List[List[int]]
    box: List[List[float]]
    fits: bool


@dataclass
class PatternResult:
    width: int
    height: int
    ms: float
    found: Optional[Found]
    checks: List[CheckResult]
    result: str
    reason: Optional[str]
    error: Optional[str] = None

    @property
    def status(self) -> str:
        return STATUSES.get(self.reason, "error")

    def to_dict(self) -> Dict[str, Any]:
        found = None if self.found is None else {
            "x": round(self.found.x, 1), "y": round(self.found.y, 1),
            "angle": round(self.found.angle, 2), "score": round(self.found.score, 4)}
        return {
            "width": self.width, "height": self.height, "ms": round(self.ms, 1), "found": found,
            "checks": [{"name": c.name, "score": c.score, "defect_px": c.defect_px, "spots": c.spots,
                        "box": [[round(x, 1), round(y, 1)] for x, y in c.box], "fits": c.fits} for c in self.checks],
            "result": self.result, "reason": self.reason, "error": self.error,
        }


def error_result(error: str, width: int = 0, height: int = 0) -> PatternResult:
    return PatternResult(width, height, 0.0, None, [], "reject", REASON_PATTERN_ERROR, error)


def match_percent(raw: float, typical: float) -> int:
    if typical <= 0:
        return 0
    return int(round(min(max(raw / typical, 0.0), 1.0) * 100))


def match_values(result: PatternResult) -> Dict[str, Any]:
    """What a product keeps of its pattern check: the main Match %, the status, each check's Match % and
    biggest defect (the checks not done left out); only the status for an error."""
    if result.reason == REASON_PATTERN_ERROR:
        return {"status": "error"}
    return {
        "score": result.checks[0].score if result.checks else 0,
        "status": result.status,
        "checks": {c.name: {"score": c.score, "defect_px": c.defect_px} for c in result.checks},
    }


def _turn(angle: float) -> Tuple[float, float]:
    a = math.radians(angle)
    return math.cos(a), math.sin(a)


def _to_picture(place: Found, dx: float, dy: float) -> Tuple[float, float]:
    """A point dx, dy from a found place's centre (in the training picture's frame) in the picture."""
    c, s = _turn(place.angle)
    return place.x + c * dx + s * dy, place.y - s * dx + c * dy


def _half(box: Box) -> Box:
    w, h = box[2] - box[0], box[3] - box[1]
    return (-w / 2, -h / 2, w / 2, h / 2)


def line_up(picture: np.ndarray, found: Found, box: Box) -> Tuple[np.ndarray, np.ndarray]:
    """``box`` (pixels from ``found``'s centre) cut out of the picture turned back by ``found.angle``, so it
    sits as in the training picture; and where that cut-out is inside the picture."""
    x1, y1, x2, y2 = box
    w, h = int(round(x2 - x1)), int(round(y2 - y1))
    c, s = _turn(found.angle)
    back = np.float32([[c, s, c * x1 + s * y1 + found.x], [-s, c, -s * x1 + c * y1 + found.y]])
    cut = cv2.warpAffine(picture, back, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=0)
    inside = cv2.warpAffine(np.full(picture.shape[:2], 255, np.uint8), back, (w, h),
                            flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP, borderValue=0) > 0
    return cut, inside


def level_light(lab: np.ndarray, like: np.ndarray, use: np.ndarray) -> np.ndarray:
    """``lab`` (float Lab) with its lightness's mean and spread over ``use`` matched to ``like``'s."""
    out = lab.astype(np.float32, copy=True)
    if not use.any():
        return out
    light, target = out[..., 0][use], like[..., 0][use]
    spread = float(light.std())
    gain = float(target.std()) / spread if spread >= 1.0 else 1.0
    out[..., 0] = (out[..., 0] - float(light.mean())) * gain + float(target.mean())
    return out


def defects(lined_up: np.ndarray, check: CheckModel, colour: bool, sensitivity: str,
            outside: Optional[np.ndarray] = None) -> Tuple[int, List[List[int]]]:
    """The biggest defect's area (pixels) and the largest spots as x, y, w, h in the lined-up box."""
    k, floor = SENSITIVITY.get(sensitivity, SENSITIVITY["normal"])
    bgr = cv2.cvtColor(lined_up, cv2.COLOR_GRAY2BGR) if lined_up.ndim == 2 else lined_up[..., :3]
    skip = check.ignore.copy()
    if outside is not None:
        skip |= outside
    skip[:BORDER_PX, :] = skip[-BORDER_PX:, :] = True
    skip[:, :BORDER_PX] = skip[:, -BORDER_PX:] = True
    use = ~skip
    if not use.any():
        return 0, []
    lab = level_light(cv2.cvtColor(np.ascontiguousarray(bgr), cv2.COLOR_BGR2LAB).astype(np.float32), check.mean_lab, use)
    different = np.zeros(use.shape, bool)
    for channel in range(3 if colour else 1):
        tolerance = cv2.dilate(np.maximum(check.spread[..., channel] * k, floor), np.ones((3, 3), np.uint8))
        different |= np.abs(lab[..., channel] - check.mean_lab[..., channel]) > tolerance
    different = cv2.morphologyEx((different & use).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(different, connectivity=8)
    if count <= 1:
        return 0, []
    areas = stats[1:, cv2.CC_STAT_AREA]
    order = np.argsort(-areas)[:MAX_SPOTS]
    return int(areas.max()), [[int(v) for v in stats[1 + i, :4]] for i in order]


def _corners(place: Found, box: Box) -> List[List[float]]:
    x1, y1, x2, y2 = box
    return [list(_to_picture(place, x, y)) for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2))]


def _spot_box(place: Found, box: Box, spot: List[int]) -> List[int]:
    x, y, w, h = spot
    points = _corners(place, (box[0] + x, box[1] + y, box[0] + x + w, box[1] + y + h))
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [int(math.floor(min(xs))), int(math.floor(min(ys))), int(math.ceil(max(xs))), int(math.ceil(max(ys)))]


def _area(search: Any, width: int, height: int) -> Optional[Tuple[int, int, int, int]]:
    if not search:
        return None
    x1, y1, x2, y2 = search
    return (int(round(x1 * width)), int(round(y1 * height)), int(round(x2 * width)), int(round(y2 * height)))


def _reason(results: List[CheckResult], settings: List[Dict[str, Any]]) -> Optional[str]:
    """Not found first, then the first check that does not match, then the first defect too big."""
    if not results or results[0].score < settings[0]["min_score"]:
        return REASON_PATTERN_NOT_FOUND
    if any(r.score < s["min_score"] for r, s in zip(results[1:], settings[1:])):
        return REASON_PATTERN_MISMATCH
    if any(r.defect_px > s["max_defect_px"] for r, s in zip(results, settings)):
        return REASON_PATTERN_DEFECT
    return None


def check_picture(ref: ReferenceSet, match: Dict[str, Any], picture: np.ndarray) -> PatternResult:
    """Check one picture with a reference set and a camera's ``match`` settings."""
    started = time.perf_counter()
    height, width = picture.shape[:2]
    trained_w, trained_h = ref.picture_size
    if (width, height) != (trained_w, trained_h):
        # Plain ASCII and short: the video's bar draws it whole.
        return error_result(f"Picture is {width} x {height}, trained on {trained_w} x {trained_h}: train it again",
                            width, height)
    colour_picture = picture.ndim == 3 and picture.shape[2] >= 3
    grey = to_grey(picture)
    angle = int(match.get("angle", 15))
    settings = list(match.get("checks") or [])[:len(ref.checks)]
    found = find(ref.edge_model(0, angle), grey, _area(match.get("search_area"), width, height))
    results: List[CheckResult] = []
    for index, (check, wanted) in enumerate(zip(ref.checks, settings)):
        half = _half(check.box)
        if index == 0:
            place = found if found is not None else Found(width / 2, height / 2, 0.0, 0.0)
            score = match_percent(place.score, check.typical)
            if score < wanted["min_score"]:
                results.append(CheckResult(wanted["name"], score, 0, [], _corners(place, half), False))
                break
        else:
            x, y = _to_picture(found, (check.box[0] + check.box[2]) / 2, (check.box[1] + check.box[3]) / 2)
            place = Found(x, y, found.angle, 0.0)
            if line_up(grey, place, half)[1].mean() < 0.5:
                score = 0
            else:
                place = refine(ref.edge_model(index, angle), grey, place, CHECK_SHIFT_PX, CHECK_TURN_DEG)
                score = match_percent(place.score, check.typical)
        defect_px, spots = 0, []
        if score > 0:
            cut, inside = line_up(picture, place, half)
            defect_px, cut_spots = defects(cut, check, bool(wanted.get("colour")) and colour_picture,
                                           wanted.get("sensitivity", "normal"), ~inside)
            spots = [_spot_box(place, half, spot) for spot in cut_spots]
        fits = score >= wanted["min_score"] and defect_px <= wanted["max_defect_px"]
        results.append(CheckResult(wanted["name"], score, defect_px, spots, _corners(place, half), fits))
    reason = _reason(results, settings)
    ms = (time.perf_counter() - started) * 1000
    return PatternResult(width, height, ms, found, results, "good" if reason is None else "reject", reason)
