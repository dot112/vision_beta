"""Siemens S7 PLC driver — pure asyncio ISO-on-TCP (port 102), no third-party library required."""
from __future__ import annotations

import asyncio
import math
import struct
from typing import Optional, Tuple

from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── S7 Address Parser ─────────────────────────────────────────────────────────

def _parse_s7_address(address: str) -> dict:
    """
    Parse a Siemens S7 address string.

    Supported formats:
      Q0.0   → Output bit  (byte 0, bit 0)
      I0.0   → Input bit   (byte 0, bit 0)
      M10.0  → Memory bit  (byte 10, bit 0)
      Q0     → Output byte (byte 0)
      DB5.DBX2.0  → Data Block 5, bit at offset 2.0
      DB5.DBW4    → Data Block 5, word at offset 4
      DB5.DBD8    → Data Block 5, double-word at offset 8
      DB5.DBB1    → Data Block 5, byte at offset 1
      MB10 / MW10 / MD10 (also QB/QW/QD, IB/IW/ID, German A/E) → area byte/word/dword
      DB5.DBD8:REAL / MD10:REAL → 32-bit floating point value
    """
    addr = address.strip().upper()
    real = False
    if addr.endswith(":REAL"):
        addr, real = addr[:-5].strip(), True

    parsed = _parse_s7_location(addr, address)
    if real:
        if parsed["type"] not in ("db_dword", "dword"):
            raise ValueError("S7 :REAL needs a double-word address such as DB1.DBD4 or MD10")
        parsed["real"] = True
    return parsed


def _parse_s7_location(addr: str, address: str) -> dict:
    # Data Block addressing: DB<n>.DB<type><offset>
    if addr.startswith("DB"):
        parts = addr.split(".", 1)
        if len(parts) != 2 or not parts[0][2:].isdigit():
            raise ValueError(f"Invalid S7 data block address: '{address}'")
        db_num = int(parts[0][2:])
        if not 1 <= db_num <= 0xFFFF:
            raise ValueError("S7 data block number must be between 1 and 65535")
        rest = parts[1]
        if rest.startswith("DBX"):                     # bit
            match = rest[3:].split(".")
            if len(match) != 2 or not all(value.isdigit() for value in match):
                raise ValueError(f"Invalid S7 DB bit address: '{address}'")
            byte_offset, bit_offset = map(int, match)
            if not 0 <= bit_offset <= 7:
                raise ValueError("S7 bit offset must be between 0 and 7")
            return {"type": "db_bit", "area": "db", "db": db_num, "byte": byte_offset, "bit": bit_offset}
        for prefix, kind in (("DBW", "db_word"), ("DBD", "db_dword"), ("DBB", "db_byte")):
            if rest.startswith(prefix):
                offset = rest[3:]
                if not offset.isdigit():
                    raise ValueError(f"Invalid S7 DB address: '{address}'")
                return {"type": kind, "area": "db", "db": db_num, "byte": int(offset)}
        raise ValueError(f"Invalid S7 data block address: '{address}' (use DBX, DBB, DBW or DBD)")

    # Standard area addressing (English Q/I/M and German A/E mnemonics)
    area_map = {"Q": "output", "I": "input", "M": "memory", "A": "output", "E": "input"}
    size_map = {"B": "byte", "W": "word", "D": "dword"}
    prefix = addr[:1]
    area = area_map.get(prefix)
    if area is None:
        raise ValueError(f"Cannot parse S7 address: '{address}'")
    rest = addr[1:]
    if rest[:1] in size_map:                          # MB10 / MW10 / MD10
        offset = rest[1:]
        if not offset.isdigit():
            raise ValueError(f"Invalid S7 address: '{address}'")
        return {"type": size_map[rest[0]], "area": area, "byte": int(offset)}
    if rest.startswith("X"):                         # MX10.0 (TIA-style bit)
        rest = rest[1:]
    if "." in rest:
        byte_str, bit_str = rest.split(".", 1)
        if not byte_str.isdigit() or not bit_str.isdigit() or not 0 <= int(bit_str) <= 7:
            raise ValueError(f"Invalid S7 bit address: '{address}'")
        return {"type": "bit", "area": area, "byte": int(byte_str), "bit": int(bit_str)}
    if not rest.isdigit():
        raise ValueError(f"Invalid S7 byte address: '{address}'")
    return {"type": "byte", "area": area, "byte": int(rest)}


