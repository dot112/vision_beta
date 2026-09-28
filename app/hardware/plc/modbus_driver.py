"""Modbus TCP PLC driver — extends the existing ModbusTCPClient."""
from __future__ import annotations

import asyncio
import math
import struct
from typing import List, Tuple

from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Register data types accepted as an address suffix, e.g. "R10:DINT" or "40001:REAL".
# value → (registers, struct format for one big-endian value)
_REGISTER_TYPES = {
    "UINT": (1, ">H"),
    "WORD": (1, ">H"),
    "INT": (1, ">h"),
    "UDINT": (2, ">I"),
    "DWORD": (2, ">I"),
    "DINT": (2, ">i"),
    "REAL": (2, ">f"),
    "FLOAT": (2, ">f"),
}


def _parse_modbus_target(address: str) -> Tuple[str, int, str]:
    """
    Parse a Modbus address string into (type, index, data_type).

    Supported formats:
      "10"      → coil 10
      "C10"     → coil 10
      "R10"     → holding register 10
      "40010"   → holding register 9   (standard 1-based 4xxxx notation, 40001 = register 0)
      "400010"  → holding register 9   (6-digit 4xxxxx notation)
      "R10:DINT" / "40001:REAL" → 32-bit value across two registers (INT, UINT, DINT, UDINT, REAL)

    Discrete inputs (1xxxx) and input registers (3xxxx) are read-only in Modbus,
    so those notations are rejected instead of being written as coils.
    """
    addr = str(address or "").strip().upper()
    data_type = ""
    if ":" in addr:
        addr, data_type = (part.strip() for part in addr.split(":", 1))
        if data_type not in _REGISTER_TYPES:
            raise ValueError(
                f"Unknown Modbus data type '{data_type}'; use INT, UINT, DINT, UDINT or REAL"
            )
    if not addr:
        raise ValueError("Modbus address is empty")

    if addr.startswith("HR"):
        kind, index = "register", _modbus_int(addr[2:], address)
    elif addr.startswith("R"):
        kind, index = "register", _modbus_int(addr[1:], address)
    elif addr.startswith("C"):
        kind, index = "coil", _modbus_int(addr[1:], address)
    else:
        numeric = _modbus_int(addr, address)
        digits = len(addr)
        if digits == 6 and addr[0] in "13":
            raise ValueError(_read_only_message(address))
        if digits == 6 and addr[0] == "4" and numeric >= 400001:
            kind, index = "register", numeric - 400001
        elif 40001 <= numeric <= 49999 and digits == 5:
            kind, index = "register", numeric - 40001
        elif digits == 5 and (10001 <= numeric <= 19999 or 30001 <= numeric <= 39999):
            raise ValueError(_read_only_message(address))
        elif numeric >= 40001:
            kind, index = "register", numeric - 40001
        else:
            kind, index = "coil", numeric

    if not 0 <= index <= 0xFFFF:
        raise ValueError(f"Modbus address '{address}' is outside 0..65535")
    if data_type and kind != "register":
        raise ValueError("Modbus data types can only be used with holding register addresses")
    return kind, index, data_type or ("UINT" if kind == "register" else "")


def _modbus_int(text: str, original: str) -> int:
    if not text.isdigit():
        raise ValueError(f"Invalid Modbus address '{original}'")
    return int(text)


def _read_only_message(address: str) -> str:
    return (
        f"Modbus address '{address}' is a discrete input (1xxxx) or input register (3xxxx), "
        "which are read-only; use a coil (C..) or holding register (4xxxx / R..) for outputs"
    )


def _parse_modbus_address(address: str) -> Tuple[str, int]:
    """Backward-compatible (type, index) view of _parse_modbus_target()."""
    kind, index, _ = _parse_modbus_target(address)
    return kind, index


def _encode_register_value(value: float, data_type: str, word_order: str) -> List[int]:
    """Encode a number into 16-bit registers for the given data type."""
    count, fmt = _REGISTER_TYPES[data_type]
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"WRITE value must be a number for {data_type}") from exc
    if not math.isfinite(numeric):
        raise ValueError("WRITE value must be finite")
    if fmt[-1] == "f":
        payload = struct.pack(fmt, numeric)
        if not math.isfinite(struct.unpack(fmt, payload)[0]):
            raise ValueError("WRITE value is outside the REAL (float32) range")
    else:
        if not numeric.is_integer():
            raise ValueError(f"WRITE value must be a whole number for {data_type}")
        try:
            payload = struct.pack(fmt, int(numeric))
        except struct.error as exc:
            ranges = {
                "UINT": "0 to 65535", "WORD": "0 to 65535", "INT": "-32768 to 32767",
                "UDINT": "0 to 4294967295", "DWORD": "0 to 4294967295",
                "DINT": "-2147483648 to 2147483647",
            }
            raise ValueError(f"WRITE value must be an integer from {ranges[data_type]} for {data_type}") from exc
    words = [struct.unpack(">H", payload[i:i + 2])[0] for i in range(0, len(payload), 2)]
    if count == 2 and word_order == "low_first":
        words.reverse()
    return words


