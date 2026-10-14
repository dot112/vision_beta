"""Send cards with the trigger "Timer": a message every N ms, while the line runs or always.

Asked for by the owner on 2026-10-14. The value box holds the interval in ms. A timer card
fires on its own clock, never for a product, a code, a line start or an alarm.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.services.card_triggers import card_fires, line_state_event, normalize_trigger
from app.services.line_config import PRIMARY_LINE_ID, TIMER_MAX_MS, TIMER_MIN_MS, normalize_send_card
from app.services.send_dispatcher_service import SendDispatcherService, sample_payload
from tests.test_send_cards import EVENTS, _channel, _drain, _set_cards, _with_loop, lines, listeners  # noqa: F401


@pytest.fixture(autouse=True)
def no_timers_left(request, monkeypatch):
    """Each test starts its own timers; one that uses the app's client gets the app's own."""
    if "client" not in request.fixturenames:
        monkeypatch.setattr(SendDispatcherService, "_timers", {})
        monkeypatch.setattr(SendDispatcherService, "_timer_loop", None)


def _timer(card_id="t", every="100", condition="while_running", **extra):
    return {"id": card_id, "endpoint_id": "tcp-timer", "trigger": "timer", "condition": condition, "set_value": every,
            "fields": ["event", "line", "line_state", "metrics"], **extra}


# ── The card ──────────────────────────────────────────────────────────────────


def test_a_timer_card_gets_its_defaults():
    card = normalize_send_card({"trigger": "timer"})
    assert (card["trigger"], card["condition"], card["set_value"]) == ("timer", "while_running", "1000")
    assert "camera_id" not in normalize_send_card({"trigger": "timer", "camera_id": "vis"})


@pytest.mark.parametrize("condition", ["while_running", "always"])
def test_a_timer_card_keeps_its_condition_and_interval(condition):
    card = normalize_send_card(_timer(every=" 2500 ", condition=condition))
    assert (card["condition"], card["set_value"]) == (condition, "2500")


@pytest.mark.parametrize("raw, words", [
    ({"set_value": str(TIMER_MIN_MS - 1)}, f"{TIMER_MIN_MS}"),
    ({"set_value": str(TIMER_MAX_MS + 1)}, f"{TIMER_MAX_MS}"),
    ({"set_value": "fast"}, "whole number of ms"),
    ({"set_value": "1.5"}, "whole number of ms"),
    ({"condition": "on"}, "while the line runs"),
])
def test_a_timer_card_with_a_bad_interval_or_condition_is_refused(raw, words):
    with pytest.raises(ValueError) as err:
        normalize_send_card({**_timer(), **raw})
    assert words in str(err.value) and "Send card" in str(err.value)


def test_a_timer_card_fires_for_no_event():
    card = normalize_trigger({"line_id": PRIMARY_LINE_ID, **_timer()})
    assert [name for name, event in EVENTS.items() if card_fires(card, event)] == []


def test_the_test_button_sends_an_example_timer_message():
    sample = sample_payload(normalize_trigger(_timer()), "Line 1")
    assert sample["event"] == "TIMER" and sample["line_state"] == "started" and sample["metrics"]["total_inspected"] > 0


# ── Sending ───────────────────────────────────────────────────────────────────


def _run(lines, seconds, running=True, before=None, during=None):  # noqa: F811
    """Start the timers, let them run for ``seconds`` with the line started or stopped, stop them."""
    lines.get(PRIMARY_LINE_ID).enabled = running

    async def steps():
        SendDispatcherService.start_timers()
        try:
            if before:
                before()
            if during:
                await during()
            await asyncio.sleep(seconds)
        finally:
            await SendDispatcherService.stop_timers()

    _with_loop(steps)
    _drain()


def test_a_timer_sends_every_interval_while_the_line_runs(lines, listeners):  # noqa: F811
    target = listeners()
    _channel("tcp-timer", target, "Timer")
    _set_cards([_timer(every="100")])
    _run(lines, 0.55)
    got = target.wait(5, timeout=2)
    assert 4 <= len(got) <= 6, len(got)
    assert got[0]["event"] == "TIMER" and got[0]["line_id"] == PRIMARY_LINE_ID and got[0]["line_state"] == "started"
    assert "total_inspected" in got[0]["metrics"]
    assert SendDispatcherService.get_status(PRIMARY_LINE_ID)["t"]["sent_count"] == len(got)


def test_while_the_line_runs_sends_nothing_while_it_is_stopped(lines, listeners):  # noqa: F811
    target = listeners()
    _channel("tcp-timer", target, "Timer")
    _set_cards([_timer(every="100")])
    _run(lines, 0.45, running=False)
    assert target.wait(1, timeout=0.5) == []


def test_always_sends_while_the_line_is_stopped(lines, listeners):  # noqa: F811
    target = listeners()
    _channel("tcp-timer", target, "Timer")
    _set_cards([_timer(every="100", condition="always")])
    _run(lines, 0.45, running=False)
    got = target.wait(3, timeout=2)
    assert len(got) >= 3 and got[0]["line_state"] == "stopped"


def test_the_first_message_comes_one_interval_after_the_line_starts(lines, listeners):  # noqa: F811
    target = listeners()
    _channel("tcp-timer", target, "Timer")
    _set_cards([_timer(every="300")])
    started = {}

    async def start_after_a_while():
        await asyncio.sleep(0.2)                    # stopped: nothing
        lines.get(PRIMARY_LINE_ID).enabled = True
        started["at"] = time.monotonic()
        SendDispatcherService.line_state_changed(line_state_event(PRIMARY_LINE_ID, "Line 1", True))

    _run(lines, 0.42, running=False, during=start_after_a_while)
    got = target.wait(1, timeout=2)
    assert len(got) == 1                            # at 300 ms after the start; 600 ms is after the end


def test_a_stopped_line_stops_its_timer(lines, listeners):  # noqa: F811
    target = listeners()
    _channel("tcp-timer", target, "Timer")
    _set_cards([_timer(every="100")])

    async def stop_soon():
        await asyncio.sleep(0.25)
        lines.get(PRIMARY_LINE_ID).enabled = False
        SendDispatcherService.line_state_changed(line_state_event(PRIMARY_LINE_ID, "Line 1", False))

    _run(lines, 0.5, during=stop_soon)
    assert 1 <= len(target.wait(2, timeout=1)) <= 3


def test_a_card_switched_off_or_removed_stops_and_a_new_interval_takes_over(lines, listeners):  # noqa: F811
    target = listeners()
    _channel("tcp-timer", target, "Timer")
    _set_cards([_timer("a", every="100"), _timer("b", every="100")])
    counts = {}

    async def change_cards():
        await asyncio.sleep(0.35)
        _set_cards([_timer("a", every="100", enabled=False)])       # a off, b gone
        await asyncio.sleep(0.35)
        counts["off"] = SendDispatcherService.get_status(PRIMARY_LINE_ID)["a"]["sent_count"]
        _set_cards([_timer("a", every="250")])
        await asyncio.sleep(0.3)
        counts["slower"] = SendDispatcherService.get_status(PRIMARY_LINE_ID)["a"]["sent_count"]

    _run(lines, 0.0, during=change_cards)
    assert SendDispatcherService._timers == {}
    assert counts["slower"] - counts["off"] == 1                     # 250 ms into the last 300 ms


def test_a_timer_that_fell_behind_skips_the_missed_messages(lines, listeners):  # noqa: F811
    target = listeners()
    _channel("tcp-timer", target, "Timer")
    _set_cards([_timer(every="100")])

    async def block_the_loop():
        await asyncio.sleep(0.05)
        time.sleep(0.45)                            # the server is busy for four and a half intervals

    _run(lines, 0.12, during=block_the_loop)
    # One message when the loop is free again, not four at once.
    assert len(target.wait(2, timeout=1)) <= 2


def test_timers_do_not_run_before_they_are_started(lines, listeners):  # noqa: F811
    target = listeners()
    _channel("tcp-timer", target, "Timer")
    lines.get(PRIMARY_LINE_ID).enabled = True
    _set_cards([_timer(every="100")])
    time.sleep(0.3)
    assert SendDispatcherService._timers == {} and target.messages == []


# ── Through the API, as the dashboard does it ─────────────────────────────────

from tests.test_send_cards import api_channels  # noqa: E402,F401  (a fixture)


def test_a_timer_card_on_a_line_sends_while_it_runs(client, admin_headers, api_channels):  # noqa: F811
    channels, mes, _ = api_channels
    # The app's startup starts the timers; a test that ran another app and shut it down stopped them.
    client.portal.call(SendDispatcherService.start_timers)
    card = {"id": "send_timer", "endpoint_id": channels["Send test MES"], "trigger": "timer",
            "condition": "while_running", "set_value": "150", "fields": ["event", "line", "line_state"]}
    line = client.post("/api/v1/lines", json={"name": "Timer line", "send_actions": [card]}, headers=admin_headers).json()
    try:
        assert client.get(f"/api/v1/lines/{line['id']}", headers=admin_headers).json()["send_actions"][0]["set_value"] == "150"
        time.sleep(0.4)
        assert mes.messages == []                   # not started yet
        assert client.post(f"/api/v1/lines/{line['id']}/start", headers=admin_headers).status_code == 200
        got = mes.wait(3, timeout=3)
        assert [m["event"] for m in got[:3]] == ["TIMER"] * 3
        assert all((m["line_name"], m["line_state"]) == ("Timer line", "started") for m in got[:3])
        assert client.post(f"/api/v1/lines/{line['id']}/stop", headers=admin_headers).status_code == 200
        time.sleep(0.3)
        settled = len(mes.messages)
        time.sleep(0.5)
        assert len(mes.messages) == settled         # nothing after the stop
    finally:
        client.delete(f"/api/v1/lines/{line['id']}", headers=admin_headers)
    assert all(key[0] != line["id"] for key in SendDispatcherService._timers)


def test_the_api_refuses_a_timer_that_is_too_fast(client, admin_headers, api_channels):  # noqa: F811
    channels, _, _ = api_channels
    res = client.post("/api/v1/lines", json={"name": "Too fast", "send_actions": [
        {"endpoint_id": channels["Send test MES"], "trigger": "timer", "condition": "always", "set_value": "20"}]},
        headers=admin_headers)
    assert res.status_code == 422 and "100 to 86400000 ms" in res.text, res.text


def test_a_restart_within_an_interval_starts_the_interval_again(lines, listeners):  # noqa: F811
    # Stopped and started again before the next message was due: that message comes one
    # interval after the restart, not on the schedule of the first start.
    target = listeners()
    _channel("tcp-timer", target, "Timer")
    _set_cards([_timer(every="400")])

    async def stop_and_start():
        await asyncio.sleep(0.1)
        lines.get(PRIMARY_LINE_ID).enabled = False
        SendDispatcherService.line_state_changed(line_state_event(PRIMARY_LINE_ID, "Line 1", False))
        await asyncio.sleep(0.1)
        lines.get(PRIMARY_LINE_ID).enabled = True
        SendDispatcherService.line_state_changed(line_state_event(PRIMARY_LINE_ID, "Line 1", True))

    _run(lines, 0.3, during=stop_and_start)         # ends at 0.5 s: 400 ms after the first start, 300 after the restart
    assert target.wait(1, timeout=0.5) == []