# ── ISO-on-TCP PDU Builders ───────────────────────────────────────────────────

_S7_AREA_CODE = {
    "input":  0x81,
    "output": 0x82,
    "memory": 0x83,
    "db":     0x84,
}

def _address_spec(parsed: dict, word_len: int, count: int = 1) -> bytes:
    """Encode the 12-byte S7-Any address specification used by Read/Write Var."""
    addr_type = parsed["type"]
    is_db = addr_type.startswith("db_")
    area = 0x84 if is_db else _S7_AREA_CODE[parsed["area"]]
    db_num = int(parsed.get("db", 0)) if is_db else 0
    byte_offset = int(parsed["byte"])
    if byte_offset < 0:
        raise ValueError("S7 byte offset must be non-negative")
    if word_len == 0x01:  # S7 bit address is encoded as an absolute bit offset.
        bit = int(parsed["bit"])
        if not 0 <= bit <= 7:
            raise ValueError("S7 bit offset must be between 0 and 7")
        start = byte_offset * 8 + bit
    else:
        start = byte_offset * 8
    if not 0 <= db_num <= 0xFFFF or not 0 <= start <= 0xFFFFFF:
        raise ValueError("S7 address is outside the supported range")
    return (
        bytes((0x12, 0x0A, 0x10, word_len))
        + struct.pack(">HHB", count, db_num, area)
        + start.to_bytes(3, "big")
    )


def _wrap_s7_request(parameters: bytes, data: bytes = b"", pdu_ref: int = 1) -> bytes:
    """Wrap an S7 job in COTP Data and ISO-on-TCP TPKT framing."""
    s7_header = struct.pack(
        ">BBHHHH", 0x32, 0x01, 0, pdu_ref & 0xFFFF, len(parameters), len(data)
    )
    cotp = b"\x02\xF0\x80"
    s7 = s7_header + parameters + data
    return struct.pack(">BBH", 0x03, 0x00, 4 + len(cotp) + len(s7)) + cotp + s7


def _build_s7_write_word_pdu(parsed: dict, value: int, size: int = 2, pdu_ref: int = 1) -> bytes:
    """Build a correctly framed S7 Write Var request for 1, 2, or 4 bytes."""
    if size not in (1, 2, 4):
        raise ValueError("S7 byte writes must be 1, 2, or 4 bytes")
    # S7 address word lengths: BYTE=0x02, WORD=0x04, DWORD=0x06.
    word_len = {1: 0x02, 2: 0x04, 4: 0x06}[size]
    params = b"\x05\x01" + _address_spec(parsed, word_len=word_len)
    data_bytes = int(value).to_bytes(size, "big", signed=False)
    data = struct.pack(">BBH", 0x00, 0x04, size * 8) + data_bytes
    return _wrap_s7_request(params, data, pdu_ref)


def _build_s7_write_bit_pdu(parsed: dict, value: bool, pdu_ref: int = 1) -> bytes:
    """Build a Write Var request that writes exactly one bit (transport size BIT)."""
    params = b"\x05\x01" + _address_spec(parsed, word_len=0x01)
    data = struct.pack(">BBH", 0x00, 0x03, 1) + bytes((1 if value else 0,))
    return _wrap_s7_request(params, data, pdu_ref)


def _build_s7_write_bytes_pdu(parsed: dict, payload: bytes, pdu_ref: int = 1) -> bytes:
    """Build a Write Var request for 1, 2 or 4 raw bytes (BYTE, WORD, DWORD/REAL)."""
    word_len = {1: 0x02, 2: 0x04, 4: 0x06}[len(payload)]
    params = b"\x05\x01" + _address_spec(parsed, word_len=word_len)
    data = struct.pack(">BBH", 0x00, 0x04, len(payload) * 8) + payload
    return _wrap_s7_request(params, data, pdu_ref)


