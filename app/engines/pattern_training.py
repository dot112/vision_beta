"""Training a pattern check: from a draft (the training picture with its reference box, up to 3 more
check boxes and a mask, and the good samples) to a reference set and suggested thresholds.

1. The reference and its boxes are checked (each at least MIN_BOX_PX, enough edges to be found).
2. Each sample is lined up: the reference is found in it with a first edge model made from the
   training picture, each check is searched close to where the reference puts it, and every box is cut
   out turned back. A sample the reference is not found in is refused by its number.
3. Per check, the samples' light is levelled to the first one's, then their average picture and how
   much each pixel varied (Lab) make the check; its final edge model is made from the average.
4. The typical edge score and the suggested thresholds: with 3 samples or more, each sample is scored
   against a check made from the others (leave one out); with fewer, the samples turned by +-angle/2
   give the typical score and the thresholds are the defaults.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from app.engines.pattern_check import (CHECK_SHIFT_PX, CHECK_TURN_DEG, CheckModel, ReferenceSet, _half,
                                       _to_picture, defects, is_area, level_light, line_up, match_percent)
from app.engines.pattern_find import Found, build_edge_model, find, refine, to_grey

MIN_BOX_PX = 16
MIN_EDGE_POINTS = 100
MIN_TOP_POINTS = 20
# A sample is found when its edge score is at least this share of the training picture's own score
# (a label with another big word on it scores about 0.69 of it, a good one 0.95 or more).
SAMPLE_SHARE = 0.8
MAX_SAMPLES = 20
MAX_CHECKS = 4
DEFAULT_SUGGESTION = {"min_score": 80, "max_defect_px": 30}
FEW_SAMPLES_NOTE = "Add more good samples (5 to 20) to get suggested thresholds."


class TrainingRefused(ValueError):
    """Why a draft cannot be trained (the message is shown to the user)."""


@dataclass
class DraftData:
    """A draft in pixels of the training picture: the reference box and the other checks' boxes
    (x1, y1, x2, y2), the mask (True = ignore) and the samples; sample 1 is the training picture."""
    picture: np.ndarray
    reference: List[int]
    checks: List[List[int]]
    ignore: np.ndarray
    samples: List[np.ndarray]


def _crop(img: np.ndarray, box: List[int]) -> np.ndarray:
    x1, y1, x2, y2 = box
    return img[y1:y2, x1:x2]


def _bgr(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else np.ascontiguousarray(img[..., :3])


def _first_model(draft: DraftData, box: List[int], angle: int):
    return build_edge_model(_crop(to_grey(draft.picture), box), _crop(draft.ignore, box), angle)


def find_sample(draft: DraftData, picture: np.ndarray, angle: int) -> Optional[Found]:
    """Where the reference is in a sample; its ``score`` is its share of the training picture's own score
    (0-1). None when it is under SAMPLE_SHARE."""
    model = _first_model(draft, draft.reference, angle)
    own = find(model, to_grey(draft.picture))
    found = find(model, to_grey(picture))
    if found is None or own is None or own.score <= 0 or found.score < SAMPLE_SHARE * own.score:
        return None
    return found._replace(score=min(found.score / own.score, 1.0))


def _check_boxes(draft: DraftData) -> List[List[int]]:
    height, width = draft.picture.shape[:2]
    if not draft.reference:
        raise TrainingRefused("Draw the reference box first")
    boxes = [[int(v) for v in draft.reference]] + [[int(v) for v in box] for box in draft.checks]
    if len(boxes) > MAX_CHECKS:
        raise TrainingRefused(f"A pattern check has at most {MAX_CHECKS} checks")
    for index, (x1, y1, x2, y2) in enumerate(boxes):
        name = "The reference box" if index == 0 else f"Check {index + 1}'s box"
        if x2 - x1 < MIN_BOX_PX or y2 - y1 < MIN_BOX_PX:
            raise TrainingRefused(f"{name} must be at least {MIN_BOX_PX} px wide and high")
        if x1 < 0 or y1 < 0 or x2 > width or y2 > height:
            raise TrainingRefused(f"{name} must be inside the picture")
    return boxes


def _places(draft: DraftData, boxes: List[List[int]], relative: List[Tuple[float, ...]], angle: int) -> List[List[Found]]:
    """Each sample's found place for each check."""
    height, width = draft.picture.shape[:2]
    firsts = [_first_model(draft, box, angle) for box in boxes]
    main = firsts[0]
    if main.points < MIN_EDGE_POINTS or main.top_points < MIN_TOP_POINTS:
        raise TrainingRefused("Draw the reference around printing or a shape: it has too few edges to be found")
    own = find(main, to_grey(draft.picture))
    places = []
    for number, sample in enumerate(draft.samples, start=1):
        if sample.shape[:2] != (height, width):
            raise TrainingRefused(f"Sample {number} is not the size of the training picture: take it again")
        grey = to_grey(sample)
        found = find(main, grey)
        if found is None or own is None or found.score < SAMPLE_SHARE * own.score:
            raise TrainingRefused(f"The reference is not found in sample {number}: remove it or take it again")
        row = [found]
        for index in range(1, len(boxes)):
            box = relative[index]
            x, y = _to_picture(found, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            row.append(refine(firsts[index], grey, Found(x, y, found.angle, 0.0), CHECK_SHIFT_PX, CHECK_TURN_DEG))
        places.append(row)
    return places


def _model(labs: np.ndarray, box: Tuple[float, ...], ignore: np.ndarray) -> CheckModel:
    mean = cv2.cvtColor(labs.mean(0).clip(0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
    return CheckModel(tuple(box), mean, labs.std(0).astype(np.float32), ignore, 1.0)


def _suggestion(scores: List[int], biggest: List[int]) -> Dict[str, int]:
    return {"min_score": max(50, min(95, min(scores) - 5)),
            "max_defect_px": max(10, int(math.ceil(1.5 * max(biggest) + 5)))}


def _turned_raw(sample: np.ndarray, main: Found, place: Found, model: Any, turn: float) -> float:
    """The edge score of a check in the sample turned ``turn`` degrees about the main reference."""
    rotation = cv2.getRotationMatrix2D((main.x, main.y), turn, 1.0)
    picture = cv2.warpAffine(to_grey(sample), rotation, (sample.shape[1], sample.shape[0]), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)
    x = rotation[0, 0] * place.x + rotation[0, 1] * place.y + rotation[0, 2]
    y = rotation[1, 0] * place.x + rotation[1, 1] * place.y + rotation[1, 2]
    return refine(model, picture, Found(x, y, place.angle + turn, 0.0), CHECK_SHIFT_PX, CHECK_TURN_DEG).score


def train(draft: DraftData, angle: int, checks: List[Dict[str, Any]], set_id: str) -> Tuple[ReferenceSet, Dict[str, Any]]:
    """The reference set of a draft, and ``{"suggested": [{min_score, max_defect_px}], "samples": [{scores,
    defects}], "note"}``. ``checks``: each check's ``colour`` and ``sensitivity`` (for the suggestions)."""
    angle = int(angle)
    boxes = _check_boxes(draft)
    if not draft.samples:
        raise TrainingRefused("Add the training picture as a good sample first")
    if len(draft.samples) > MAX_SAMPLES:
        raise TrainingRefused(f"At most {MAX_SAMPLES} good samples")
    rx, ry = (boxes[0][0] + boxes[0][2]) / 2, (boxes[0][1] + boxes[0][3]) / 2
    relative = [(b[0] - rx, b[1] - ry, b[2] - rx, b[3] - ry) for b in boxes]
    places = _places(draft, boxes, relative, angle)
    colour = all(s.ndim == 3 and s.shape[2] >= 3 for s in draft.samples)
    wanted = [{"colour": False, "sensitivity": "normal", **(checks[i] if i < len(checks) else {})} for i in range(len(boxes))]
    count = len(draft.samples)
    ignores = [_crop(draft.ignore, box).copy() for box in boxes]
    models, cuts_by_check, labs_by_check = [], [], []
    for index, box in enumerate(relative):
        cuts = [_bgr(line_up(sample, row[index], _half(box))[0]) for sample, row in zip(draft.samples, places)]
        raw = [cv2.cvtColor(cut, cv2.COLOR_BGR2LAB).astype(np.float32) for cut in cuts]
        use = ~ignores[index]
        labs = np.stack([level_light(lab, raw[0], use) for lab in raw])
        model = _model(labs, box, ignores[index])
        # A check (not the reference) with no edges to find is an area check: its marks and colour only.
        model.area = index > 0 and is_area(model.mean, model.ignore)
        models.append(model)
        cuts_by_check.append(cuts)
        labs_by_check.append(labs)
    ref = ReferenceSet(set_id, (draft.picture.shape[1], draft.picture.shape[0]), models, count)
    samples = [{"scores": [], "defects": []} for _ in range(count)]
    suggested: List[Dict[str, int]] = []
    for index, model in enumerate(models):
        greys = [to_grey(s) for s in draft.samples]
        use_colour = bool(wanted[index]["colour"]) and colour
        sensitivity = wanted[index]["sensitivity"]
        if model.area:
            model.typical = 1.0
            scores = [100] * count
            if count >= 3:
                biggest = [defects(cuts_by_check[index][left_out],
                                   _model(np.delete(labs_by_check[index], left_out, axis=0), relative[index], ignores[index]),
                                   use_colour, sensitivity)[0] for left_out in range(count)]
                suggested.append(_suggestion(scores, biggest))
            else:
                biggest = [defects(cut, model, use_colour, sensitivity)[0] for cut in cuts_by_check[index]]
                suggested.append(dict(DEFAULT_SUGGESTION))
        elif count >= 3:
            raws, biggest = [], []
            for left_out in range(count):
                others = np.delete(labs_by_check[index], left_out, axis=0)
                check = _model(others, relative[index], ignores[index])
                edges = build_edge_model(to_grey(check.mean), ignores[index], angle)
                raws.append(refine(edges, greys[left_out], places[left_out][index], CHECK_SHIFT_PX, CHECK_TURN_DEG).score)
                biggest.append(defects(cuts_by_check[index][left_out], check, use_colour, sensitivity)[0])
            model.typical = float(statistics.median(raws))
            scores = [match_percent(raw, model.typical) for raw in raws]
            suggested.append(_suggestion(scores, biggest))
        else:
            edges = ref.edge_model(index, angle)
            turns = (-angle / 2, angle / 2) if angle else (0.0,)
            model.typical = float(statistics.median(
                _turned_raw(s, row[0], row[index], edges, t) for s, row in zip(draft.samples, places) for t in turns))
            scores = [match_percent(refine(edges, g, row[index], CHECK_SHIFT_PX, CHECK_TURN_DEG).score, model.typical)
                      for g, row in zip(greys, places)]
            biggest = [defects(cut, model, use_colour, sensitivity)[0] for cut in cuts_by_check[index]]
            suggested.append(dict(DEFAULT_SUGGESTION))
        for number in range(count):
            samples[number]["scores"].append(scores[number])
            samples[number]["defects"].append(biggest[number])
    return ref, {"suggested": suggested, "samples": samples, "note": FEW_SAMPLES_NOTE if count < 3 else None,
                 "area": [m.area for m in models]}
