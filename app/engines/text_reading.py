"""Reading one picture for a text reader: each field's area, the reads, the lines matched to fields.

A field looks in a region of the picture, in the boxes of one class of a YOLO model, or in the
whole picture. Each distinct area is read once. Lines are matched to fields in two passes over the
fields in order, each line used once: first a field takes the line that fits its format (the
surest when several do); then a field still without one takes the closest line (to show what is
wrong). The picture is good when every field fits; otherwise the first field in order that does
not fit gives the reason: text_wrong (its line does not fit) or no_text (it has none).
No thread and no line: the reader's worker, the trigger path and the setup preview all use this.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

import numpy as np

from app.engines.ocr_engine import OcrLine, join_dots
from app.engines.text_format import allowed_characters, fit
from app.services.line_config import REASON_NO_TEXT, REASON_READER_ERROR, REASON_TEXT_WRONG

Area = Tuple[int, int, int, int]
# A YOLO box narrower or lower than this (after clipping to the picture) is not read.
MIN_BOX_PX = 2


class TextField(NamedTuple):
    """One field of a picture: its text after the look-alike fixes (None: no line), the line as read,
    whether it fits its format, the line's confidence and box, and the area it looked in (pixels)."""
    name: str
    text: Optional[str]
    raw: Optional[str]
    fits: bool
    confidence: Optional[float]
    box: Optional[List[List[int]]]
    area: Optional[List[int]]


class TextResult(NamedTuple):
    """What one picture says: the fields, every line read, the time it took, "good" or "reject" with
    the reason, and why the reader could not read (None when it could)."""
    fields: List[TextField]
    lines: List[OcrLine]
    ms: float
    result: str
    reason: Optional[str]
    error: Optional[str]

    @property
    def status(self) -> str:
        """"good"; "none" when no field has a line; else "wrong"."""
        if self.result == "good":
            return "good"
        return "wrong" if any(f.text is not None for f in self.fields) else "none"


def error_result(ocr: Dict[str, Any], why: str, ms: float = 0.0) -> TextResult:
    """The result of a picture the reader could not read: every field empty, reject "reader_error"."""
    fields = [TextField(f["name"], None, None, False, None, None, None) for f in ocr.get("fields") or []]
    return TextResult(fields, [], ms, "reject", REASON_READER_ERROR, why)


def text_values(result: TextResult) -> Dict[str, str]:
    """{field name: text} for the fields that have a line."""
    return {f.name: f.text for f in result.fields if f.text is not None}


def _clip(box: Any, w: int, h: int) -> Optional[Area]:
    x1, y1 = max(0, int(box.x1)), max(0, int(box.y1))
    x2, y2 = min(w, int(box.x2)), min(h, int(box.y2))
    return (x1, y1, x2, y2) if x2 - x1 >= MIN_BOX_PX and y2 - y1 >= MIN_BOX_PX else None


def _areas(field: Dict[str, Any], w: int, h: int, found: List[Any]) -> List[Area]:
    if field["find"] == "region":
        x1, y1, x2, y2 = field["region"]
        return [(round(x1 * w), round(y1 * h), round(x2 * w), round(y2 * h))]
    if field["find"] == "yolo":
        boxes = [_clip(d.bbox, w, h) for d in found if d.class_name == field["yolo_class"]]
        return [box for box in boxes if box is not None]
    return [(0, 0, w, h)]


def _read_area(mat: np.ndarray, area: Area, ocr: Dict[str, Any], engine: Any, allowed: Optional[str]) -> List[OcrLine]:
    x1, y1, x2, y2 = area
    crop = mat[y1:y2, x1:x2]
    if ocr.get("join_dots"):
        crop = join_dots(crop, int(ocr["join_dots"]), ocr.get("ink") or "dark")
    lines = engine.read(crop, upside_down=bool(ocr.get("upside_down")), allowed=allowed)
    floor = float(ocr.get("min_confidence") or 0.0)
    return [OcrLine(line.text, line.confidence, [[x + x1, y + y1] for x, y in line.box])
            for line in lines if line.confidence >= floor]


