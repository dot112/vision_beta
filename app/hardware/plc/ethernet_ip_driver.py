"""EtherNet/IP (CIP) PLC driver — Allen-Bradley / Rockwell, pure asyncio port 44818."""
from __future__ import annotations

import asyncio
import struct
from typing import Optional, Tuple

from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── ENIP/CIP Constants ────────────────────────────────────────────────────────

_ENIP_PORT = 44818
_ENIP_HEADER_FMT = "<HHIIQQ"   # command, length, session, status, sender, options
_ENIP_HEADER_LEN = 24

_CMD_REGISTER_SESSION = 0x0065
_CMD_SEND_RR_DATA     = 0x006F

_CIP_WRITE_BOOL  = 0x4D
_CIP_READ_TAG    = 0x4C
_SYMBOL_INSTANCE = 0x24   # CIP symbol class


def _build_enip_header(command: int, length: int, session: int = 0) -> bytes:
    return struct.pack(_ENIP_HEADER_FMT, command, length, session, 0, 0, 0)


def _build_register_session() -> bytes:
    data = struct.pack("<HH", 1, 0)  # Protocol version 1, options 0
    return _build_enip_header(_CMD_REGISTER_SESSION, len(data)) + data


def _build_cip_write_bool(tag: str, value: bool) -> bytes:
    """
    Build a CIP Write Tag (0x4D) request for a BOOL tag by name.
    Uses the symbolic segment (ANSI Extended Symbol) addressing.
    """
    tag_bytes = tag.encode("ascii")
    # Symbolic segment: 0x91 = ANSI Extended Symbol, length, tag chars
    segment = bytes([0x91, len(tag_bytes)]) + tag_bytes
    if len(tag_bytes) % 2:
        segment += b"\x00"  # pad to even

    # CIP Write Tag request:  service, path-size, path, data-type (BOOL=0xC1), count, value
    request = bytes([_CIP_WRITE_BOOL, len(segment) // 2]) + segment
    request += struct.pack("<HH", 0x00C1, 1)   # BOOL type, 1 element
    request += bytes([0xFF if value else 0x00])
    request += b"\x00"  # padding

    # Connected Data Item wrapper (type 0xB2) + Address Item (null = 0x00)
    addr_item = struct.pack("<HH", 0x0000, 0)          # null address
    data_item = struct.pack("<HH", 0x00B2, len(request)) + request
    cpf = struct.pack("<H", 2) + addr_item + data_item  # item count = 2

    # Interface handle + timeout
    srr_data = struct.pack("<IH", 0, 10) + cpf

    return _build_enip_header(_CMD_SEND_RR_DATA, len(srr_data)) + srr_data


def _build_cip_read_bool(tag: str) -> bytes:
    """Build a CIP Read Tag request for one BOOL value."""
    tag_bytes = tag.encode("ascii")
    if not tag_bytes or len(tag_bytes) > 255:
        raise ValueError("EtherNet/IP BOOL tag must contain 1 to 255 ASCII bytes")
    segment = bytes([0x91, len(tag_bytes)]) + tag_bytes
    if len(tag_bytes) % 2:
        segment += b"\x00"
    request = bytes([_CIP_READ_TAG, len(segment) // 2]) + segment + struct.pack("<H", 1)

    addr_item = struct.pack("<HH", 0x0000, 0)
    data_item = struct.pack("<HH", 0x00B2, len(request)) + request
    cpf = struct.pack("<H", 2) + addr_item + data_item
    srr_data = struct.pack("<IH", 0, 10) + cpf
    return _build_enip_header(_CMD_SEND_RR_DATA, len(srr_data)) + srr_data


def _extract_cip_reply(response: bytes) -> bytes:
    """Return the CIP reply payload from a SendRRData encapsulation response."""
    if len(response) < _ENIP_HEADER_LEN + 8:
        raise ConnectionError("Truncated EtherNet/IP response")
    command, payload_len = struct.unpack_from("<HH", response, 0)
    if command != _CMD_SEND_RR_DATA:
        raise ConnectionError(f"Unexpected EtherNet/IP command 0x{command:04X}")
    if len(response) < _ENIP_HEADER_LEN + payload_len:
        raise ConnectionError("Incomplete EtherNet/IP response payload")

    offset = _ENIP_HEADER_LEN + 6  # interface handle + timeout
    item_count = struct.unpack_from("<H", response, offset)[0]
    offset += 2
    for _ in range(item_count):
        if offset + 4 > len(response):
            raise ConnectionError("Truncated EtherNet/IP item header")
        item_type, item_len = struct.unpack_from("<HH", response, offset)
        offset += 4
        item_end = offset + item_len
        if item_end > len(response):
            raise ConnectionError("Truncated EtherNet/IP item data")
        if item_type in (0x00B1, 0x00B2):
            return response[offset:item_end]
        offset = item_end
    raise ConnectionError("EtherNet/IP response did not contain CIP data")


def _build_cip_write_int(tag: str, value: int, size: int = 2) -> bytes:
    """Build a CIP Write Tag for INT (0xC3) or DINT (0xC4) tag."""
    tag_bytes = tag.encode("ascii")
    segment = bytes([0x91, len(tag_bytes)]) + tag_bytes
    if len(tag_bytes) % 2:
        segment += b"\x00"

    data_type = 0x00C3 if size == 2 else 0x00C4   # INT or DINT
    packed_val = struct.pack("<h" if size == 2 else "<i", int(value))
    request = bytes([_CIP_WRITE_BOOL, len(segment) // 2]) + segment
    request += struct.pack("<HH", data_type, 1) + packed_val

    addr_item = struct.pack("<HH", 0x0000, 0)
    data_item = struct.pack("<HH", 0x00B2, len(request)) + request
    cpf = struct.pack("<H", 2) + addr_item + data_item
    srr_data = struct.pack("<IH", 0, 10) + cpf
    return _build_enip_header(_CMD_SEND_RR_DATA, len(srr_data)) + srr_data


class EtherNetIPDriver(PLCDriver):
    """
    Allen-Bradley / Rockwell EtherNet/IP driver using pure asyncio CIP over TCP.

    Supports tag-name based addressing (symbolic ANSI segments).
    For BOOL tags: SET/RESET/PULSE.  For INT/DINT tags: WRITE.

    Tag examples: "RejectorCoil", "Output:O.Data[0]", "Conveyor_Speed"
    """

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._session_id: int = 0
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        try:
            port = self.port if self.port not in (502, 102) else _ENIP_PORT
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, port),
                timeout=self.timeout,
            )
            # Register session
            self._writer.write(_build_register_session())
            await self._writer.drain()
            resp = await asyncio.wait_for(self._reader.read(28), timeout=self.timeout)
            if len(resp) >= 4 and struct.unpack_from("<H", resp, 0)[0] == _CMD_REGISTER_SESSION:
                self._session_id = struct.unpack_from("<I", resp, 4)[0]
            self.is_connected = True
            logger.info("EtherNet/IP connected: %s:%d  session=0x%08X", self.host, port, self._session_id)
            return True
        except Exception as exc:
            logger.warning("EtherNet/IP connect failed: %s", exc)
            self.is_connected = False
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
        self._session_id = 0
        self.is_connected = False

    def _patch_session(self, pdu: bytes) -> bytes:
        """Patch session ID into an ENIP header."""
        return pdu[:4] + struct.pack("<I", self._session_id) + pdu[8:]

    async def _send(self, pdu: bytes) -> Tuple[bool, str]:
        if not self._writer:
            return False, "EIP not connected"
        async with self._lock:
            try:
                pdu = self._patch_session(pdu)
                self._writer.write(pdu)
                await self._writer.drain()
                resp = await asyncio.wait_for(self._reader.read(256), timeout=self.timeout)
                # CIP general status is at byte 42 in the response (simplified check)
                if len(resp) >= 43:
                    status = resp[42]
                    return status == 0x00, f"CIP status 0x{status:02X}"
                return True, "Sent (response too short to verify)"
            except asyncio.TimeoutError:
                self.is_connected = False
                return False, "EIP write timeout"
            except Exception as exc:
                self.is_connected = False
                return False, f"EIP write error: {exc}"

    async def set(self, address: str) -> Tuple[bool, str]:
        ok, msg = await self._send(_build_cip_write_bool(address, True))
        return ok, f"EIP SET {address} = ON — {msg}"

    async def reset(self, address: str) -> Tuple[bool, str]:
        ok, msg = await self._send(_build_cip_write_bool(address, False))
        return ok, f"EIP RESET {address} = OFF — {msg}"

    async def _read_bool(self, address: str) -> Tuple[bool, Optional[bool], str]:
        if not self._reader or not self._writer:
            return False, None, "EIP not connected"
        async with self._lock:
            try:
                request = self._patch_session(_build_cip_read_bool(address))
                self._writer.write(request)
                await self._writer.drain()
                header = await asyncio.wait_for(self._reader.readexactly(_ENIP_HEADER_LEN), timeout=self.timeout)
                payload_len = struct.unpack_from("<H", header, 2)[0]
                payload = await asyncio.wait_for(self._reader.readexactly(payload_len), timeout=self.timeout)
                cip = _extract_cip_reply(header + payload)
                if len(cip) < 4 or cip[0] != (_CIP_READ_TAG | 0x80):
                    raise ConnectionError("Unexpected CIP Read Tag response")
                status = cip[2]
                if status != 0:
                    additional_words = cip[3]
                    extra_start = 4
                    extra_end = extra_start + additional_words * 2
                    extra = cip[extra_start:extra_end].hex()
                    return False, None, f"CIP read status 0x{status:02X}" + (f" (additional status {extra})" if extra else "")
                data_offset = 4 + cip[3] * 2
                data = cip[data_offset:]
                if len(data) < 3 or struct.unpack_from("<H", data, 0)[0] != 0x00C1:
                    return False, None, "CIP tag is not a readable BOOL"
                return True, bool(data[2]), "CIP BOOL read acknowledged"
            except asyncio.TimeoutError:
                self.is_connected = False
                return False, None, "EIP BOOL read timed out"
            except Exception as exc:
                self.is_connected = False
                return False, None, f"EIP BOOL read error: {exc}"

    async def toggle(self, address: str) -> Tuple[bool, str]:
        if not self.is_connected and not await self.connect():
            return False, f"EIP TOGGLE could not connect to {address}"
        ok, current, message = await self._read_bool(address)
        if not ok or current is None:
            return False, f"EIP TOGGLE could not read {address}: {message}"
        target = not current
        ok, message = await self._send(_build_cip_write_bool(address, target))
        if not ok:
            return False, f"EIP TOGGLE write failed for {address}: {message}"
        return True, f"EIP TOGGLE {address}: {'ON' if current else 'OFF'} → {'ON' if target else 'OFF'}"

    async def pulse(self, address: str, duration_ms: int) -> Tuple[bool, str]:
        ok1, _ = await self.set(address)
        if not ok1:
            return False, f"EIP PULSE ON failed for {address}"
        await asyncio.sleep(duration_ms / 1000.0)
        ok2, _ = await self.reset(address)
        return ok2, f"EIP PULSE {address} {duration_ms} ms — {'OK' if ok2 else 'OFF failed'}"

    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        int_val = int(value)
        size = 4 if abs(int_val) > 32767 else 2  # auto INT vs DINT
        ok, msg = await self._send(_build_cip_write_int(address, int_val, size))
        return ok, f"EIP WRITE {address} = {int_val} ({'INT' if size == 2 else 'DINT'}) — {msg}"
