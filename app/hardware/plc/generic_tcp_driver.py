"""Generic TCP PLC driver — sends configurable ASCII/hex frames over a plain TCP socket."""
from __future__ import annotations

import asyncio
import re
from typing import Optional, Tuple

from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)


class GenericTCPDriver(PLCDriver):
    """
    Generic TCP PLC driver for any device that accepts raw ASCII or hex frames.

    Frame templates (stored in the endpoint config):
      set_template:    "01 05 {addr:04X} FF 00 {crc}"   (hex bytes with placeholders)
      reset_template:  "01 05 {addr:04X} 00 00 {crc}"
      toggle_template: device-specific command to invert the addressed BOOL
      write_template:  "01 06 {addr:04X} {value:04X} {crc}"

    If templates are not configured, falls back to sending a JSON payload.

    Endpoint options:
      frame_format      auto (default) | hex | ascii — how a rendered template is sent.
                        auto sends hex when every token is hex bytes, e.g. "01 05 000A FF00".
      ascii_terminator  lf (default) | crlf | cr | none — appended to ASCII/JSON frames
      response_mode     none (default: fire and forget) | any (wait for any reply) |
                        match (wait for a reply containing response_match)
      response_match    text (or hex bytes in hex mode) that a good reply contains
    """

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        await self.disconnect()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
            self._mark_connected()
            logger.info("GenericTCP connected: %s:%d", self.host, self.port)
            return True
        except Exception as exc:
            self._mark_connect_failed(f"{type(exc).__name__}: {exc}")
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

    def _terminator(self) -> bytes:
        option = str(self._ep.get("ascii_terminator", "lf") or "lf").strip().lower()
        return {"lf": b"\n", "crlf": b"\r\n", "cr": b"\r", "none": b""}.get(option, b"\n")

    def _render_frame(self, template: str, address: str, value: float = 0) -> bytes:
        """
        Render a frame template string into bytes.

        Template variables: {addr}, {addr_int}, {address}, {value}
        {addr}/{addr_int} need a numeric address (decimal or 0x..); {address} is the raw text.
        If the template looks like hex bytes ("01 05 FF 00", "01 05 {addr:04X}"), it is
        sent as bytes; otherwise as ASCII text. frame_format forces either mode.
        """
        try:
            addr_int = int(address, 0) if address.lower().startswith("0x") else int(address)
        except ValueError:
            addr_int = None
        if addr_int is None and re.search(r"\{addr(_int)?[}:!]", template):
            raise ValueError("This frame template uses {addr}; the target address must be a number")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Frame template value must be a number") from exc
        value_arg = int(numeric) if numeric.is_integer() else numeric
        if isinstance(value_arg, float) and re.search(r"\{value:[^}]*[xXdbo]\}", template):
            raise ValueError("Integer frame formats such as {value:04X} need a whole-number value")

        crc_requested = "{crc}" in template.lower()
        rendered = re.sub(r"\{crc\}", "", template, flags=re.IGNORECASE).format(
            addr=addr_int,
            addr_int=addr_int,
            address=address,
            value=value_arg,
        )

        frame_format = str(self._ep.get("frame_format", "auto") or "auto").strip().lower()
        parts = rendered.strip().split()
        hex_tokens = bool(parts) and all(
            len(p) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts
        )
        if frame_format == "auto":
            # Two-character tokens were always treated as hex bytes. Longer even
            # tokens only count as hex when the template asks for hex formatting,
            # so an ASCII template like "{addr} {value}" is not misread.
            wide_tokens = any(len(p) > 2 for p in parts)
            is_hex = hex_tokens and (not wide_tokens or bool(re.search(r"\{[^}]*:[^}]*[xX]\}", template)))
        elif frame_format == "hex":
            if not hex_tokens:
                raise ValueError(f"Rendered frame is not hex bytes: '{rendered.strip()}'")
            is_hex = True
        else:
            is_hex = False

        if is_hex:
            frame = bytes.fromhex("".join(parts))
            if crc_requested:
                crc = 0xFFFF
                for byte in frame:
                    crc ^= byte
                    for _ in range(8):
                        crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
                frame += bytes((crc & 0xFF, crc >> 8))
            return frame

        if crc_requested:
            raise ValueError("CRC placeholder is only supported for hex frame templates")

        return rendered.encode("utf-8") + self._terminator()

    def _json_frame(self, payload: dict) -> bytes:
        import json
        return json.dumps(payload).encode() + self._terminator()

    async def _send(self, frame: bytes) -> Tuple[bool, str]:
        if not self._writer or self._writer.is_closing() or (self._reader and self._reader.at_eof()):
            # The peer closed the socket; writing would "succeed" into the void.
            self.is_connected = False
            if not await self.connect():
                return False, "GenericTCP not connected"
        mode = str(self._ep.get("response_mode", "none") or "none").strip().lower()
        async with self._lock:
            try:
                if mode != "none":
                    self._drain_stale_input()
                self._writer.write(frame)
                await self._writer.drain()
                if mode == "none":
                    return True, f"Sent {len(frame)} bytes (no reply expected)"
                reply = await self._read_reply()
            except asyncio.TimeoutError:
                self.is_connected = False
                return False, "GenericTCP reply timeout; device outcome is unknown"
            except Exception as exc:
                self.is_connected = False
                return False, f"GenericTCP send error: {exc}"
        if not reply:
            self.is_connected = False
            return False, "GenericTCP device closed the connection without replying"
        if mode == "match":
            expected = self._expected_reply()
            if expected not in reply:
                return False, f"GenericTCP reply did not match: {reply[:64]!r}"
        return True, f"Sent {len(frame)} bytes, device replied {reply[:64]!r}"

    def _drain_stale_input(self) -> None:
        """Discard unsolicited bytes so they are not mistaken for this request's reply."""
        buffer = getattr(self._reader, "_buffer", None)
        if buffer:
            buffer.clear()

    async def _read_reply(self) -> bytes:
        reply = await asyncio.wait_for(self._reader.read(4096), timeout=self.timeout)
        expected = self._expected_reply() if str(self._ep.get("response_mode", "")).lower() == "match" else b""
        # Keep reading briefly if the reply arrived in pieces and does not match yet.
        while reply and expected and expected not in reply and len(reply) < 4096:
            try:
                more = await asyncio.wait_for(self._reader.read(4096), timeout=min(self.timeout, 0.2))
            except asyncio.TimeoutError:
                break
            if not more:
                break
            reply += more
        return reply

    def _expected_reply(self) -> bytes:
        text = str(self._ep.get("response_match", "") or "")
        if str(self._ep.get("frame_format", "")).lower() == "hex":
            return bytes.fromhex("".join(text.split()))
        return text.encode("utf-8")

    async def set(self, address: str) -> Tuple[bool, str]:
        tmpl = self._ep.get("set_template", "")
        if tmpl:
            frame = self._render_frame(tmpl, address)
        else:
            frame = self._json_frame({"op": "SET", "address": address})
        ok, msg = await self._send(frame)
        return ok, f"GenericTCP SET {address} — {msg}"

    async def reset(self, address: str) -> Tuple[bool, str]:
        tmpl = self._ep.get("reset_template", "")
        if tmpl:
            frame = self._render_frame(tmpl, address)
        else:
            frame = self._json_frame({"op": "RESET", "address": address})
        ok, msg = await self._send(frame)
        return ok, f"GenericTCP RESET {address} — {msg}"

    async def toggle(self, address: str) -> Tuple[bool, str]:
        tmpl = self._ep.get("toggle_template", "")
        if tmpl:
            frame = self._render_frame(tmpl, address)
        else:
            frame = self._json_frame({"op": "TOGGLE", "address": address})
        ok, msg = await self._send(frame)
        return ok, f"GenericTCP TOGGLE {address} — {msg}"

    async def pulse(self, address: str, duration_ms: int) -> Tuple[bool, str]:
        ok1, _ = await self.set(address)
        if not ok1:
            return False, f"GenericTCP PULSE ON failed for {address}"
        try:
            await asyncio.sleep(duration_ms / 1000.0)
        finally:
            reset_task = asyncio.create_task(self.reset(address))
            try:
                ok2, _ = await asyncio.shield(reset_task)
            except asyncio.CancelledError:
                await reset_task
                raise
        return ok2, f"GenericTCP PULSE {address} {duration_ms} ms — {'OK' if ok2 else 'OFF failed'}"

    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        tmpl = self._ep.get("write_template", "")
        if tmpl:
            frame = self._render_frame(tmpl, address, value)
        else:
            frame = self._json_frame({"op": "WRITE", "address": address, "value": value})
        ok, msg = await self._send(frame)
        return ok, f"GenericTCP WRITE {address} = {value} — {msg}"
