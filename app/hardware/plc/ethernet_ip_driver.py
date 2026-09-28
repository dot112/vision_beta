"""EtherNet/IP (CIP) PLC driver — Allen-Bradley / Rockwell Logix, pure asyncio port 44818."""
from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import struct
from typing import Dict, List, Optional, Tuple

from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── ENIP/CIP Constants ────────────────────────────────────────────────────────

_ENIP_PORT = 44818
_ENIP_HEADER_FMT = "<HHIIQI"   # command, length, session, status, sender context, options
_ENIP_HEADER_LEN = 24

_CMD_REGISTER_SESSION = 0x0065
_CMD_UNREGISTER_SESSION = 0x0066
_CMD_SEND_RR_DATA     = 0x006F

_CIP_READ_TAG    = 0x4C
_CIP_WRITE_TAG   = 0x4D
_CIP_UNCONNECTED_SEND = 0x52
_CIP_WRITE_BOOL  = _CIP_WRITE_TAG   # historical name
_SYMBOL_INSTANCE = 0x24   # CIP symbol class

# Logix atomic data types: code → (name, struct format)
_CIP_TYPES: Dict[int, Tuple[str, str]] = {
    0x00C1: ("BOOL", "<B"),
    0x00C2: ("SINT", "<b"),
    0x00C3: ("INT", "<h"),
    0x00C4: ("DINT", "<i"),
    0x00C5: ("LINT", "<q"),
    0x00C6: ("USINT", "<B"),
    0x00C7: ("UINT", "<H"),
    0x00C8: ("UDINT", "<I"),
    0x00C9: ("ULINT", "<Q"),
    0x00CA: ("REAL", "<f"),
    0x00CB: ("LREAL", "<d"),
}
_BOOL = 0x00C1

_CIP_STATUS_TEXT = {
    0x01: "connection failure",
    0x04: "path segment error (tag does not exist?)",
    0x05: "path destination unknown (tag does not exist?)",
    0x06: "partial transfer",
    0x08: "service not supported",
    0x0F: "privilege violation (tag is read-only / External Access)",
    0x10: "device state conflict (controller mode?)",
    0x13: "not enough data",
    0x15: "too much data",
    0x1E: "embedded service error",
    0xFF: "general error (data type mismatch?)",
}

_TAG_PART = re.compile(r"^([A-Za-z_][A-Za-z0-9_:]*)((?:\[\s*\d+\s*(?:,\s*\d+\s*)*\])?)$")


def _build_enip_header(command: int, length: int, session: int = 0) -> bytes:
    return struct.pack(_ENIP_HEADER_FMT, command, length, session, 0, 0, 0)


def _build_register_session() -> bytes:
    data = struct.pack("<HH", 1, 0)  # Protocol version 1, options 0
    return _build_enip_header(_CMD_REGISTER_SESSION, len(data)) + data


def _split_tag(tag: str) -> List[str]:
    """Split a tag on '.' members while keeping [..] indices intact."""
    parts, depth, current = [], 0, ""
    for char in tag:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "." and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def _encode_tag_path(tag: str) -> bytes:
    """
    Encode a Logix tag name as a CIP request path.

    Handles members and array elements, e.g. "Rejector", "Program:Main.Step",
    "Line1.Stations[2].Reject", "Output:O.Data[0]", "Grid[1,2]".
    """
    tag = str(tag or "").strip()
    if not tag or len(tag) > 255:
        raise ValueError("EtherNet/IP tag must contain 1 to 255 characters")
    path = b""
    for part in _split_tag(tag):
        match = _TAG_PART.match(part.strip())
        if not match:
            if part.strip().isdigit():
                raise ValueError(
                    f"EtherNet/IP bit-of-word tags such as '{tag}' are not supported; use a BOOL tag"
                )
            raise ValueError(f"Invalid EtherNet/IP tag '{tag}'")
        name = match.group(1).encode("ascii")
        segment = bytes([0x91, len(name)]) + name
        if len(name) % 2:
            segment += b"\x00"  # pad to even
        path += segment
        if match.group(2):
            for index_text in match.group(2).strip("[]").split(","):
                index = int(index_text)
                if index < 0x100:
                    path += bytes([0x28, index])
                elif index < 0x10000:
                    path += b"\x29\x00" + struct.pack("<H", index)
                elif index < 0x100000000:
                    path += b"\x2A\x00" + struct.pack("<I", index)
                else:
                    raise ValueError(f"EtherNet/IP array index too large in '{tag}'")
    if len(path) > 510:
        raise ValueError("EtherNet/IP tag path is too long")
    return path


