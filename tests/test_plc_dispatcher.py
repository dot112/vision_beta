"""Unit tests for PLCDispatcherService — trigger evaluation, re-arm, execution policy."""
from __future__ import annotations

import asyncio
import time
import pytest

from app.services.plc_dispatcher_service import (
    PLCDispatcherService,
    _camera_matches,
    _eval_condition,
    _eval_counter,
    _normalize_card,
    _CardState,
    alarm_event,
    line_state_event,
)
from app.events.alarm_events import ALARM_CATALOG, AlarmCode, alarm_manager
from app.services.health_service import HealthAlarmCode


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


class TestLineStateTrigger:
    """Line started / stopped cards: On = started, Off = stopped, Toggled = either."""

    started = line_state_event("line-1", "Line 1", True)
    stopped = line_state_event("line-1", "Line 1", False)

    def _card(self, condition, **extra):
        return _normalize_card({"trigger": "line_state", "condition": condition, **extra})

    def test_on_off_toggled(self):
        assert _eval_condition(self._card("on"), self.started) is True
        assert _eval_condition(self._card("on"), self.stopped) is False
        assert _eval_condition(self._card("off"), self.stopped) is True
        assert _eval_condition(self._card("off"), self.started) is False
        assert _eval_condition(self._card("toggled"), self.started) is True
        assert _eval_condition(self._card("toggled"), self.stopped) is True

    def test_only_line_state_cards_fire_on_a_start_or_stop(self):
        crossing_any = _normalize_card({"trigger": "cross_line", "condition": "any"})
        assert _eval_condition(crossing_any, self.started) is False
        crossing = {"event_id": "x", "result": "good", "good_count": 1, "reject_count": 0, "detected_classes": []}
        assert _eval_condition(self._card("toggled"), crossing) is False

    def test_a_card_that_names_a_camera_still_fires(self):
        assert _camera_matches(self._card("on", camera_id="cam-2"), self.started) is True

    def test_events_are_unique_so_every_change_fires(self):
        assert line_state_event("line-1", "Line 1", True)["event_id"] != line_state_event("line-1", "Line 1", True)["event_id"]


class TestAlarmTrigger:
    """Alarm cards: Raised = a picked alarm goes active, Cleared = the last picked one clears."""

    @staticmethod
    def _alarm(code, line_id=None, card_id=None, alarm_id="a1"):
        details = {}
        if line_id:
            details["line_id"] = line_id
        if card_id:
            details["card_id"] = card_id
        return {"id": alarm_id, "code": code, "source": "x", "severity": "critical", "message": "m", "details": details}

    def _card(self, condition, codes, **extra):
        return _normalize_card({"id": "lamp", "trigger": "alarm", "condition": condition,
                                "alarm_codes": codes, "line_id": "line-1", **extra})

    def test_raised_cleared_toggled(self):
        raised = alarm_event(self._alarm("camera.stalled", "line-1"), True, [])
        cleared = alarm_event(self._alarm("camera.stalled", "line-1"), False, [])
        assert _eval_condition(self._card("raised", ["camera.stalled"]), raised) is True
        assert _eval_condition(self._card("raised", ["camera.stalled"]), cleared) is False
        assert _eval_condition(self._card("cleared", ["camera.stalled"]), cleared) is True
        assert _eval_condition(self._card("cleared", ["camera.stalled"]), raised) is False
        assert _eval_condition(self._card("toggled", ["camera.stalled"]), raised) is True
        assert _eval_condition(self._card("toggled", ["camera.stalled"]), cleared) is True

    def test_only_picked_alarms_fire_and_star_means_any(self):
        raised = alarm_event(self._alarm("inference.stalled"), True, [])
        assert _eval_condition(self._card("raised", ["camera.stalled", "plc.connect_failed"]), raised) is False
        assert _eval_condition(self._card("raised", ["camera.stalled", "inference.stalled"]), raised) is True
        assert _eval_condition(self._card("raised", ["*"]), raised) is True
        assert _eval_condition(self._card("raised", []), raised) is False
        assert _eval_condition(self._card("raised", "camera.stalled, inference.stalled"), raised) is True

    def test_cleared_waits_until_no_picked_alarm_of_this_line_is_left(self):
        cleared = self._alarm("camera.stalled", "line-1")
        card = self._card("cleared", ["camera.stalled", "inference.stalled"])
        still_on = [self._alarm("inference.stalled", alarm_id="a2")]
        assert _eval_condition(card, alarm_event(cleared, False, still_on)) is False
        # Another line's camera, or an alarm the card did not pick, does not hold it on.
        others = [self._alarm("camera.stalled", "line-2", alarm_id="a3"), self._alarm("flow.overload", alarm_id="a4")]
        assert _eval_condition(card, alarm_event(cleared, False, others)) is True

    def test_line_alarms_go_to_their_line_server_alarms_to_every_line(self):
        assert alarm_event(self._alarm("camera.stalled", "line-2"), True, [])["line_id"] == "line-2"
        assert alarm_event(self._alarm("plc.action_failed", "line-2"), True, [])["line_id"] == "line-2"
        assert alarm_event(self._alarm("inference.stalled"), True, [])["line_id"] == "*"
        assert alarm_event(self._alarm("plc.connect_failed"), True, [])["line_id"] == "*"
        # A camera on no line concerns no line; a free-form alarm without a line concerns all.
        assert alarm_event(self._alarm("camera.disconnected"), True, [])["line_id"] == ""
        assert alarm_event(self._alarm("custom.thing"), True, [])["line_id"] == "*"

    def test_a_cards_own_failure_does_not_fire_it_again(self):
        own = alarm_event(self._alarm("plc.action_failed", "line-1", card_id="lamp"), True, [])
        other = alarm_event(self._alarm("plc.action_failed", "line-1", card_id="reject"), True, [])
        card = self._card("raised", ["plc.action_failed"])
        assert _eval_condition(card, own) is False
        assert _eval_condition(card, other) is True

    def test_only_alarm_cards_fire_on_an_alarm(self):
        raised = alarm_event(self._alarm("camera.stalled", "line-1"), True, [])
        assert _eval_condition(_normalize_card({"trigger": "cross_line", "condition": "any"}), raised) is False
        assert _eval_condition(_normalize_card({"trigger": "line_state", "condition": "toggled"}), raised) is False
        crossing = {"event_id": "x", "result": "good", "good_count": 1, "reject_count": 0, "detected_classes": []}
        assert _eval_condition(self._card("toggled", ["*"]), crossing) is False
        assert _camera_matches(self._card("raised", ["*"], camera_id="cam-2"), raised) is True

    def test_catalog_lists_every_known_alarm_once(self):
        codes = [entry["code"] for entry in ALARM_CATALOG]
        assert len(codes) == len(set(codes))
        known = {v for k, v in vars(AlarmCode).items() if k.isupper()}
        known |= {v for k, v in vars(HealthAlarmCode).items() if k.isupper()}
        assert known == set(codes)
        assert all(entry["scope"] in ("line", "server") and entry["label"] for entry in ALARM_CATALOG)


