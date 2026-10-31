"""Line setup's Text reader card (assets/text_reader.js and its place in production_lines.js).

The card's pure parts run in node (module.exports), as data_backups.js does: the fold's markup, the
settings it sends, the format a reading suggests and the region a drag on the picture makes.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.engines.text_format import suggest_format

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "assets" / "text_reader.js"
PAGE = ROOT / "dashboard.html"
LINES_JS = ROOT / "assets" / "production_lines.js"
OPTS = {"models": ["small"], "newModel": "small", "yoloModels": [], "readOnly": False}


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=ROOT)
    assert run.returncode == 0, run.stderr[:800]
    return json.loads(run.stdout)


def _api(expression: str):
    return _node(f"""
        const api = require("./assets/text_reader.js");
        process.stdout.write(JSON.stringify({expression}));
    """)


def _fold(ocr, **opts):
    return _api(f"api.foldHtml({json.dumps(ocr)}, {json.dumps({**OPTS, **opts})})")


def _field(name, find="all", format="LOT A99999", **extra):
    return {"name": name, "find": find, "format": format, **extra}


def test_script_is_valid_and_loaded():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    assert subprocess.run([node, "--check", str(SCRIPT)], capture_output=True).returncode == 0
    page = PAGE.read_text(encoding="utf-8")
    tag = '<script src="/assets/text_reader.js?v=1.0.0"></script>'
    assert tag in page and page.index(tag) < page.index('<script src="/assets/production_lines.js')


def test_suggest_format_matches_the_server():
    readings = ["LOT 12345", "EXP 03/2027", " a1 ", "l0t-9z"]
    assert _api(f"{json.dumps(readings)}.map(api.suggestFormat)") == [suggest_format(r) for r in readings]


def test_region_from_a_drag():
    assert _api("api.regionFrom(500, 300, 100, 100, 1000, 500)") == [0.1, 0.2, 0.5, 0.6]
    assert _api("api.regionFrom(-50, 10, 5, 12, 1000, 500)") == [0, 0.02, 0.02, 0.04]
    assert _api("api.regionFrom(995, 498, 1200, 600, 1000, 500)") == [0.98, 0.98, 1, 1]


def test_values_to_settings():
    assert _api('api.ocrFromValues({model: "tiny", locator: "", upside_down: false, join_dots: "0", ink: "dark", '
                'min_confidence: "0.5", fields: [{name: " Lot ", find: "region", region: [0.1,0.2,0.5,0.6], '
                'format: " LOT A99999 ", yolo_class: "x"}]})') == {
        "model": "tiny", "locator_model_id": None, "upside_down": False, "join_dots": 0, "ink": "dark", "min_confidence": 0.5,
        "fields": [{"name": "Lot", "find": "region", "region": [0.1, 0.2, 0.5, 0.6], "format": "LOT A99999"}]}
    sent = _api('api.ocrFromValues({model: "small", locator: "m1", upside_down: true, join_dots: "7", ink: "light", '
                'min_confidence: "", fields: [{name: "Lot", find: "yolo", yolo_class: "dateBox", format: "LOT A99999"}, '
                '{name: "Exp", find: "region", region: null, format: "99/99"}]})')
    assert (sent["locator_model_id"], sent["upside_down"], sent["join_dots"], sent["ink"], sent["min_confidence"]) == \
        ("m1", True, 7, "light", 0.5)
    assert sent["fields"] == [{"name": "Lot", "find": "yolo", "yolo_class": "dateBox", "format": "LOT A99999"},
                              {"name": "Exp", "find": "region", "region": [0.25, 0.25, 0.75, 0.5], "format": "99/99"}]


def test_the_preview_reads_a_field_without_a_format_yet():
    """A new field has no format: the preview still reads (any character), so Read can fill it in."""
    entry = {"camera_id": "c1", "role": "ocr", "ocr": {"fields": [_field("Lot", format=""), _field("Exp", format="99/99")]}}
    sent = _api(f"api.previewSettings({json.dumps(entry)})")
    assert [f["format"] for f in sent["ocr"]["fields"]] == ["*", "99/99"] and entry["ocr"]["fields"][0]["format"] == ""
    assert _api('api.previewSettings({camera_id: "c1", role: "vision"})') == {"camera_id": "c1", "role": "vision"}


def test_the_fold_has_four_fields_at_most_and_help_only_where_needed():
    html = _fold({"fields": [_field(f"F{i}") for i in range(5)]})
    assert html.count("data-text-field=") == 4 and html.count("data-help=") == 2
    assert 'data-tf="format"' in html and 'data-f="ocr_join_dots"' in html and 'data-act="tr-add"' in html
    assert re.search(r'data-act="tr-add"[^>]*disabled', html)                     # four fields: no more
    for act in ("tr-live", "tr-hold", "tr-read"):                                  # they save nothing: not locked
        assert not re.search(rf'<button data-lock[^>]*data-act="{act}"', html)
    assert re.search(r'<button data-lock[^>]*data-act="tr-remove"', html)


def test_a_new_reader_gets_the_new_model_and_one_field():
    html = _fold(None, models=["tiny", "small"], newModel="tiny")
    assert 'value="tiny" selected' in html and html.count("data-text-field=") == 1
    saved = _fold({"model": "medium", "fields": [_field("Lot")]}, models=["tiny", "small"], newModel="tiny")
    assert 'value="medium" selected' in saved and "not on this server" in saved


def test_a_field_list_offers_the_locators_classes():
    yolo = [{"id": "m1", "name": "datebox", "version": "v1.0", "classes": ["dateBox", "datepage"]}]
    html = _fold({"locator_model_id": "m1", "fields": [_field("Lot", find="yolo", yolo_class="dateBox")]}, yoloModels=yolo)
    assert '<option value="dateBox" selected>' in html and '<option value="datepage" >' in html
    assert '<option value="m1" selected>' in html


def test_text_is_escaped():
    html = _fold({"fields": [_field('<b>"x"', format="<i>")]})
    assert "<b>" not in html and "&lt;b&gt;&quot;x&quot;" in html and "&lt;i&gt;" in html


def test_the_line_setup_page_has_the_text_reader_job():
    script = LINES_JS.read_text(encoding="utf-8")
    assert 'text: { label: "Text reader", short: "Text reader", role: "ocr"' in script
    assert 'cam.role === "ocr" ? "text"' in script
    assert "ocr: TextReader.readCard(card)" in script
    assert re.search(r'const START_JOBS = \[[^\]]*"text"', script)
    assert '["text", "Text fields"]' in script
