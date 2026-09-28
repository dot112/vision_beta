"""Abstract base class for all PLC protocol drivers."""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from typing import Tuple


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
        self._operation_lock = asyncio.Lock()

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
        Set target to ON, wait duration_ms, then set to OFF.
        Returns (success, message) after the full pulse completes.
        """

    @abstractmethod
    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        """Write an integer/float value to a register/word/tag. Returns (success, message)."""

    async def toggle(self, address: str) -> Tuple[bool, str]:
        """Invert a readable/writable BOOL. Drivers opt in when their protocol supports it."""
        return False, f"TOGGLE is not supported by {type(self).__name__}"

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
