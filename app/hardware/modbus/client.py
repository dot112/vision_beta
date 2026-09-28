from __future__ import annotations

import asyncio
import struct
from typing import List, Optional, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)


class ModbusExceptionResponse(ValueError):
    """The server answered with a Modbus exception; the connection itself is still healthy."""

_EXCEPTION_NAMES = {
    0x01: " (illegal function)",
    0x02: " (illegal data address)",
    0x03: " (illegal data value)",
    0x04: " (server device failure)",
    0x06: " (server device busy)",
    0x0A: " (gateway path unavailable)",
    0x0B: " (gateway target device failed to respond)",
}


class ModbusTCPClient:
    """Async Modbus TCP client with explicit opt-in simulation mode."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 502,
        timeout: int = 3,
        unit_id: int = 1,
        simulation_mode: bool = False,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.unit_id = unit_id
        self.simulation_mode = simulation_mode
        self.is_connected = False
        self._real_socket = False
        self._simulation_active = False
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._tx_id = 0
        self._io_lock = asyncio.Lock()
        self._coils = [False] * 512
        self._registers = [0] * 512

    async def connect(self) -> bool:
        if self._writer is not None:
            await self.disconnect()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=float(self.timeout)
            )
            self._real_socket = True
            self._simulation_active = False
            self.is_connected = True
            logger.info("Connected to Modbus TCP server at %s:%s", self.host, self.port)
            return True
        except (OSError, asyncio.TimeoutError) as exc:
            self._reader = None
            self._writer = None
            self._real_socket = False
            self._simulation_active = bool(self.simulation_mode)
            self.is_connected = self._simulation_active
            if self._simulation_active:
                logger.warning("Explicit Modbus simulation enabled for %s:%s (%s)", self.host, self.port, exc)
            else:
                logger.warning("Modbus TCP server unavailable at %s:%s: %s", self.host, self.port, exc)
            return self.is_connected

    async def disconnect(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        self._real_socket = False
        self._simulation_active = False
        self.is_connected = False

    async def _request(self, pdu: bytes) -> bytes:
        """Send one Modbus PDU (function code + data) and return the validated response frame."""
        async with self._io_lock:
            if not self.is_connected or not self._real_socket or not self._reader or not self._writer:
                raise ConnectionError("Modbus TCP connection is not established")
            writer = self._writer
            reader = self._reader
            try:
                self._tx_id = (self._tx_id + 1) & 0xFFFF
                tx_id = self._tx_id
                function = pdu[0]
                packet = struct.pack(">HHHB", tx_id, 0, len(pdu) + 1, self.unit_id) + pdu
                writer.write(packet)
                await writer.drain()
                header = await asyncio.wait_for(reader.readexactly(7), timeout=float(self.timeout))
                response_length = struct.unpack(">H", header[4:6])[0]
                if response_length < 2 or response_length > 260:
                    raise ValueError(f"Invalid Modbus response length: {response_length}")
                body = await asyncio.wait_for(reader.readexactly(response_length - 1), timeout=float(self.timeout))
                response = header + body
                response_tx, protocol_id = struct.unpack(">HH", response[:4])
                if response_tx != tx_id or protocol_id != 0 or response[6] != self.unit_id:
                    raise ValueError("Modbus response header does not match request")
                response_function = response[7]
                if response_function & 0x80:
                    code = response[8] if len(response) > 8 else 0
                    raise ModbusExceptionResponse(f"Modbus exception 0x{code:02X}{_EXCEPTION_NAMES.get(code, '')}")
                if response_function != function:
                    raise ValueError(f"Unexpected Modbus function 0x{response_function:02X}")
                if function in (0x05, 0x06) and response[8:12] != packet[8:12]:
                    raise ValueError("Modbus write response did not echo the requested address and value")
                if function in (0x0F, 0x10) and response[8:12] != packet[8:12]:
                    raise ValueError("Modbus write response did not echo the requested address and quantity")
                return response
            except ModbusExceptionResponse:
                raise
            except Exception:
                # A timed-out or malformed response can desynchronize the stream.
                # Fail closed before another queued request can reuse this socket.
                self.is_connected = False
                self._real_socket = False
                self._simulation_active = False
                self._reader = None
                self._writer = None
                writer.close()
                raise

    async def _exchange(self, function: int, address: int, value: int) -> bytes:
        return await self._request(struct.pack(">BHH", function, address, value))

    async def write_coil(self, address: int, value: bool) -> Tuple[bool, Optional[str]]:
        if address < 0 or address > 0xFFFF:
            return False, "Modbus coil address must be between 0 and 65535"
        if self._simulation_active:
            if address >= len(self._coils):
                return False, f"Simulation coil address out of range (0..{len(self._coils)-1})"
            self._coils[address] = value
            return True, None
        try:
            await self._exchange(0x05, address, 0xFF00 if value else 0x0000)
            return True, None
        except ModbusExceptionResponse as exc:
            return False, str(exc)
        except Exception as exc:
            self.is_connected = False
            logger.warning("Modbus write_coil(%d) failed: %s", address, exc)
            return False, str(exc)

    async def read_coils(self, address: int, count: int = 1) -> Tuple[bool, List[bool], Optional[str]]:
        if address < 0 or address > 0xFFFF or count < 1 or count > 2000 or address + count > 0x10000:
            return False, [], f"Invalid Modbus coil range {address}..{address + count - 1}"
        if self._simulation_active:
            if address + count > len(self._coils):
                return False, [], f"Simulation coil range exceeds local memory (0..{len(self._coils)-1})"
            return True, self._coils[address:address + count], None
        try:
            response = await self._exchange(0x01, address, count)
            byte_count = response[8]
            coil_bytes = response[9:9 + byte_count]
            if len(coil_bytes) != byte_count or byte_count < (count + 7) // 8:
                raise ValueError("Incomplete Modbus coil response")
            values = [bool((coil_bytes[i // 8] >> (i % 8)) & 1) for i in range(count)]
            return True, values, None
        except ModbusExceptionResponse as exc:
            return False, [], str(exc)
        except Exception as exc:
            self.is_connected = False
            return False, [], str(exc)

    async def write_register(self, address: int, value: int) -> Tuple[bool, Optional[str]]:
        if address < 0 or address > 0xFFFF:
            return False, "Modbus register address must be between 0 and 65535"
        if value < 0 or value > 0xFFFF:
            return False, "Modbus register value must be between 0 and 65535"
        value16 = value
        if self._simulation_active:
            if address >= len(self._registers):
                return False, f"Simulation register address out of range (0..{len(self._registers)-1})"
            self._registers[address] = value16
            return True, None
        try:
            await self._exchange(0x06, address, value16)
            return True, None
        except ModbusExceptionResponse as exc:
            return False, str(exc)
        except Exception as exc:
            self.is_connected = False
            logger.warning("Modbus write_register(%d) failed: %s", address, exc)
            return False, str(exc)

    async def write_registers(self, address: int, values: List[int]) -> Tuple[bool, Optional[str]]:
        """Write consecutive holding registers with function 16 (used for 32-bit values)."""
        if not values or len(values) > 123:
            return False, "Modbus multi-register write needs 1 to 123 registers"
        if address < 0 or address + len(values) > 0x10000:
            return False, "Modbus register address must be between 0 and 65535"
        if any(value < 0 or value > 0xFFFF for value in values):
            return False, "Modbus register value must be between 0 and 65535"
        if self._simulation_active:
            if address + len(values) > len(self._registers):
                return False, f"Simulation register address out of range (0..{len(self._registers)-1})"
            self._registers[address:address + len(values)] = list(values)
            return True, None
        try:
            pdu = struct.pack(">BHHB", 0x10, address, len(values), len(values) * 2)
            pdu += b"".join(struct.pack(">H", value) for value in values)
            await self._request(pdu)
            return True, None
        except ModbusExceptionResponse as exc:
            return False, str(exc)
        except Exception as exc:
            self.is_connected = False
            logger.warning("Modbus write_registers(%d, n=%d) failed: %s", address, len(values), exc)
            return False, str(exc)

    async def read_registers(self, address: int, count: int = 1) -> Tuple[bool, List[int], Optional[str]]:
        if address < 0 or address > 0xFFFF or count < 1 or count > 125 or address + count > 0x10000:
            return False, [], f"Invalid Modbus register range {address}..{address + count - 1}"
        if self._simulation_active:
            if address + count > len(self._registers):
                return False, [], f"Simulation register range exceeds local memory (0..{len(self._registers)-1})"
            return True, self._registers[address:address + count], None
        try:
            response = await self._exchange(0x03, address, count)
            byte_count = response[8]
            data = response[9:9 + byte_count]
            if byte_count != count * 2 or len(data) != byte_count:
                raise ValueError("Incomplete Modbus register response")
            values = [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, len(data), 2)]
            return True, values, None
        except ModbusExceptionResponse as exc:
            return False, [], str(exc)
        except Exception as exc:
            self.is_connected = False
            return False, [], str(exc)
