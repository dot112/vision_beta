"""PLC signals end to end: the real Modbus TCP driver against the PLC simulator.

The simulator of ``test codes/plc_simulator.py`` runs as its own process on a
free port. A coil starts the line and a counter register counts its products,
through the same code a real PLC drives: the driver's read, the signal poller,
the line's counter and the product records.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import app.services.line_service as line_module
import app.services.production_records_service as records_module
from app.events.alarm_events import alarm_manager
from app.hardware.modbus.client import ModbusTCPClient
from app.hardware.plc import factory as plc_factory
from app.hardware.plc.modbus_driver import ModbusTCPDriver
from app.routes.v1.lines import set_line_batch
from app.services.plc_input_service import PLCActor, PLCInputService
from tests.test_joined_cameras import _run, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import WHO, _audit, _enabled, _line_with, _pass, _signal, _status
from tests.test_production_records import _Recorder, _rows, sessions  # noqa: F401  (a fixture)

SIMULATOR = Path(__file__).resolve().parents[1] / "test codes" / "plc_simulator.py"


@pytest.fixture
def simulator():
    """The PLC simulator on a free port, stopped when the test ends. Gives the port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(SIMULATOR), "--port", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                assert process.poll() is None, "the PLC simulator stopped before it listened"
                assert time.monotonic() < deadline, "the PLC simulator did not listen in time"
                time.sleep(0.1)
        yield port
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_a_coil_starts_the_line_and_a_register_counts_products(lines, plc, sessions, simulator, monkeypatch):  # noqa: F811
    endpoint, _ = plc
    endpoint["port"] = simulator
    # No driver in the pool: the factory makes the real one for this channel.
    monkeypatch.setattr(plc_factory, "_driver_pool", {})
    recorder = _Recorder(sessions)
    monkeypatch.setattr(records_module, "production_recorder", recorder)
    PLCInputService.reset()
    alarm_manager.reset()

    line = _line_with(_signal("start_line", address="C5"),
                      _signal("product_sensor", address="R10", data_type="counter"),
                      name="Simulated PLC", enabled=False)
    set_line_batch(line["id"], "LOT-E2E", PLCActor("tester", role="SUPERVISOR"))
    seen = []

    async def steps():
        plc_program = ModbusTCPClient(host="127.0.0.1", port=simulator, timeout=2)
        assert await plc_program.connect()
        try:
            await _pass()                               # the first read is the starting point
            seen.append(_enabled(line))
            assert (await plc_program.write_coil(5, True))[0]
            await _pass()
            seen.append(_enabled(line))
            for counter in (3, 5):
                assert (await plc_program.write_register(10, counter))[0]
                await _pass()
            await recorder.flush()
        finally:
            await plc_program.disconnect()
            for driver in plc_factory._driver_pool.values():
                await driver.disconnect()

    try:
        _run(steps)
        assert isinstance(plc_factory._driver_pool["ep1"], ModbusTCPDriver)
        assert seen == [False, True]
        assert (WHO, "PLC", "START_LINE") in _audit(line)
        assert _status(line, "plcin_product_sensor")["value"] == 5 and _status(line, "plcin_product_sensor")["error"] is None
        runtime = line_module.line_manager.get(line["id"])
        assert (runtime.counter.total_inspected, runtime.counter.good_count) == (5, 5)
        rows = _rows(sessions)
        assert [(row.kind, row.counted, row.result, row.batch, row.line_id) for row in rows] \
            == [("product", True, "good", "LOT-E2E", line["id"])] * 5
    finally:
        PLCInputService.reset()
        alarm_manager.reset()
