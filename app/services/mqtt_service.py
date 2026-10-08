from __future__ import annotations

import asyncio
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.hardware.mqtt.client import MQTTClient
from app.state.application_state import app_state
from app.utils.logger import get_logger

logger = get_logger(__name__)

CERTS_DIR = str(Path(__file__).resolve().parents[2] / "certs" / "mqtt")

_mqtt_client = MQTTClient(
    host=settings.MQTT_BROKER_HOST,
    port=settings.MQTT_BROKER_PORT,
    username=settings.MQTT_USERNAME if settings.MQTT_USERNAME else None,
    password=settings.MQTT_PASSWORD if settings.MQTT_PASSWORD else None,
    tls_enabled=settings.MQTT_TLS_ENABLED,
    ca_cert_path=settings.MQTT_CA_CERT_PATH if os.path.exists(settings.MQTT_CA_CERT_PATH) else None,
)


def _track_connection(connected: bool) -> None:
    # paho reconnects in the background, so keep the health flag current too.
    app_state.mqtt_connected = bool(_mqtt_client.is_connected or MQTTChannels.any_connected())


_mqtt_client.on_state_change = _track_connection


def _resolve_cert(filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,127}", filename):
        raise ValueError("Certificate references must be plain filenames")
    cert_root = Path(CERTS_DIR).resolve()
    cert_path = (cert_root / filename).resolve()
    if not cert_path.is_relative_to(cert_root):
        raise ValueError("Certificate path must stay inside the MQTT certificate directory")
    if not cert_path.is_file():
        raise FileNotFoundError(f"Certificate file not found: {filename}. Upload it first via POST /api/v1/mqtt/certs/upload")
    return str(cert_path)


# The settings of an MQTT channel that its broker connection is made from. A
# change to any of them makes a new connection; a new name or description does not.
_CONNECTION_KEYS = (
    "host", "port", "username", "password", "client_id", "keepalive", "clean_session", "protocol_version",
    "tls_enabled", "sni_server_name", "ca_cert_filename", "client_cert_filename", "client_key_filename",
    "birth_topic", "birth_payload", "birth_qos", "birth_retain",
    "close_topic", "close_payload", "close_qos", "close_retain",
    "will_topic", "will_payload", "will_qos", "will_retain",
)
_LIFECYCLE_KEYS = tuple(key for key in _CONNECTION_KEYS if key.startswith(("birth_", "close_", "will_")))


def client_for_channel(endpoint: Dict[str, Any], client_id: Optional[str] = None) -> MQTTClient:
    """A client that connects the way one saved MQTT channel says."""
    return MQTTClient(
        host=str(endpoint.get("host") or ""),
        port=int(endpoint.get("port") or 1883),
        username=endpoint.get("username") or None,
        password=endpoint.get("password") or None,
        client_id=client_id or endpoint.get("client_id") or f"vision_comm_{str(endpoint.get('id') or '')[:6]}",
        keepalive=int(endpoint.get("keepalive") or 60),
        clean_session=bool(endpoint.get("clean_session", True)),
        protocol_version=str(endpoint.get("protocol_version") or "3.1.1"),
        tls_enabled=bool(endpoint.get("tls_enabled")),
        tls_insecure=False,
        sni_server_name=endpoint.get("sni_server_name") or None,
        ca_cert_path=_resolve_cert(endpoint.get("ca_cert_filename")),
        client_cert_path=_resolve_cert(endpoint.get("client_cert_filename")),
        client_key_path=_resolve_cert(endpoint.get("client_key_filename")),
        **{key: endpoint.get(key) for key in _LIFECYCLE_KEYS},
    )


