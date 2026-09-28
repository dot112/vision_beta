"""Mitsubishi MELSEC driver — SLMP / MC protocol 3E binary frame over TCP."""
from __future__ import annotations

import asyncio
import math
import struct
from typing import Optional, Tuple

from app.hardware.plc._common import encode_words, pulse_with_reset, split_type_suffix
from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)

_CMD_BATCH_READ = 0x0401
_CMD_BATCH_WRITE = 0x1401
_SUB_WORD = 0x0000
_SUB_BIT = 0x0001

# device → (binary device code, is_bit_device, number base)
_DEVICES = {
    "X": (0x9C, True, 16),
    "Y": (0x9D, True, 16),
    "M": (0x90, True, 10),
    "L": (0x92, True, 10),
    "F": (0x93, True, 10),
    "V": (0x94, True, 10),
    "B": (0xA0, True, 16),
    "SM": (0x91, True, 10),
    "SB": (0xA1, True, 16),
    "D": (0xA8, False, 10),
    "W": (0xB4, False, 16),
    "R": (0xAF, False, 10),
    "SD": (0xA9, False, 10),
    "SW": (0xB5, False, 16),
}

_END_CODES = {
    0xC050: "ASCII data received while the PLC is set to binary",
    0xC051: "too many points requested",
    0xC056: "device address is out of range",
    0xC059: "command or subcommand not supported",
    0xC05B: "the device cannot be written",
    0xC05C: "request contents error",
    0xC061: "request data length mismatch",
}


def _parse_melsec_address(address: str, xy_octal: bool = False) -> dict:
    """
    Parse "Y0", "Y1F", "M100", "D200", "D200:DINT", "W1A:REAL".

    X/Y/B/W/SB/SW numbers are hexadecimal on Q/L/iQ-R CPUs. iQ-F (FX5) CPUs
    number X/Y in octal; set melsec_xy_octal on the channel for those.
    """
    base, data_type = split_type_suffix(address)
    for name in sorted(_DEVICES, key=len, reverse=True):
        if base.startswith(name) and base[len(name):]:
            code, is_bit, radix = _DEVICES[name]
            if name in ("X", "Y") and xy_octal:
                radix = 8
            number_text = base[len(name):].strip()
            try:
                number = int(number_text, radix)
            except ValueError as exc:
                raise ValueError(f"Invalid MELSEC device number in '{address}'") from exc
            if not 0 <= number <= 0xFFFFFF:
                raise ValueError(f"MELSEC device number out of range in '{address}'")
            if is_bit and data_type:
                raise ValueError(f"Data type suffix only applies to word devices, not '{address}'")
            return {"device": name, "code": code, "bit": is_bit, "number": number, "type": data_type}
    raise ValueError(
        f"Cannot parse MELSEC address '{address}'; use X/Y/M/L/B/SM for bits or D/W/R/SD for words"
    )


