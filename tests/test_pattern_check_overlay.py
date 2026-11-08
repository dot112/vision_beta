"""A pattern check's video: the PATTERN CHECK bar (what it is doing, GOOD or REJECT with why), each check's
box green when it passes and red when not with its Match % and biggest defect, the defect spots; and the
picture a trigger took, with the same drawing."""
from __future__ import annotations

import numpy as np
import pytest

from app.engines.inference_engine import STATUS_NO_LINE, STATUS_STOPPED, STATUS_WAITING_TRIGGER
from app.services import match_service
from app.services.match_references import ReferenceMissing
from app.services.match_service import PatternCheckPipeline, bar_parts, render_capture
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.pattern_helpers import DEFECT, ERROR, GOOD, MISMATCH, NOT_FOUND, checker, write_set_file
from tests.test_joined_cameras import lines  # noqa: F401  (a fixture)


class _Worker:
    def __init__(self, result=GOOD):
        self._result = result

    def latest(self):
        return self._result


def _save(lines, cam, state={}):  # noqa: B006 - one line per test run, saved again on each call
    line = SettingsPersistenceService.save_line({"id": state.get(id(lines)), "name": "Pattern", "cameras": [cam],
                                                 "enabled": True})
    state[id(lines)] = line["id"]
    lines.apply_state()
    return lines.get(line["id"])


@pytest.fixture
def check(lines, monkeypatch):  # noqa: F811
    write_set_file()

    def make(result=GOOD, count_mode=None, ready=True):
        cam = checker("m1", count_mode=count_mode or "track")      # sent each time: a mode left out is kept
        runtime = _save(lines, cam)

        def load(set_id):
            if not ready:
                raise ReferenceMissing(f"the reference set {set_id} is missing")
            return object()

        monkeypatch.setattr(match_service, "load_set", load)
        monkeypatch.setattr(PatternCheckPipeline, "peek", classmethod(lambda cls, cid: _Worker(result)))
        return runtime
    return make


def test_the_bar_says_what_the_check_is_doing(check):
    runtime = check()
    assert bar_parts("m1") == ["PATTERN CHECK", "GOOD", "MATCH 98%", "38ms"]
    assert bar_parts("elsewhere") == ["PATTERN CHECK", STATUS_NO_LINE]
    runtime.enabled = False
    assert bar_parts("m1") == ["PATTERN CHECK", STATUS_STOPPED]
    check(ready=False)
    assert bar_parts("m1") == ["PATTERN CHECK", "NO REFERENCE"]
    check(count_mode="trigger")
    assert bar_parts("m1") == ["PATTERN CHECK", STATUS_WAITING_TRIGGER]
    check(result=DEFECT)
    assert bar_parts("m1") == ["PATTERN CHECK", "REJECT", "Cap: defect 48 px", "38ms"]
    check(result=NOT_FOUND)
    assert bar_parts("m1") == ["PATTERN CHECK", "REJECT", "Label: not found", "38ms"]
    check(result=MISMATCH)
    assert bar_parts("m1") == ["PATTERN CHECK", "REJECT", "Cap: match 20%", "38ms"]
    check(result=ERROR)
    # The video draws plain ASCII only: no part has a character OpenCV cannot draw.
    assert bar_parts("m1") == ["PATTERN CHECK", "CANNOT CHECK", "the reference set ref-0a1b2c3d is missing"]
    check(result=DEFECT)
    assert all(part.isascii() for part in bar_parts("m1"))


def test_checks_are_drawn_green_and_red_with_their_spots(check):
    check(result=DEFECT)
    img = PatternCheckPipeline.draw_checks("m1", np.full((480, 640, 3), 128, np.uint8))
    assert tuple(img[150, 100]) == (0, 200, 0)          # Label's left edge: it passes
    assert tuple(img[170, 290]) == (0, 0, 230)          # Cap's right edge: its defect is too big
    assert tuple(img[165, 260]) == (0, 0, 255)          # the spot's outline


def test_a_stopped_line_draws_no_old_result(check):
    runtime = check(result=DEFECT)
    runtime.enabled = False
    img = PatternCheckPipeline.draw_checks("m1", np.full((480, 640, 3), 128, np.uint8))
    assert tuple(img[150, 100]) == (128, 128, 128) and tuple(img[165, 260]) == (128, 128, 128)


def test_a_triggered_check_draws_its_result_on_its_picture_not_the_video(check):
    check(result=DEFECT, count_mode="trigger")
    img = PatternCheckPipeline.draw_checks("m1", np.full((480, 640, 3), 128, np.uint8))
    assert tuple(img[150, 100]) == (128, 128, 128)
    jpeg = render_capture(np.full((480, 640, 3), 128, np.uint8), DEFECT)
    assert jpeg[:2] == b"\xff\xd8"


def test_render_capture_never_raises():
    assert render_capture(None, GOOD) is None
    assert render_capture(np.zeros((48, 64), np.uint8), GOOD)[:2] == b"\xff\xd8"     # a grey picture
    assert render_capture("not a picture", GOOD) is None


def test_the_video_says_train_it_again(check):
    """Review: on One picture per trigger the bar only said WAITING FOR A TRIGGER, whatever the last check said."""
    from app.engines.pattern_check import error_result

    size = error_result("Picture is 640 x 480, trained on 1280 x 960: train it again", 640, 480)
    check(result=size, count_mode="trigger")
    assert bar_parts("m1") == ["PATTERN CHECK", "CANNOT CHECK", "Picture is 640 x 480, trained on 1280 x 960: train it again"]
    check(result=GOOD, count_mode="trigger")
    assert bar_parts("m1") == ["PATTERN CHECK", STATUS_WAITING_TRIGGER]
    plain = np.full((480, 640, 3), 128, np.uint8)
    import cv2
    drawn = cv2.imdecode(np.frombuffer(render_capture(plain, size), np.uint8), cv2.IMREAD_COLOR)
    assert np.abs(drawn[:20].astype(int) - 128).mean() > 20          # the trigger picture carries the error bar
