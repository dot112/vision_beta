"""The Line setup form's markup: its grid of line settings closes every box it opens.

An unclosed box there pulls the handshake section, the warnings and the Save / Start / Reset
buttons into the grid, where each squeezes into one narrow column.

The camera cards: a small card per camera (Settings, Delete), and the camera's page with its menu.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINES = ROOT / "assets" / "production_lines.js"


def _function(script: str, name: str) -> str:
    found = re.search(rf"\n    (?:async )?function {name}\(.*?\n    \}}\n", script, re.S)
    assert found, f"function {name} is not in production_lines.js"
    return found.group(0)


def test_the_card_has_settings_and_delete_and_no_fold_buttons():
    script = LINES.read_text(encoding="utf-8")
    assert 'data-act="settings">Settings</button>' in script
    assert re.search(r'<button data-lock type="button" class="btn-action btn-red restricted-l2" data-act="remove"[^>]*>Delete</button>', script)
    assert "pl-fold-head" not in script and "openFolds(" not in script and "pl_card_folds" not in script
    assert 'pageHtml("image", "Image and video"' in script and 'data-fold="video"' not in script
    assert "CameraPages.menuHtml(" in script and "CameraPages.summary(" in script


def test_the_text_reader_no_longer_listens_for_fold_buttons():
    assert "pl-fold-head" not in (ROOT / "assets" / "text_reader.js").read_text(encoding="utf-8")


def test_the_camera_page_opens_closes_and_comes_back():
    script = LINES.read_text(encoding="utf-8")
    for name in ("function openCameraPage(card, asked)", "function showCameraPage(card, page)", "function closeCameraPage()"):
        assert name in script
    assert "CameraPages.reopen(setup.openCamera, state.lineId" in script        # renderLineSetup draws it again
    assert "pl_card_page::" in script and "pl-page-mode" in script


def test_leaving_a_page_wakes_the_text_reader():
    script = LINES.read_text(encoding="utf-8")
    assert "TextReader.wake(" in _function(script, "closeCameraPage")
    assert "TextReader.wake(" in _function(script, "showCameraPage")


def test_reopen_follows_the_device():
    script = LINES.read_text(encoding="utf-8")
    assert re.search(r"setup\.openCamera && setup\.openCamera\.id === previous\)\s*setup\.openCamera\.id = ", script)


def test_the_ways_into_a_camera_page():
    script = LINES.read_text(encoding="utf-8")
    assert "openCameraPage(added, CameraPages.startPage(jobKind(job)))" in script
    for problem in ("no_model", "no_list", "no_yolo", "no_format"):
        assert f'CameraPages.pageForProblem("{problem}")' in script
    assert 'openCameraPage(card, "image")' in _function(script, "focusCard")


def test_a_second_save_waits_for_the_first():
    """Review I1: the camera page's Save stays clickable while a save runs (only #plSaveSetup was greyed out)."""
    body = _function(LINES.read_text(encoding="utf-8"), "saveLineSetup")
    assert body.index("if (setup.saving) return;") < body.index("await api(")
    assert re.search(r"finally \{\s*setup\.saving = false;", body)


def test_camera_page_notes_do_not_point_below():
    """Review: on a camera page nothing is below it; only the line panel's note may say Inputs below."""
    script = LINES.read_text(encoding="utf-8")
    assert script.count("Inputs below") == 1 and "(Inputs below; apply a new input first)" in script
    assert script.count("under Inputs on Line setup") == 3


def test_back_names_the_line():
    """The spec's top bar: "← <line name>"."""
    script = LINES.read_text(encoding="utf-8")
    assert re.search(r'data-act="back"[^>]*>\$\{ICON\([^`]*?\)\}<span class="pl-back-line">\$\{esc\(', script)


def test_a_redraw_without_a_page_to_reopen_shows_the_cards():
    body = _function(LINES.read_text(encoding="utf-8"), "renderLineSetup")
    assert re.search(r'else \{\s*setup\.openCamera = null;\s*page\.classList\.remove\("pl-page-mode"\);', body)


def test_the_line_settings_grid_closes_every_box_it_opens():
    script = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    start = script.index('<div class="pl-form-grid">', script.index("Line setup"))
    assert "Line name</label>" in script[start:start + 400]
    end = script.index("${handshakeSetupHtml(d)}", start)
    grid = script[start:end]
    opened, closed = len(re.findall(r"<div[\s>]", grid)), grid.count("</div>")
    assert opened == closed, f"the grid opens {opened} <div> and closes {closed}"
    # The handshake, the warnings and the buttons come after the grid, not in it.
    after = script[end:script.index('<div class="pl-cam-grid"', end)]
    assert 'id="plSetupWarnings"' in after and 'class="pl-setup-actions"' in after


def test_the_line_settings_are_short_and_without_a_sync_box():
    script = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    start = script.index('<div class="pl-form-grid">', script.index("Line setup"))
    grid = script[start:script.index("${handshakeSetupHtml(d)}", start)]
    # Sync is saved on by itself wherever the line can pair: no box or text about it.
    assert "Products and codes" not in grid and "plSyncState" not in script
    # Each setting's label fits one line, so the fields sit level; the explanation goes in a note under it.
    labels = re.findall(r'<label class="form-label"[^>]*>([^<]*)</label>', grid)
    assert "Minimum frames/s" in labels
    assert all(len(label) <= 24 for label in labels), labels
    assert "0 = no alarm" in grid
