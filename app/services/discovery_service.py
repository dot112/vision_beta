from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

logger = logging.getLogger(__name__)

DISCOVERY_PORT = 8888
MAGIC_REQUEST = "DISCOVER_VISION_SERVER"


def get_local_ip() -> str:
    """Determine the local LAN IP of this host."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't have to be reachable; used to determine outbound routing interface
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, http_port: int = 8000):
        self.http_port = http_port
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport):  # type: ignore[override]
        self.transport = transport
        logger.info(f"UDP Auto-Discovery Beacon listening on 0.0.0.0:{DISCOVERY_PORT}")

    def datagram_received(self, data: bytes, addr: tuple[str, int]):
        try:
            message = data.decode("utf-8", errors="ignore").strip()
            if MAGIC_REQUEST in message:
                local_ip = get_local_ip()
                response = f"VISION_SERVER_BEACON|http://{local_ip}:{self.http_port}|Industrial Vision Server"
                if self.transport:
                    self.transport.sendto(response.encode("utf-8"), addr)
                    logger.debug(f"Replied to discovery probe from {addr} with IP {local_ip}")
        except Exception as exc:
            logger.debug(f"Error handling UDP datagram from {addr}: {exc}")


class ServerDiscoveryService:
    _transport: Optional[asyncio.DatagramTransport] = None

    @classmethod
    async def start(cls, http_port: int = 8000) -> None:
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: DiscoveryProtocol(http_port=http_port),
                local_addr=("0.0.0.0", DISCOVERY_PORT),
                allow_broadcast=True,
            )
            cls._transport = transport
            logger.info(f"Server Discovery Beacon started on UDP port {DISCOVERY_PORT}")
        except Exception as exc:
            logger.warning(f"Could not start UDP discovery beacon on port {DISCOVERY_PORT}: {exc}")

    @classmethod
    def stop(cls) -> None:
        if cls._transport:
            cls._transport.close()
            cls._transport = None
            logger.info("Server Discovery Beacon stopped")
