"""Unit tests for PLCDispatcherService — trigger evaluation, re-arm, execution policy."""
from __future__ import annotations

import asyncio
import time
import pytest

from app.services.plc_dispatcher_service import (
    PLCDispatcherService,
    _eval_condition,
    _eval_counter,
    _CardState,
)


# ── _eval_counter ─────────────────────────────────────────────────────────────

class TestEvalCounter:
    def test_gte_passes(self):
        assert _eval_counter(">=10", 10, 10) is True
        assert _eval_counter(">=10", 15, 10) is True

    def test_gte_fails(self):
        assert _eval_counter(">=10", 9, 10) is False

    def test_exact_equal(self):
        assert _eval_counter("==5", 5, 5) is True
        assert _eval_counter("==5", 4, 5) is False

    def test_modulo(self):
        assert _eval_counter("%10==0", 10, 10) is True
        assert _eval_counter("%10==0", 20, 10) is True
        assert _eval_counter("%10==0", 0, 10) is False   # count must be > 0
        assert _eval_counter("%10==0", 11, 10) is False

    def test_modulo_with_n_in_condition(self):
        assert _eval_counter("%5==0", 15, 5) is True


# ── _eval_condition ───────────────────────────────────────────────────────────

class TestEvalCondition:
    def _event(self, result="good", good=5, reject=2, classes=None):
        return {
            "event_id": "test-1",
            "result": result,
            "good_count": good,
            "reject_count": reject,
            "detected_classes": classes or [],
        }

    def test_line_cross_any(self):
        card = {"trigger_type": "line_cross", "trigger_condition": "any"}
        assert _eval_condition(card, self._event("good")) is True
        assert _eval_condition(card, self._event("reject")) is True

    def test_line_cross_good(self):
        card = {"trigger_type": "line_cross", "trigger_condition": "good"}
        assert _eval_condition(card, self._event("good")) is True
        assert _eval_condition(card, self._event("reject")) is False

    def test_line_cross_reject(self):
        card = {"trigger_type": "line_cross", "trigger_condition": "reject"}
        assert _eval_condition(card, self._event("reject")) is True
        assert _eval_condition(card, self._event("good")) is False

    def test_good_counter_gte(self):
        card = {"trigger_type": "good_counter", "trigger_condition": ">=5", "trigger_value": 5}
        assert _eval_condition(card, self._event(good=5)) is True
        assert _eval_condition(card, self._event(good=4)) is False

    def test_reject_counter_modulo(self):
        card = {"trigger_type": "reject_counter", "trigger_condition": "%10==0", "trigger_value": 10}
        assert _eval_condition(card, self._event(reject=10)) is True
        assert _eval_condition(card, self._event(reject=11)) is False

    def test_class_detected_eq(self):
        card = {"trigger_type": "class_detected", "trigger_condition": "class==bottle"}
        assert _eval_condition(card, self._event(classes=["bottle"])) is True
        assert _eval_condition(card, self._event(classes=["can"])) is False

    def test_class_detected_in(self):
        card = {"trigger_type": "class_detected", "trigger_condition": "class_in:bottle,can"}
        assert _eval_condition(card, self._event(classes=["can"])) is True
        assert _eval_condition(card, self._event(classes=["cup"])) is False

    def test_unknown_trigger_returns_false(self):
        card = {"trigger_type": "unknown_trigger", "trigger_condition": "any"}
        assert _eval_condition(card, self._event()) is False


# ── PLCDispatcherService ──────────────────────────────────────────────────────

def _reset_dispatcher():
    PLCDispatcherService._cards = []
    PLCDispatcherService._states = {}
    PLCDispatcherService._lock = None


