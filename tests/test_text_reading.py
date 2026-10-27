"""Reading one picture for a text reader: each field's area, the reads, the lines matched to the
fields, and the result (good, or reject with text_wrong / no_text / reader_error)."""
from __future__ import annotations

import numpy as np

from app.engines import text_reading
from app.engines.ocr_engine import OcrLine, OcrUnavailable
from app.engines.text_reading import read_picture, text_values
from app.schemas.vision import BoundingBox, DetectionItem

B = [[0, 0], [40, 0], [40, 12], [0, 12]]


class _FakeEngine:
    """Gives the queued lines, one list per read, and keeps (shape, upside_down, allowed) of each read."""

    def __init__(self, queued=None, raise_=None):
        self.queued = list(queued or [])
        self.raise_ = raise_
        self.calls = []

    def read(self, img, *, upside_down=False, allowed=None):
        self.calls.append((img.shape, upside_down, allowed))
        if self.raise_ is not None:
            raise self.raise_
        return self.queued.pop(0) if self.queued else []


def _mat(w=640, h=480):
    return np.full((h, w, 3), 200, np.uint8)


def _field(name, find="region", region=(0.1, 0.2, 0.6, 0.4), format="LOT A99999", **extra):
    field = {"name": name, "find": find, "format": format, **extra}
    if find == "region":
        field["region"] = list(region)
    return field


def _ocr(*fields, **extra):
    return {"model": "small", "locator_model_id": None, "upside_down": False, "join_dots": 0, "ink": "dark",
            "min_confidence": 0.5, "fields": list(fields), **extra}


def _det(name, x1, y1, x2, y2):
    return DetectionItem(class_id=0, class_name=name, confidence=0.9,
                         bbox=BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2, width=x2 - x1, height=y2 - y1))


def test_a_region_field_reads_its_cut_out_and_maps_boxes_back():
    engine = _FakeEngine([[OcrLine("LOT A12345", 0.95, [[2, 3], [80, 3], [80, 20], [2, 20]])]])
    r = read_picture(_mat(1000, 500), _ocr(_field("Lot", region=[0.1, 0.2, 0.5, 0.6])), engine)
    assert engine.calls[0][0][:2] == (200, 400)                    # rows 100..300, columns 100..500
    lot = r.fields[0]
    assert (lot.text, lot.fits, lot.area, lot.box[0]) == ("LOT A12345", True, [100, 100, 500, 300], [102, 103])
    assert (lot.raw, lot.confidence) == ("LOT A12345", 0.95)
    assert (r.result, r.reason, r.status, r.error) == ("good", None, "good", None)
    assert engine.calls[0][2] == " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"   # the formats' characters
    assert r.ms >= 0 and r.lines[0].box[0] == [102, 103]


def test_a_field_takes_the_line_that_fits_else_the_closest():
    lines = [OcrLine("EXP 12/03/2027", 0.9, B), OcrLine("LOT A1234", 0.99, B), OcrLine("LOT A12345", 0.6, B)]
    r = read_picture(_mat(), _ocr(_field("Lot", find="all")), _FakeEngine([lines]))
    assert (r.fields[0].text, r.fields[0].fits) == ("LOT A12345", True)
    r = read_picture(_mat(), _ocr(_field("Lot", find="all")), _FakeEngine([lines[:2]]))
    assert (r.fields[0].text, r.fields[0].fits, r.reason, r.status) == ("LOT A1234", False, "text_wrong", "wrong")


def test_a_line_goes_to_one_field_only():
    lines = [OcrLine("LOT A12345", 0.9, B), OcrLine("LOT B67890", 0.8, B)]
    engine = _FakeEngine([lines])
    r = read_picture(_mat(), _ocr(_field("Lot", find="all"), _field("Lot2", find="all")), engine)
    assert [f.text for f in r.fields] == ["LOT A12345", "LOT B67890"]
    assert len(engine.calls) == 1 and len(r.lines) == 2           # the whole picture is read once


def test_a_fitting_line_goes_to_the_field_it_fits_before_any_closest_match():
    r = read_picture(_mat(), _ocr(_field("A", find="all", format="99/99"), _field("B", find="all", format="LOT A99999")),
                     _FakeEngine([[OcrLine("LOT A12345", 0.9, B)]]))
    assert ([f.text for f in r.fields], r.reason, r.status) == ([None, "LOT A12345"], "no_text", "wrong")


