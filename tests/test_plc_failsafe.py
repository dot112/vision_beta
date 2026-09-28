"""Tests for the opt-in PLC fail-safe layer: safe states, link-loss recovery, heartbeat."""
from __future__ import annotations

import asyncio

import pytest

from app.hardware.plc import factory as plc_factory
from app.hardware.plc.base import PLCDriver
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.plc_failsafe_service import (
    PLCFailsafeService,
    safe_operation,
    validate_safe_state,
)
from app.services.settings_persistence_service import SettingsPersistenceService


class FakeDriver(PLCDriver):
    """In-memory PLC: records every write and can simulate a dead link."""

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        self.outputs: dict = {}
        self.ops: list = []
        self.reachable = True

    async def connect(self) -> bool:
        self.is_connected = self.reachable
        return self.is_connected

    async def disconnect(self) -> None:
        self.is_connected = False

    async def _write(self, op, address, value):
        if not self.reachable:
            self.is_connected = False
            raise ConnectionError("link down")
        self.ops.append((op, address, value))
        self.outputs[address] = value
        return True, f"{op} {address}={value}"

    async def set(self, address):
        return await self._write("SET", address, 1)

    async def reset(self, address):
        return await self._write("RESET", address, 0)

    async def pulse(self, address, duration_ms):
        return await self._write("PULSE", address, 0)

    async def write(self, address, value):
        return await self._write("WRITE", address, value)


ENDPOINT = {"id": "ep1", "name": "Line PLC", "protocol": "plc", "enabled": True,
            "plc_sub_protocol": "modbus_tcp", "host": "127.0.0.1", "port": 502,
            "timeout": 1, "heartbeat": 1, "reconnect_interval": 1}


def _card(cid, address, safe_state="none", **extra):
    return {"id": cid, "name": cid, "enabled": True, "plc_endpoint_id": "ep1",
            "trigger_type": "line_cross", "trigger_condition": "any",
            "operation": "SET", "target_address": address, "rearm_lockout_ms": 0,
            "execution_policy": "every_frame", "safe_state": safe_state, **extra}


@pytest.fixture
def plc(monkeypatch):
    endpoint = dict(ENDPOINT)
    monkeypatch.setattr(SettingsPersistenceService, "get_endpoints",
                        classmethod(lambda cls, protocol=None: [endpoint]))
    driver = FakeDriver(endpoint)
    monkeypatch.setattr(plc_factory, "_driver_pool", {"ep1": driver})
    PLCDispatcherService._cards = []
    PLCDispatcherService._states = {}
    PLCDispatcherService._endpoint_locks = {}
    PLCFailsafeService.reset()
    yield endpoint, driver
    PLCDispatcherService._cards = []
    PLCFailsafeService.reset()


# ── Configuration ─────────────────────────────────────────────────────────────

class TestSafeStateConfig:
    def test_default_is_disabled(self):
        assert safe_operation({}) is None
        assert safe_operation({"safe_state": "none"}) is None

    def test_operations(self):
        assert safe_operation({"safe_state": "reset"}) == ("RESET", 0.0)
        assert safe_operation({"safe_state": "OFF"}) == ("RESET", 0.0)
        assert safe_operation({"safe_state": "set"}) == ("SET", 0.0)
        assert safe_operation({"safe_state": "write", "safe_value": 7}) == ("WRITE", 7.0)

    def test_invalid_safe_state_rejected(self):
        with pytest.raises(ValueError):
            validate_safe_state({"id": "c1", "safe_state": "rest"})
        with pytest.raises(ValueError):
            validate_safe_state({"id": "c1", "safe_state": "write", "safe_value": "abc"})
        validate_safe_state({"id": "c1"})  # absent is fine

    def test_targets_skip_disabled_and_other_endpoints(self, plc):
        PLCDispatcherService.set_cards([
            _card("a", "10", "reset"),
            _card("b", "11", "reset", enabled=False),
            _card("c", "12", "reset", plc_endpoint_id="other"),
            _card("d", "13"),
        ])
        assert PLCFailsafeService.safe_targets("ep1") == [("10", "RESET", 0.0)]