def _build_s7_read_byte_pdu(parsed: dict, pdu_ref: int = 1) -> bytes:
    """Build a Read Var request for the byte containing a bit address."""
    params = b"\x04\x01" + _address_spec(parsed, word_len=0x02)
    return _wrap_s7_request(params, pdu_ref=pdu_ref)


async def _read_tpkt(reader: asyncio.StreamReader, timeout: float) -> bytes:
    """Read one complete ISO-on-TCP packet; TCP reads may split or combine packets."""
    header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    if header[0] != 0x03 or header[1] != 0x00:
        raise ConnectionError(f"Invalid TPKT header: {header.hex()}")
    packet_length = int.from_bytes(header[2:4], "big")
    if packet_length < 7:
        raise ConnectionError(f"Invalid TPKT length: {packet_length}")
    return header + await asyncio.wait_for(reader.readexactly(packet_length - 4), timeout=timeout)


async def _s7_connect_negotiate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    rack: int = 0,
    slot: int = 1,
    timeout: float = 3.0,
) -> bool:
    """Perform COTP connection setup and validate the S7 communication negotiation."""
    if not 0 <= rack <= 7 or not 0 <= slot <= 31:
        raise ValueError("S7 rack must be 0..7 and slot must be 0..31")

    # S7-1200/1500 commonly use rack 0, slot 1; slot 2 is typical for S7-300.
    # Rack/slot are encoded into the low byte of the remote TSAP.
    remote_tsap = 0x0100 | (rack << 5) | slot
    cotp_cr = bytes([
        0x03, 0x00, 0x00, 0x16,        # TPKT header (22 bytes)
        0x11,                           # COTP length
        0xE0,                           # Connection Request
        0x00, 0x00,                     # Destination reference
        0x00, 0x01,                     # Source reference
        0x00,                           # Class 0
        0xC0, 0x01, 0x0A,              # Max TPDU size (1024 bytes)
        0xC1, 0x02, 0x01, 0x00,        # Source TSAP (PG)
        0xC2, 0x02, remote_tsap >> 8, remote_tsap & 0xFF,
    ])
    writer.write(cotp_cr)
    await writer.drain()

    cotp_cc = await _read_tpkt(reader, timeout)
    if len(cotp_cc) < 7 or cotp_cc[5] != 0xD0:
        raise ConnectionError(f"PLC rejected the COTP connection (response {cotp_cc.hex()})")

    # S7 Setup Communication request; PDU size requested is 960 bytes.
    negotiate = bytes([
        0x03, 0x00, 0x00, 0x19,
        0x02, 0xF0, 0x80,
        0x32, 0x01, 0x00, 0x00,
        0x00, 0x01,
        0x00, 0x08,
        0x00, 0x00,
        0xF0, 0x00,
        0x00, 0x01,
        0x00, 0x01,
        0x03, 0xC0,
    ])
    writer.write(negotiate)
    await writer.drain()

    response = await _read_tpkt(reader, timeout)
    if len(response) < 19 or response[4:7] != b"\x02\xF0\x80":
        raise ConnectionError(f"Invalid S7 setup response ({response.hex()})")

    s7 = response[7:]
    if s7[0] != 0x32 or s7[1] != 0x03:
        raise ConnectionError(f"PLC returned an unexpected S7 response ({response.hex()})")
    parameter_length = int.from_bytes(s7[6:8], "big")
    data_length = int.from_bytes(s7[8:10], "big")
    if len(s7) < 12 + parameter_length + data_length:
        raise ConnectionError("Truncated S7 setup response")
    if s7[10] != 0 or s7[11] != 0:
        raise ConnectionError(f"PLC rejected S7 setup (error {s7[10]:02x}{s7[11]:02x})")
    parameters = s7[12:12 + parameter_length]
    if len(parameters) < 8 or parameters[0] != 0xF0:
        raise ConnectionError("PLC returned an invalid S7 setup acknowledgement")
    return int.from_bytes(parameters[6:8], "big") > 0


