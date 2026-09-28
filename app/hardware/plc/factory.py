"""PLC Driver Factory — resolves endpoint sub-protocol → correct PLCDriver instance."""
from __future__ import annotations

from typing import Dict, Optional

from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Module-level driver pool — one driver per endpoint ID, reused across dispatches
_driver_pool: Dict[str, PLCDriver] = {}


def _make_driver(endpoint: dict) -> PLCDriver:
    """Instantiate the correct driver for the given endpoint."""
    sub = str(endpoint.get("plc_sub_protocol", "modbus_tcp")).lower()

    if sub in ("modbus_tcp", "modbus"):
        from app.hardware.plc.modbus_driver import ModbusTCPDriver
        return ModbusTCPDriver(endpoint)

    elif sub in ("s7", "siemens_s7", "siemens"):
        from app.hardware.plc.s7_driver import S7Driver
        return S7Driver(endpoint)

    elif sub in ("ethernet_ip", "ethernetip", "eip", "ab"):
        from app.hardware.plc.ethernet_ip_driver import EtherNetIPDriver
        return EtherNetIPDriver(endpoint)

    elif sub in ("opc_ua", "opcua"):
        from app.hardware.plc.opcua_driver import OPCUADriver
        return OPCUADriver(endpoint)

    elif sub in ("generic_tcp", "generic", "tcp"):
        from app.hardware.plc.generic_tcp_driver import GenericTCPDriver
        return GenericTCPDriver(endpoint)

    elif sub in ("melsec", "fins"):
        raise ValueError(f"PLC protocol '{sub}' has no native driver; refusing unsafe generic TCP fallback")

    else:
        raise ValueError(f"Unsupported PLC sub-protocol '{sub}' for endpoint '{endpoint.get('id')}'")


class PLCDriverFactory:
    """
    Connection-pooled factory for PLC drivers.

    Drivers are keyed by endpoint ID and reused across dispatches to avoid
    reconnection overhead.  Call `invalidate()` when an endpoint is deleted or
    its config changes.
    """

    @classmethod
    def get_driver(cls, endpoint: dict, fresh: bool = False) -> PLCDriver:
        """
        Return the cached driver for this endpoint, or create a new one.

        Args:
            endpoint: Endpoint dict from SettingsPersistenceService.
            fresh:    If True, always create a new (disconnected) driver instance.
                      Use this for connection tests where you want a clean socket.
        """
        ep_id = endpoint.get("id", "")
        if fresh or ep_id not in _driver_pool:
            driver = _make_driver(endpoint)
            if not fresh:
                _driver_pool[ep_id] = driver
            return driver
        return _driver_pool[ep_id]

    @classmethod
    def invalidate(cls, endpoint_id: str) -> None:
        """Remove and disconnect the cached driver for an endpoint."""
        driver = _driver_pool.pop(endpoint_id, None)
        if driver:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(driver.disconnect())
            except RuntimeError:
                pass  # no running loop — driver will be GC'd

    @classmethod
    def clear_all(cls) -> None:
        """Disconnect and remove all cached drivers."""
        for ep_id in list(_driver_pool.keys()):
            cls.invalidate(ep_id)

    @classmethod
    async def close_all(cls) -> None:
        drivers = list(_driver_pool.values())
        _driver_pool.clear()
        for driver in drivers:
            try:
                await driver.disconnect()
            except Exception as exc:
                logger.warning("Error closing PLC driver %s: %s", driver.endpoint_id, exc)

    @classmethod
    def pool_ids(cls) -> list:
        """Return list of cached endpoint IDs (for diagnostics)."""
        return list(_driver_pool.keys())