class MQTTChannels:
    """One broker connection for each MQTT channel under Connections.

    A send card publishes through the connection of the channel it names, so
    two channels can be two brokers. Each connection reconnects on its own.
    Used from the API's event loop and from the telemetry dispatcher's thread.
    """

    _clients: Dict[str, Tuple[Tuple[Any, ...], MQTTClient]] = {}
    _lock = threading.Lock()
    _connecting: set = set()

    @staticmethod
    def _signature(endpoint: Dict[str, Any]) -> Tuple[Any, ...]:
        return tuple(endpoint.get(key) for key in _CONNECTION_KEYS)

    @classmethod
    def client(cls, endpoint: Dict[str, Any]) -> MQTTClient:
        """The channel's connection, made new when the channel's settings changed."""
        channel_id = str(endpoint.get("id") or "")
        signature = cls._signature(endpoint)
        with cls._lock:
            held = cls._clients.get(channel_id)
            if held is not None and held[0] == signature:
                return held[1]
            client = client_for_channel(endpoint)
            client.on_state_change = _track_connection
            cls._clients[channel_id] = (signature, client)
        if held is not None:
            cls._close_later(held[1])
        return client

    @staticmethod
    def _close_later(client: MQTTClient) -> None:
        # close() joins paho's network thread, so it gets a thread of its own.
        threading.Thread(target=client.close, name="MQTTChannelClose", daemon=True).start()

    @classmethod
    async def publish(cls, endpoint: Dict[str, Any], topic: str, payload: Any) -> Tuple[bool, str]:
        """Publish to a topic on one channel's broker, with the channel's QoS and retain.

        Returns (sent, what happened).
        """
        if not endpoint.get("host"):
            return False, "The MQTT channel has no broker address"
        try:
            client = cls.client(endpoint)
        except (ValueError, FileNotFoundError) as exc:
            return False, str(exc)
        ok = await client.publish(topic, payload, qos=int(endpoint.get("qos") or 0), retain=bool(endpoint.get("retain")))
        if ok:
            return True, f"Published to {topic}"
        why = f" ({client.last_error})" if client.last_error else ""
        return False, f"Not connected to the MQTT broker {client.host}:{client.port}{why}"

    @classmethod
    def apply(cls, endpoint: Dict[str, Any]) -> None:
        """A channel was saved: connect it in the background, or drop its connection when it is switched off."""
        channel_id = str(endpoint.get("id") or "")
        if str(endpoint.get("protocol", "")).lower() != "mqtt" or endpoint.get("enabled", True) is not True or not endpoint.get("host"):
            cls.drop(channel_id)
            return
        try:
            client = cls.client(endpoint)
        except (ValueError, FileNotFoundError) as exc:
            logger.warning("MQTT channel '%s' cannot connect: %s", endpoint.get("name") or channel_id, exc)
            return
        if client.is_connected:
            return
        try:
            task = asyncio.get_running_loop().create_task(client.connect())
        except RuntimeError:
            return  # no event loop here: the first message connects it
        cls._connecting.add(task)
        task.add_done_callback(cls._connecting.discard)

    @classmethod
    def drop(cls, channel_id: str) -> None:
        """A channel was deleted or switched off: close its connection."""
        with cls._lock:
            held = cls._clients.pop(str(channel_id), None)
        if held is not None:
            cls._close_later(held[1])

    @classmethod
    def sync(cls, endpoints: List[Dict[str, Any]]) -> None:
        """Connect every MQTT channel that is switched on and drop the connections of the rest."""
        live = {str(ep.get("id")) for ep in endpoints if isinstance(ep, dict)}
        with cls._lock:
            gone = [channel_id for channel_id in cls._clients if channel_id not in live]
        for channel_id in gone:
            cls.drop(channel_id)
        for endpoint in endpoints:
            if isinstance(endpoint, dict) and str(endpoint.get("protocol", "")).lower() == "mqtt":
                cls.apply(endpoint)

    @classmethod
    def connected_client(cls, endpoint: Dict[str, Any]) -> Optional[MQTTClient]:
        """The channel's connection when it is up and was made from these settings."""
        with cls._lock:
            held = cls._clients.get(str(endpoint.get("id") or ""))
        if held is not None and held[0] == cls._signature(endpoint) and held[1].is_connected:
            return held[1]
        return None

    @classmethod
    def status(cls) -> Dict[str, Dict[str, Any]]:
        """{channel id: {"connected", "host", "port", "last_error"}} for every channel that has a connection."""
        with cls._lock:
            clients = {channel_id: held[1] for channel_id, held in cls._clients.items()}
        return {channel_id: {"connected": bool(c.is_connected), "host": c.host, "port": c.port, "last_error": c.last_error}
                for channel_id, c in clients.items()}

    @classmethod
    def any_connected(cls) -> bool:
        with cls._lock:
            return any(held[1].is_connected for held in cls._clients.values())

    @classmethod
    async def close_all(cls) -> None:
        with cls._lock:
            clients = [held[1] for held in cls._clients.values()]
            cls._clients = {}
        for client in clients:
            await client.disconnect()


def default_channel() -> Optional[Dict[str, Any]]:
    """The first MQTT channel that is switched on: where a message with no channel of its own goes."""
    from app.services.settings_persistence_service import SettingsPersistenceService
    return next((ep for ep in SettingsPersistenceService.get_endpoints("mqtt")
                 if ep.get("enabled", True) is True and ep.get("host")), None)


