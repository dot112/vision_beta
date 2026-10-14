"""An input sees what an output action's pulse makes the PLC do.

The owner's test rig (2026-10-14): a product crossing pulses Tag_1 for 200 ms
on an S7-1500 over OPC UA, the PLC program copies Tag_1 to Tag_2, and an input
reads Tag_2 every 50 ms to take a code picture. The pulse used to hold the PLC
connection for its whole length, so the input could only read Tag_2 before
and after the pulse, when it is off both times, and never saw it.
"""
from __future__ import annotations

import asyncio

import pytest

from app.events.alarm_events import alarm_manager
from app.hardware.plc import factory as plc_factory
from app.services.plc_dispatcher_service import PLCDispatcherService, _CardState, _normalize_card
from app.services.plc_input_service import PLCInputService
from tests.test_joined_cameras import _run, lines  # noqa: F401  (a fixture)
from tests.test_opcua_tag_names import SIEMENS, _driver
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import _line_with, _signal, acts  # noqa: F401  (a fixture)

TAG_1, TAG_2 = 'ns=3;s="Tag_1"', 'ns=3;s="Tag_2"'


class _Program(dict):
    """The PLC's memory, with its program: Tag_2 is whatever Tag_1 is."""

    def __getitem__(self, node_id):
        return super().__getitem__(TAG_1 if node_id == TAG_2 else node_id)


@pytest.fixture
def s7(lines, plc, monkeypatch):  # noqa: F811
    """The real OPC UA driver on channel ep1, talking to a stand-in S7-1500 that runs the program above."""
    driver, server = _driver(SIEMENS, {TAG_1: False, TAG_2: False})
    server.values = _Program(server.values)
    monkeypatch.setattr(plc_factory, "_driver_pool", {"ep1": driver})
    PLCInputService.reset()
    alarm_manager.reset()
    yield driver, server
    PLCInputService.reset()
    alarm_manager.reset()


def _pulse_card(pulse_ms=200):
    return _normalize_card({"id": "kick", "name": "Kick", "enabled": True, "plc_endpoint_id": "ep1",
                            "operation": "pulse", "target_address": "Tag_1", "pulse_duration_ms": pulse_ms})


def _input(**extra):
    return _signal("start_line", address="Tag_2", poll_ms=50, edge="rising", **extra)


def _pulses_seen(pulses, pulse_ms=200, gap_s=0.5, read_seconds=0.0, s7=None):
    """Pulse Tag_1 ``pulses`` times while the input service polls Tag_2; returns what the input did."""
    driver, server = s7
    if read_seconds:
        plain_read = driver._read

        async def slow_read(address, data_type):
            await asyncio.sleep(read_seconds)       # the PLC's answer takes this long
            return await plain_read(address, data_type)

        driver._read = slow_read
    card = _pulse_card(pulse_ms)
    results = []

    async def steps():
        PLCInputService.start()
        try:
            await asyncio.sleep(0.15)               # the first read is the starting point
            for n in range(pulses):
                state = _CardState(card_id="kick")
                await PLCDispatcherService._dispatch(card, state, {"event_id": f"e{n}"})
                results.append(state.status)
                await asyncio.sleep(gap_s)
        finally:
            await PLCInputService.stop()

    _run(steps)
    assert results == ["sent"] * pulses
    assert [value for node, value in server.writes] == [True, False] * pulses
    return results


def test_an_input_sees_a_200_ms_pulse_of_an_output_action(s7, acts):  # noqa: F811
    _line_with(_input())
    _pulses_seen(1, s7=s7)
    assert acts == [("plcin_start_line", False, True, 1)]


def test_an_input_sees_every_pulse_of_a_running_line(s7, acts):  # noqa: F811
    _line_with(_input())
    _pulses_seen(5, gap_s=0.3, s7=s7)
    assert acts == [("plcin_start_line", False, True, 1)] * 5


def test_an_input_sees_a_pulse_when_the_plc_answers_slowly(s7, acts):  # noqa: F811
    # An OPC UA read over the network takes some milliseconds; a busy server takes more.
    _line_with(_input())
    _pulses_seen(3, gap_s=0.3, read_seconds=0.03, s7=s7)
    assert acts == [("plcin_start_line", False, True, 1)] * 3


def test_a_pulse_cancelled_while_it_waits_still_turns_off_and_frees_the_connection(s7):
    driver, server = s7

    async def steps():
        pulse = asyncio.create_task(driver.execute_operation("PULSE", "Tag_1", pulse_duration_ms=500))
        await asyncio.sleep(0.1)
        pulse.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pulse
        assert not driver._operation_lock.locked()
        return await asyncio.wait_for(driver.read("Tag_2"), timeout=1)

    ok, value, _ = asyncio.run(steps())
    assert (ok, value) == (True, False)
    assert [value for node, value in server.writes] == [True, False]


def test_a_read_that_times_out_during_a_pulse_drops_the_link_only_after_the_pulse(s7, acts):  # noqa: F811
    # The channel's timeout is 1 s, so a read gives up after 2 s; this pulse lasts 2.5 s.
    driver, server = s7
    _line_with(_input())
    plain_read, plain_disconnect = driver._read, driver.disconnect
    hang = asyncio.Event()
    order = []

    async def read(address, data_type):
        if hang.is_set():
            await asyncio.sleep(10)                 # the PLC stops answering reads
        return await plain_read(address, data_type)

    async def disconnect():
        order.append(("disconnect", [value for node, value in server.writes]))
        await plain_disconnect()

    driver._read, driver.disconnect = read, disconnect
    card = _pulse_card(2500)

    async def steps():
        await PLCInputService.poll_once("ep1")      # the starting point
        state = _CardState(card_id="kick")
        kick = asyncio.create_task(PLCDispatcherService._dispatch(card, state, {"event_id": "e"}))
        await asyncio.sleep(0.1)                    # the output is on
        hang.set()
        await PLCInputService.poll_once("ep1")
        await kick
        return state.status

    status = []
    _run(lambda: _keep(steps(), status))
    assert status == ["sent"]
    assert [value for node, value in server.writes] == [True, False]
    assert order == [("disconnect", [True, False])]   # the output was off before the link was dropped


async def _keep(coroutine, into):
    into.append(await coroutine)