# ── Behaviour ─────────────────────────────────────────────────────────────────

class TestFailsafeBehaviour:
    def test_no_opt_in_means_no_writes(self, plc):
        endpoint, driver = plc
        PLCDispatcherService.set_cards([_card("a", "10")])

        async def _run():
            PLCFailsafeService.start()
            await PLCFailsafeService.tick(now=100.0)
            await PLCFailsafeService.shutdown()

        asyncio.run(_run())
        assert driver.ops == []

    def test_startup_applies_safe_state(self, plc):
        endpoint, driver = plc
        PLCDispatcherService.set_cards([_card("a", "10", "reset"), _card("b", "R5", "write", safe_value=3)])

        async def _run():
            PLCFailsafeService.start()
            PLCFailsafeService._task.cancel()
            await PLCFailsafeService.tick(now=100.0)

        asyncio.run(_run())
        assert ("RESET", "10", 0) in driver.ops
        assert ("WRITE", "R5", 3.0) in driver.ops
        assert not PLCFailsafeService.needs_safe_state("ep1")

    def test_link_loss_then_recovery_applies_safe_state(self, plc):
        endpoint, driver = plc
        PLCDispatcherService.set_cards([_card("a", "10", "reset")])

        async def _run():
            await driver.connect()
            await PLCFailsafeService.tick(now=100.0)          # healthy, nothing pending
            assert driver.ops == []
            driver.outputs["10"] = 1                          # output held ON
            driver.reachable = False
            driver.is_connected = False                       # link drops
            await PLCFailsafeService.tick(now=101.0)
            assert PLCFailsafeService.needs_safe_state("ep1")
            assert driver.ops == []                           # cannot reach PLC yet
            driver.reachable = True
            await PLCFailsafeService.tick(now=103.0)          # reconnect after interval

        asyncio.run(_run())
        assert driver.outputs["10"] == 0
        assert not PLCFailsafeService.needs_safe_state("ep1")

    def test_dispatch_failure_marks_link_and_next_dispatch_restores_safe_state_first(self, plc):
        endpoint, driver = plc
        PLCDispatcherService.set_cards([_card("a", "10", "reset")])
        card = PLCDispatcherService._cards[0]

        async def _run():
            driver.reachable = False
            await PLCDispatcherService.dispatch_manual(card)
            assert PLCFailsafeService.needs_safe_state("ep1")
            driver.reachable = True
            await PLCDispatcherService.dispatch_manual(card)

        asyncio.run(_run())
        assert driver.ops == [("RESET", "10", 0), ("SET", "10", 1)]
        assert not PLCFailsafeService.needs_safe_state("ep1")

    def test_dispatch_failure_without_opt_in_does_not_flag(self, plc):
        endpoint, driver = plc
        PLCDispatcherService.set_cards([_card("a", "10")])

        async def _run():
            driver.reachable = False
            await PLCDispatcherService.dispatch_manual(PLCDispatcherService._cards[0])

        asyncio.run(_run())
        assert not PLCFailsafeService.needs_safe_state("ep1")

    def test_shutdown_applies_safe_state(self, plc):
        endpoint, driver = plc
        PLCDispatcherService.set_cards([_card("a", "10", "reset")])

        async def _run():
            await driver.connect()
            await driver.set("10")
            await PLCFailsafeService.shutdown()

        asyncio.run(_run())
        assert driver.outputs["10"] == 0


# ── Heartbeat ─────────────────────────────────────────────────────────────────

class TestHeartbeat:
    def test_heartbeat_toggles_on_interval(self, plc):
        endpoint, driver = plc
        endpoint["heartbeat_address"] = "100"

        async def _run():
            await driver.connect()
            for now in (100.0, 100.5, 101.0, 102.0):
                await PLCFailsafeService.tick(now=now)

        asyncio.run(_run())
        beats = [value for op, addr, value in driver.ops if addr == "100"]
        assert beats == [1.0, 0.0, 1.0]   # 100.5 was inside the 1 s interval
        assert PLCFailsafeService.get_status()[0]["heartbeat_ok"] is True

    def test_heartbeat_failure_flags_link_lost(self, plc):
        endpoint, driver = plc
        endpoint["heartbeat_address"] = "100"
        PLCDispatcherService.set_cards([_card("a", "10", "reset")])

        async def _run():
            await driver.connect()
            await PLCFailsafeService.tick(now=100.0)
            driver.reachable = False
            await PLCFailsafeService.tick(now=102.0)

        asyncio.run(_run())
        assert PLCFailsafeService.needs_safe_state("ep1")
        assert PLCFailsafeService.get_status()[0]["heartbeat_ok"] is False