class S7Driver(PLCDriver):
    """
    Siemens S7 PLC driver using pure asyncio ISO-on-TCP (port 102).

    Covers basic bit and word write operations for Q/I/M areas and Data Blocks.
    For production use with full DB/UDT support, install python-snap7.
    """

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._bit_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self.last_error = ""
        self._pdu_ref = 1  # Connection setup uses reference 1.
        self.rack = int(endpoint.get("s7_rack", endpoint.get("plc_rack", 0)))
        self.slot = int(endpoint.get("s7_slot", endpoint.get("plc_slot", 1)))

    async def connect(self) -> bool:
        async with self._connect_lock:
            if self.is_connected and self._writer and not self._writer.is_closing():
                return True
            await self.disconnect()
            try:
                port = self.port if self.port != 502 else 102  # S7 default port
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, port),
                    timeout=self.timeout,
                )
                await _s7_connect_negotiate(
                    self._reader, self._writer,
                    rack=self.rack, slot=self.slot, timeout=self.timeout,
                )
                self.is_connected = True
                self.last_error = ""
                logger.info("S7 connected: %s:%d (rack %d, slot %d)", self.host, port, self.rack, self.slot)
                return True
            except Exception as exc:
                self.last_error = str(exc) or type(exc).__name__
                logger.warning("S7 connect failed to %s:%s (rack %d, slot %d): %s", self.host, self.port, self.rack, self.slot, self.last_error)
                self.is_connected = False
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

    async def _send_pdu(self, pdu: bytes) -> Tuple[bool, str]:
        """Send one S7 request and validate the complete response and write result."""
        if not self._writer or not self._reader:
            return False, "S7 not connected"
        async with self._lock:
            try:
                self._writer.write(pdu)
                await self._writer.drain()
                resp = await _read_tpkt(self._reader, self.timeout)
                if len(resp) < 19 or resp[4:7] != b"\x02\xF0\x80":
                    raise ConnectionError(f"Invalid S7 response frame ({resp.hex()})")
                s7 = resp[7:]
                if s7[0] != 0x32 or s7[1] not in (0x02, 0x03):
                    raise ConnectionError(f"Unexpected S7 response type ({s7[:2].hex()})")
                request_ref = int.from_bytes(pdu[11:13], "big")
                response_ref = int.from_bytes(s7[4:6], "big")
                if response_ref != request_ref:
                    raise ConnectionError(f"S7 response reference mismatch ({response_ref} != {request_ref})")
                param_len = int.from_bytes(s7[6:8], "big")
                data_len = int.from_bytes(s7[8:10], "big")
                if len(s7) != 12 + param_len + data_len:
                    raise ConnectionError("Malformed or truncated S7 acknowledgement")
                if s7[10] or s7[11]:
                    code = (s7[10] << 8) | s7[11]
                    raise ConnectionError(f"PLC returned S7 error 0x{code:04x}")
                params = s7[12:12 + param_len]
                if len(params) < 2 or params[0] not in (0x04, 0x05) or params[1] != 1:
                    raise ConnectionError(f"Unexpected S7 variable response parameters ({params.hex()})")
                data = s7[12 + param_len:]
                if data and data[0] != 0xFF:
                    self.last_error = f"PLC rejected S7 variable write (return code 0x{data[0]:02x})"
                    return False, self.last_error
                self.last_error = ""
                return True, "PLC acknowledged S7 variable request"
            except asyncio.TimeoutError:
                self.is_connected = False
                self.last_error = "S7 response timeout; PLC operation outcome is unknown"
                return False, self.last_error
            except Exception as exc:
                self.is_connected = False
                self.last_error = f"S7 request error: {exc}"
                return False, self.last_error

    def _next_pdu_ref(self) -> int:
        self._pdu_ref = (self._pdu_ref + 1) & 0xFFFF
        if self._pdu_ref == 0:
            self._pdu_ref = 1
        return self._pdu_ref

    async def _read_byte(self, parsed: dict) -> Tuple[bool, Optional[int], str]:
        """Read the byte containing an S7 bit or byte address."""
        if not self.is_connected and not await self.connect():
            return False, None, self.last_error or "S7 not connected"
        async with self._lock:
            try:
                pdu = _build_s7_read_byte_pdu(parsed, self._next_pdu_ref())
                self._writer.write(pdu)
                await self._writer.drain()
                resp = await _read_tpkt(self._reader, self.timeout)
                s7 = resp[7:]
                if len(s7) < 16 or s7[0] != 0x32 or s7[1] != 0x03:
                    raise ConnectionError(f"Invalid S7 read response ({resp.hex()})")
                if int.from_bytes(s7[4:6], "big") != int.from_bytes(pdu[11:13], "big"):
                    raise ConnectionError("S7 read response reference mismatch")
                param_len = int.from_bytes(s7[6:8], "big")
                data_len = int.from_bytes(s7[8:10], "big")
                if len(s7) != 12 + param_len + data_len or s7[10] or s7[11]:
                    raise ConnectionError("S7 read failed or returned a malformed acknowledgement")
                params = s7[12:12 + param_len]
                data = s7[12 + param_len:]
                if len(params) < 2 or params[0] != 0x04 or params[1] != 1:
                    raise ConnectionError(f"Unexpected S7 read response parameters ({params.hex()})")
                if len(data) < 5 or data[0] != 0xFF or data[1] != 0x04 or int.from_bytes(data[2:4], "big") < 8:
                    code = data[0] if data else 0
                    raise ConnectionError(f"PLC rejected S7 read (return code 0x{code:02x})")
                self.last_error = ""
                return True, data[4], "PLC read acknowledged"
            except Exception as exc:
                self.is_connected = False
                self.last_error = f"S7 read error: {exc}"
                return False, None, self.last_error

    async def read_bit(self, address: str) -> Tuple[bool, Optional[bool], str]:
        """Read one PLC bit, primarily for diagnostics and simulator verification."""
        parsed = _parse_s7_address(address)
        if parsed["type"] not in ("bit", "db_bit"):
            return False, None, f"READ BIT requires a bit address, got '{address}'"
        ok, byte_value, message = await self._read_byte(parsed)
        if not ok or byte_value is None:
            return False, None, message
        return True, bool(byte_value & (1 << parsed["bit"])), message

    async def _write_bit(self, address: str, value: bool) -> Tuple[bool, str]:
        """Write one BOOL with a native S7 bit write.

        A bit write only touches the addressed bit. Reading the whole byte and
        writing it back would overwrite any other bit in that byte that the PLC
        program changed between the read and the write.
        """
        parsed = _parse_s7_address(address)
        if parsed["type"] not in ("bit", "db_bit"):
            return False, f"S7 BOOL operation requires a bit address, got '{address}'"
        async with self._bit_lock:
            pdu = _build_s7_write_bit_pdu(parsed, value, pdu_ref=self._next_pdu_ref())
            return await self._send_pdu(pdu)

    async def set(self, address: str) -> Tuple[bool, str]:
        parsed = _parse_s7_address(address)
        if not self.is_connected and not await self.connect():
            return False, f"S7 SET could not connect: {self.last_error or 'connection failed'}"
        if parsed["type"] in ("bit", "db_bit"):
            ok, msg = await self._write_bit(address, True)
            return ok, f"S7 SET {address} = ON — {msg}"
        return False, f"SET not supported for S7 type '{parsed['type']}' — use WRITE"

    async def reset(self, address: str) -> Tuple[bool, str]:
        parsed = _parse_s7_address(address)
        if not self.is_connected and not await self.connect():
            return False, f"S7 RESET could not connect: {self.last_error or 'connection failed'}"
        if parsed["type"] in ("bit", "db_bit"):
            ok, msg = await self._write_bit(address, False)
            return ok, f"S7 RESET {address} = OFF — {msg}"
        return False, f"RESET not supported for S7 type '{parsed['type']}'"

    async def toggle(self, address: str) -> Tuple[bool, str]:
        parsed = _parse_s7_address(address)
        if parsed["type"] not in ("bit", "db_bit"):
            return False, f"S7 TOGGLE requires a bit address, got '{address}'"
        if not self.is_connected and not await self.connect():
            return False, f"S7 TOGGLE could not connect: {self.last_error or 'connection failed'}"
        async with self._bit_lock:
            ok, current_byte, message = await self._read_byte(parsed)
            if not ok or current_byte is None:
                return False, f"Could not read {address} before toggle: {message}"
            was_on = bool(current_byte & (1 << parsed["bit"]))
            ok, message = await self._send_pdu(
                _build_s7_write_bit_pdu(parsed, not was_on, pdu_ref=self._next_pdu_ref())
            )
            if not ok:
                return False, f"S7 TOGGLE write failed for {address}: {message}"
            return True, f"S7 TOGGLE {address}: {'ON' if was_on else 'OFF'} → {'OFF' if was_on else 'ON'}"

    async def pulse(self, address: str, duration_ms: int) -> Tuple[bool, str]:
        ok1, _ = await self.set(address)
        if not ok1:
            # A response timeout can mean the ON write reached the PLC but its
            # acknowledgement was lost. Attempt OFF immediately as a safety cleanup.
            ok_off, off_msg = await self.reset(address)
            suffix = "OFF cleanup acknowledged" if ok_off else f"OFF cleanup failed: {off_msg}"
            return False, f"S7 PULSE ON failed for {address}; {suffix}"
        try:
            await asyncio.sleep(duration_ms / 1000.0)
        finally:
            reset_task = asyncio.create_task(self.reset(address))
            try:
                ok2, _ = await asyncio.shield(reset_task)
            except asyncio.CancelledError:
                await reset_task
                raise
        if not ok2:
            return False, f"S7 PULSE OFF failed for {address} — may be stuck ON"
        return True, f"S7 PULSE {address} for {duration_ms} ms — OK"

    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        parsed = _parse_s7_address(address)
        kind = parsed["type"]
        if kind in ("bit", "db_bit"):
            return await self.set(address) if value else await self.reset(address)
        try:
            payload, shown = _encode_s7_value(parsed, value)
        except ValueError as exc:
            return False, f"S7 WRITE {address}: {exc}"
        if not self.is_connected and not await self.connect():
            return False, f"S7 WRITE could not connect: {self.last_error or 'connection failed'}"
        pdu = _build_s7_write_bytes_pdu(parsed, payload, pdu_ref=self._next_pdu_ref())
        ok, msg = await self._send_pdu(pdu)
        return ok, f"S7 WRITE {address} = {shown} — {msg}"


