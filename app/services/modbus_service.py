from __future__ import annotations

from typing import Any, List, Optional, Tuple
from app.config import settings
from app.hardware.modbus.client import ModbusTCPClient

_modbus_client = ModbusTCPClient(host=settings.MODBUS_HOST, port=settings.MODBUS_PORT)


class ModbusService:
    @staticmethod
    async def get_client() -> ModbusTCPClient:
        if not _modbus_client.is_connected:
            await _modbus_client.connect()
        return _modbus_client

    @staticmethod
    async def write_coil(address: int, value: bool) -> Tuple[bool, Optional[str]]:
        client = await ModbusService.get_client()
        return await client.write_coil(address, value)

    @staticmethod
    async def read_coils(address: int, count: int = 1) -> Tuple[bool, List[bool], Optional[str]]:
        client = await ModbusService.get_client()
        return await client.read_coils(address, count)

    @staticmethod
    async def write_register(address: int, value: int) -> Tuple[bool, Optional[str]]:
        client = await ModbusService.get_client()
        return await client.write_register(address, value)

    @staticmethod
    async def read_registers(address: int, count: int = 1) -> Tuple[bool, List[int], Optional[str]]:
        client = await ModbusService.get_client()
        return await client.read_registers(address, count)
