"""Reject confirmation: a PLC action that waits for a signal from the PLC.

A PLC action card can name a signal card of its line with the action "Confirm
a PLC action". After the card's output is sent, the dispatcher waits for that
signal's edge: it comes in time and the fire is "confirmed", or it does not and
the fire is "not_confirmed", with a critical alarm until a later fire is
confirmed. This is what the removed "Wait for PLC ACK" setting only claimed
to do.
"""
from __future__ import annotations

import asyncio

import pytest

from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager
from app.services.line_config import PLC_CARD_KEPT_KEYS, PRIMARY_LINE_ID
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.plc_failsafe_service import validate_confirmation
from app.services.plc_input_service import PLCInputService
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_joined_cameras import _run, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import _line_with, _pass, _signal, signals  # noqa: F401  (fixtures)

SIGNAL = "plcin_confirm_card"
NOT_CONFIRMED = AlarmCode.PLC_ACTION_NOT_CONFIRMED


def _gate(cid="kick", **extra):
    """A reject gate that waits for the PLC to confirm it."""
    return {"id": cid, "name": "Reject gate", "enabled": True, "plc_endpoint_id": "ep1", "trigger": "cross_line",
            "condition": "reject", "operation": "pulse", "target_address": "Q0.0", "duration_ms": 10, "delay_ms": 0,
            "debounce_ms": 0, "exec_policy": "every_frame", "confirm_input_id": SIGNAL, "confirm_timeout_ms": 300, **extra}


def _reject(line, n=1):
    return {"event_type": "crossing", "event_id": f"product-{n}", "line_id": line["id"], "camera_id": "vis",
            "counting_camera": True, "result": "reject", "good_count": 0, "reject_count": n, "detected_classes": ["dent"]}


@pytest.fixture
def gate(signals):  # noqa: F811
    """A line with a confirm signal on 10001 and a reject gate that waits for it. Gives (line, driver)."""
    PLCDispatcherService._confirm_waiters = {}
    PLCDispatcherService._dispatch_tasks = set()
    PLCDispatcherService._line_tasks = {}
    line = _line_with(_signal("confirm_card"))
    yield line, signals
    PLCDispatcherService._confirm_waiters = {}


def _cards(line, *cards):
    saved = SettingsPersistenceService.replace_line_plc_actions(line["id"], list(cards))
    PLCDispatcherService.set_cards(saved, line_id=line["id"])
    return saved


def _state(card_id="kick"):
    return PLCDispatcherService.get_status(card_id=card_id)[0]


async def _fire(line, n=1):
    await PLCDispatcherService.evaluate(_reject(line, n))


