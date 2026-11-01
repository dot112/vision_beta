"""The ROI box on a camera's picture (assets/camera_settings.js): drawn by dragging on the picture, then moved
by dragging inside it, resized by its corners, and removed by its x. The form runs in node with a stand-in
window, as tests/test_trigger_card_markup.py does.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORM = ROOT / "assets" / "camera_settings.js"
CSS = ROOT / "assets" / "dashboard.css"


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = f"""
        global.window = {{}};
        global.document = {{ hidden: false }};
        global.Event = class {{ constructor(type) {{ this.type = type; }} }};
        require({json.dumps(str(FORM))});
        const form = window.CameraSettingsForm;
        {code}
    """
    run = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr[:800]
    return json.loads(run.stdout)


def _drag(mode, box, start, end):
    return _node(f"process.stdout.write(JSON.stringify(form.roiDrag({json.dumps(mode)}, {json.dumps(box)}, {json.dumps(start)}, {json.dumps(end)})));")


BOX = {"x": 10, "y": 10, "w": 30, "h": 20}


def test_drawing_a_box_from_any_corner():
    assert _drag("draw", None, {"x": 50, "y": 50}, {"x": 20, "y": 30}) == {"x": 20, "y": 30, "w": 30, "h": 20}


def test_moving_keeps_the_size_and_stays_in_the_picture():
    assert _drag("move", BOX, {"x": 20, "y": 20}, {"x": 30, "y": 25}) == {"x": 20, "y": 15, "w": 30, "h": 20}
    assert _drag("move", BOX, {"x": 20, "y": 20}, {"x": 100, "y": 100}) == {"x": 70, "y": 80, "w": 30, "h": 20}
    assert _drag("move", BOX, {"x": 20, "y": 20}, {"x": 0, "y": 0}) == {"x": 0, "y": 0, "w": 30, "h": 20}


def test_a_corner_resizes_and_the_opposite_corner_stays():
    assert _drag("se", BOX, {"x": 40, "y": 30}, {"x": 60, "y": 50}) == {"x": 10, "y": 10, "w": 50, "h": 40}
    assert _drag("ne", BOX, {"x": 40, "y": 10}, {"x": 50, "y": 0}) == {"x": 10, "y": 0, "w": 40, "h": 30}
    assert _drag("sw", BOX, {"x": 10, "y": 30}, {"x": 0, "y": 40}) == {"x": 0, "y": 10, "w": 40, "h": 30}
    # Past the opposite corner: the box keeps a size of at least 3 % of the picture.
    assert _drag("nw", BOX, {"x": 10, "y": 10}, {"x": 60, "y": 60}) == {"x": 37, "y": 27, "w": 3, "h": 3}


REMOVE = """
const inputs = { roi_enabled: { checked: true }, roi_x: { value: "10", disabled: %(locked)s }, roi_y: { value: "10" },
                 roi_w: { value: "30" }, roi_h: { value: "20" } };
const box = { hidden: false, style: {} };
const del = { disabled: %(level1)s };
const events = [];
const root = {
    querySelector: (q) => { const m = q.match(/data-cs="(\\w+)"/); if (m) return inputs[m[1]] || null;
                            return ({ ".cs-roi-box": box, ".cs-roi-fields": { classList: { toggle() {} } }, ".roi-del": del })[q] || null; },
    dispatchEvent: (e) => events.push(e.type),
};
form.removeROI(root);
process.stdout.write(JSON.stringify({ on: inputs.roi_enabled.checked, roi: ["x", "y", "w", "h"].map((k) => inputs["roi_" + k].value),
                                      boxHidden: box.hidden, events }));
"""


def test_the_x_removes_the_roi():
    out = _node(REMOVE % {"locked": "false", "level1": "false"})
    assert out == {"on": False, "roi": ["0", "0", "100", "100"], "boxHidden": True, "events": ["change"]}


def test_a_locked_or_read_only_roi_is_not_removed():
    for locked, level1 in (("true", "false"), ("false", "true")):
        out = _node(REMOVE % {"locked": locked, "level1": level1})
        assert out == {"on": True, "roi": ["10", "10", "30", "20"], "boxHidden": False, "events": []}


def test_the_box_has_four_corners_and_an_x():
    form = FORM.read_text(encoding="utf-8")
    for corner in ("nw", "ne", "sw", "se"):
        assert f'<span class="roi-handle roi-{corner}" data-roi="{corner}"></span>' in form
    assert re.search(r'<button data-lock type="button" class="roi-del restricted-l2" title="Remove the ROI" aria-label="Remove the ROI">', form)
    css = CSS.read_text(encoding="utf-8")
    box = re.search(r"\.roi-box \{([^}]*)\}", css).group(1)
    assert "pointer-events: none" not in box and "cursor: move" in box
    assert re.search(r"\.roi-handle \{[^}]*cursor:", css) and ".roi-del {" in css
