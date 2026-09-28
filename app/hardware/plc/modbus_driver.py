"""Modbus TCP PLC driver — extends the existing ModbusTCPClient."""
from __future__ import annotations

import asyncio
from typing import Tuple

from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _parse_modbus_address(address: str) -> Tuple[str, int]:
    """
    Parse a Modbus address string into (type, index).

    Supported formats:
      "10"    → coil 10
      "C10"   → coil 10
      "R10"   → holding register 10
      "40010" → holding register 10  (standard Modbus 4xxxx notation)
    """
    addr = address.strip().upper()
    if addr.startswith("R"):
        return "register", int(addr[1:])
    if addr.startswith("C"):
        return "coil", int(addr[1:])
    numeric = int(addr)
    if numeric >= 40001:
        return "register", numeric - 40001
    return "coil", numeric


class ModbusTCPDriver(PLCDriver):
    """
    Modbus TCP Master driver.

    Uses the existing ModbusTCPClient internally but manages its own
    per-endpoint connection instance (not the global singleton).
    """

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        from app.hardware.modbus.client import ModbusTCPClient
        self._client = ModbusTCPClient(
            host=self.host,
            port=self.port,
            timeout=int(self.timeout),
            unit_id=int(endpoint.get("modbus_unit_id", 1)),
            simulation_mode=bool(endpoint.get("simulation_mode", False)),
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            ok = await asyncio.wait_for(self._client.connect(), timeout=self.timeout)
            self.is_connected = bool(ok)
            if self.is_connected:
                logger.info("ModbusTCP connected: %s:%d", self.host, self.port)
            return self.is_connected
        except asyncio.TimeoutError:
            logger.warning("ModbusTCP connect timeout: %s:%d", self.host, self.port)
            self.is_connected = False
            return False
        except Exception as exc:
            logger.error("ModbusTCP connect error: %s", exc)
            self.is_connected = False
            return False

    async def disconnect(self) -> None:
        try:
            await self._client.disconnect()
        except Exception:
            pass
        self.is_connected = False

    # ── Operations ────────────────────────────────────────────────────────────

    async def set(self, address: str) -> Tuple[bool, str]:
        addr_type, idx = _parse_modbus_address(address)
        if addr_type == "register":
            ok, err = await self._client.write_register(idx, 1)
            return ok, f"SET register {idx} = 1" if ok else f"SET register error: {err}"
        ok, err = await self._client.write_coil(idx, True)
        return ok, f"SET coil {idx} = ON" if ok else f"SET coil error: {err}"

    async def reset(self, address: str) -> Tuple[bool, str]:
        addr_type, idx = _parse_modbus_address(address)
        if addr_type == "register":
            ok, err = await self._client.write_register(idx, 0)
            return ok, f"RESET register {idx} = 0" if ok else f"RESET register error: {err}"
        ok, err = await self._client.write_coil(idx, False)
        return ok, f"RESET coil {idx} = OFF" if ok else f"RESET coil error: {err}"

    async def toggle(self, address: str) -> Tuple[bool, str]:
        addr_type, idx = _parse_modbus_address(address)
        if addr_type != "coil":
            return False, "TOGGLE requires a Modbus coil address; register targets are not BOOLs"
        ok, values, err = await self._client.read_coils(idx, 1)
        if not ok or not values:
            return False, f"TOGGLE coil {idx} read error: {err or 'no value returned'}"
        new_value = not values[0]
        ok, err = await self._client.write_coil(idx, new_value)
        if not ok:
            return False, f"TOGGLE coil {idx} write error: {err}"
        return True, f"TOGGLE coil {idx}: {'ON' if values[0] else 'OFF'} → {'ON' if new_value else 'OFF'}"

    async def pulse(self, address: str, duration_ms: int) -> Tuple[bool, str]:
        addr_type, idx = _parse_modbus_address(address)
        # SET
        if addr_type == "register":
            ok, err = await self._client.write_register(idx, 1)
        else:
            ok, err = await self._client.write_coil(idx, True)
        if not ok:
            return False, f"PULSE ON error: {err}"
        try:
            await asyncio.sleep(duration_ms / 1000.0)
        finally:
            reset_task = asyncio.create_task(
                self._client.write_register(idx, 0) if addr_type == "register"
                else self._client.write_coil(idx, False)
            )
            try:
                ok2, err2 = await asyncio.shield(reset_task)
            except asyncio.CancelledError:
                await reset_task
                raise
        if not ok2:
            return False, f"PULSE OFF error (coil may be stuck ON): {err2}"
        return True, f"PULSE {addr_type} {idx} for {duration_ms} ms — OK"

    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        addr_type, idx = _parse_modbus_address(address)
        if addr_type == "register":
            try:
                int_val = int(value)
            except (TypeError, ValueError, OverflowError):
                return False, "WRITE register error: value must be an integer from 0 to 65535"
            if int_val != value or not 0 <= int_val <= 0xFFFF:
                return False, "WRITE register error: value must be an integer from 0 to 65535"
            ok, err = await self._client.write_register(idx, int_val)
            return ok, f"WRITE register {idx} = {int_val}" if ok else f"WRITE error: {err}"
        # For coils, treat non-zero as True
        ok, err = await self._client.write_coil(idx, bool(value))
        return ok, f"WRITE coil {idx} = {bool(value)}" if ok else f"WRITE coil error: {err}"