async def _done():
    tasks = list(PLCDispatcherService._dispatch_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def _alarm(card_id="kick"):
    return alarm_manager.get(NOT_CONFIRMED, f"plc_card:{card_id}")


# ── The two settings ──────────────────────────────────────────────────────────

def test_confirmation_settings_are_checked():
    validate_confirmation({"id": "a"})                                      # a card without confirmation
    validate_confirmation({"id": "a", "confirm_input_id": "", "confirm_timeout_ms": ""})
    for good in (50, 10000, "250"):
        validate_confirmation({"id": "a", "confirm_input_id": SIGNAL, "confirm_timeout_ms": good})
    for bad in (49, 10001, "soon", 1.5, True):
        with pytest.raises(ValueError, match="50 to 10000"):
            validate_confirmation({"id": "a", "name": "Gate", "confirm_input_id": SIGNAL, "confirm_timeout_ms": bad})
    with pytest.raises(ValueError, match="confirmation signal"):
        validate_confirmation({"id": "a", "confirm_input_id": ["x"]})


def test_a_card_keeps_its_confirmation_when_a_client_leaves_it_out(gate):
    line, _ = gate
    assert "confirm_input_id" in PLC_CARD_KEPT_KEYS and "confirm_timeout_ms" in PLC_CARD_KEPT_KEYS
    _cards(line, _gate(confirm_timeout_ms=750))
    older = {key: value for key, value in _gate(name="Renamed").items() if not key.startswith("confirm_")}
    saved = _cards(line, older)[0]
    assert (saved["name"], saved["confirm_input_id"], saved["confirm_timeout_ms"]) == ("Renamed", SIGNAL, 750)
    # Sent empty, it is switched off.
    assert _cards(line, {**older, "confirm_input_id": ""})[0]["confirm_input_id"] == ""
    with pytest.raises(ValueError, match="50 to 10000"):
        _cards(line, _gate(confirm_timeout_ms=5))


def test_a_card_must_name_a_confirm_signal_of_its_own_line(gate):
    line, _ = gate
    with pytest.raises(ValueError, match="'Reject gate'.*not on this line"):
        _cards(line, _gate(confirm_input_id="plcin_gone"))
    SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": [_signal("confirm_card"), _signal("start_line")]})
    with pytest.raises(ValueError, match="'Reject gate'.*Confirm a PLC action"):
        _cards(line, _gate(confirm_input_id="plcin_start_line"))
    # The same when the line is saved as a whole, cards and signals together.
    with pytest.raises(ValueError, match="not on this line"):
        SettingsPersistenceService.save_line({"id": line["id"], "plc_actions": [_gate()], "plc_inputs": []})
    whole = SettingsPersistenceService.save_line({"id": line["id"], "plc_actions": [_gate()], "plc_inputs": [_signal("confirm_card")]})
    assert whole["plc_actions"][0]["confirm_input_id"] == SIGNAL
    # Line 1 keeps its cards at the top level of the settings and its signals in its entry.
    with pytest.raises(ValueError, match="not on this line"):
        SettingsPersistenceService.replace_line_plc_actions(PRIMARY_LINE_ID, [_gate()])
    SettingsPersistenceService.save_line({"id": PRIMARY_LINE_ID, "plc_inputs": [_signal("confirm_card")]})
    assert SettingsPersistenceService.replace_line_plc_actions(PRIMARY_LINE_ID, [_gate()])[0]["confirm_input_id"] == SIGNAL


def test_a_signal_an_action_names_cannot_be_deleted(gate):
    line, _ = gate
    _cards(line, _gate())
    for inputs in ([], [_signal("start_line", id=SIGNAL)]):       # deleted, or made into something else
        with pytest.raises(ValueError, match="'Reject gate'"):
            SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": inputs})
        assert [card["action"] for card in SettingsPersistenceService.get_line(line["id"])["plc_inputs"]] == ["confirm_card"]
    # A client that sends the cards without their confirmation keeps it, so the signal still cannot go.
    older = {key: value for key, value in _gate().items() if not key.startswith("confirm_")}
    with pytest.raises(ValueError, match="'Reject gate'"):
        SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": [], "plc_actions": [older]})
    assert len(SettingsPersistenceService.get_line(line["id"])["plc_inputs"]) == 1
    # With the action's confirmation switched off first, the signal can be deleted.
    saved = SettingsPersistenceService.save_line(
        {"id": line["id"], "plc_inputs": [], "plc_actions": [{**_gate(), "confirm_input_id": ""}]})
    assert saved["plc_inputs"] == []


# ── A fire and its confirmation ───────────────────────────────────────────────

def test_a_confirmed_fire(gate):
    line, driver = gate
    _cards(line, _gate())

    async def steps():
        await _fire(line)
        await asyncio.sleep(0.02)
        assert _state()["status"] == "sent"                 # sent, and waiting for the PLC
        assert PLCDispatcherService.confirm_signal(line["id"], SIGNAL) == 1
        await _done()

    _run(steps)
    state = _state()
    assert state["status"] == "confirmed" and state["last_result"]["confirmed"] is True
    assert state["last_result"]["success"] is True and "confirmed" in state["last_result"]["message"]
    assert ("PULSE", "Q0.0") in driver.calls and _alarm() is None


def test_a_fire_that_is_not_confirmed_raises_the_alarm_and_the_next_confirmed_fire_clears_it(gate):
    line, _ = gate
    _cards(line, _gate(confirm_timeout_ms=50))

    async def unanswered():
        await _fire(line, 1)
        await _done()

    _run(unanswered)
    state = _state()
    assert state["status"] == "not_confirmed" and state["last_result"]["confirmed"] is False
    assert "50 ms" in state["last_result"]["message"]
    alarm = _alarm()
    assert alarm is not None and alarm.severity == AlarmSeverity.CRITICAL
    assert alarm.details["line_id"] == line["id"] and alarm.details["card_id"] == "kick"
    assert "Reject gate" in alarm.message
    # The output itself was sent: that is not the fault.
    assert alarm_manager.get(AlarmCode.PLC_ACTION_FAILED, "plc_card:kick") is None

    async def answered():
        await _fire(line, 2)
        await asyncio.sleep(0.02)
        PLCDispatcherService.confirm_signal(line["id"], SIGNAL)
        await _done()

    _run(answered)
    assert _state()["status"] == "confirmed" and _alarm() is None


def test_two_fires_waiting_are_confirmed_in_order(gate):
    line, _ = gate
    _cards(line, _gate(confirm_timeout_ms=400))

    async def steps():
        await _fire(line, 1)
        await asyncio.sleep(0.03)
        await _fire(line, 2)
        await asyncio.sleep(0.03)
        first, second = list(PLCDispatcherService._confirm_waiters["kick"])
        assert PLCDispatcherService.confirm_signal(line["id"], SIGNAL) == 1
        await asyncio.sleep(0.01)
        assert first.done() and not second.done()           # the older fire got the signal
        assert PLCDispatcherService.confirm_signal(line["id"], SIGNAL) == 1
        await _done()
        assert PLCDispatcherService.confirm_signal(line["id"], SIGNAL) == 0     # nothing waits any more

    _run(steps)
    assert _state()["status"] == "confirmed" and _alarm() is None
    assert not PLCDispatcherService._confirm_waiters.get("kick")


def test_a_signal_that_comes_during_the_pulse_counts(gate):
    line, driver = gate
    driver.write_seconds = 0.08                             # the PLC confirms before the pulse is over
    _cards(line, _gate(confirm_timeout_ms=50))

    async def steps():
        await _fire(line)
        await asyncio.sleep(0.03)
        assert _state()["status"] == "executing"
        assert PLCDispatcherService.confirm_signal(line["id"], SIGNAL) == 1
        await _done()

    _run(steps)
    assert _state()["status"] == "confirmed" and _alarm() is None


def test_a_failed_write_waits_for_no_confirmation(gate):
    line, driver = gate
    driver.reachable = False
    _cards(line, _gate(confirm_timeout_ms=50))
    seen = {}

    async def steps():
        await _fire(line)
        await _done()
        seen["late"] = PLCDispatcherService.confirm_signal(line["id"], SIGNAL)

    _run(steps)
    assert _state()["status"] == "failed" and "confirmed" not in _state()["last_result"]
    assert seen["late"] == 0 and _alarm() is None
    assert alarm_manager.get(AlarmCode.PLC_ACTION_FAILED, "plc_card:kick") is not None


def test_a_card_with_a_strobe_waits_only_after_its_strobe(gate):
    line, driver = gate
    driver.write_seconds = 0.06                             # the write and the strobe take longer than the wait
    _cards(line, _gate(operation="write", target_address="40001", write_value=2, strobe_address="M0.1",
                       strobe_pulse_ms=10, confirm_timeout_ms=60))

    async def steps():
        await _fire(line)
        while ("PULSE", "M0.1") not in driver.calls:
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.08)                           # the strobe is out; the 60 ms start now
        assert PLCDispatcherService.confirm_signal(line["id"], SIGNAL) == 1
        await _done()

    _run(steps)
    assert _state()["status"] == "confirmed"

    driver.unwritable = {"M0.1"}

    async def strobe_fails():
        await _fire(line, 2)
        await _done()
        assert PLCDispatcherService.confirm_signal(line["id"], SIGNAL) == 0

    _run(strobe_fails)
    assert _state()["status"] == "failed" and _alarm() is None


