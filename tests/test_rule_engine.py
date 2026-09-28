"""Rule engine: operator semantics and which actions fire on a match."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.engines import rule_engine
from app.engines.rule_engine import RuleEngine, _compare
from app.schemas.actions import ActionExecutionResult


def _rule(rule_id="r1", field="defect_count", op="gt", threshold="0", action_id=None, enabled=True):
    return SimpleNamespace(
        id=rule_id, name=f"rule {rule_id}", condition_field=field, operator=op,
        threshold_value=threshold, action_id=action_id, is_enabled=enabled,
    )


def _action(action_id="a1", enabled=True):
    return SimpleNamespace(id=action_id, name=f"action {action_id}", is_enabled=enabled)


@pytest.fixture
def executed(monkeypatch):
    """Record ActionEngine.execute calls instead of driving any output."""
    calls = []

    async def fake_execute(action, context):
        from datetime import datetime, timezone
        calls.append((action.id, dict(context)))
        return ActionExecutionResult(
            action_id=action.id, action_name=action.name, success=True,
            message="fake ok", executed_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(rule_engine.ActionEngine, "execute", staticmethod(fake_execute))
    return calls


class TestCompare:
    @pytest.mark.parametrize("actual, op, threshold, expected", [
        (3, "gt", "2", True),
        (2, "gt", "2", False),
        (2, "gte", "2", True),
        (1, "lt", "2", True),
        (2, "lte", "2", True),
        (3, "lte", "2", False),
        ("2.0", "eq", "2", True),
        (2, "neq", "3", True),
        (0.85, "gte", "0.80", True),
    ])
    def test_numeric(self, actual, op, threshold, expected):
        assert _compare(actual, op, threshold) is expected

    def test_string_equality_is_case_insensitive(self):
        assert _compare("Scratch", "eq", "scratch") is True
        assert _compare("dent", "neq", "scratch") is True

    def test_contains(self):
        assert _compare("BATCH_A_0042", "contains", "batch_a") is True
        assert _compare("BATCH_B_0042", "contains", "batch_a") is False

    def test_missing_value_never_matches(self):
        assert _compare(None, "eq", "0") is False
        assert _compare(None, "neq", "0") is False

    def test_numeric_operator_on_text_does_not_match(self):
        assert _compare("abc", "gt", "1") is False

    def test_unknown_operator(self):
        assert _compare(1, "between", "0") is False


class TestEvaluateRules:
    def test_matched_rule_fires_its_action(self, executed):
        rules = [_rule(action_id="a1")]
        results = asyncio.run(RuleEngine.evaluate_rules(rules, {"a1": _action()}, {"defect_count": 2}))

        assert [r.matched for r in results] == [True]
        assert results[0].actual_value == 2
        assert results[0].action_triggered == "Executed action a1: fake ok"
        assert executed == [("a1", {"defect_count": 2})]

    def test_unmatched_rule_fires_nothing(self, executed):
        results = asyncio.run(RuleEngine.evaluate_rules([_rule(action_id="a1")], {"a1": _action()}, {"defect_count": 0}))
        assert results[0].matched is False
        assert results[0].action_triggered is None
        assert executed == []

    def test_disabled_rule_is_skipped(self, executed):
        results = asyncio.run(RuleEngine.evaluate_rules([_rule(enabled=False, action_id="a1")], {"a1": _action()}, {"defect_count": 5}))
        assert results == []
        assert executed == []

    def test_disabled_action_is_not_executed(self, executed):
        results = asyncio.run(RuleEngine.evaluate_rules([_rule(action_id="a1")], {"a1": _action(enabled=False)}, {"defect_count": 5}))
        assert results[0].matched is True
        assert results[0].action_triggered is None
        assert executed == []

    def test_unknown_action_id_is_ignored(self, executed):
        results = asyncio.run(RuleEngine.evaluate_rules([_rule(action_id="missing")], {}, {"defect_count": 5}))
        assert results[0].matched is True
        assert executed == []

    def test_missing_field_does_not_match(self, executed):
        results = asyncio.run(RuleEngine.evaluate_rules([_rule(field="max_confidence", op="gte", threshold="0.5")], {}, {}))
        assert results[0].matched is False
        assert results[0].actual_value is None

    def test_each_matching_rule_is_reported_in_order(self, executed):
        rules = [
            _rule("r1", field="class_name", op="eq", threshold="scratch", action_id="a1"),
            _rule("r2", field="max_confidence", op="lt", threshold="0.5"),
            _rule("r3", field="qr_data", op="contains", threshold="LOT7", action_id="a2"),
        ]
        ctx = {"class_name": "SCRATCH", "max_confidence": 0.9, "qr_data": "LOT7-001"}
        results = asyncio.run(RuleEngine.evaluate_rules(rules, {"a1": _action("a1"), "a2": _action("a2")}, ctx))

        assert [(r.rule_id, r.matched) for r in results] == [("r1", True), ("r2", False), ("r3", True)]
        assert [call[0] for call in executed] == ["a1", "a2"]