class MQTTService:

    @staticmethod
    def get_client() -> MQTTClient:
        return _mqtt_client

    @staticmethod
    async def connect_default() -> bool:
        success = await _mqtt_client.connect()
        app_state.mqtt_connected = success
        return success

    @staticmethod
    async def connect(
        host: str,
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: str = "industrial_vision_api",
        tls_enabled: bool = False,
        ca_cert_filename: Optional[str] = None,
        client_cert_filename: Optional[str] = None,
        client_key_filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        ca_path = _resolve_cert(ca_cert_filename)
        cert_path = _resolve_cert(client_cert_filename)
        key_path = _resolve_cert(client_key_filename)

        _mqtt_client.reconfigure(
            host=host,
            port=port,
            username=username,
            password=password,
            client_id=client_id,
            tls_enabled=tls_enabled,
            ca_cert_path=ca_path,
            client_cert_path=cert_path,
            client_key_path=key_path,
        )

        success = await _mqtt_client.connect()
        _track_connection(success)

        return {
            "success": success,
            "broker_host": _mqtt_client.host,
            "broker_port": _mqtt_client.port,
            "is_connected": _mqtt_client.is_connected,
            "client_id": _mqtt_client.client_id,
            "tls_enabled": _mqtt_client.tls_enabled,
            "ca_cert": ca_cert_filename,
            "client_cert": client_cert_filename,
        }

    @staticmethod
    async def disconnect() -> Dict[str, Any]:
        await _mqtt_client.disconnect()
        _track_connection(False)
        return {"success": True, "is_connected": False}

    @staticmethod
    async def publish(topic: str, payload: Dict[str, Any], qos: int = 0, retain: bool = False) -> bool:
        # Preserve exact topic string as requested (e.g. home\control or home/control)
        # The broker set with POST /mqtt/connect (or MQTT_BROKER_HOST) when there
        # is one, else the first MQTT channel under Connections.
        if not _mqtt_client.host:
            channel = default_channel()
            if channel is None:
                return False
            try:
                return await MQTTChannels.client(channel).publish(topic, payload, qos=qos, retain=retain)
            except (ValueError, FileNotFoundError) as exc:
                logger.warning("MQTT channel '%s' cannot connect: %s", channel.get("name"), exc)
                return False
        return await _mqtt_client.publish(topic, payload, qos=qos, retain=retain)

    @staticmethod
    async def subscribe(topic: str) -> bool:
        return await _mqtt_client.subscribe(topic)

    @staticmethod
    def get_status() -> Dict[str, Any]:
        return {
            "broker_host": _mqtt_client.host,
            "broker_port": _mqtt_client.port,
            "is_connected": _mqtt_client.is_connected,
            "client_id": _mqtt_client.client_id,
            "tls_enabled": _mqtt_client.tls_enabled,
            "ca_cert": os.path.basename(_mqtt_client.ca_cert_path) if _mqtt_client.ca_cert_path else None,
            "client_cert": os.path.basename(_mqtt_client.client_cert_path) if _mqtt_client.client_cert_path else None,
            "subscriptions": _mqtt_client.subscriptions,
        }

    @staticmethod
    def list_certs() -> Dict[str, Any]:
        if not os.path.exists(CERTS_DIR):
            return {"ca_certs": [], "client_certs": [], "private_keys": [], "all_files": []}

        all_raw = [
            f for f in os.listdir(CERTS_DIR)
            if os.path.isfile(os.path.join(CERTS_DIR, f))
        ]
        valid_files = [
            f for f in all_raw
            if f.lower().endswith((".crt", ".pem", ".key", ".cer", ".ca", ".der"))
        ]
        valid_files.sort(key=str.lower)

        ca_certs = [f for f in valid_files if "ca" in f.lower() or f.lower().endswith((".pem", ".crt", ".cer", ".ca"))]
        client_certs = [f for f in valid_files if not f.lower().endswith(".key") and "ca" not in f.lower()]
        private_keys = [f for f in valid_files if f.lower().endswith(".key") or "key" in f.lower()]

        return {
            "ca_certs": ca_certs,
            "client_certs": client_certs,
            "private_keys": private_keys,
            "all_files": valid_files,
        }

    @staticmethod
    def delete_cert(filename: str) -> bool:
        safe_name = os.path.basename(filename or "")
        if not safe_name or safe_name != filename or safe_name in {".", ".."}:
            return False
        root = os.path.realpath(CERTS_DIR)
        path = os.path.realpath(os.path.join(root, safe_name))
        if os.path.commonpath([root, path]) != root:
            return False
        if not os.path.exists(path):
            return False
        os.remove(path)
        logger.info("Deleted certificate file: %s", filename)
        return True
