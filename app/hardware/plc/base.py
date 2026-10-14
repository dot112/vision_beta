"""Abstract base class for all PLC protocol drivers."""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from typing import Any, Tuple

from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager
from app.utils.logger import get_logger

logger = get_logger(__name__)

# What read() can be asked to return. "auto" takes the type from the address:
# its suffix (":DINT", ":REAL"), or what the protocol says the address holds.
READ_DATA_TYPES = ("auto", "bool", "int16", "uint16", "int32", "real", "string")


class PLCDriver(ABC):
    """
    Protocol-agnostic interface for all PLC communication drivers.

    Every concrete driver (Modbus TCP, Siemens S7, EtherNet/IP, Generic TCP)
    implements this interface.  The PLCDispatcherService talks *only* to this
    interface — it never touches protocol-specific details.
    """

    def __init__(self, endpoint: dict):
        self._ep = endpoint
        self.is_connected: bool = False
        self.last_connect_error: str = ""
        # Throwaway drivers used for "test connection" must not raise or clear
        # the production alarm for the endpoint; the factory turns this off.
        self.report_alarms: bool = True
        # One request at a time on the connection. A pulse frees it between its
        # ON and OFF writes (_pulse_wait), so an input can read what the pulse does.
        self._operation_lock = asyncio.Lock()
        self._lock_holder: "asyncio.Task[Any] | None" = None

    @property
    def endpoint_id(self) -> str:
        return self._ep.get("id", "")

    @property
    def host(self) -> str:
        return self._ep.get("host", "")

    @property
    def port(self) -> int:
        return int(self._ep.get("port", 502))

    @property
    def timeout(self) -> float:
        return float(self._ep.get("timeout", 3.0))

    @property
    def protocol(self) -> str:
        return str(self._ep.get("plc_sub_protocol", type(self).__name__))

    @property
    def alarm_source(self) -> str:
        return f"plc:{self.endpoint_id or self.host}"

    # ── Connection state reporting ────────────────────────────────────────────

    def _mark_connected(self) -> None:
        """Record a successful connection and clear any connect alarm."""
        self.is_connected = True
        self.last_connect_error = ""
        if self.report_alarms:
            alarm_manager.clear_alarm(AlarmCode.PLC_CONNECT_FAILED, self.alarm_source, "connected")
            alarm_manager.clear_alarm(AlarmCode.PLC_DISCONNECT_ERROR, self.alarm_source, "connected again")

    def _mark_connect_failed(self, reason: str) -> None:
        """Record a failed connection attempt and raise a critical alarm."""
        self.is_connected = False
        self.last_connect_error = reason
        if self.report_alarms:
            alarm_manager.raise_alarm(
                AlarmCode.PLC_CONNECT_FAILED,
                self.alarm_source,
                f"Cannot connect to PLC {self._ep.get('name') or self.endpoint_id} at {self.host}:{self.port}: {reason}",
                AlarmSeverity.CRITICAL,
                {"endpoint_id": self.endpoint_id, "protocol": self.protocol, "host": self.host, "port": self.port},
            )
        else:
            logger.warning("PLC test connection to %s:%s failed: %s", self.host, self.port, reason)

    def _log_disconnect_error(self, exc: BaseException) -> None:
        """Closing a socket failed. Not fatal, but never silent: a warning alarm
        until the next successful connect, since the PLC may still hold the old session."""
        logger.warning(
            "PLC %s (%s:%s) did not close cleanly: %s: %s",
            self.endpoint_id or "?", self.host, self.port, type(exc).__name__, exc,
        )
        if self.report_alarms:
            alarm_manager.raise_alarm(
                AlarmCode.PLC_DISCONNECT_ERROR,
                self.alarm_source,
                f"PLC {self._ep.get('name') or self.endpoint_id} at {self.host}:{self.port} did not close "
                f"its connection cleanly: {type(exc).__name__}: {exc}",
                AlarmSeverity.WARNING,
                {"endpoint_id": self.endpoint_id, "protocol": self.protocol, "host": self.host, "port": self.port},
            )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> bool:
        """Open connection to the PLC. Returns True on success."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection gracefully."""

    async def ensure_connected(self) -> bool:
        """Connect if not already connected. Returns True if ready."""
        if not self.is_connected:
            return await self.connect()
        return True

    # ── Operations ────────────────────────────────────────────────────────────

    @abstractmethod
    async def set(self, address: str) -> Tuple[bool, str]:
        """Set target address to ON / 1. Returns (success, message)."""

    @abstractmethod
    async def reset(self, address: str) -> Tuple[bool, str]:
        """Set target address to OFF / 0. Returns (success, message)."""

    @abstractmethod
    async def pulse(self, address: str, duration_ms: int) -> Tuple[bool, str]:
        """
        Set target to ON, wait duration_ms (with _pulse_wait), then set to OFF.
        Returns (success, message) after the full pulse completes.
        """

    async def _pulse_wait(self, duration_ms: int) -> None:
        """The wait between a pulse's ON and OFF writes.

        Inside execute_operation() the connection is freed for the wait: other
        requests, such as an input reading what the pulse switches on, are not
        held up for the whole pulse. A PLC program that copies the output to an
        input would otherwise be read only before and after the pulse.
        """
        seconds = max(0, duration_ms) / 1000.0
        task = asyncio.current_task()
        if task is None or self._lock_holder is not task:
            await asyncio.sleep(seconds)
            return
        self._lock_holder = None
        self._operation_lock.release()
        try:
            await asyncio.sleep(seconds)
        finally:
            await self._take_lock_back(task)

    async def _take_lock_back(self, task: "asyncio.Task[Any]") -> None:
        """Take the operation lock again for ``task``, even through a cancel: its caller releases it."""
        taking = asyncio.ensure_future(self._operation_lock.acquire())
        cancelled = False
        while not taking.done():
            try:
                await asyncio.shield(taking)
            except asyncio.CancelledError:
                cancelled = True
        self._lock_holder = task
        if cancelled:
            raise asyncio.CancelledError()

    @abstractmethod
    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        """Write an integer/float value to a register/word/tag. Returns (success, message)."""

    async def toggle(self, address: str) -> Tuple[bool, str]:
        """Invert a readable/writable BOOL. Drivers opt in when their protocol supports it."""
        return False, f"TOGGLE is not supported by {type(self).__name__}"

    # ── Reading ───────────────────────────────────────────────────────────────

    async def read(self, address: str, data_type: str = "auto") -> Tuple[bool, Any, str]:
        """
        Read one value from the PLC. Returns (success, value, message): the
        value is a bool for a bit, an int, a float for a REAL or text for a
        string, and None when the read failed.

        Runs under the same lock as execute_operation(): one request at a time.
        A read can come while a pulse waits between its ON and OFF writes, and
        then sees what the pulse switched on.
        """
        wanted = str(data_type or "auto").strip().lower()
        if wanted not in READ_DATA_TYPES:
            return False, None, f"Unknown data type '{data_type}'; use one of: {', '.join(READ_DATA_TYPES)}"
        async with self._operation_lock:
            self._lock_holder = asyncio.current_task()
            try:
                return await self._read(str(address or "").strip(), wanted)
            except ValueError as exc:
                # A bad address or a type the address cannot hold: the link is fine.
                return False, None, str(exc)
            finally:
                self._lock_holder = None

    async def _read(self, address: str, data_type: str) -> Tuple[bool, Any, str]:
        """One read, already under the lock. Drivers whose protocol can be read implement this."""
        return False, None, f"Reading is not supported by {type(self).__name__}"

    # ── Convenience ───────────────────────────────────────────────────────────

    async def execute_operation(
        self,
        operation: str,
        address: str,
        write_value: float = 0.0,
        pulse_duration_ms: int = 150,
    ) -> Tuple[bool, str]:
        """
        Dispatch a named operation string (SET/RESET/PULSE/TOGGLE/WRITE) to the
        correct method.  Called by PLCDispatcherService.
        """
        async with self._operation_lock:
            self._lock_holder = asyncio.current_task()
            try:
                op = operation.upper()
                if op == "SET":
                    return await self.set(address)
                elif op == "RESET":
                    return await self.reset(address)
                elif op == "PULSE":
                    return await self.pulse(address, pulse_duration_ms)
                elif op == "TOGGLE":
                    return await self.toggle(address)
                elif op == "WRITE":
                    return await self.write(address, write_value)
                else:
                    return False, f"Unknown PLC operation: '{operation}'"
            finally:
                self._lock_holder = None
