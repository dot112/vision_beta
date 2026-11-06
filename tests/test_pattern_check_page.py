"""Line setup's Pattern and checks page (assets/pattern_check.js) and the Pattern check job in Line setup
(assets/camera_pages.js, assets/production_lines.js).

The page's pure parts run in node (module.exports), as text_reader.js does: the markup, the settings it sends,
the box a drag makes, the mask brush, the suggested thresholds after Train and when to train again.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "assets" / "pattern_check.js"
PAGE = ROOT / "dashboard.html"
LINES_JS = ROOT / "assets" / "production_lines.js"


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=ROOT)
    assert run.returncode == 0, run.stderr[:800]
    return json.loads(run.stdout)


def _api(expression: str):
    return _node(f"""
        const api = require("./assets/pattern_check.js");
        process.stdout.write(JSON.stringify({expression}));
    """)


def _pages(expression: str):
    return _node(f"""
        const api = require("./assets/camera_pages.js");
        process.stdout.write(JSON.stringify({expression}));
    """)


def test_script_is_valid_and_loaded():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    assert subprocess.run([node, "--check", str(SCRIPT)], capture_output=True).returncode == 0
    page = PAGE.read_text(encoding="utf-8")
    tag = '<script src="/assets/pattern_check.js?v=1.0.4"></script>'
    assert tag in page and page.index(tag) < page.index("/assets/production_lines.js")


def test_box_from_a_drag():
    assert _api("api.boxFrom(500, 300, 100, 100, 640, 480, 1280, 960)") == [0.1563, 0.2083, 0.7813, 0.625]
    # At least 16 px of the real picture each way.
    assert _api("api.boxFrom(10, 10, 12, 12, 640, 480, 1280, 960)") == [0.0156, 0.0208, 0.0281, 0.0375]
    # The search area: at least 0.02 too; a drag past the picture's edge stays inside it.
    assert _api("api.boxFrom(-50, 10, 5, 12, 640, 480, 1280, 960, 0.02)") == [0, 0.0208, 0.02, 0.0408]


def test_paint_and_erase():
    assert _node("""
        const api = require("./assets/pattern_check.js");
        const mask = new Uint8Array(20 * 20);
        api.paint(mask, 20, 20, 5, 5, 3, true);
        const painted = mask.reduce((a, b) => a + b, 0);
        api.paint(mask, 20, 20, 5, 5, 3, false);
        api.paint(mask, 20, 20, -2, 0, 3, true);       // half outside: no error
        process.stdout.write(JSON.stringify([painted, mask[0] === 1, mask[19 * 20 + 19] === 0]));
    """) == [29, True, True]


def test_a_fast_brush_stroke_paints_a_continuous_line():
    """Owner 2026-11-05 ("I can't do the mask"): the brush painted a dab only where the pointer was seen, so a fast
    stroke left a dotted line with gaps; it paints the whole line between two pointer positions now."""
    assert _node("""
        const api = require("./assets/pattern_check.js");
        const mask = new Uint8Array(40 * 20);
        api.paintLine(mask, 40, 20, 2, 10, 38, 10, 2, true);
        const row = [...mask.slice(10 * 40, 11 * 40)];
        const gaps = row.slice(2, 39).filter((v) => !v).length;
        api.paintLine(mask, 40, 20, 2, 10, 38, 10, 2, false);
        process.stdout.write(JSON.stringify([gaps, mask.reduce((a, b) => a + b, 0)]));
    """) == [0, 0]


def test_a_box_masks_a_whole_area():
    """A Box size: a drag masks (or clears) the whole rectangle, for a date or a shiny area."""
    assert _node("""
        const api = require("./assets/pattern_check.js");
        const mask = new Uint8Array(20 * 20);
        api.paintBox(mask, 20, 20, 15, 12, 3, 4, true);
        const box = mask.reduce((a, b) => a + b, 0);
        api.paintBox(mask, 20, 20, 3, 4, 8, 12, false);
        const left = mask.reduce((a, b) => a + b, 0);
        const edge = new Uint8Array(20 * 20);
        api.paintBox(edge, 20, 20, -5, -5, 2, 30, true);      // past the picture's edge: no error
        process.stdout.write(JSON.stringify([box, left, edge.reduce((a, b) => a + b, 0)]));
    """) == [13 * 9, 7 * 9, 3 * 20]
    html = _api('api.foldHtml(null, {readOnly: false})')
    assert 'data-act="pc-size" data-size="Box"' in html


def test_an_area_check_is_named_on_the_page():
    """An area check has no shape to find: the result line shows its defect only, and Train says which are."""
    line = _api('api.resultLine({result: "reject", reason: "pattern_defect", ms: 41.6, checks: ['
                '{name: "Main", score: 99, defect_px: 0, area: false}, {name: "Check2", score: 100, defect_px: 57, area: true}]})')
    assert line == "REJECT (pattern defect) · Main 99% 0 px · Check2 area 57 px · 42 ms"
    note = _api('api.trainNote({samples: [1, 2, 3], note: null, area: [false, true, false]}, ["Main", "Blank", "Cap"])')
    assert note == ("Thresholds suggested from 3 samples. Save to keep them. Blank: an area check"
                    " (no shape to find), only its marks and colour count.")
    assert _api('api.trainNote({samples: [1], note: "Add more.", area: [false]}, ["Main"])') == "Add more."


def test_values_to_settings():
    assert _api('api.matchFromValues({reference_id: "ref-0a1b2c3d", search_area: null, angle: "15", '
                'checks: [{name: " Label ", min_score: "85", max_defect_px: "40", colour: "grey", sensitivity: "normal"},'
                ' {name: "Cap", min_score: "", max_defect_px: "", colour: "colour", sensitivity: "high"}]})') == {
        "reference_id": "ref-0a1b2c3d", "search_area": None, "angle": 15,
        "checks": [{"name": "Label", "min_score": 85, "max_defect_px": 40, "colour": False, "sensitivity": "normal"},
                   {"name": "Cap", "min_score": 80, "max_defect_px": 30, "colour": True, "sensitivity": "high"}]}
    assert _api('api.matchFromValues({reference_id: "", search_area: [0.1, 0.1, 0.9, 0.9], angle: "", checks: []})') == {
        "reference_id": None, "search_area": [0.1, 0.1, 0.9, 0.9], "angle": 15, "checks": []}


def test_suggestions_fill_min_match_and_max_defect_only():
    assert _api('api.applySuggestions([{name: "Label", min_score: 80, max_defect_px: 30, colour: true, sensitivity: "high"}],'
                ' [{min_score: 92, max_defect_px: 14}])') == [
        {"name": "Label", "min_score": 92, "max_defect_px": 14, "colour": True, "sensitivity": "high"}]


def test_when_to_train_again():
    assert _api('api.trainAgain([{colour: false, sensitivity: "normal"}], [{colour: true, sensitivity: "normal"}])') is True
    assert _api('api.trainAgain([{colour: false, sensitivity: "normal"}], [{colour: false, sensitivity: "normal"}])') is False
    assert _api('api.trainAgain(null, [{colour: false, sensitivity: "low"}])') is False


def test_new_check_names():
    assert _api('api.newCheckName(["Main"])') == "Check2" and _api('api.newCheckName(["Main", "Check2"])') == "Check3"
    assert _api('api.newCheckName([])') == "Main"


def test_the_page_has_help_only_where_needed():
    html = _api('api.foldHtml({reference_id: "ref-0a1b2c3d", angle: 20, checks: [1, 2, 3, 4, 5].map((i) => ({name: "C" + i}))},'
                ' {readOnly: false})')
    assert html.count("data-help=") == 2 and html.count("data-pc-check=") == 4
    assert 'value="ref-0a1b2c3d"' in html and 'data-f="pc_angle"' in html and 'value="20"' in html
    for act in ("pc-add", "pc-train", "pc-edit", "pc-start-over"):
        assert f'data-lock type="button" class="btn-action' in html and f'data-act="{act}"' in html
    new = _api('api.foldHtml(null, {readOnly: false})')
    assert new.count("data-pc-check=") == 1 and 'value="Main"' in new
    assert _api('api.foldHtml(null, {readOnly: true})').count("disabled") >= 4


def test_camera_pages_for_a_pattern_check():
    kind = "{pattern: true}"
    assert _pages(f"api.pagesFor({kind})") == ["camera", "job", "counting", "pattern", "image"]
    assert _pages(f'api.pageTitle("counting", {kind})') == "When to check"
    assert _pages(f'api.pageTitle("pattern", {kind})') == "Pattern and checks"
    assert _pages(f"api.startPage({kind})") == "pattern" and _pages('api.pageForProblem("no_reference")') == "pattern"
    assert _pages(f'api.summary({{index: 1, type: "IP", kind: {kind}, countMode: "trigger"}})') == "Camera 2 · IP · One picture per trigger"
    assert _pages(f'api.summary({{index: 1, type: "IP", kind: {kind}, countMode: "track"}})') == "Camera 2 · IP · Shows pattern"
    assert 'data-page="pattern"' in _pages(f'api.menuHtml({kind}, "pattern")')
    assert _pages('api.pagesFor({vision: true})') == ["camera", "job", "model", "counting", "image"]


def test_line_setup_has_the_pattern_check_job():
    js = LINES_JS.read_text(encoding="utf-8")
    assert 'pattern: { label: "Pattern check", short: "Pattern check", role: "match"' in js
    assert 'cam.role === "match" ? "pattern"' in js
    assert "match: PatternCheck.readCard(card)" in js
    assert "train its reference on Pattern and checks." in js and 'pageForProblem("no_reference")' in js
    assert "PatternCheck.bind(card," in js and js.count("PatternCheck.wake(") >= 5


def test_a_redrawn_row_keeps_its_colour_choice():
    """The rows are drawn again from the form's own values ("grey" / "colour"): Grey must stay Grey (live check)."""
    html = _api('api.foldHtml({checks: [{name: "A", colour: "grey"}, {name: "B", colour: "colour"}, {name: "C", colour: true},'
                ' {name: "D", colour: false}]}, {readOnly: false})')
    assert html.count('value="grey" selected') == 2 and html.count('value="colour" selected') == 2


def test_the_first_reference_box_is_drawn_and_opens_the_training_picture():
    """Review I1: the first drag on a held picture drew nothing and never showed the training picture."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, str(ROOT / "tests" / "pattern_first_box.js"), str(SCRIPT)], capture_output=True, text=True,
                         encoding="utf-8", timeout=30, cwd=ROOT)
    assert run.returncode == 0, run.stderr[:800]
    seen = json.loads(run.stdout.strip().splitlines()[-1])
    assert seen["serverDraftStarted"] and seen["boxesDrawnDuringDrag"] > 0 and seen["boxesDrawnAfter"] > 0, seen
    assert seen["trainingButtonHidden"] is True and seen["note"].startswith("Drag on the picture"), seen
