"""One set of value names for every event (app.services.event_values), and inputs that name their value.

The same names are used by written values, formulas, message templates and
only-when rules: result, class, code, product.<field>, input.<name>...
A name an event does not have is missing: it is not in the values at all.
"""
from __future__ import annotations

import pytest

import app.services.line_service as line_module
from app.services.card_triggers import line_state_event
from app.services.event_values import values_for
from app.services.line_config import normalize_plc_input, normalize_plc_inputs
from app.services.plc_input_service import PLCInputService
from app.services.product_fields import flat_product_values
from tests.test_plc_inputs import _line_with, _pass, _runtime, _save, _signal, _vision, acts, signals  # noqa: F401
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_joined_cameras import _run, lines  # noqa: F401  (a fixture)
from tests.test_production_records import _cross

WIDGET = {"code": "J-1", "name": "Widget", "list": "Juices", "list_id": "list-1", "fields": {"lane": 3, "recipe": "R-12"}}


def _values_line(lines):  # noqa: F811
    line = _save(name="Values", cameras=[_vision("top")])
    lines.apply_state()
    return line, lines.get(line["id"])


def test_a_product_event_gives_every_value(lines, monkeypatch):  # noqa: F811
    line, runtime = _values_line(lines)
    runtime.set_batch("B-7")
    monkeypatch.setattr(PLCInputService, "values", classmethod(lambda cls, line_id: {"speed": 12}))
    event = {"event_type": "crossing", "line_id": line["id"], "camera_id": "top", "result": "reject",
             "reject_reason": "vision_class", "good_count": 4, "reject_count": 2, "detected_classes": ["Apple"],
             "confidence": 0.91, "qr_code": "J-1", "qr_status": "known", "product": WIDGET}
    assert values_for(event) == {
        "result": "reject", "result_code": 2, "reject_reason": "vision_class", "class": "Apple", "confidence": 0.91,
        "code": "J-1", "code_status": "known", "good_count": 4, "reject_count": 2, "total_count": 6, "batch": "B-7",
        "line": "Values", "camera": "top", **flat_product_values(WIDGET), "input.speed": 12}


def test_a_line_event_has_the_lines_values_and_no_result(lines):  # noqa: F811
    line, runtime = _values_line(lines)
    assert values_for(line_state_event(line["id"], "Values", True)) == {
        "good_count": 0, "reject_count": 0, "total_count": 0, "line": "Values"}
    runtime.set_batch("B-8")
    assert values_for(line_state_event(line["id"], "Values", False))["batch"] == "B-8"


def test_a_good_product_has_no_reject_reason_and_an_unknown_line_has_no_line_values():
    values = values_for({"event_type": "crossing", "line_id": "no-such-line", "result": "good", "reject_reason": None,
                         "good_count": 1, "reject_count": 0, "class_name": "bottle"})
    assert values == {"result": "good", "result_code": 1, "class": "bottle", "good_count": 1, "reject_count": 0,
                      "total_count": 1}


def test_finish_crossing_puts_the_confidence_on_the_plc_event(lines):  # noqa: F811
    line, _ = _values_line(lines)
    runtime = _runtime(line)
    _cross(runtime.counter, "top")
    assert runtime.sent[0][1]["confidence"] == 0.87


def test_an_input_value_name_is_checked_and_unique():
    assert normalize_plc_input({"action": "start_line", "value_name": "speed"})["value_name"] == "speed"
    assert "value_name" not in normalize_plc_input({"action": "start_line", "value_name": ""})
    assert "value_name" not in normalize_plc_input({"action": "start_line"})
    for bad, word in (({"action": "start_line", "value_name": "1x"}, "value name"),
                      ({"action": "product_sensor", "data_type": "counter", "value_name": "n"}, "counter"),
                      ({"action": "start_line", "value_name": "previous"}, "previous"),
                      ({"action": "start_line", "value_name": "value"}, "value")):
        with pytest.raises(ValueError, match=word):
            normalize_plc_input(bad)
    with pytest.raises(ValueError, match="speed"):
        normalize_plc_inputs([{"action": "start_line", "value_name": "speed"}, {"action": "stop_line", "value_name": "speed"}])


def test_values_gives_the_last_accepted_value(signals, acts):  # noqa: F811
    line = _line_with(_signal("start_line", value_name="speed"), _signal("stop_line", address="10002"))
    assert PLCInputService.values(line["id"]) == {}            # not read yet

    async def steps():
        for value in (5, 7):
            signals.values["10001"] = value
            signals.values["10002"] = value
            await _pass()

    _run(steps)
    assert PLCInputService.values(line["id"]) == {"speed": 7}
    assert PLCInputService.values("other-line") == {}
    assert line_module.line_manager.get(line["id"]) is not None
