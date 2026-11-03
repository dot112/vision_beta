"""The pattern check on the Line dashboard (the Latest pattern checks panel, the camera's tile) and on the
Records page (each check's Match % and biggest defect with the product). The renderers run in node."""
from __future__ import annotations

from pathlib import Path

from tests.test_pattern_check_page import _api

LINES_JS = Path(__file__).resolve().parents[1] / "assets" / "production_lines.js"
CHECKS = ('[{timestamp: 1762160000000, camera_id: "m1", score: 97, status: "good", result: "good", reason: null, track_id: 4,'
          ' checks: [{name: "Label", score: 97, defect_px: 0, fits: true}, {name: "Cap", score: 92, defect_px: 4, fits: true}]},'
          ' {timestamp: 1762160001000, camera_id: "m1", score: 96, status: "defect", result: "reject", reason: "pattern_defect",'
          ' track_id: 5, checks: [{name: "<b>Label</b>", score: 96, defect_px: 48, fits: false}]}]')


def test_the_panel():
    html = _api("api.checksPanelHtml()")
    assert 'id="plPatternBody"' in html and "Latest pattern checks" in html


def test_the_rows():
    rows = _api(f'api.checksRowsHtml({CHECKS}, {{name: (id) => "Bottle camera", reasons: {{pattern_defect: "pattern defect"}}}})')
    assert rows.count("<tr>") == 2 and "Bottle camera" in rows
    assert "Label 97% · 0 px" in rows and "Cap 92% · 4 px" in rows and "product #4" in rows
    assert "Good" in rows and "Rejected" in rows and "pattern defect" in rows
    assert "<b>Label</b>" not in rows and "&lt;b&gt;Label&lt;/b&gt;" in rows
    assert "pc-wrong" in rows                           # a check that does not pass is marked
    assert "No checks yet." in _api("api.checksRowsHtml([], {})")


def test_a_records_cell():
    cell = _api('api.recordText({match: {score: 97, status: "defect", checks: {Label: {score: 97, defect_px: 0}, Cap: {score: 92, defect_px: 48}}}})')
    assert "Label 97% 0 px" in cell and "Cap 92% 48 px" in cell and "pc-record" in cell
    assert "pattern check error" in _api('api.recordText({match: {status: "error"}})')
    assert _api("api.recordText({})") == "" and _api("api.recordText(null)") == ""


def test_the_line_dashboard_and_records_use_them():
    js = LINES_JS.read_text(encoding="utf-8")
    for reason, label in (("pattern_not_found", "pattern not found"), ("pattern_mismatch", "pattern mismatch"),
                          ("pattern_defect", "pattern defect"), ("pattern_error", "pattern check error")):
        assert f'{reason}: "{label}"' in js
    assert 'if (cam.role === "match")' in js and "Pattern check · ${triggered ? \"one picture per trigger\" : \"shows the pattern\"}" in js
    assert 'const hasPattern = cams.some((c) => c.role === "match");' in js and "PatternCheck.checksPanelHtml()" in js
    assert "/match/recent?limit=25" in js and "PatternCheck.checksRowsHtml(" in js
    assert "PatternCheck.recordText(r.details)" in js
