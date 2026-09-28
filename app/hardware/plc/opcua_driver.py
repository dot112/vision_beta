"""OPC UA PLC driver using the asyncua client."""
from __future__ import annotations

import asyncio
import math
from typing import Tuple

from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)


class OPCUADriver(PLCDriver):
    """Write OPC UA variable nodes addressed by standard NodeId strings."""

    _INTEGER_RANGES = {
        "SByte": (-128, 127),
        "Byte": (0, 255),
        "Int16": (-32768, 32767),
        "UInt16": (0, 65535),
        "Int32": (-2147483648, 2147483647),
        "UInt32": (0, 4294967295),
        "Int64": (-9223372036854775808, 9223372036854775807),
        "UInt64": (0, 18446744073709551615),
    }

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        self._client = None
        self.last_error = ""

    def _server_url(self) -> str:
        host = self.host.strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        url_host = f"[{host}]" if ":" in host else host
        path = str(self._ep.get("opcua_path") or "").strip()
        if path and not path.startswith("/"):
            path = f"/{path}"
        if any(char in path for char in ("://", "?", "#")):
            raise ValueError("OPC UA endpoint path must be a URL path")
        return f"opc.tcp://{url_host}:{self.port}{path}"

    async def connect(self) -> bool:
        if self.is_connected and self._client is not None:
            return True

        security = str(self._ep.get("opcua_security", "None")).strip()
        if security != "None":
            self.last_error = (
                f"OPC UA security mode '{security}' needs client certificate and private-key "
                "settings, which are not configured for this channel"
            )
            return False

        try:
            from asyncua import Client

            client = Client(self._server_url(), timeout=self.timeout)
            await client.connect()
            self._client = client
            self.is_connected = True
            self.last_error = ""
            logger.info("OPC UA connected: %s", self._server_url())
            return True
        except ImportError:
            self.last_error = "OPC UA support requires the asyncua package; install the project requirements"
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

        self.is_connected = False
        client = locals().get("client")
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        logger.warning("OPC UA connect failed for %s: %s", self._ep.get("id", ""), self.last_error)
        return False

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self.is_connected = False
        if client is not None:
            await client.disconnect()

    def _node(self, address: str):
        if not self.is_connected or self._client is None:
            raise ConnectionError("OPC UA client is not connected")
        node_id = str(address or "").strip()
        if not node_id or len(node_id) > 512:
            raise ValueError("OPC UA NodeId must contain 1 to 512 characters")
        if "=" not in node_id:
            raise ValueError(
                'OPC UA target must be a full NodeId, for example ns=3;s="Tag_1"; '
                "a BrowseName such as tag_1 by itself is not a NodeId"
            )
        return self._client.get_node(node_id)

    async def _write_value(self, address: str, value) -> Tuple[bool, str]:
        try:
            from asyncua import ua

            node = self._node(address)
            # Siemens S7-1500 rejects writes with SourceTimestamp. Passing a
            # Variant directly makes asyncua add one, so send a value-only
            # DataValue just like the working PLC test client does.
            data_value = ua.DataValue(ua.Variant(value, ua.VariantType.Boolean))
            await node.write_value(data_value)
            return True, f"OPC UA wrote {value!s} to {address}"
        except Exception as exc:
            if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
                self.is_connected = False
            return False, f"OPC UA write failed for {address}: {type(exc).__name__}: {exc}"

    async def set(self, address: str) -> Tuple[bool, str]:
        return await self._write_value(address, True)

    async def reset(self, address: str) -> Tuple[bool, str]:
        return await self._write_value(address, False)

    async def toggle(self, address: str) -> Tuple[bool, str]:
        try:
            node = self._node(address)
            value = await node.read_value()
            if not isinstance(value, bool):
                return False, f"OPC UA toggle requires a Boolean node: {address}"
            return await self._write_value(address, not value)
        except Exception as exc:
            if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
                self.is_connected = False
            return False, f"OPC UA toggle failed for {address}: {type(exc).__name__}: {exc}"

    async def pulse(self, address: str, duration_ms: int) -> Tuple[bool, str]:
        ok, message = await self.set(address)
        if not ok:
            return False, f"OPC UA pulse ON failed: {message}"
        try:
            await asyncio.sleep(max(0, duration_ms) / 1000.0)
        finally:
            reset_task = asyncio.create_task(self.reset(address))
            try:
                reset_ok, reset_message = await asyncio.shield(reset_task)
            except asyncio.CancelledError:
                await reset_task
                raise
        return reset_ok, f"OPC UA pulse {address} for {duration_ms} ms: {reset_message}"

    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        try:
            from asyncua import ua

            numeric_value = float(value)
            if not math.isfinite(numeric_value):
                return False, "OPC UA numeric value must be finite"
            node = self._node(address)
            variant_type = await node.read_data_type_as_variant_type()
            type_name = getattr(variant_type, "name", str(variant_type))
            if type_name in self._INTEGER_RANGES:
                if not numeric_value.is_integer():
                    return False, f"OPC UA node {address} requires an integer value"
                typed_value = int(numeric_value)
                minimum, maximum = self._INTEGER_RANGES[type_name]
                if not minimum <= typed_value <= maximum:
                    return False, f"Value is outside the {type_name} range for OPC UA node {address}"
            elif type_name in {"Float", "Double"}:
                typed_value = numeric_value
            else:
                return False, f"OPC UA WRITE supports numeric scalar nodes; {address} is {type_name}"
            # Do not let asyncua add SourceTimestamp; Siemens S7-1500 can reject it.
            data_value = ua.DataValue(ua.Variant(typed_value, variant_type))
            await node.write_value(data_value)
            return True, f"OPC UA wrote {typed_value} to {address}"
        except Exception as exc:
            if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
                self.is_connected = False
            return False, f"OPC UA write failed for {address}: {type(exc).__name__}: {exc}"