class ModbusTCPDriver(PLCDriver):
    """
    Modbus TCP Master driver.

    Uses the existing ModbusTCPClient internally but manages its own
    per-endpoint connection instance (not the global singleton).

    Endpoint options:
      modbus_unit_id     unit / slave id (default 1)
      modbus_word_order  "high_first" (default, ABCD) or "low_first" (CDAB) for 32-bit values
    """

    _label = "ModbusTCP"

    def __init__(self, endpoint: dict):
        from app.hardware.modbus.client import ModbusTCPClient
        self._client = None
        self._connected_flag = False
        super().__init__(endpoint)
        self._client = ModbusTCPClient(
            host=self.host,
            port=self.port,
            timeout=self.timeout,
            unit_id=int(endpoint.get("modbus_unit_id", 1)),
            simulation_mode=bool(endpoint.get("simulation_mode", False)),
        )
        order = str(endpoint.get("modbus_word_order", "high_first") or "high_first").strip().lower()
        self.word_order = "low_first" if order in ("low_first", "cdab", "swapped", "little") else "high_first"

    # The client drops its socket on any I/O error. Report that as a lost
    # connection so the dispatcher reconnects instead of failing forever.
    @property
    def is_connected(self) -> bool:
        return self._connected_flag and self._client is not None and self._client.is_connected

    @is_connected.setter
    def is_connected(self, value: bool) -> None:
        self._connected_flag = bool(value)

    def _target(self) -> str:
        return f"{self.host}:{self.port}"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            ok = await asyncio.wait_for(self._client.connect(), timeout=self.timeout)
            self.is_connected = bool(ok)
            if self.is_connected:
                logger.info("%s connected: %s", self._label, self._target())
            return self.is_connected
        except asyncio.TimeoutError:
            logger.warning("%s connect timeout: %s", self._label, self._target())
            self.is_connected = False
            return False
        except Exception as exc:
            logger.error("%s connect error: %s", self._label, exc)
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
        addr_type, idx, data_type = _parse_modbus_target(address)
        if addr_type == "register":
            try:
                words = _encode_register_value(value, data_type, self.word_order)
            except ValueError as exc:
                return False, f"WRITE register error: {exc}"
            if len(words) == 1:
                ok, err = await self._client.write_register(idx, words[0])
            else:
                ok, err = await self._client.write_registers(idx, words)
            shown = value if data_type in ("REAL", "FLOAT") else int(value)
            return ok, f"WRITE register {idx} = {shown} ({data_type})" if ok else f"WRITE error: {err}"
        # For coils, treat non-zero as True
        ok, err = await self._client.write_coil(idx, bool(value))
        return ok, f"WRITE coil {idx} = {bool(value)}" if ok else f"WRITE coil error: {err}"


class ModbusRTUDriver(ModbusTCPDriver):
    """
    Modbus RTU master over RS-485 / RS-232, or RTU frames over TCP via a serial gateway.

    Addresses, data types and operations are identical to Modbus TCP.

    Endpoint options:
      rtu_transport   serial (default) | tcp ("RTU over TCP" through a serial device server)
      serial_port     COM3, /dev/ttyUSB0, or a pyserial URL such as rfc2217://host:port
      serial_baudrate 19200 by default (Modbus standard); serial_parity E/N/O (default E);
      serial_stopbits 1 or 2. Data bits are always 8, as RTU requires.
      modbus_unit_id  slave address 1-247; several channels may share one serial port.
    """

    _label = "Modbus RTU"

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        from app.hardware.modbus.rtu_client import ModbusRTUClient
        self._client = ModbusRTUClient(
            transport=str(endpoint.get("rtu_transport", "serial") or "serial").lower(),
            serial_port=str(endpoint.get("serial_port", "") or "").strip(),
            baudrate=int(endpoint.get("serial_baudrate", 19200) or 19200),
            parity=str(endpoint.get("serial_parity", "E") or "E"),
            stopbits=int(endpoint.get("serial_stopbits", 1) or 1),
            host=self.host,
            port=self.port,
            timeout=self.timeout,
            unit_id=int(endpoint.get("modbus_unit_id", 1)),
            simulation_mode=bool(endpoint.get("simulation_mode", False)),
        )

    def _target(self) -> str:
        return self._client.describe()

    async def probe(self) -> Tuple[bool, str]:
        """Ask the slave for holding register 0 to prove it answers on the bus."""
        ok, _, err = await self._client.read_registers(0, 1)
        if ok:
            return True, f"Modbus RTU unit {self._client.unit_id} answered at {self._target()}"
        if err and err.startswith("Modbus exception"):
            # A Modbus exception is still a valid answer from the device.
            return True, f"Modbus RTU unit {self._client.unit_id} answered at {self._target()} ({err} for register 0)"
        return False, f"Modbus RTU unit {self._client.unit_id} did not answer at {self._target()}: {err}"
