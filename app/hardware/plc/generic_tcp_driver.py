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
    """

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
            self.is_connected = True
            logger.info("GenericTCP connected: %s:%d", self.host, self.port)
            return True
        except Exception as exc:
            logger.warning("GenericTCP connect failed %s:%d — %s", self.host, self.port, exc)
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
        self.is_connected = False

    def _render_frame(self, template: str, address: str, value: float = 0) -> bytes:
        """
        Render a frame template string into bytes.

        Template variables: {addr}, {value}, {addr_int}
        If template looks like hex bytes (space-separated "01 05 ..."), encode as bytes.
        Otherwise encode as UTF-8 ASCII (for text-based devices).
        """
        try:
            addr_int = int(address, 0) if address.lower().startswith("0x") else int(address)
        except ValueError as exc:
            raise ValueError("Hex frame templates require a numeric address") from exc

        crc_requested = "{crc}" in template.lower()
        rendered = re.sub(r"\{crc\}", "", template, flags=re.IGNORECASE).format(
            addr=addr_int,
            addr_int=addr_int,
            address=address,
            value=int(value),
        )

        # Detect hex-byte format: "01 05 FF 00"
        parts = rendered.strip().split()
        if all(len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts):
            frame = bytes(int(p, 16) for p in parts)
            if crc_requested:
                crc = 0xFFFF
                for byte in frame:
                    crc ^= byte
                    for _ in range(8):
                        crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
                frame += bytes((crc & 0xFF, crc >> 8))
            return frame

        if crc_requested:
            raise ValueError("CRC placeholder is only supported for space-separated hex frame templates")

        return (rendered + "\n").encode("utf-8")

    async def _send(self, frame: bytes) -> Tuple[bool, str]:
        if not self._writer:
            return False, "GenericTCP not connected"
        async with self._lock:
            try:
                self._writer.write(frame)
                await self._writer.drain()
                return True, f"Sent {len(frame)} bytes"
            except Exception as exc:
                self.is_connected = False
                return False, f"GenericTCP send error: {exc}"

    async def set(self, address: str) -> Tuple[bool, str]:
        tmpl = self._ep.get("set_template", "")
        if tmpl:
            frame = self._render_frame(tmpl, address)
        else:
            import json
            frame = (json.dumps({"op": "SET", "address": address}) + "\n").encode()
        ok, msg = await self._send(frame)
        return ok, f"GenericTCP SET {address} — {msg}"

    async def reset(self, address: str) -> Tuple[bool, str]:
        tmpl = self._ep.get("reset_template", "")
        if tmpl:
            frame = self._render_frame(tmpl, address)
        else:
            import json
            frame = (json.dumps({"op": "RESET", "address": address}) + "\n").encode()
        ok, msg = await self._send(frame)
        return ok, f"GenericTCP RESET {address} — {msg}"

    async def toggle(self, address: str) -> Tuple[bool, str]:
        tmpl = self._ep.get("toggle_template", "")
        if tmpl:
            frame = self._render_frame(tmpl, address)
        else:
            import json
            frame = (json.dumps({"op": "TOGGLE", "address": address}) + "\n").encode()
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
            import json
            frame = (json.dumps({"op": "WRITE", "address": address, "value": value}) + "\n").encode()
        ok, msg = await self._send(frame)
        return ok, f"GenericTCP WRITE {address} = {value} — {msg}"
