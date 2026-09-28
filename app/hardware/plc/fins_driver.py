"""Omron FINS driver — FINS/TCP (port 9600) for CS / CJ / CP / NJ / NX CPUs."""
from __future__ import annotations

import asyncio
import re
import struct
from typing import Optional, Tuple

from app.hardware.plc._common import encode_words, pulse_with_reset, split_type_suffix
from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)

_FINS_MAGIC = b"FINS"
_TCP_NODE_REQUEST = 0
_TCP_NODE_RESPONSE = 1
_TCP_FRAME = 2

# area → (bit area code, word area code)
_AREAS = {
    "CIO": (0x30, 0xB0),
    "W": (0x31, 0xB1),
    "H": (0x32, 0xB2),
    "A": (0x33, 0xB3),
    "D": (0x02, 0x82),
    "DM": (0x02, 0x82),
}

_ADDRESS = re.compile(r"^(CIO|DM|D|W|H|A)?\s*(\d+)(?:\.(\d{1,2}))?$")

_TCP_ERRORS = {
    0x01: "header is not 'FINS'",
    0x02: "data length too long",
    0x03: "command not supported",
    0x20: "all connections are in use",
    0x21: "the client node is already connected",
    0x22: "client IP address is not in the PLC's allowed list",
    0x23: "client node address is out of range",
    0x24: "the same node address is used twice",
    0x25: "all node addresses are in use",
}


def _parse_fins_address(address: str) -> dict:
    """Parse "CIO 0.00", "0.05", "W10.01", "H5.15", "D100", "D100.03", "D100:DINT"."""
    base, data_type = split_type_suffix(address)
    match = _ADDRESS.match(base)
    if not match:
        raise ValueError(f"Cannot parse Omron address '{address}'; use e.g. CIO 0.00, W10.01 or D100")
    area = match.group(1) or "CIO"
    word = int(match.group(2))
    if word > 0xFFFF:
        raise ValueError(f"Omron word address out of range in '{address}'")
    bit_code, word_code = _AREAS[area]
    if match.group(3) is not None:
        bit = int(match.group(3))
        if bit > 15:
            raise ValueError(f"Omron bit number must be 00-15 in '{address}'")
        if data_type:
            raise ValueError(f"Data type suffix only applies to word addresses, not '{address}'")
        return {"area": area, "code": bit_code, "word": word, "bit": bit, "is_bit": True, "type": ""}
    return {"area": area, "code": word_code, "word": word, "bit": 0, "is_bit": False, "type": data_type}


