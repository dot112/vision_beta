"""Failures in PLC drivers, the PLC dispatcher and the flow engine raise alarms instead of passing silently."""
from __future__ import annotations

import asyncio
import logging
import os
import socket

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production-use-0123456789")

import pytest  # noqa: E402

from app.engines import flow_engine
from app.engines.flow_engine import FlowEngine, _execute_node
from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager
from app.hardware.plc.factory import PLCDriverFactory, _driver_pool
from app.hardware.plc.generic_tcp_driver import GenericTCPDriver
from app.services.plc_dispatcher_service import PLCDispatcherService


@pytest.fixture(autouse=True)
def _reset_alarms():
    alarm_manager.reset()
    yield
    alarm_manager.reset()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _endpoint(port: int, **extra) -> dict:
    return {"id": "ep-line1", "name": "Line 1 PLC", "host": "127.0.0.1", "port": port,
            "timeout": 0.5, "plc_sub_protocol": "generic_tcp", **extra}


# ── PLC drivers ───────────────────────────────────────────────────────────────

def test_failed_plc_connect_raises_critical_alarm_and_success_clears_it():
    async def run():
        port = _closed_port()
        driver = GenericTCPDriver(_endpoint(port))
        assert await driver.connect() is False

        alarm = alarm_manager.get(AlarmCode.PLC_CONNECT_FAILED, "plc:ep-line1")
        assert alarm is not None
        assert alarm.severity == AlarmSeverity.CRITICAL
        assert alarm.details["protocol"] == "generic_tcp"
        assert driver.last_connect_error

        async def accept_and_close(reader, writer):
            writer.close()

        server = await asyncio.start_server(accept_and_close, "127.0.0.1", port)
        try:
            assert await driver.connect() is True
            assert alarm_manager.get(AlarmCode.PLC_CONNECT_FAILED, "plc:ep-line1") is None
            assert driver.last_connect_error == ""
            await driver.disconnect()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_test_connection_driver_does_not_touch_production_alarms():
    async def run():
        driver = PLCDriverFactory.get_driver(_endpoint(_closed_port()), fresh=True)
        assert driver.report_alarms is False
        assert await driver.connect() is False
        assert alarm_manager.active() == []

    asyncio.run(run())


def test_disconnect_error_is_logged_not_swallowed(caplog):
    class BrokenWriter:
        def close(self):
            raise OSError("socket already gone")

    async def run():
        driver = GenericTCPDriver(_endpoint(1))
        driver._writer = BrokenWriter()
        driver.is_connected = True
        with caplog.at_level(logging.WARNING):
            await driver.disconnect()
        assert driver.is_connected is False

    asyncio.run(run())
    assert "did not close cleanly" in caplog.text
    assert "socket already gone" in caplog.text
    alarm = alarm_manager.get(AlarmCode.PLC_DISCONNECT_ERROR, "plc:ep-line1")
    assert alarm is not None
    assert alarm.severity == AlarmSeverity.WARNING
    assert "socket already gone" in alarm.message