class MelsecSLMPDriver(PLCDriver):
    """
    Mitsubishi Q / L / iQ-R / iQ-F driver using SLMP (MC protocol 3E frame, binary).

    Enable "binary" communication on the PLC's Ethernet port (SLMP / MC protocol).
    SET/RESET/PULSE/TOGGLE for bit devices; WRITE for word devices (16-bit by
    default, D100:DINT / D100:REAL for 32-bit values across D100-D101).

    Endpoint options: melsec_network (default 0), melsec_station (PC number,
    default 255), melsec_xy_octal (true for iQ-F / FX5 X/Y numbering).
    """

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self.last_error = ""
        self.network = int(endpoint.get("melsec_network", 0) or 0) & 0xFF
        station = endpoint.get("melsec_station", 255)
        self.station = int(255 if station is None else station) & 0xFF
        self.xy_octal = bool(endpoint.get("melsec_xy_octal", False))

    @property
    def port(self) -> int:
        # 502 is the generic default for PLC channels; it is never the SLMP port.
        port = int(self._ep.get("port", 5007) or 5007)
        return 5007 if port == 502 else port

    async def connect(self) -> bool:
        await self.disconnect()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout
            )
            self._mark_connected()
            self.last_error = ""
            logger.info("MELSEC SLMP connected: %s:%d", self.host, self.port)
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._mark_connect_failed(self.last_error)
            return False

    async def disconnect(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as exc:
                self._log_disconnect_error(exc)
        self._reader = None
        self._writer = None
        self.is_connected = False

    def _frame(self, command: int, subcommand: int, data: bytes) -> bytes:
        timer = max(1, min(0xFFFF, math.ceil(self.timeout / 0.25)))  # units of 250 ms
        body = struct.pack("<HHH", timer, command, subcommand) + data
        return (
            b"\x50\x00"
            + bytes((self.network, self.station))
            + struct.pack("<HBH", 0x03FF, 0x00, len(body))
            + body
        )

    async def _request(self, command: int, subcommand: int, data: bytes) -> bytes:
        if not self.is_connected and not await self.connect():
            raise ConnectionError(f"MELSEC not connected: {self.last_error or 'connection failed'}")
        async with self._lock:
            try:
                self._writer.write(self._frame(command, subcommand, data))
                await self._writer.drain()
                header = await asyncio.wait_for(self._reader.readexactly(9), timeout=self.timeout)
                if header[0:2] != b"\xD0\x00":
                    raise ConnectionError(f"Invalid SLMP response header {header.hex()}")
                length = struct.unpack_from("<H", header, 7)[0]
                if length < 2:
                    raise ConnectionError("Truncated SLMP response")
                body = await asyncio.wait_for(self._reader.readexactly(length), timeout=self.timeout)
            except asyncio.TimeoutError:
                self.is_connected = False
                raise ConnectionError("MELSEC response timeout; PLC operation outcome is unknown")
            except Exception:
                self.is_connected = False
                raise
        end_code = struct.unpack_from("<H", body, 0)[0]
        if end_code:
            text = _END_CODES.get(end_code, "see the SLMP end-code table")
            raise ValueError(f"PLC returned SLMP end code 0x{end_code:04X} ({text})")
        return body[2:]

    @staticmethod
    def _device_spec(parsed: dict, points: int) -> bytes:
        return parsed["number"].to_bytes(3, "little") + bytes((parsed["code"],)) + struct.pack("<H", points)

    async def _write_bit(self, address: str, value: bool) -> Tuple[bool, str]:
        try:
            parsed = _parse_melsec_address(address, self.xy_octal)
            if not parsed["bit"]:
                return False, f"{address} is a word device; use WRITE"
            await self._request(_CMD_BATCH_WRITE, _SUB_BIT, self._device_spec(parsed, 1) + (b"\x10" if value else b"\x00"))
            return True, "SLMP write acknowledged"
        except Exception as exc:
            return False, str(exc)

    async def read_bit(self, address: str) -> Tuple[bool, Optional[bool], str]:
        try:
            parsed = _parse_melsec_address(address, self.xy_octal)
            if not parsed["bit"]:
                return False, None, f"{address} is a word device"
            data = await self._request(_CMD_BATCH_READ, _SUB_BIT, self._device_spec(parsed, 1))
            if not data:
                return False, None, "SLMP read returned no data"
            return True, bool(data[0] & 0x10), "SLMP read acknowledged"
        except Exception as exc:
            return False, None, str(exc)

    async def set(self, address: str) -> Tuple[bool, str]:
        ok, msg = await self._write_bit(address, True)
        return ok, f"MELSEC SET {address} = ON — {msg}"

    async def reset(self, address: str) -> Tuple[bool, str]:
        ok, msg = await self._write_bit(address, False)
        return ok, f"MELSEC RESET {address} = OFF — {msg}"

    async def toggle(self, address: str) -> Tuple[bool, str]:
        ok, current, msg = await self.read_bit(address)
        if not ok or current is None:
            return False, f"MELSEC TOGGLE could not read {address}: {msg}"
        ok, msg = await self._write_bit(address, not current)
        if not ok:
            return False, f"MELSEC TOGGLE write failed for {address}: {msg}"
        return True, f"MELSEC TOGGLE {address}: {'ON' if current else 'OFF'} → {'OFF' if current else 'ON'}"

    async def pulse(self, address: str, duration_ms: int) -> Tuple[bool, str]:
        ok, msg = await pulse_with_reset(
            lambda: self.set(address), lambda: self.reset(address), duration_ms, f"MELSEC {address}"
        )
        return ok, msg

    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        try:
            parsed = _parse_melsec_address(address, self.xy_octal)
            if parsed["bit"]:
                return await (self.set(address) if value else self.reset(address))
            words = encode_words(value, parsed["type"])
            payload = b"".join(struct.pack("<H", word) for word in words)
            await self._request(_CMD_BATCH_WRITE, _SUB_WORD, self._device_spec(parsed, len(words)) + payload)
            return True, f"MELSEC WRITE {address} = {value:g} — SLMP write acknowledged"
        except Exception as exc:
            return False, f"MELSEC WRITE {address} failed: {exc}"