class TestPLCDispatcherService:
    def setup_method(self):
        _reset_dispatcher()

    def test_set_and_get_cards(self):
        cards = [{"id": "c1", "name": "Card 1", "enabled": True}]
        PLCDispatcherService.set_cards(cards)
        assert len(PLCDispatcherService._cards) == 1
        assert "c1" in PLCDispatcherService._states

    def test_get_status_empty(self):
        assert PLCDispatcherService.get_status() == []

    def test_get_status_with_card(self):
        PLCDispatcherService.set_cards([
            {"id": "c1", "name": "Reject Kicker", "enabled": True}
        ])
        statuses = PLCDispatcherService.get_status()
        assert len(statuses) == 1
        assert statuses[0]["card_id"] == "c1"
        assert statuses[0]["status"] == "idle"

    def test_get_status_by_card_id(self):
        PLCDispatcherService.set_cards([
            {"id": "c1", "name": "A", "enabled": True},
            {"id": "c2", "name": "B", "enabled": True},
        ])
        statuses = PLCDispatcherService.get_status(card_id="c2")
        assert len(statuses) == 1
        assert statuses[0]["card_id"] == "c2"

    def test_evaluate_disabled_card_skipped(self):
        PLCDispatcherService.set_cards([{
            "id": "c1", "name": "Disabled", "enabled": False,
            "trigger_type": "line_cross", "trigger_condition": "any",
            "plc_endpoint_id": "",
        }])
        event = {"event_id": "e1", "result": "reject", "good_count": 0, "reject_count": 1, "detected_classes": []}
        asyncio.run(PLCDispatcherService.evaluate(event))
        assert PLCDispatcherService._states["c1"].status == "idle"

    def test_evaluate_rearm_lockout(self):
        PLCDispatcherService.set_cards([{
            "id": "c1", "name": "Kicker", "enabled": True,
            "trigger_type": "line_cross", "trigger_condition": "any",
            "rearm_lockout_ms": 5000,   # 5 second lockout
            "plc_endpoint_id": "",
            "operation": "PULSE", "target_address": "0",
            "travel_delay_ms": 0, "pulse_duration_ms": 10,
            "execution_policy": "every_frame",
        }])
        event = {"event_id": "e1", "result": "reject", "good_count": 0, "reject_count": 1, "detected_classes": []}

        async def _run():
            await PLCDispatcherService.evaluate(event)
            await asyncio.sleep(0.1)
            first_fired = PLCDispatcherService._states["c1"].last_fired_at
            await PLCDispatcherService.evaluate({**event, "event_id": "e2"})
            await asyncio.sleep(0.1)
            return first_fired, PLCDispatcherService._states["c1"].last_fired_at

        first, second = asyncio.run(_run())
        assert second == first  # re-arm blocked second fire

    def test_evaluate_once_per_event_dedup(self):
        PLCDispatcherService.set_cards([{
            "id": "c1", "name": "Kicker", "enabled": True,
            "trigger_type": "line_cross", "trigger_condition": "any",
            "rearm_lockout_ms": 0, "plc_endpoint_id": "",
            "operation": "PULSE", "target_address": "0",
            "travel_delay_ms": 0, "pulse_duration_ms": 10,
            "execution_policy": "once_per_event",
        }])
        event = {"event_id": "same-id", "result": "reject", "good_count": 0, "reject_count": 1, "detected_classes": []}

        async def _run():
            await PLCDispatcherService.evaluate(event)
            await asyncio.sleep(0.1)
            first = PLCDispatcherService._states["c1"].last_fired_at
            await PLCDispatcherService.evaluate(event)   # same event_id
            await asyncio.sleep(0.1)
            return first, PLCDispatcherService._states["c1"].last_fired_at

        first, second = asyncio.run(_run())
        assert second == first

    def test_dispatch_without_endpoint_fails_safe(self):
        PLCDispatcherService.set_cards([{
            "id": "sim1", "name": "Sim", "enabled": True,
            "trigger_type": "line_cross", "trigger_condition": "any",
            "rearm_lockout_ms": 0, "plc_endpoint_id": "",
            "operation": "PULSE", "target_address": "0",
            "travel_delay_ms": 0, "pulse_duration_ms": 10,
            "execution_policy": "every_frame",
        }])
        event = {"event_id": "e1", "result": "reject", "good_count": 0, "reject_count": 1, "detected_classes": []}

        async def _run():
            await PLCDispatcherService.evaluate(event)
            await asyncio.sleep(0.15)

        asyncio.run(_run())
        state = PLCDispatcherService._states["sim1"]
        # A card with no endpoint must never report a send it did not make.
        assert state.status == "failed"
        assert state.last_result.get("success") is False
        assert "not sent" in state.last_result.get("message", "")