def _encode_route_path(route: str) -> bytes:
    """
    Encode a Logix route such as "1,0" (backplane, slot 0) or
    "1,0,2,192.168.1.20,1,3" into CIP port segments.
    """
    items = [item.strip() for item in str(route or "").split(",") if item.strip()]
    if len(items) % 2:
        raise ValueError(f"EtherNet/IP path '{route}' must be port,link pairs such as 1,0")
    encoded = b""
    for port_text, link_text in zip(items[0::2], items[1::2]):
        if not port_text.isdigit() or not 1 <= int(port_text) <= 14:
            raise ValueError(f"EtherNet/IP path port '{port_text}' must be between 1 and 14")
        port = int(port_text)
        if link_text.isdigit() and int(link_text) <= 255:
            encoded += bytes([port, int(link_text)])
            continue
        try:
            ipaddress.ip_address(link_text)
        except ValueError as exc:
            raise ValueError(f"EtherNet/IP path link '{link_text}' must be a slot number or IP address") from exc
        link = link_text.encode("ascii")
        segment = bytes([0x10 | port, len(link)]) + link
        if len(segment) % 2:
            segment += b"\x00"
        encoded += segment
    return encoded


def _cip_request(service: int, path: bytes, data: bytes = b"") -> bytes:
    return bytes([service, len(path) // 2]) + path + data


def _wrap_unconnected_send(request: bytes, route: bytes) -> bytes:
    """Wrap a CIP request in Connection Manager Unconnected Send so it is routed to the CPU."""
    body = bytes([0x0A, 0x05]) + struct.pack("<H", len(request)) + request
    if len(request) % 2:
        body += b"\x00"
    body += bytes([len(route) // 2, 0x00]) + route
    return _cip_request(_CIP_UNCONNECTED_SEND, b"\x20\x06\x24\x01", body)


def _wrap_send_rr_data(cip: bytes, session: int = 0) -> bytes:
    addr_item = struct.pack("<HH", 0x0000, 0)                   # null address
    data_item = struct.pack("<HH", 0x00B2, len(cip)) + cip      # unconnected data item
    cpf = struct.pack("<H", 2) + addr_item + data_item
    srr_data = struct.pack("<IH", 0, 10) + cpf                  # interface handle + timeout
    return _build_enip_header(_CMD_SEND_RR_DATA, len(srr_data), session) + srr_data


def _build_cip_write_bool(tag: str, value: bool) -> bytes:
    """Build a CIP Write Tag request for a BOOL tag (direct, unrouted)."""
    request = _cip_request(_CIP_WRITE_TAG, _encode_tag_path(tag), struct.pack("<HH", _BOOL, 1) + (b"\xff" if value else b"\x00"))
    return _wrap_send_rr_data(request)


def _build_cip_read_bool(tag: str) -> bytes:
    """Build a CIP Read Tag request for one element (direct, unrouted)."""
    return _wrap_send_rr_data(_cip_request(_CIP_READ_TAG, _encode_tag_path(tag), struct.pack("<H", 1)))


def _build_cip_write_int(tag: str, value: int, size: int = 2) -> bytes:
    """Build a CIP Write Tag for an INT (size 2) or DINT (size 4) tag (direct, unrouted)."""
    data_type = 0x00C3 if size == 2 else 0x00C4
    packed = struct.pack("<h" if size == 2 else "<i", int(value))
    return _wrap_send_rr_data(_cip_request(_CIP_WRITE_TAG, _encode_tag_path(tag), struct.pack("<HH", data_type, 1) + packed))


def _extract_cip_reply(response: bytes) -> bytes:
    """Return the CIP reply payload from a SendRRData encapsulation response."""
    if len(response) < _ENIP_HEADER_LEN + 8:
        raise ConnectionError("Truncated EtherNet/IP response")
    command, payload_len = struct.unpack_from("<HH", response, 0)
    if command != _CMD_SEND_RR_DATA:
        raise ConnectionError(f"Unexpected EtherNet/IP command 0x{command:04X}")
    status = struct.unpack_from("<I", response, 8)[0]
    if status:
        raise ConnectionError(f"EtherNet/IP encapsulation error 0x{status:08X}")
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


class CIPError(Exception):
    """The PLC answered with a CIP error status. The session is still usable."""


def _parse_cip_reply(cip: bytes, service: int) -> bytes:
    """Validate a CIP reply and return its response data."""
    if len(cip) < 4:
        raise ConnectionError("Truncated CIP reply")
    reply_service = cip[0]
    status = cip[2]
    extra_words = cip[3]
    data_offset = 4 + extra_words * 2
    if reply_service not in (service | 0x80, _CIP_UNCONNECTED_SEND | 0x80):
        raise ConnectionError(f"Unexpected CIP reply service 0x{reply_service:02X}")
    if status:
        extended = cip[4:data_offset].hex()
        text = _CIP_STATUS_TEXT.get(status, "error")
        raise CIPError(f"CIP status 0x{status:02X} {text}" + (f" (extended {extended})" if extended else ""))
    if reply_service != service | 0x80:
        raise ConnectionError("CIP routing reply did not contain the requested service")
    return cip[data_offset:]


class EtherNetIPDriver(PLCDriver):
    """
    Allen-Bradley / Rockwell EtherNet/IP driver using pure asyncio CIP over TCP.

    Supports symbolic tag addressing including members and array elements.
    SET/RESET/PULSE/TOGGLE for BOOL tags; WRITE for any atomic type (SINT, INT,
    DINT, LINT, unsigned types, REAL, LREAL) — the tag's real type is read from
    the controller, so the written value always matches it.

    Endpoint options:
      eip_path  route to the CPU as port,link pairs; "1,0" = backplane slot 0
                (ControlLogix / CompactLogix). Empty or "direct" sends straight
                to the adapter's message router (Micro800 and similar).
      eip_slot  CPU slot, used when eip_path is left at the default "1,0".

    Tag examples: "RejectorCoil", "Program:Main.Reject", "Output:O.Data[0]", "Speed_SP"
    """

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._session_id: int = 0
        self._lock = asyncio.Lock()
        self._type_cache: Dict[str, int] = {}
        self.last_error = ""
        self._route = self._route_from_endpoint(endpoint)

    @staticmethod
    def _route_from_endpoint(endpoint: dict) -> bytes:
        raw = str(endpoint.get("eip_path", "1,0") if endpoint.get("eip_path") is not None else "1,0").strip()
        if raw.lower() in ("", "none", "direct"):
            return b""
        slot = int(endpoint.get("eip_slot", 0) or 0)
        if raw.replace(" ", "") == "1,0" and slot:
            raw = f"1,{slot}"
        return _encode_route_path(raw)

    async def connect(self) -> bool:
        await self.disconnect()
        try:
            port = self.port if self.port not in (502, 102) else _ENIP_PORT
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, port),
                timeout=self.timeout,
            )
            self._writer.write(_build_register_session())
            await self._writer.drain()
            response = await self._read_packet()
            command, _length, session, status = struct.unpack_from("<HHII", response, 0)
            if command != _CMD_REGISTER_SESSION or status != 0 or session == 0:
                raise ConnectionError(
                    f"RegisterSession rejected (command 0x{command:04X}, status 0x{status:08X})"
                )
            self._session_id = session
            self._mark_connected()
            self.last_error = ""
            logger.info("EtherNet/IP connected: %s:%d  session=0x%08X", self.host, port, self._session_id)
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._mark_connect_failed(self.last_error)
            await self.disconnect()
            self.is_connected = False
            return False

    async def disconnect(self) -> None:
        if self._writer:
            try:
                if self._session_id and not self._writer.is_closing():
                    self._writer.write(_build_enip_header(_CMD_UNREGISTER_SESSION, 0, self._session_id))
                    await asyncio.wait_for(self._writer.drain(), timeout=1.0)
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as exc:
                self._log_disconnect_error(exc)
        self._reader = None
        self._writer = None
        self._session_id = 0
        self._type_cache.clear()
        self.is_connected = False

    async def _read_packet(self) -> bytes:
        header = await asyncio.wait_for(self._reader.readexactly(_ENIP_HEADER_LEN), timeout=self.timeout)
        payload_len = struct.unpack_from("<H", header, 2)[0]
        payload = await asyncio.wait_for(self._reader.readexactly(payload_len), timeout=self.timeout)
        return header + payload

    def _patch_session(self, pdu: bytes) -> bytes:
        """Patch session ID into an ENIP header."""
        return pdu[:4] + struct.pack("<I", self._session_id) + pdu[8:]

    async def _request(self, service: int, request: bytes) -> bytes:
        """Send one CIP request (routed if configured) and return its validated reply data."""
        if not self.is_connected and not await self.connect():
            raise ConnectionError(f"EIP not connected: {self.last_error or 'connection failed'}")
        cip = _wrap_unconnected_send(request, self._route) if self._route else request
        async with self._lock:
            try:
                self._writer.write(_wrap_send_rr_data(cip, self._session_id))
                await self._writer.drain()
                reply = _extract_cip_reply(await self._read_packet())
            except asyncio.TimeoutError:
                self.is_connected = False
                raise ConnectionError("EIP response timeout; PLC operation outcome is unknown")
            except Exception:
                self.is_connected = False
                raise
        return _parse_cip_reply(reply, service)

    async def _read_tag(self, tag: str) -> Tuple[int, bytes]:
        data = await self._request(_CIP_READ_TAG, _cip_request(_CIP_READ_TAG, _encode_tag_path(tag), struct.pack("<H", 1)))
        if len(data) < 2:
            raise ConnectionError("CIP Read Tag reply has no data")
        type_code = struct.unpack_from("<H", data, 0)[0]
        if type_code == 0x02A0:
            raise CIPError(f"Tag '{tag}' is a structure (UDT); address one of its members")
        if type_code not in _CIP_TYPES:
            raise CIPError(f"Tag '{tag}' has unsupported CIP type 0x{type_code:04X}")
        self._type_cache[tag] = type_code
        return type_code, data[2:]

    async def _tag_type(self, tag: str) -> int:
        if tag not in self._type_cache:
            await self._read_tag(tag)
        return self._type_cache[tag]

    async def _write_tag(self, tag: str, type_code: int, value) -> None:
        name, fmt = _CIP_TYPES[type_code]
        if type_code == _BOOL:
            payload = b"\xff" if value else b"\x00"
        else:
            payload = struct.pack(fmt, value)
        request = _cip_request(_CIP_WRITE_TAG, _encode_tag_path(tag), struct.pack("<HH", type_code, 1) + payload)
        await self._request(_CIP_WRITE_TAG, request)

    async def _write_bool(self, tag: str, value: bool) -> Tuple[bool, str]:
        try:
            type_code = await self._tag_type(tag)
            if type_code != _BOOL:
                return False, f"tag is {_CIP_TYPES[type_code][0]}, not BOOL; use WRITE"
            await self._write_tag(tag, _BOOL, value)
            return True, "CIP write acknowledged"
        except (CIPError, ValueError) as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"EIP error: {exc}"

    async def set(self, address: str) -> Tuple[bool, str]:
        ok, msg = await self._write_bool(address, True)
        return ok, f"EIP SET {address} = ON — {msg}"

    async def reset(self, address: str) -> Tuple[bool, str]:
        ok, msg = await self._write_bool(address, False)
        return ok, f"EIP RESET {address} = OFF — {msg}"

    async def _read_bool(self, address: str) -> Tuple[bool, Optional[bool], str]:
        try:
            type_code, data = await self._read_tag(address)
            if type_code != _BOOL or not data:
                return False, None, "CIP tag is not a readable BOOL"
            return True, bool(data[0]), "CIP BOOL read acknowledged"
        except (CIPError, ValueError) as exc:
            return False, None, str(exc)
        except Exception as exc:
            return False, None, f"EIP BOOL read error: {exc}"

    async def toggle(self, address: str) -> Tuple[bool, str]:
        ok, current, message = await self._read_bool(address)
        if not ok or current is None:
            return False, f"EIP TOGGLE could not read {address}: {message}"
        target = not current
        ok, message = await self._write_bool(address, target)
        if not ok:
            return False, f"EIP TOGGLE write failed for {address}: {message}"
        return True, f"EIP TOGGLE {address}: {'ON' if current else 'OFF'} → {'ON' if target else 'OFF'}"

    async def pulse(self, address: str, duration_ms: int) -> Tuple[bool, str]:
        ok1, msg1 = await self.set(address)
        if not ok1:
            return False, f"EIP PULSE ON failed for {address}: {msg1}"
        try:
            await asyncio.sleep(duration_ms / 1000.0)
        finally:
            reset_task = asyncio.create_task(self.reset(address))
            try:
                ok2, msg2 = await asyncio.shield(reset_task)
            except asyncio.CancelledError:
                await reset_task
                raise
        if not ok2:
            return False, f"EIP PULSE OFF failed for {address} — may be stuck ON: {msg2}"
        return True, f"EIP PULSE {address} {duration_ms} ms — OK"

    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        try:
            numeric = float(value)
            if not math.isfinite(numeric):
                return False, f"EIP WRITE {address}: value must be finite"
            type_code = await self._tag_type(address)
            name, fmt = _CIP_TYPES[type_code]
            if type_code == _BOOL:
                typed = bool(numeric)
            elif name in ("REAL", "LREAL"):
                typed = numeric
                if name == "REAL" and not math.isfinite(struct.unpack("<f", struct.pack("<f", numeric))[0]):
                    return False, f"EIP WRITE {address}: value is outside the REAL range"
            else:
                if not numeric.is_integer():
                    return False, f"EIP WRITE {address}: {name} tag needs a whole number"
                typed = int(numeric)
                try:
                    struct.pack(fmt, typed)
                except struct.error:
                    return False, f"EIP WRITE {address}: value {typed} does not fit {name}"
            await self._write_tag(address, type_code, typed)
            return True, f"EIP WRITE {address} = {typed} ({name}) — CIP write acknowledged"
        except (CIPError, ValueError) as exc:
            return False, f"EIP WRITE {address} failed: {exc}"
        except Exception as exc:
            return False, f"EIP WRITE {address} error: {exc}"