def _bounds(box: List[List[int]]) -> Tuple[float, float, float, float]:
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def _same_print(a: List[List[int]], b: List[List[int]]) -> bool:
    """Two lines read in different areas are one print when their boxes overlap by more than half of the
    smaller one (a region inside the whole picture, two overlapping YOLO boxes)."""
    ax1, ay1, ax2, ay2 = _bounds(a)
    bx1, by1, bx2, by2 = _bounds(b)
    overlap = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
    smaller = min((ax2 - ax1) * (ay2 - ay1), (bx2 - bx1) * (by2 - by1))
    return smaller > 0 and overlap > smaller / 2


def _prints(read: Dict[Area, List[OcrLine]]) -> Dict[Tuple[Area, int], int]:
    """Each line read (area, index) and the print it is: lines of different areas that are the same print
    share a number, so one print goes to one field only."""
    keys = [(area, i) for area, lines in read.items() for i in range(len(lines))]
    group = {key: n for n, key in enumerate(keys)}
    for n, a in enumerate(keys):
        for b in keys[n + 1:]:
            if a[0] != b[0] and group[a] != group[b] and _same_print(read[a[0]][a[1]].box, read[b[0]][b[1]].box):
                old, new = group[b], group[a]
                group.update({key: new for key, g in group.items() if g == old})
    return group


def read_picture(mat: np.ndarray, ocr: Dict[str, Any], engine: Any,
                 locate: Optional[Callable[[np.ndarray], List[Any]]] = None) -> TextResult:
    """Read one picture with a text reader's ``ocr`` settings. ``locate`` gives the YOLO detections of
    the picture (needed only when a field finds its text by YOLO class). Never raises: a picture the
    reader cannot read is an error_result."""
    started = time.perf_counter()
    fields = ocr.get("fields") or []
    elapsed = lambda: round((time.perf_counter() - started) * 1000, 1)  # noqa: E731
    h, w = mat.shape[:2]
    try:
        found: List[Any] = []
        if any(field["find"] == "yolo" for field in fields):
            if locate is None:
                return error_result(ocr, "the YOLO model of the text reader is not loaded", elapsed())
            found = list(locate(mat))
        allowed = allowed_characters(field["format"] for field in fields)
        areas = [_areas(field, w, h, found) for field in fields]
        read: Dict[Area, List[OcrLine]] = {}
        for area in (a for field_areas in areas for a in field_areas):
            if area not in read:
                read[area] = _read_area(mat, area, ocr, engine, allowed)
    except Exception as exc:  # noqa: BLE001 - the reader could not read: the picture says so
        return error_result(ocr, str(exc) or type(exc).__name__, elapsed())

    prints = _prints(read)
    used = set()                                      # the prints a field has taken
    chosen: List[Optional[Tuple[Area, int]]] = [None] * len(fields)
    candidates = [[(area, i) for area in field_areas for i in range(len(read[area]))] for field_areas in areas]
    for index, field in enumerate(fields):            # 1: the lines that fit
        fitting = [c for c in candidates[index]
                   if prints[c] not in used and fit(field["format"], read[c[0]][c[1]].text).fits]
        if fitting:
            chosen[index] = max(fitting, key=lambda c: read[c[0]][c[1]].confidence)
            used.add(prints[chosen[index]])
    for index, field in enumerate(fields):            # 2: the closest line, for a field still without one
        free = [c for c in candidates[index] if prints[c] not in used]
        if chosen[index] is None and free:
            chosen[index] = min(free, key=lambda c: (fit(field["format"], read[c[0]][c[1]].text).differences,
                                                     -read[c[0]][c[1]].confidence))
            used.add(prints[chosen[index]])

    results: List[TextField] = []
    reason: Optional[str] = None
    for index, field in enumerate(fields):
        pick = chosen[index]
        if pick is None:
            first = areas[index][0] if areas[index] else None
            results.append(TextField(field["name"], None, None, False, None, None, list(first) if first else None))
            reason = reason or REASON_NO_TEXT
            continue
        line = read[pick[0]][pick[1]]
        checked = fit(field["format"], line.text)
        results.append(TextField(field["name"], checked.text, line.text, checked.fits, line.confidence, line.box,
                                 list(pick[0])))
        if not checked.fits:
            reason = reason or REASON_TEXT_WRONG
    # Every print once: the line read with the most confidence.
    best: Dict[int, OcrLine] = {}
    for key, n in prints.items():
        line = read[key[0]][key[1]]
        if n not in best or line.confidence > best[n].confidence:
            best[n] = line
    lines = list(best.values())
    return TextResult(results, lines, elapsed(), "good" if reason is None else "reject", reason, None)
