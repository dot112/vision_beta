"""Modbus RTU client: RS-485/RS-232 serial ports and RTU framing tunnelled over TCP."""
from __future__ import annotations

import asyncio
import struct
import time
from typing import Dict, Optional, Tuple

from app.hardware.modbus.client import ModbusExceptionResponse, ModbusTCPClient, _EXCEPTION_NAMES
from app.utils.logger import get_logger

logger = get_logger(__name__)

SERIAL_BAUDRATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)


def crc16_modbus(frame: bytes) -> bytes:
    """Modbus RTU CRC-16 (poly 0xA001), low byte first."""
    crc = 0xFFFF
    for byte in frame:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return bytes((crc & 0xFF, crc >> 8))


class _SerialBus:
    """
    One open serial port shared by every channel on the same RS-485 bus.

    Several PLCs or drives often hang off one bus with different unit IDs.
    The port can only be opened once, and requests must not interleave on
    the wire, so channels share the handle and a lock per port.
    """

    _buses: Dict[str, "_SerialBus"] = {}

    def __init__(self, port: str, settings: Tuple[int, str, int]):
        self.port = port
        self.settings = settings
        self.serial = None
        self.lock = asyncio.Lock()
        self.users = 0
        self.last_io = 0.0

    @classmethod
    async def acquire(cls, port: str, settings: Tuple[int, str, int], timeout: float) -> "_SerialBus":
        bus = cls._buses.get(port)
        if bus is not None and bus.settings != settings:
            baud, parity, stop = bus.settings
            raise ConnectionError(
                f"Serial port {port} is already open at {baud} baud 8{parity}{stop}; every channel "
                "on one RS-485 bus must use the same baud rate, parity and stop bits"
            )
        if bus is None:
            bus = cls(port, settings)
            cls._buses[port] = bus
        if bus.serial is None or not bus.serial.is_open:
            try:
                bus.serial = await asyncio.to_thread(bus._open, timeout)
            except Exception:
                if bus.users == 0:
                    cls._buses.pop(port, None)
                raise
        bus.users += 1
        return bus

    def _open(self, timeout: float):
        try:
            import serial
        except ImportError as exc:
            raise ConnectionError("Modbus RTU over a serial port needs the pyserial package") from exc
        baud, parity, stop = self.settings
        return serial.serial_for_url(
            self.port,
            baudrate=baud,
            bytesize=8,
            parity=parity,
            stopbits=stop,
            timeout=timeout,
            write_timeout=timeout,
        )

    async def release(self) -> None:
        self.users -= 1
        if self.users > 0:
            return
        self._buses.pop(self.port, None)
        port, self.serial = self.serial, None
        if port is not None:
            try:
                await asyncio.to_thread(port.close)
            except Exception:
                pass