class OmronFINSDriver(PLCDriver):
    """
    Omron FINS/TCP driver.

    SET/RESET/PULSE/TOGGLE for bit addresses (CIO 0.00, W10.01, D100.03);
    WRITE for word addresses (16-bit by default, D100:DINT / D100:REAL for
    32-bit values across D100-D101).

    Endpoint options: fins_network (destination network, default 0),
    fins_unit (destination unit, default 0 = CPU). The PLC and client node
    numbers are assigned by the FINS/TCP handshake.
    """

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self.last_error = ""
        self.network = int(endpoint.get("fins_network", 0) or 0) & 0x7F
        self.unit = int(endpoint.get("fins_unit", 0) or 0) & 0xFF
        self.client_node = 0
        self.server_node = int(endpoint.get("fins_node", 0) or 0) & 0xFF
        self._sid = 0

    @property
    def port(self) -> int:
        # 502 is the generic default for PLC channels; it is never the FINS port.
        port = int(self._ep.get("port", 9600) or 9600)
        return 9600 if port == 502 else port

    async def connect(self) -> bool:
        await self.disconnect()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout
            )
            self._writer.write(_FINS_MAGIC + struct.pack(">IIII", 12, _TCP_NODE_REQUEST, 0, 0))
            await self._writer.drain()
            command, error, payload = await self._read_tcp_frame()
            if error:
                raise ConnectionError(f"FINS/TCP handshake refused: {_TCP_ERRORS.get(error, hex(error))}")
            if command != _TCP_NODE_RESPONSE or len(payload) < 8:
                raise ConnectionError("Invalid FINS/TCP handshake response")
            self.client_node, self.server_node = struct.unpack(">II", payload[:8])
            self.client_node &= 0xFF
            self.server_node &= 0xFF
            self.is_connected = True
            self.last_error = ""
            logger.info(
                "Omron FINS connected: %s:%d (client node %d, PLC node %d)",
                self.host, self.port, self.client_node, self.server_node,
            )
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Omron FINS connect failed %s:%d — %s", self.host, self.port, self.last_error)
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        self.is_connected = False

    async def _read_tcp_frame(self) -> Tuple[int, int, bytes]:
        header = await asyncio.wait_for(self._reader.readexactly(16), timeout=self.timeout)
        if header[:4] != _FINS_MAGIC:
            raise ConnectionError(f"Invalid FINS/TCP header {header[:4]!r}")
        length, command, error = struct.unpack(">III", header[4:16])
        if length < 8 or length > 4096:
            raise ConnectionError(f"Invalid FINS/TCP length {length}")
        payload = await asyncio.wait_for(self._reader.readexactly(length - 8), timeout=self.timeout)
        return command, error, payload

    def _next_sid(self) -> int:
        self._sid = (self._sid + 1) & 0xFF
        return self._sid

    async def _command(self, mrc: int, src: int, data: bytes) -> bytes:
        if not self.is_connected and not await self.connect():
            raise ConnectionError(f"FINS not connected: {self.last_error or 'connection failed'}")
        async with self._lock:
            sid = self._next_sid()
            fins = bytes((
                0x80, 0x00, 0x02,                         # ICF (command, response required), RSV, GCT
                self.network, self.server_node, self.unit,  # destination network / node / unit
                0x00, self.client_node, 0x00,             # source network / node / unit
                sid, mrc, src,
            )) + data
            try:
                self._writer.write(_FINS_MAGIC + struct.pack(">III", 8 + len(fins), _TCP_FRAME, 0) + fins)
                await self._writer.drain()
                command, error, payload = await self._read_tcp_frame()
            except asyncio.TimeoutError:
                self.is_connected = False
                raise ConnectionError("FINS response timeout; PLC operation outcome is unknown")
            except Exception:
                self.is_connected = False
                raise
        if error:
            self.is_connected = False
            raise ConnectionError(f"FINS/TCP error: {_TCP_ERRORS.get(error, hex(error))}")
        if command != _TCP_FRAME or len(payload) < 14:
            self.is_connected = False
            raise ConnectionError("Invalid FINS response frame")
        if payload[9] != sid or payload[10] != mrc or payload[11] != src:
            self.is_connected = False
            raise ConnectionError("FINS response does not match the request")
        main_code, sub_code = payload[12] & 0x7F, payload[13] & 0x3F
        if main_code or sub_code:
            raise ValueError(f"PLC returned FINS end code {main_code:02X}{sub_code:02X}")
        return payload[14:]

    @staticmethod
    def _memory_spec(parsed: dict, count: int) -> bytes:
        return bytes((parsed["code"],)) + struct.pack(">HBH", parsed["word"], parsed["bit"], count)

    async def _write_bit(self, address: str, value: bool) -> Tuple[bool, str]:
        try:
            parsed = _parse_fins_address(address)
            if not parsed["is_bit"]:
                return False, f"{address} is a word address; use WRITE or a bit address such as {address}.00"
            await self._command(0x01, 0x02, self._memory_spec(parsed, 1) + (b"\x01" if value else b"\x00"))
            return True, "FINS write acknowledged"
        except Exception as exc:
            return False, str(exc)

    async def read_bit(self, address: str) -> Tuple[bool, Optional[bool], str]:
        try:
            parsed = _parse_fins_address(address)
            if not parsed["is_bit"]:
                return False, None, f"{address} is a word address"
            data = await self._command(0x01, 0x01, self._memory_spec(parsed, 1))
            if not data:
                return False, None, "FINS read returned no data"
            return True, bool(data[0] & 0x01), "FINS read acknowledged"
        except Exception as exc:
            return False, None, str(exc)

    async def set(self, address: str) -> Tuple[bool, str]:
        ok, msg = await self._write_bit(address, True)
        return ok, f"FINS SET {address} = ON — {msg}"

    async def reset(self, address: str) -> Tuple[bool, str]:
        ok, msg = await self._write_bit(address, False)
        return ok, f"FINS RESET {address} = OFF — {msg}"

    async def toggle(self, address: str) -> Tuple[bool, str]:
        ok, current, msg = await self.read_bit(address)
        if not ok or current is None:
            return False, f"FINS TOGGLE could not read {address}: {msg}"
        ok, msg = await self._write_bit(address, not current)
        if not ok:
            return False, f"FINS TOGGLE write failed for {address}: {msg}"
        return True, f"FINS TOGGLE {address}: {'ON' if current else 'OFF'} → {'OFF' if current else 'ON'}"

    async def pulse(self, address: str, duration_ms: int) -> Tuple[bool, str]:
        return await pulse_with_reset(
            lambda: self.set(address), lambda: self.reset(address), duration_ms, f"FINS {address}"
        )

    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        try:
            parsed = _parse_fins_address(address)
            if parsed["is_bit"]:
                return await (self.set(address) if value else self.reset(address))
            words = encode_words(value, parsed["type"])
            payload = b"".join(struct.pack(">H", word) for word in words)
            await self._command(0x01, 0x02, self._memory_spec(parsed, len(words)) + payload)
            return True, f"FINS WRITE {address} = {value:g} — FINS write acknowledged"
        except Exception as exc:
            return False, f"FINS WRITE {address} failed: {exc}"
