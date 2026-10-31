"""The Line dashboard's Latest text reads panel and the read text on the Records page.

The rows and the records cell come from assets/text_reader.js and run in node; production_lines.js
shows the panel for a line with a text reader and refreshes it with the code reads.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_text_reader_card import _api

ROOT = Path(__file__).resolve().parents[1]
LINES_JS = ROOT / "assets" / "production_lines.js"

SHOWN = {"timestamp": "2026-10-31T10:00:00+00:00", "camera_id": "c1", "status": "good", "result": None, "reason": None,
         "track_id": None, "shown_only": True, "fields": [{"name": "Lot", "text": "LOT A12345", "fits": True}]}
REJECTED = {"timestamp": "2026-10-31T10:00:05+00:00", "camera_id": "c2", "status": "wrong", "result": "reject",
            "reason": "text_wrong", "track_id": 7, "shown_only": False,
            "fields": [{"name": "Lot", "text": "L0T 1234", "fits": False}, {"name": "Exp", "text": None, "fits": False}]}
NAMES = '{name: (id) => (id === "c1" ? "Printer cam" : id)}'


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace(" :", ":").strip()


def _rows(reads):
    html = _api(f"api.readsRowsHtml({json.dumps(reads)}, {NAMES})")
    return html, [_text(row) for row in re.split(r"<tr[^>]*>", html)[1:]]


def test_a_shown_read_and_a_triggered_reject():
    _, (shown, rejected) = _rows([SHOWN, REJECTED])
    assert "Printer cam" in shown and "Lot: LOT A12345 fits" in shown and "shown only" in shown
    assert "c2" in rejected and "Lot: L0T 1234 does not fit" in rejected and "Exp: no text" in rejected
    assert "Rejected" in rejected and "text does not fit" in rejected and "product #7" in rejected


def test_no_reads_yet_and_a_good_product():
    _, rows = _rows([])
    assert rows == ["No reads yet."]
    _, (good,) = _rows([{**REJECTED, "result": "good", "reason": None, "status": "good",
                          "fields": [{"name": "Lot", "text": "LOT A12345", "fits": True}]}])
    assert "Good" in good and "Rejected" not in good


def test_text_is_escaped():
    html, _ = _rows([{**SHOWN, "camera_id": "<i>", "fields": [{"name": "Lot", "text": "<b>x</b>", "fits": False}]}])
    assert "<b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html and "&lt;i&gt;" in html


def test_the_panel_has_its_table_body():
    html = _api("api.readsPanelHtml()")
    assert 'id="plTextBody"' in html and "Latest text reads" in html


def test_the_records_cell_shows_the_text():
    assert _text(_api('api.recordText({text: {Lot: "A12345", Exp: "12/03/2027"}, text_status: "good"})')) == \
        "Lot: A12345 · Exp: 12/03/2027"
    assert "does not fit" in _text(_api('api.recordText({text: {Lot: "A1"}, text_status: "wrong"})'))
    assert _api("[api.recordText(null), api.recordText({}), api.recordText({text: {}})]") == ["", "", ""]
    assert "&lt;b&gt;" in _api('api.recordText({text: {Lot: "<b>"}})')


def test_the_line_dashboard_and_records_use_them():
    script = LINES_JS.read_text(encoding="utf-8")
    for reason, label in (("text_wrong", "text does not fit"), ("no_text", "no text"), ("reader_error", "text reader error")):
        assert f'{reason}: "{label}"' in script
    assert "Text reader · ${" in script and '"one picture per trigger" : "reads continuously"' in script
    assert "TextReader.readsPanelHtml()" in script and "/ocr/recent?limit=25" in script
    assert "TextReader.readsRowsHtml(" in script and "TextReader.recordText(r.details)" in script