class ModbusRTUClient(ModbusTCPClient):
    """
    Modbus RTU master with the same read/write API as ModbusTCPClient.

    transport="serial": a local COM port / tty (or any pyserial URL such as
    rfc2217://host:port for a network serial server).
    transport="tcp": raw RTU frames (with CRC) over a TCP socket, as used by
    serial device servers and gateways in "RTU over TCP" / transparent mode.
    """

    def __init__(
        self,
        *,
        transport: str = "serial",
        serial_port: str = "",
        baudrate: int = 19200,
        parity: str = "E",
        stopbits: int = 1,
        host: str = "",
        port: int = 502,
        timeout: float = 1.0,
        unit_id: int = 1,
        simulation_mode: bool = False,
    ):
        super().__init__(host=host, port=port, timeout=timeout, unit_id=unit_id, simulation_mode=simulation_mode)
        self.transport = "tcp" if transport == "tcp" else "serial"
        self.serial_port = serial_port
        self.settings = (int(baudrate), str(parity).upper()[:1] or "E", int(stopbits))
        self._bus: Optional[_SerialBus] = None
        baud = self.settings[0]
        # Modbus RTU needs 3.5 character times of silence between frames
        # (fixed at 1.75 ms above 19200 baud). One character is 11 bits.
        self._frame_gap = 3.5 * 11 / baud if baud <= 19200 else 0.00175
        self._last_io = 0.0

    def describe(self) -> str:
        if self.transport == "tcp":
            return f"{self.host}:{self.port} (RTU over TCP)"
        baud, parity, stop = self.settings
        return f"{self.serial_port} {baud} 8{parity}{stop}"

    async def connect(self) -> bool:
        await self.disconnect()
        try:
            if self.transport == "tcp":
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=float(self.timeout)
                )
            else:
                if not self.serial_port:
                    raise ConnectionError("No serial port is configured for this Modbus RTU channel")
                self._bus = await _SerialBus.acquire(self.serial_port, self.settings, float(self.timeout))
            self._real_socket = True
            self._simulation_active = False
            self.is_connected = True
            logger.info("Modbus RTU link open: %s", self.describe())
            return True
        except Exception as exc:
            self._reader = None
            self._writer = None
            self._real_socket = False
            self._simulation_active = bool(self.simulation_mode)
            self.is_connected = self._simulation_active
            if self._simulation_active:
                logger.warning("Explicit Modbus simulation enabled for %s (%s)", self.describe(), exc)
            else:
                logger.warning("Modbus RTU link unavailable at %s: %s", self.describe(), exc)
            return self.is_connected

    async def disconnect(self) -> None:
        await super().disconnect()
        bus, self._bus = self._bus, None
        if bus is not None:
            await bus.release()

    async def _request(self, pdu: bytes) -> bytes:
        """Send one PDU framed as RTU and return the reply in the MBAP layout the base parsers read."""
        if not self.is_connected or not self._real_socket:
            raise ConnectionError("Modbus RTU link is not open")
        frame = bytes((self.unit_id,)) + pdu
        frame += crc16_modbus(frame)
        lock = self._bus.lock if self._bus is not None else self._io_lock
        async with lock:
            try:
                await self._wait_frame_gap()
                if self._bus is not None:
                    reply = await self._serial_exchange(frame, pdu[0])
                else:
                    reply = await self._tcp_exchange(frame, pdu[0])
            except ModbusExceptionResponse:
                raise
            except Exception:
                if self._bus is None:
                    # A late reply would be read as the answer to the next request.
                    writer, self._writer, self._reader = self._writer, None, None
                    if writer is not None:
                        writer.close()
                self.is_connected = False
                self._real_socket = False
                raise
            finally:
                self._mark_io()
        function = pdu[0]
        if function in (0x05, 0x06, 0x0F, 0x10) and reply[1:5] != pdu[1:5]:
            raise ValueError("Modbus RTU write reply did not echo the requested address and value")
        # Present the reply exactly like a Modbus TCP frame so the shared parsers work.
        return struct.pack(">HHHB", 0, 0, len(reply) + 1, self.unit_id) + reply

    async def _wait_frame_gap(self) -> None:
        last = self._bus.last_io if self._bus is not None else self._last_io
        remaining = self._frame_gap - (time.monotonic() - last)
        if remaining > 0:
            await asyncio.sleep(remaining)

    def _mark_io(self) -> None:
        now = time.monotonic()
        self._last_io = now
        if self._bus is not None:
            self._bus.last_io = now

    async def _serial_exchange(self, frame: bytes, function: int) -> bytes:
        port = self._bus.serial
        timeout = float(self.timeout)

        def read_exact(count: int) -> bytes:
            data = port.read(count)
            if len(data) != count:
                raise TimeoutError(
                    f"no reply from Modbus unit {self.unit_id} within {timeout:g}s"
                    if not data else "incomplete Modbus RTU reply"
                )
            return data

        def exchange() -> bytes:
            port.reset_input_buffer()   # drop late replies and line noise
            port.write(frame)
            port.flush()
            head = read_exact(2)
            if head[1] in (0x01, 0x02, 0x03, 0x04):
                count = read_exact(1)
                return head + count + read_exact(count[0] + 2)
            return head + read_exact(3 if head[1] & 0x80 else 6)

        try:
            reply = await asyncio.wait_for(asyncio.to_thread(exchange), timeout=timeout * 3 + 1)
        except Exception as exc:
            if not getattr(port, "is_open", False):
                raise ConnectionError(f"Serial port {self.serial_port} is no longer open") from exc
            raise
        return _parse_rtu_reply(reply, self.unit_id, function)

    async def _tcp_exchange(self, frame: bytes, function: int) -> bytes:
        self._writer.write(frame)
        await self._writer.drain()

        async def read_exact(count: int) -> bytes:
            return await asyncio.wait_for(self._reader.readexactly(count), timeout=float(self.timeout))

        head = await read_exact(2)
        if head[1] in (0x01, 0x02, 0x03, 0x04):
            count = await read_exact(1)
            rest = count + await read_exact(count[0] + 2)
        else:
            rest = await read_exact(3 if head[1] & 0x80 else 6)
        return _parse_rtu_reply(head + rest, self.unit_id, function)


def _parse_rtu_reply(frame: bytes, unit_id: int, function: int) -> bytes:
    """Validate a complete RTU reply and return its PDU (function code + data)."""
    if len(frame) < 5:
        raise ValueError("Truncated Modbus RTU reply")
    if crc16_modbus(frame[:-2]) != frame[-2:]:
        raise ValueError("Modbus RTU reply failed its CRC check (noise or wrong baud rate/parity?)")
    if frame[0] != unit_id:
        raise ValueError(f"Modbus RTU reply came from unit {frame[0]}, expected {unit_id}")
    reply_function = frame[1]
    if reply_function & 0x80:
        code = frame[2]
        raise ModbusExceptionResponse(f"Modbus exception 0x{code:02X}{_EXCEPTION_NAMES.get(code, '')}")
    if reply_function != function:
        raise ValueError(f"Unexpected Modbus function 0x{reply_function:02X} in RTU reply")
    return frame[1:-2]