_S7_INTEGER_RANGES = {
    1: (-128, 255, "BYTE (-128..255)"),
    2: (-32768, 65535, "WORD/INT (-32768..65535)"),
    4: (-2147483648, 4294967295, "DWORD/DINT (-2147483648..4294967295)"),
}


def _encode_s7_value(parsed: dict, value: float) -> Tuple[bytes, str]:
    """Encode a WRITE value for a byte/word/dword address, rejecting values that do not fit."""
    size = {"byte": 1, "db_byte": 1, "word": 2, "db_word": 2, "dword": 4, "db_dword": 4}.get(parsed["type"])
    if size is None:
        raise ValueError(f"unsupported address type '{parsed['type']}'")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("value must be a number") from exc
    if not math.isfinite(numeric):
        raise ValueError("value must be finite")
    if parsed.get("real"):
        payload = struct.pack(">f", numeric)
        if not math.isfinite(struct.unpack(">f", payload)[0]):
            raise ValueError("value is outside the REAL range")
        return payload, f"{numeric:g} (REAL)"
    if not numeric.is_integer():
        raise ValueError("value must be a whole number; add :REAL to a DBD/MD address for decimals")
    minimum, maximum, label = _S7_INTEGER_RANGES[size]
    int_val = int(numeric)
    if not minimum <= int_val <= maximum:
        raise ValueError(f"value {int_val} does not fit {label}")
    # Negative numbers are sent as two's complement (INT / DINT / SINT).
    payload = (int_val & ((1 << (size * 8)) - 1)).to_bytes(size, "big")
    return payload, f"{int_val} ({ {1: 'BYTE', 2: 'WORD', 4: 'DWORD'}[size] })"