def test_no_line_at_all_is_no_text():
    r = read_picture(_mat(), _ocr(_field("A", find="all")), _FakeEngine([[]]))
    assert (r.result, r.reason, r.status) == ("reject", "no_text", "none")
    assert r.fields[0] == text_reading.TextField("A", None, None, False, None, None, [0, 0, 640, 480])


def test_lines_under_the_lowest_confidence_are_dropped():
    r = read_picture(_mat(), _ocr(_field("Lot", find="all"), min_confidence=0.7),
                     _FakeEngine([[OcrLine("LOT A12345", 0.65, B)]]))
    assert r.reason == "no_text" and r.lines == []


def test_yolo_fields_read_in_their_class_boxes():
    found = [_det("dateBox", 10, 20, 210, 70), _det("datepage", 0, 0, 50, 50), _det("dateBox", 300, 20, 500, 70)]
    engine = _FakeEngine([[], [OcrLine("LOT A12345", 0.9, B)]])
    r = read_picture(_mat(), _ocr(_field("Lot", find="yolo", yolo_class="dateBox"), locator_model_id="m1"), engine,
                     locate=lambda img: found)
    assert [c[0][:2] for c in engine.calls] == [(50, 200), (50, 200)] and r.fields[0].area == [300, 20, 500, 70]
    assert r.fields[0].box[0] == [300, 20] and r.result == "good"


def test_yolo_boxes_are_clipped_and_empty_ones_skipped():
    found = [_det("dateBox", -40, 450, 100, 600), _det("dateBox", 30, 30, 30, 90)]     # picture 640 x 480
    engine = _FakeEngine([[]])
    read_picture(_mat(640, 480), _ocr(_field("Lot", find="yolo", yolo_class="dateBox"), locator_model_id="m1"), engine,
                 locate=lambda img: found)
    assert [c[0][:2] for c in engine.calls] == [(30, 100)]


def test_a_yolo_field_without_a_model_and_an_engine_that_fails_are_reader_errors():
    r = read_picture(_mat(), _ocr(_field("Lot", find="yolo", yolo_class="dateBox")), _FakeEngine([]))
    assert (r.result, r.reason, r.status) == ("reject", "reader_error", "none") and "YOLO model" in r.error
    r = read_picture(_mat(), _ocr(_field("Lot")), _FakeEngine(raise_=OcrUnavailable("no model")))
    assert (r.reason, r.error, [f.text for f in r.fields]) == ("reader_error", "no model", [None])
    r = read_picture(_mat(), _ocr(_field("Lot")), _FakeEngine(raise_=RuntimeError("boom")))
    assert r.reason == "reader_error" and "boom" in r.error


def test_join_dots_and_upside_down_reach_the_engine(monkeypatch):
    joined = []
    monkeypatch.setattr(text_reading, "join_dots", lambda img, size, ink: joined.append((size, ink)) or img)
    engine = _FakeEngine([[]])
    read_picture(_mat(), _ocr(_field("Lot"), join_dots=7, ink="light", upside_down=True), engine)
    assert joined == [(7, "light")] and engine.calls[0][1] is True


def test_a_star_format_reads_without_a_character_limit():
    engine = _FakeEngine([[]])
    read_picture(_mat(), _ocr(_field("Any", find="all", format="L*9")), engine)
    assert engine.calls[0][2] is None


def test_text_values():
    r = read_picture(_mat(), _ocr(_field("Lot", find="all"), _field("Exp", find="all", format="99/99")),
                     _FakeEngine([[OcrLine("LOT A12345", 0.9, B)]]))
    assert text_values(r) == {"Lot": "LOT A12345"}


def test_error_result():
    r = text_reading.error_result(_ocr(_field("Lot"), _field("Exp")), "no picture")
    assert [f.name for f in r.fields] == ["Lot", "Exp"] and all(f.text is None for f in r.fields)
    assert (r.result, r.reason, r.error, r.status, r.lines) == ("reject", "reader_error", "no picture", "none", [])
