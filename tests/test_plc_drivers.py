"""Unit tests for PLC hardware drivers and factory."""
from __future__ import annotations

import asyncio
import pytest

from app.hardware.plc.modbus_driver import ModbusTCPDriver, _parse_modbus_address
from app.hardware.plc.factory import PLCDriverFactory, _driver_pool


# ── Address Parser ────────────────────────────────────────────────────────────

class TestModbusAddressParser:
    def test_bare_numeric_coil(self):
        t, i = _parse_modbus_address("10")
        assert t == "coil" and i == 10

    def test_c_prefix_coil(self):
        t, i = _parse_modbus_address("C5")
        assert t == "coil" and i == 5

    def test_r_prefix_register(self):
        t, i = _parse_modbus_address("R20")
        assert t == "register" and i == 20

    def test_4xxxx_register(self):
        t, i = _parse_modbus_address("40010")
        assert t == "register" and i == 9

    def test_40001_register_zero(self):
        t, i = _parse_modbus_address("40001")
        assert t == "register" and i == 0


# ── ModbusTCPDriver ───────────────────────────────────────────────────────────

def _make_driver():
    # Port 1 on loopback is never a Modbus server, so connect() falls back to
    # the in-memory simulator that stands in for a PLC in these tests.
    ep = {"id": "ep1", "host": "127.0.0.1", "port": 1, "timeout": 1,
          "plc_sub_protocol": "modbus_tcp", "simulation_mode": True}
    return ModbusTCPDriver(ep)


class TestModbusTCPDriver:
    def test_connect_and_set_coil(self):
        async def _run():
            drv = _make_driver()
            ok = await drv.connect()
            assert ok is True
            ok2, msg = await drv.set("5")
            assert ok2 is True
            assert "coil" in msg.lower()
        asyncio.run(_run())

    def test_reset_coil(self):
        async def _run():
            drv = _make_driver()
            await drv.connect()
            ok, msg = await drv.reset("C3")
            assert ok is True
            assert "OFF" in msg
        asyncio.run(_run())

    def test_pulse_coil(self):
        async def _run():
            drv = _make_driver()
            await drv.connect()
            ok, msg = await drv.pulse("0", 20)
            assert ok is True
            assert "PULSE" in msg
        asyncio.run(_run())

    def test_write_register(self):
        async def _run():
            drv = _make_driver()
            await drv.connect()
            ok, msg = await drv.write("R10", 255)
            assert ok is True
            assert "255" in msg
        asyncio.run(_run())

    def test_write_value_outside_16bit_is_rejected(self):
        async def _run():
            drv = _make_driver()
            await drv.connect()
            ok, msg = await drv.write("R0", 70000)
            assert ok is False
            assert "0 to 65535" in msg
        asyncio.run(_run())

    def test_execute_operation_dispatch(self):
        async def _run():
            drv = _make_driver()
            await drv.connect()
            ok, _ = await drv.execute_operation("SET", "1")
            assert ok is True
            ok2, _ = await drv.execute_operation("RESET", "1")
            assert ok2 is True
            ok3, msg3 = await drv.execute_operation("UNKNOWN_OP", "1")
            assert ok3 is False
            assert "Unknown" in msg3
        asyncio.run(_run())

    def test_address_out_of_range(self):
        async def _run():
            drv = _make_driver()
            await drv.connect()
            ok, msg = await drv.set("999")
            assert ok is False
            assert "out of range" in msg.lower()
        asyncio.run(_run())

    def test_disconnect_resets_flag(self):
        async def _run():
            drv = _make_driver()
            await drv.connect()
            await drv.disconnect()
            assert drv.is_connected is False
        asyncio.run(_run())


# ── PLCDriverFactory ──────────────────────────────────────────────────────────

class TestPLCDriverFactory:
    def setup_method(self):
        _driver_pool.clear()

    def test_returns_modbus_driver(self):
        ep = {"id": "ep-mb", "plc_sub_protocol": "modbus_tcp", "host": "127.0.0.1", "port": 502, "timeout": 1}
        drv = PLCDriverFactory.get_driver(ep)
        assert isinstance(drv, ModbusTCPDriver)

    def test_caches_driver(self):
        ep = {"id": "ep-cache", "plc_sub_protocol": "modbus_tcp", "host": "127.0.0.1", "port": 502, "timeout": 1}
        drv1 = PLCDriverFactory.get_driver(ep)
        drv2 = PLCDriverFactory.get_driver(ep)
        assert drv1 is drv2

    def test_fresh_skips_cache(self):
        ep = {"id": "ep-fresh", "plc_sub_protocol": "modbus_tcp", "host": "127.0.0.1", "port": 502, "timeout": 1}
        drv1 = PLCDriverFactory.get_driver(ep)
        drv2 = PLCDriverFactory.get_driver(ep, fresh=True)
        assert drv1 is not drv2
        assert _driver_pool.get("ep-fresh") is drv1

    def test_invalidate_removes_from_pool(self):
        ep = {"id": "ep-inv", "plc_sub_protocol": "modbus_tcp", "host": "127.0.0.1", "port": 502, "timeout": 1}
        PLCDriverFactory.get_driver(ep)
        assert "ep-inv" in _driver_pool
        PLCDriverFactory.invalidate("ep-inv")
        assert "ep-inv" not in _driver_pool

    def test_fins_has_no_unsafe_generic_fallback(self):
        ep = {"id": "ep-fins", "plc_sub_protocol": "fins", "host": "127.0.0.1", "port": 9600, "timeout": 1}
        with pytest.raises(ValueError, match="no native driver"):
            PLCDriverFactory.get_driver(ep, fresh=True)

    def test_pool_ids(self):
        ep = {"id": "ep-ids", "plc_sub_protocol": "modbus_tcp", "host": "127.0.0.1", "port": 502, "timeout": 1}
        PLCDriverFactory.get_driver(ep)
        assert "ep-ids" in PLCDriverFactory.pool_ids()

    def test_clear_all(self):
        ep = {"id": "ep-clear", "plc_sub_protocol": "modbus_tcp", "host": "127.0.0.1", "port": 502, "timeout": 1}
        PLCDriverFactory.get_driver(ep)
        PLCDriverFactory.clear_all()
        assert PLCDriverFactory.pool_ids() == []