def test_the_test_button_does_not_wait_for_the_signal(gate):
    line, _ = gate
    card = _cards(line, _gate(confirm_timeout_ms=5000))[0]
    seen = {}

    async def steps():
        seen["result"] = await asyncio.wait_for(PLCDispatcherService.dispatch_manual({**card, "line_id": line["id"]}), timeout=1.0)
        seen["late"] = PLCDispatcherService.confirm_signal(line["id"], SIGNAL)

    _run(steps)
    assert seen["result"]["status"] == "sent" and seen["result"]["success"] is True
    assert seen["late"] == 0 and _alarm() is None


def test_an_old_wait_for_ack_card_still_reports_sent(gate):
    line, _ = gate
    _cards(line, {key: value for key, value in _gate(ack_mode="wait").items() if not key.startswith("confirm_")})

    async def steps():
        await _fire(line)
        await _done()

    _run(steps)
    assert _state()["status"] == "sent" and "confirmed" not in _state()["last_result"]


def test_two_cards_can_share_one_confirmation_signal(gate):
    line, _ = gate
    _cards(line, _gate("kick"), _gate("lamp", name="Reject lamp", target_address="Q0.1"))
    seen = {}

    async def steps():
        await _fire(line)
        await asyncio.sleep(0.03)
        seen["confirmed"] = PLCDispatcherService.confirm_signal(line["id"], SIGNAL)
        await _done()

    _run(steps)
    assert seen["confirmed"] == 2
    assert _state("kick")["status"] == "confirmed" and _state("lamp")["status"] == "confirmed"
    # A signal of another line, or another signal, confirms nothing.
    assert PLCDispatcherService.confirm_signal("other-line", SIGNAL) == 0
    assert PLCDispatcherService.confirm_signal(line["id"], "plcin_other") == 0


def test_a_card_that_is_deleted_while_it_waits_raises_no_alarm(gate):
    line, _ = gate
    _cards(line, _gate(confirm_timeout_ms=5000))

    async def steps():
        await _fire(line)
        await asyncio.sleep(0.03)
        _cards(line)                                        # the card is deleted
        await asyncio.wait_for(_done(), timeout=1.0)

    _run(steps)
    assert _alarm() is None and not PLCDispatcherService._confirm_waiters.get("kick")


# ── From the PLC ──────────────────────────────────────────────────────────────

def test_a_confirm_signals_edge_confirms_through_the_poller(gate):
    line, driver = gate
    _cards(line, _gate(confirm_timeout_ms=1000))

    async def steps():
        driver.values["10001"] = False
        await _pass()
        await _fire(line)
        await asyncio.sleep(0.03)
        await _pass()                                       # still off: the fire keeps waiting
        assert _state()["status"] == "sent"
        driver.values["10001"] = True
        await _pass()
        await _done()

    _run(steps)
    assert _state()["status"] == "confirmed" and _alarm() is None
    assert PLCInputService.get_status(line["id"])[0]["value"] is True