# ── Real drivers on the wire ──────────────────────────────────────────────────

def _use_real_endpoint(monkeypatch, endpoint):
    monkeypatch.setattr(SettingsPersistenceService, "get_endpoints",
                        classmethod(lambda cls, protocol=None: [endpoint]))
    monkeypatch.setattr(plc_factory, "_driver_pool", {})
    PLCDispatcherService._endpoint_locks = {}
    PLCFailsafeService.reset()


async def _startup_tick_and_shutdown(endpoint, safe_address):
    PLCDispatcherService.set_cards([_card("a", safe_address, "reset", plc_endpoint_id=endpoint["id"])])
    PLCFailsafeService.start()
    PLCFailsafeService._task.cancel()
    await PLCFailsafeService.tick(now=100.0)
    assert not PLCFailsafeService.needs_safe_state(endpoint["id"])
    status = PLCFailsafeService.get_status()[0]
    assert status["heartbeat_ok"] is True, status
    await plc_factory.PLCDriverFactory.close_all()


class TestNewProtocolDrivers:
    """Safe state and heartbeat through the Modbus RTU, MELSEC and FINS drivers."""

    def teardown_method(self):
        PLCDispatcherService._cards = []
        PLCFailsafeService.reset()

    def test_modbus_rtu(self, monkeypatch):
        from tests.test_plc_protocols import FakeRTUBus, _rtu_endpoint, _serve

        async def run():
            bus = FakeRTUBus()
            bus.coils[1][3] = True                        # output left ON by a crash
            server, port = await _serve(bus.handle)
            async with server:
                endpoint = _rtu_endpoint(port, name="RTU", enabled=True, heartbeat=1,
                                         heartbeat_address="C10")
                _use_real_endpoint(monkeypatch, endpoint)
                await _startup_tick_and_shutdown(endpoint, "C3")
            return bus

        bus = asyncio.run(run())
        assert bus.coils[1][3] is False
        assert bus.coils[1][10] is True

    def test_melsec(self, monkeypatch):
        from tests.test_plc_protocols import FakeMelsec, _serve

        async def run():
            fake = FakeMelsec()
            server, port = await _serve(fake.handle)
            async with server:
                endpoint = {"id": "mc", "name": "MELSEC", "enabled": True, "plc_sub_protocol": "melsec",
                            "host": "127.0.0.1", "port": port, "timeout": 1, "heartbeat": 1,
                            "heartbeat_address": "M100"}
                _use_real_endpoint(monkeypatch, endpoint)
                await _startup_tick_and_shutdown(endpoint, "Y10")
            return fake

        fake = asyncio.run(run())
        assert fake.writes == [(0x9D, 0x10, 1, b"\x00"), (0x90, 100, 1, b"\x10")]

    def test_fins(self, monkeypatch):
        from tests.test_plc_protocols import FakeFins, _serve

        async def run():
            fake = FakeFins()
            server, port = await _serve(fake.handle)
            async with server:
                endpoint = {"id": "f", "name": "FINS", "enabled": True, "plc_sub_protocol": "fins",
                            "host": "127.0.0.1", "port": port, "timeout": 1, "heartbeat": 1,
                            "heartbeat_address": "W10.00"}
                _use_real_endpoint(monkeypatch, endpoint)
                await _startup_tick_and_shutdown(endpoint, "CIO 0.05")
            return fake

        fake = asyncio.run(run())
        assert fake.writes == [(1, 0x30, 0, 5, b"\x00"), (1, 0x31, 10, 0, b"\x01")]
