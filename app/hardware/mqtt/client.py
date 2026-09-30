from __future__ import annotations

import asyncio
import json
import ssl
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

from app.utils.logger import get_logger

logger = get_logger(__name__)


class MQTTClient:
    """
    Production-grade MQTT Client with full TLS / mTLS support.
    Supports EMQX, Mosquitto, HiveMQ, AWS IoT Core, Azure IoT Hub.
    Accepts uploaded certificate files for mutual TLS authentication.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: str = "industrial_vision_api",
        keepalive: int = 60,
        clean_session: bool = True,
        protocol_version: str = "3.1.1",
        tls_enabled: bool = False,
        tls_insecure: bool = False,
        sni_server_name: Optional[str] = None,
        ca_cert_path: Optional[str] = None,
        client_cert_path: Optional[str] = None,
        client_key_path: Optional[str] = None,
        # Node-RED Lifecycle Messages
        birth_topic: Optional[str] = None,
        birth_payload: Optional[str] = None,
        birth_qos: int = 0,
        birth_retain: bool = False,
        close_topic: Optional[str] = None,
        close_payload: Optional[str] = None,
        close_qos: int = 0,
        close_retain: bool = False,
        will_topic: Optional[str] = None,
        will_payload: Optional[str] = None,
        will_qos: int = 0,
        will_retain: bool = False,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id
        self.keepalive = keepalive
        self.clean_session = clean_session
        self.protocol_version = protocol_version
        self.tls_enabled = tls_enabled
        self.tls_insecure = tls_insecure
        self.sni_server_name = sni_server_name
        self.ca_cert_path = ca_cert_path
        self.client_cert_path = client_cert_path
        self.client_key_path = client_key_path

        self.birth_topic = birth_topic
        self.birth_payload = birth_payload
        self.birth_qos = birth_qos
        self.birth_retain = birth_retain

        self.close_topic = close_topic
        self.close_payload = close_payload
        self.close_qos = close_qos
        self.close_retain = close_retain

        self.will_topic = will_topic
        self.will_payload = will_payload
        self.will_qos = will_qos
        self.will_retain = will_retain

        self.is_connected = False
        self.subscriptions: List[str] = []
        self._handlers: Dict[str, List[Callable]] = {}
        self._client: Optional[mqtt.Client] = None
        # A thread lock, not asyncio.Lock: connect() is awaited from the API's event
        # loop and from the telemetry dispatcher's own loop in another thread.
        self._connect_guard = threading.Lock()
        self._last_connect_attempt = 0.0
        # True while paho's network thread runs; it reconnects on its own, with backoff.
        self._loop_running = False
        # Set by reconfigure(): the next connect() builds a new paho client.
        self._rebuild = True
        # Called with True/False whenever the broker connection comes up or drops.
        self.on_state_change: Optional[Callable[[bool], None]] = None

    def _build_client(self) -> mqtt.Client:
        proto_str = str(self.protocol_version).strip().lower()
        if proto_str in ("5", "5.0", "v5", "mqttv5"):
            client = mqtt.Client(
                client_id=self.client_id or "",
                protocol=mqtt.MQTTv5,
            )
        else:
            client = mqtt.Client(
                client_id=self.client_id or "",
                protocol=mqtt.MQTTv311,
                clean_session=self.clean_session,
            )

        if self.username:
            client.username_pw_set(self.username, self.password)

        # Node-RED Last Will & Testament (LWT)
        if self.will_topic:
            client.will_set(
                topic=self.will_topic,
                payload=self.will_payload or '{"status": "offline", "unexpected": true}',
                qos=int(self.will_qos or 0),
                retain=bool(self.will_retain),
            )

        if self.tls_enabled:
            try:
                import os
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
                if self.tls_insecure:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE

                if self.ca_cert_path and os.path.exists(self.ca_cert_path):
                    context.load_verify_locations(cafile=self.ca_cert_path)
                    logger.info("Loaded CA Certificate: %s", self.ca_cert_path)
                else:
                    context.load_default_certs()

                if (
                    self.client_cert_path
                    and self.client_key_path
                    and os.path.exists(self.client_cert_path)
                    and os.path.exists(self.client_key_path)
                ):
                    context.load_cert_chain(
                        certfile=self.client_cert_path,
                        keyfile=self.client_key_path,
                    )
                    logger.info(
                        "Loaded mTLS client cert: %s | key: %s",
                        self.client_cert_path,
                        self.client_key_path,
                    )

                client.tls_set_context(context)
                if self.tls_insecure:
                    client.tls_insecure_set(True)
                logger.info("TLS/mTLS configured for broker %s:%d (insecure=%s)", self.host, self.port, self.tls_insecure)

            except Exception as exc:
                logger.error("TLS configuration failed: %s", exc)
                raise

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _on_connect(self, client, userdata, flags, rc):
        codes = {
            0: "Connected successfully",
            1: "Bad protocol version",
            2: "Client ID rejected",
            3: "Broker unavailable",
            4: "Bad credentials",
            5: "Not authorised",
        }
        if rc == 0:
            self.is_connected = True
            self._notify_state(True)
            logger.info("MQTT connected to %s:%d — %s", self.host, self.port, codes.get(rc, "Connected successfully"))
            # Send Node-RED Birth message if configured
            if self.birth_topic:
                try:
                    payload = self.birth_payload or '{"status": "online"}'
                    client.publish(
                        self.birth_topic,
                        payload,
                        qos=int(self.birth_qos or 0),
                        retain=bool(self.birth_retain),
                    )
                    logger.info("MQTT Birth message published to [%s]", self.birth_topic)
                except Exception as b_err:
                    logger.debug("MQTT Birth message error: %s", b_err)

            for topic in self.subscriptions:
                client.subscribe(topic)
        else:
            self.is_connected = False
            logger.error("MQTT connection refused: %s", codes.get(rc, f"Unknown rc={rc}"))

    def _on_disconnect(self, client, userdata, rc):
        self.is_connected = False
        self._notify_state(False)
        if rc != 0:
            logger.warning("MQTT unexpectedly disconnected (rc=%d); reconnecting in the background", rc)
        else:
            logger.info("MQTT disconnected cleanly from %s:%d", self.host, self.port)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = msg.payload.decode("utf-8")
        logger.info("MQTT Received [%s]: %s", topic, payload)
        for pattern, handlers in self._handlers.items():
            if mqtt.topic_matches_sub(pattern, topic):
                for cb in handlers:
                    try:
                        cb(topic, payload)
                    except Exception as exc:
                        logger.error("MQTT handler error: %s", exc)

    def _notify_state(self, connected: bool) -> None:
        if self.on_state_change is not None:
            try:
                self.on_state_change(connected)
            except Exception:
                logger.exception("MQTT state listener failed")

    def _open_blocking(self) -> None:
        """Replace the paho client and open its socket. Blocks on DNS, TCP and TLS.

        paho's network thread is started even when this first attempt fails:
        it keeps retrying in the background, 1 s after the first failure and
        doubling up to 60 s, so a broker that is down at startup is picked up
        when it comes back, and a dropped connection is restored the same way.
        """
        if self._client:
            try:
                self._client.disconnect()
                self._client.loop_stop()
            except Exception:
                pass
            self._loop_running = False

        self._client = self._build_client()
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._rebuild = False
        try:
            self._client.connect(self.host, self.port, keepalive=self.keepalive)
        finally:
            self._client.loop_start()
            self._loop_running = True

    async def connect(self) -> bool:
        if self.is_connected and self._client:
            return True

        if not self._connect_guard.acquire(blocking=False):
            # Another caller is connecting; wait for its outcome without blocking the loop.
            for _ in range(100):
                await asyncio.sleep(0.1)
                if not self._connect_guard.locked():
                    break
            return bool(self.is_connected and self._client)

        try:
            if self.is_connected and self._client:
                return True
            if self._client is not None and self._loop_running and not self._rebuild:
                # paho is already reconnecting in the background; a new client
                # would only restart its backoff.
                return False

            now = time.time()
            if now - self._last_connect_attempt < 3.0:
                return False
            self._last_connect_attempt = now

            try:
                # paho's connect() is synchronous (up to its 5 s timeout, longer with DNS
                # or TLS); in a worker thread it no longer stalls every API request and
                # video stream while a broker is unreachable.
                await asyncio.to_thread(self._open_blocking)

                for _ in range(25):
                    await asyncio.sleep(0.1)
                    if self.is_connected:
                        return True

                logger.warning("MQTT connection timed out to %s:%d", self.host, self.port)
                return False

            except Exception as exc:
                logger.error("MQTT connect() exception: %s", exc)
                self.is_connected = False
                return False
        finally:
            self._connect_guard.release()

    async def disconnect(self) -> None:
        if self._client:
            if self.is_connected and self.close_topic:
                try:
                    payload = self.close_payload or '{"status": "offline"}'
                    self._client.publish(
                        self.close_topic,
                        payload,
                        qos=int(self.close_qos or 0),
                        retain=bool(self.close_retain),
                    )
                    logger.info("MQTT Close message published to [%s]", self.close_topic)
                except Exception as c_err:
                    logger.debug("MQTT Close message error: %s", c_err)

            # disconnect() first so the close message and DISCONNECT still go
            # out; loop_stop() joins paho's network thread, off the event loop.
            client = self._client
            try:
                client.disconnect()
            except Exception as exc:
                logger.debug("MQTT disconnect error: %s", exc)
            await asyncio.to_thread(client.loop_stop)
            self._loop_running = False
            self._rebuild = True
        self.is_connected = False
        self._notify_state(False)

    async def publish(self, topic: str, payload: Dict[str, Any], qos: int = 0, retain: bool = False) -> bool:
        if not self._client or not self.is_connected:
            ok = await self.connect()
            if not ok:
                return False
        try:
            msg = json.dumps(payload)
            info = self._client.publish(topic, msg, qos=qos, retain=retain)
            # paho-mqtt handles delivery in background thread via loop_start()
            logger.debug("MQTT Published -> [%s] QoS=%d", topic, qos)
            return True
        except Exception as exc:
            logger.error("MQTT publish error: %s", exc)
            return False

    async def subscribe(self, topic: str, callback: Optional[Callable] = None, qos: int = 0) -> bool:
        if topic not in self.subscriptions:
            self.subscriptions.append(topic)
        if callback:
            self._handlers.setdefault(topic, []).append(callback)
        if self._client and self.is_connected:
            self._client.subscribe(topic, qos=qos)
        logger.info("MQTT Subscribed: [%s]", topic)
        return True

    def reconfigure(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k) and v is not None:
                setattr(self, k, v)
        self.is_connected = False
        self._rebuild = True
        self._last_connect_attempt = 0.0
        logger.info("MQTT reconfigured -> %s:%d | TLS=%s | CA=%s | CERT=%s",
                    self.host, self.port, self.tls_enabled,
                    self.ca_cert_path, self.client_cert_path)
