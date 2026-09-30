from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

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
    app_state.mqtt_connected = connected


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
        app_state.mqtt_connected = success

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
        app_state.mqtt_connected = False
        return {"success": True, "is_connected": False}

    @staticmethod
    async def publish(topic: str, payload: Dict[str, Any], qos: int = 0, retain: bool = False) -> bool:
        # Preserve exact topic string as requested (e.g. home\control or home/control)
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
