"""A text reader's video: the TEXT READER bar, each field's box green when its text fits and red when
not, the regions, the corner picture; and the picture a trigger took, with its boxes."""
from __future__ import annotations

import numpy as np
import pytest

from app.engines.inference_engine import STATUS_NO_LINE, STATUS_STOPPED, STATUS_WAITING_TRIGGER
from app.engines.ocr_engine import OcrLine
from app.engines.text_reading import TextField, TextResult
from app.services import ocr_service
from app.services.ocr_service import OcrReaderPipeline, bar_parts, render_capture
from app.services.qr_service import CornerPicture
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_joined_cameras import lines  # noqa: F401  (a fixture)

LOT = [[100, 100], [300, 100], [300, 140], [100, 140]]
EXP = [[100, 200], [300, 200], [300, 240], [100, 240]]
FIELDS = [{"name": "Lot", "find": "all", "format": "LOT A99999"},
          {"name": "Exp", "find": "region", "region": [0.5, 0.6, 0.9, 0.9], "format": "99/99"}]
RESULT = TextResult([TextField("Lot", "LOT A12345", "LOT A12345", True, 0.9, LOT, [0, 0, 640, 480]),
                     TextField("Exp", "12/3", "12/3", False, 0.8, EXP, [320, 288, 576, 432])],
                    [OcrLine("LOT A12345", 0.9, LOT), OcrLine("12/3", 0.8, EXP)], 35.0, "reject", "text_wrong", None)


class _Worker:
    def __init__(self, result=RESULT, corner=None):
        self._result, self._corner = result, corner

    def latest(self):
        return self._result

    def corner(self):
        return self._corner

    def clear_corner(self):
        pass


class _Engine:
    def __init__(self, ready=True):
        self.ready = ready


def _save(lines, cam, state={}):  # noqa: B006 - one line per test run, saved again on each call
    line = SettingsPersistenceService.save_line({"id": state.get(id(lines)), "name": "OCR", "cameras": [cam], "enabled": True})
    state[id(lines)] = line["id"]
    lines.apply_state()
    return lines.get(line["id"])


@pytest.fixture
def reader(lines, monkeypatch):  # noqa: F811
    def make(count_mode=None, ready=True, worker=None, fields=FIELDS, **extra):
        cam = {"camera_id": "c1", "role": "ocr", "ocr": {"fields": fields, **extra}}
        if count_mode:
            cam["count_mode"] = count_mode
        runtime = _save(lines, cam)
        monkeypatch.setattr(ocr_service, "engine_for", lambda model: _Engine(ready))
        monkeypatch.setattr(OcrReaderPipeline, "peek", classmethod(lambda cls, cid: worker or _Worker()))
        return runtime
    return make


def test_the_bar_says_what_the_reader_is_doing(reader, lines, monkeypatch):  # noqa: F811
    runtime = reader()
    assert bar_parts("c1") == ["TEXT READER", "FIELDS 1/2 OK", "35ms"]
    assert bar_parts("elsewhere") == ["TEXT READER", STATUS_NO_LINE]
    runtime.enabled = False
    assert bar_parts("c1") == ["TEXT READER", STATUS_STOPPED]
    reader(ready=False)
    assert bar_parts("c1") == ["TEXT READER", "READER NOT READY"]
    reader(count_mode="trigger")
    assert bar_parts("c1") == ["TEXT READER", STATUS_WAITING_TRIGGER]


def test_a_yolo_field_whose_model_is_not_loaded_is_not_ready(reader, lines, monkeypatch):  # noqa: F811
    reader(locator_model_id="m1")
    monkeypatch.setattr(lines, "engine_for_camera", lambda camera_id: (_Loaded(False), "m", "m1"))
    assert bar_parts("c1") == ["TEXT READER", "FIELDS 1/2 OK", "35ms"]      # no field finds by YOLO class
    runtime = reader(fields=[{"name": "Lot", "find": "yolo", "yolo_class": "dateBox", "format": "LOT A99999"}],
                     locator_model_id="m1")
    assert runtime.camera_entry("c1")["ocr"]["locator_model_id"] == "m1"
    assert bar_parts("c1") == ["TEXT READER", "READER NOT READY"]


class _Loaded:
    def __init__(self, loaded):
        self.is_loaded = loaded


def test_fields_are_drawn_green_when_they_fit_and_red_when_not(reader):
    reader()
    img = OcrReaderPipeline.draw_reads("c1", np.full((480, 640, 3), 128, np.uint8))
    assert tuple(img[140, 200]) == (0, 200, 0)          # Lot's box, bottom edge
    assert tuple(img[240, 200]) == (0, 0, 230)          # Exp's box, bottom edge
    assert tuple(img[360, 320]) == (200, 200, 200)      # Exp's region, left edge


def test_boxes_follow_the_video_scale(reader):
    reader()
    img = OcrReaderPipeline.draw_reads("c1", np.full((240, 320, 3), 128, np.uint8), 0.5, 0.5)
    assert tuple(img[70, 100]) == (0, 200, 0)


def test_a_stopped_line_draws_no_old_result_and_no_corner(reader):
    corner = CornerPicture(np.full((20, 60, 3), 255, np.uint8), "LOT A12345", True)
    runtime = reader(worker=_Worker(corner=corner))
    shown = OcrReaderPipeline.draw_reads("c1", np.full((480, 640, 3), 128, np.uint8))
    assert tuple(shown[140, 200]) == (0, 200, 0)
    runtime.enabled = False
    stopped = OcrReaderPipeline.draw_reads("c1", np.full((480, 640, 3), 128, np.uint8))
    assert tuple(stopped[140, 200]) == (128, 128, 128)
    assert not (stopped[60:200, 0:200] == 255).all(axis=2).any()     # no white corner picture


def test_a_triggered_reader_shows_its_result_on_the_trigger_picture_not_on_the_video(reader):
    reader(count_mode="trigger")
    img = OcrReaderPipeline.draw_reads("c1", np.full((480, 640, 3), 128, np.uint8))
    assert tuple(img[140, 200]) == (128, 128, 128)


def test_render_capture_gives_a_jpeg():
    jpeg = render_capture(np.full((480, 640, 3), 128, np.uint8), RESULT)
    assert jpeg[:2] == b"\xff\xd8"
    assert render_capture(None, RESULT) is None