def test_disconnect_alarm_clears_on_next_connect_and_retired_drivers_stay_quiet():
    class BrokenWriter:
        def close(self):
            raise OSError("socket already gone")

    async def run():
        async def accept_and_close(reader, writer):
            writer.close()

        server = await asyncio.start_server(accept_and_close, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            driver = GenericTCPDriver(_endpoint(port))
            driver._writer = BrokenWriter()
            driver.is_connected = True
            await driver.disconnect()
            assert alarm_manager.get(AlarmCode.PLC_DISCONNECT_ERROR, "plc:ep-line1") is not None
            assert await driver.connect() is True
            assert alarm_manager.get(AlarmCode.PLC_DISCONNECT_ERROR, "plc:ep-line1") is None
            await driver.disconnect()

            # A driver retired because its endpoint changed closes without alarms.
            pooled = PLCDriverFactory.get_driver(_endpoint(port, id="ep-retired"))
            pooled._writer = BrokenWriter()
            pooled.is_connected = True
            PLCDriverFactory.invalidate("ep-retired")
            await asyncio.sleep(0.05)
            assert alarm_manager.active() == []
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_invalidating_a_driver_clears_its_alarms():
    async def run():
        driver = PLCDriverFactory.get_driver(_endpoint(_closed_port()))
        try:
            await driver.connect()
            assert alarm_manager.get(AlarmCode.PLC_CONNECT_FAILED, "plc:ep-line1") is not None
            PLCDriverFactory.invalidate("ep-line1")
            assert alarm_manager.active() == []
            await asyncio.sleep(0)
        finally:
            _driver_pool.pop("ep-line1", None)

    asyncio.run(run())


# ── PLC dispatcher ────────────────────────────────────────────────────────────

_CARD = {"id": "card-reject", "name": "Reject gate", "operation": "PULSE", "target_address": "5"}


def test_dispatcher_failure_raises_alarm_and_success_clears_it():
    PLCDispatcherService._report_alarm(_CARD, {"id": "ep-line1"}, "failed", "Cannot connect")
    alarm = alarm_manager.get(AlarmCode.PLC_ACTION_FAILED, "plc_card:card-reject")
    assert alarm.severity == AlarmSeverity.CRITICAL
    assert alarm.details["endpoint_id"] == "ep-line1"

    PLCDispatcherService._report_alarm(_CARD, {"id": "ep-line1"}, "sent", "ok")
    assert alarm_manager.active() == []


def test_dispatcher_timeout_raises_unknown_actuation_alarm():
    PLCDispatcherService._report_alarm(_CARD, {"id": "ep-line1"}, "timeout", "timed out")
    alarm = alarm_manager.get(AlarmCode.PLC_ACTION_TIMEOUT, "plc_card:card-reject")
    assert alarm is not None
    assert "actuation state is unknown" in alarm.message


def test_dispatch_to_unreachable_plc_raises_both_alarms(monkeypatch):
    port = _closed_port()
    endpoint = _endpoint(port, id="ep-dispatch")
    monkeypatch.setattr(
        "app.services.settings_persistence_service.SettingsPersistenceService.get_endpoints",
        staticmethod(lambda protocol=None: [endpoint]),
    )
    card = {**_CARD, "plc_endpoint_id": "ep-dispatch", "on_failure": "skip"}

    async def run():
        try:
            return await PLCDispatcherService.dispatch_manual(card)
        finally:
            _driver_pool.pop("ep-dispatch", None)

    result = asyncio.run(run())
    assert result["success"] is False
    assert alarm_manager.get(AlarmCode.PLC_CONNECT_FAILED, "plc:ep-dispatch") is not None
    assert alarm_manager.get(AlarmCode.PLC_ACTION_FAILED, "plc_card:card-reject") is not None


# ── Flow engine ───────────────────────────────────────────────────────────────

def test_failed_flow_output_raises_alarm_and_success_clears_it(monkeypatch):
    node = {"id": "n1", "type": "tcp_out", "config": {}}

    async def run():
        passed, _, _ = await _execute_node("flow-1", node, {"class_name": "defect"})
        assert passed is False
        alarm = alarm_manager.get(AlarmCode.FLOW_ACTION_FAILED, "flow:flow-1/n1")
        assert alarm is not None
        assert "no configured endpoint" in alarm.message

        async def ok_tcp(cfg, ctx, ep):
            return "TCP 10b -> 127.0.0.1:9000"
        monkeypatch.setattr(flow_engine, "_exec_tcp", ok_tcp)
        passed, _, _ = await _execute_node("flow-1", node, {"class_name": "defect"})
        assert passed is True
        assert alarm_manager.get(AlarmCode.FLOW_ACTION_FAILED, "flow:flow-1/n1") is None

    asyncio.run(run())


def test_flow_test_mode_never_raises_alarms():
    node = {"id": "n1", "type": "tcp_out", "config": {}}

    async def run():
        passed, _, _ = await _execute_node("flow-1", node, {"_test": True})
        assert passed is True

    asyncio.run(run())
    assert alarm_manager.active() == []


def test_unexpected_node_exception_raises_node_alarm():
    # A non-numeric delay makes the node raise inside _execute_node.
    node = {"id": "n2", "type": "delay", "config": {"delay_ms": "not-a-number"}}

    async def run():
        passed, _, _ = await _execute_node("flow-2", node, {})
        assert passed is False

    asyncio.run(run())
    alarm = alarm_manager.get(AlarmCode.FLOW_NODE_ERROR, "flow:flow-2/n2")
    assert alarm is not None
    assert "ValueError" in alarm.message


def test_node_error_alarm_clears_when_the_node_runs_cleanly():
    node = {"id": "n2", "type": "delay", "config": {"delay_ms": "not-a-number"}}

    async def run():
        await _execute_node("flow-2", node, {})
        assert alarm_manager.get(AlarmCode.FLOW_NODE_ERROR, "flow:flow-2/n2") is not None
        # A test run of the fixed node must not clear the live flow's alarm.
        fixed = {**node, "config": {"delay_ms": 0}}
        await _execute_node("flow-2", fixed, {"_test": True})
        assert alarm_manager.get(AlarmCode.FLOW_NODE_ERROR, "flow:flow-2/n2") is not None
        passed, _, _ = await _execute_node("flow-2", fixed, {})
        assert passed is True

    asyncio.run(run())
    assert alarm_manager.get(AlarmCode.FLOW_NODE_ERROR, "flow:flow-2/n2") is None


def test_changing_or_removing_a_flow_clears_its_alarms():
    alarm_manager.raise_alarm(AlarmCode.FLOW_NODE_ERROR, "flow:f1/n1", "boom")
    alarm_manager.raise_alarm(AlarmCode.FLOW_RUN_FAILED, "flow:f1", "boom")
    alarm_manager.raise_alarm(AlarmCode.FLOW_RUN_FAILED, "flow:f10", "another flow")

    asyncio.run(FlowEngine().unload_flow("f1"))
    assert [a.source for a in alarm_manager.active()] == ["flow:f10"]


def test_debug_sink_failure_is_logged(caplog):
    def bad_sink(evt):
        raise RuntimeError("websocket closed")

    flow_engine.register_debug_sink(bad_sink)
    try:
        with caplog.at_level(logging.WARNING):
            asyncio.run(flow_engine._emit_debug("f", "n", "passed", "msg"))
    finally:
        flow_engine.unregister_debug_sink(bad_sink)
    assert "websocket closed" in caplog.text


def test_dropped_flow_runs_raise_overload_alarm():
    async def run():
        engine = FlowEngine()
        engine._max_active_runs = 0
        await engine.load_flow({"id": "f", "is_active": True, "nodes": [], "links": []})
        await engine.dispatch("detection", {})

    asyncio.run(run())
    alarm = alarm_manager.get(AlarmCode.FLOW_OVERLOAD, "flow_engine")
    assert alarm is not None
    assert alarm.details["dropped"] == 1


def test_crashed_flow_run_raises_alarm():
    async def run():
        engine = FlowEngine()

        async def boom():
            raise RuntimeError("walk exploded")

        task = asyncio.create_task(boom())
        engine._tasks[task] = "flow-9"
        task.add_done_callback(engine._on_task_done)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(run())
    alarm = alarm_manager.get(AlarmCode.FLOW_RUN_FAILED, "flow:flow-9")
    assert alarm is not None
    assert "walk exploded" in alarm.message


def _finish(engine, coro, flow_id, test=False):
    task = asyncio.create_task(coro)
    engine._tasks[task] = flow_id
    if test:
        engine._test_tasks.add(task)
    task.add_done_callback(engine._on_task_done)
    return task


def test_flow_run_alarm_clears_on_next_clean_run_but_not_on_a_test_run():
    async def boom():
        raise RuntimeError("walk exploded")

    async def fine():
        return None

    async def run():
        engine = FlowEngine()
        await asyncio.gather(_finish(engine, boom(), "flow-9"), return_exceptions=True)
        await asyncio.sleep(0)
        assert alarm_manager.get(AlarmCode.FLOW_RUN_FAILED, "flow:flow-9") is not None
        await _finish(engine, fine(), "flow-9", test=True)
        await asyncio.sleep(0)
        assert alarm_manager.get(AlarmCode.FLOW_RUN_FAILED, "flow:flow-9") is not None
        await _finish(engine, fine(), "flow-9")
        await asyncio.sleep(0)
        assert alarm_manager.get(AlarmCode.FLOW_RUN_FAILED, "flow:flow-9") is None
        # A crashing test run never raises the live flow's alarm.
        await asyncio.gather(_finish(engine, boom(), "flow-9", test=True), return_exceptions=True)
        await asyncio.sleep(0)
        assert alarm_manager.get(AlarmCode.FLOW_RUN_FAILED, "flow:flow-9") is None

    asyncio.run(run())


def test_overload_alarm_clears_once_load_falls_to_half_the_limit():
    async def run():
        engine = FlowEngine()
        engine._max_active_runs = 4
        gate = asyncio.Event()
        await engine.load_flow({"id": "f", "is_active": True, "nodes": [], "links": []})
        tasks = [_finish(engine, gate.wait(), "f") for _ in range(4)]
        await engine.dispatch("detection", {})
        assert alarm_manager.get(AlarmCode.FLOW_OVERLOAD, "flow_engine") is not None
        gate.set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        assert alarm_manager.get(AlarmCode.FLOW_OVERLOAD, "flow_engine") is None

    asyncio.run(run())


def test_startup_migrations_keep_app_logging_enabled(client):
    # The session client ran the real startup, including the alembic migrations.
    assert client.get("/health").status_code in (200, 503)
    assert not logging.getLogger("app.engines.flow_engine").disabled
    assert not logging.getLogger("app.hardware.plc.base").disabled
