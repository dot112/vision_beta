"""What a pattern check found goes with its product: the product's message and PLC event (match,
match_status), the values of output cards, messages, only-when rules and formulas (match.score,
match.<check>.score, match.<check>.defect, match_status), the product record, and the export's pattern
column."""
from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db.models.production_record import ProductRecord
from app.engines.pattern_check import match_values
from app.services import formula
from app.services.event_values import values_for
from app.services.production_records_service import build_row
from app.services.records_export import csv_header, csv_rows
from app.services.trigger_inspection import trigger_camera
from app.services.vision_service import CameraStreamPipeline
from tests.pattern_helpers import DEFECT, GOOD
from tests.test_card_values_js import _node
from tests.test_joined_cameras import _joined, _line, _run, lines  # noqa: F401  (a fixture)
from tests.test_pattern_check_line import _checker, _trigger_check, match_workers  # noqa: F401  (a fixture)
from tests.test_trigger_inspection import _trigger_cam, workers  # noqa: F401  (a fixture)

MATCH = {"score": 97, "status": "defect", "checks": {"Label": {"score": 97, "defect_px": 0},
                                                      "Cap": {"score": 92, "defect_px": 48}}}


def test_values_of_a_pattern_product():
    values = values_for({"result": "reject", "reject_reason": "pattern_defect", "match": MATCH, "match_status": "defect"})
    assert (values["match.score"], values["match.Cap.score"], values["match.Cap.defect"], values["match_status"]) == \
        (97, 92, 48, "defect")
    assert values["match.Label.defect"] == 0 and "match.Neck.score" not in values
    error = values_for({"match": {"status": "error"}, "match_status": "error"})
    assert "match.score" not in error and error["match_status"] == "error"


def test_formulas_take_match_values():
    assert formula.parse("match.Cap.defect > 40").evaluate({"match.Cap.defect": 48}) is True
    assert formula.parse('if(match_status == "good", match.score, 0)').evaluate({"match_status": "good", "match.score": 97}) == 97
    assert formula.parse("match.Cap.score + 1").names == frozenset({"match.Cap.score"})
    for bad in ("match.Cap", "match.Cap.width", "text.Lot.score", "match", "match.1x.score"):
        with pytest.raises(formula.FormulaError):
            formula.parse(bad)


def test_only_when_rules_take_match_values():
    from app.services.rules import is_value_name, normalize_only_when, rules_pass

    assert is_value_name("match.score") and is_value_name("match.Cap.defect") and is_value_name("match_status")
    assert not is_value_name("match.Cap") and not is_value_name("match.Cap.width")
    rules = normalize_only_when({"match": "all", "rules": [
        {"left": "match_status", "op": "=", "right": "defect", "right_kind": "constant"},
        {"left": "match.Cap.defect", "op": ">", "right": "40", "right_kind": "constant"}]}, "Card 1")
    assert rules_pass(rules, values_for({"match": MATCH, "match_status": "defect"}))
    assert not rules_pass(rules, values_for({"match": {**MATCH, "checks": {"Cap": {"score": 92, "defect_px": 4}}},
                                             "match_status": "defect"}))


def test_a_send_card_sends_the_pattern_check():
    from app.routes.v1.send_actions import FIELD_LABELS
    from app.services.line_config import TEMPLATE_FIELDS, parse_template
    from app.services.send_dispatcher_service import MESSAGE_FIELDS, build_message, render_template

    assert parse_template("{match.score};{MATCH.Cap.defect};{match_status}") == (
        (True, "match.score"), (False, ";"), (True, "match.Cap.defect"), (False, ";"), (True, "match_status"))
    with pytest.raises(ValueError, match="is not a placeholder"):
        parse_template("{match.Cap}")
    assert "match_status" in TEMPLATE_FIELDS
    payload = {"event": "WIRELINE_OBJECT_CROSSED", "match": MATCH, "match_status": "defect"}
    assert render_template("{match.score}|{match.Cap.defect}|{match.Neck.score}|{match_status}", payload) == "97|48||defect"
    assert MESSAGE_FIELDS["pattern"] == ("match", "match_status") and "pattern" in dict(FIELD_LABELS)
    assert build_message({"fields": ["pattern"]}, payload) == {"match": MATCH, "match_status": "defect"}
    page = (Path(__file__).resolve().parents[1] / "dashboard.html").read_text(encoding="utf-8")
    assert "/^match\\.(score|[a-z][a-z0-9_]{0,31}\\.(score|defect))$/.test(name)" in page


def test_a_record_keeps_the_scores_and_the_export_shows_them():
    row = build_row({"is_defect": True, "class_name": "pattern defect", "match": MATCH, "match_status": "defect",
                     "camera_id": "m1", "line_id": "line-1"}, "product", False)
    assert row["details"]["match"] == MATCH
    header = next(csv.reader(io.StringIO(csv_header("UTC").lstrip("﻿"))))
    assert header.index("pattern") == header.index("text") + 1

    def record(details):
        return ProductRecord(id=1, recorded_at=datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc), kind="product",
                             counted=False, details=details)

    rows = list(csv.reader(io.StringIO(csv_rows([record(row["details"]), record(None),
                                                 record({"match": {"status": "error"}})], timezone.utc))))
    column = header.index("pattern")
    assert [r[column] for r in rows] == ["Label 97% 0px; Cap 92% 48px", "", "error"]


def test_own_and_joined_products_carry_match(lines, match_workers, workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(), _checker("m1"), {**_checker("m2"), **_joined("m2", 0, 500, "reject")}])
    for camera_id in ("vis", "m1", "m2"):
        fake_camera(camera_id)

    async def steps():
        await _trigger_check(runtime, "m1", GOOD)
        task = asyncio.create_task(trigger_camera(runtime, "vis", timeout=1.0))
        await asyncio.sleep(0.03)
        CameraStreamPipeline.get_worker("vis").process("bottle")
        await task
        await _trigger_check(runtime, "m2", DEFECT)
        await asyncio.sleep(0.1)

    _run(steps)
    own = next(p for p, _ in runtime.sent if p.get("camera_id") == "m1")
    assert own["match"] == match_values(GOOD) and own["match_status"] == "good"
    joined, event = next((p, e) for p, e in runtime.sent if p.get("camera_id") == "vis")
    assert joined["match"]["checks"]["Cap"] == {"score": 92, "defect_px": 48} and joined["match_status"] == "defect"
    assert event["match_status"] == "defect"


def test_the_card_dialogs_list_the_pattern_check_values():
    cameras = ("[{camera_id: 'v', role: 'vision'}, {camera_id: 'm1', role: 'match', match: {checks: [{name: 'Label'}, {name: 'Cap'}]}},"
               " {camera_id: 'm2', role: 'match', match: {checks: [{name: 'cap'}]}}]")
    names = [c["name"] for c in _node(f"C.matchChoices({cameras})")]
    assert names == ["match_status", "match.score", "match.Label.score", "match.Label.defect", "match.Cap.score",
                     "match.Cap.defect"]
    assert _node("C.matchChoices([{role: 'vision'}])") == []
    chosen = _node(f"C.valueChoices({{fields: [], inputs: [], texts: [], matches: C.matchChoices({cameras})}}).map(c => c.name)")
    assert chosen[-6:] == names
    page = (Path(__file__).resolve().parents[1] / "dashboard.html").read_text(encoding="utf-8")
    assert "matches: CardValues.matchChoices(window.ProductionLines ? ProductionLines.lineCameras() : [])" in page