class TestAlarmTriggerEndToEnd:
    def setup_method(self):
        _reset_dispatcher()
        alarm_manager.reset()

    def teardown_method(self):
        _reset_dispatcher()
        alarm_manager.reset()

    def test_alarms_run_the_cards_that_picked_them(self, monkeypatch):
        fired = []

        async def fake_dispatch(card, state, event):
            fired.append((card["id"], event["alarm_code"], event["result"]))

        monkeypatch.setattr(PLCDispatcherService, "_dispatch", classmethod(lambda cls, c, s, e: fake_dispatch(c, s, e)))
        lamp = {"trigger": "alarm", "alarm_codes": ["camera.stalled", "inference.stalled"], "debounce_ms": 0}
        PLCDispatcherService.set_cards([
            {"id": "l1-on", "condition": "raised", **lamp},
            {"id": "l1-off", "condition": "cleared", **lamp},
        ], line_id="line-1")
        PLCDispatcherService.set_cards([{"id": "l2-on", "condition": "raised", **lamp}], line_id="line-2")

        async def settle():
            for _ in range(5):
                await asyncio.sleep(0)

        async def run():
            PLCDispatcherService.watch_alarms()
            alarm_manager.raise_alarm(HealthAlarmCode.CAMERA_STALLED, "camera:c1", "stalled", "critical", {"line_id": "line-1"})
            await settle()
            assert fired == [("l1-on", "camera.stalled", "raised")]
            # Repeats of an active alarm do not fire again.
            alarm_manager.raise_alarm(HealthAlarmCode.CAMERA_STALLED, "camera:c1", "stalled", "critical", {"line_id": "line-1"})
            await settle()
            assert len(fired) == 1
            # A server-wide alarm reaches every line.
            alarm_manager.raise_alarm(HealthAlarmCode.INFERENCE_STALLED, "inference", "stalled", "critical")
            await settle()
            assert sorted(fired[1:]) == [("l1-on", "inference.stalled", "raised"), ("l2-on", "inference.stalled", "raised")]
            fired.clear()
            alarm_manager.clear_alarm(HealthAlarmCode.CAMERA_STALLED, "camera:c1")
            await settle()
            assert fired == []  # inference is still stalled, so the lamp stays on
            alarm_manager.clear_alarm(HealthAlarmCode.INFERENCE_STALLED, "inference")
            await settle()
            assert fired == [("l1-off", "inference.stalled", "cleared")]

        asyncio.run(run())


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

    def test_deleting_or_switching_off_an_action_clears_its_failure_alarm(self):
        alarm_manager.reset()
        for cid in ("gone", "off", "kept"):
            alarm_manager.raise_alarm(AlarmCode.PLC_ACTION_FAILED, f"plc_card:{cid}", "failed", "critical", {"line_id": "line-1"})
        PLCDispatcherService.set_cards([{"id": "gone"}, {"id": "off"}, {"id": "kept"}])
        PLCDispatcherService.set_cards([{"id": "off", "enabled": False}, {"id": "kept"}])
        assert [a.source for a in alarm_manager.active()] == ["plc_card:kept"]
        alarm_manager.reset()

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
