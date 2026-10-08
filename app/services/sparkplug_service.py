"""Sparkplug B: the production lines published as an edge node's devices.

An MQTT channel with Sparkplug switched on makes this server an edge node on
that channel's broker, and every production line one of the node's devices. A
host such as Ignition then sees each line's tags by itself, knows when the
server is offline, and can ask for everything again (a rebirth).

SparkplugNode is one channel's edge node: its own broker connection, the birth
and death messages, the sequence numbers, the changed values, the host's
online state, and the commands it hands on. It sends what it is given
(publish_lines) and knows nothing about lines.

SparkplugService keeps a node for every channel that has Sparkplug on, and
gives each node every line's metrics once per publish interval.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.hardware.mqtt.sparkplug_codec import NAMESPACE, DataType, Metric, decode_payload, encode_payload, now_ms, parse_topic, topic
from app.services.sparkplug_metrics import BD_SEQ, REBIRTH, RESET, RUNNING, device_ids, last_from_event, line_metrics, node_metrics
from app.utils.logger import get_logger

logger = get_logger(__name__)

SEQUENCE_SIZE = 256
# Seconds between the node's pings, whatever the channel says. A broker takes a
# client that lost power or its network for gone after one and a half times
# this, and only then tells the host (the NDEATH will).
KEEPALIVE_SECONDS = 15
# A command is one or two small tags. More than this in one message is not a
# command, and it must not keep the server busy.
MAX_COMMAND_METRICS = 16
MAX_COMMAND_BYTES = 64 * 1024


class BdSeqStore:
    """The birth/death sequence number of each channel, kept in a file.

    It goes up by one with every broker connection, 0 to 255. Kept across
    restarts so that a death message of an earlier connection, arriving late,
    cannot be taken for the death of the current one.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def next(self, channel_id: str) -> int:
        with self._lock:
            data = self._read()
            last = data.get(channel_id)
            valid = isinstance(last, int) and not isinstance(last, bool) and 0 <= last < SEQUENCE_SIZE
            value = (last + 1) % SEQUENCE_SIZE if valid else 0
            data[channel_id] = value
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(json.dumps(data), encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not save the Sparkplug birth/death sequence number: %s", exc)
            return value


class SparkplugNode:
    """One MQTT channel's Sparkplug B edge node."""

    def __init__(
        self,
        channel: Dict[str, Any],
        store: BdSeqStore,
        on_command: Optional[Callable[["SparkplugNode", str, Metric], None]] = None,
    ):
        from app.services.mqtt_service import client_for_channel

        self.channel_id = str(channel.get("id") or "")
        self.name = str(channel.get("name") or self.channel_id)
        self.group = str(channel.get("sparkplug_group_id") or "")
        self.node = str(channel.get("sparkplug_node_id") or "")
        self.host_id = str(channel.get("sparkplug_host_id") or "")
        self.interval = int(channel.get("sparkplug_interval_ms") or 1000) / 1000.0
        self.allow_commands = channel.get("sparkplug_allow_commands") is True
        self._store = store
        self._on_command = on_command

        # Messages arrive on paho's thread; publish_lines runs on the event loop.
        self._lock = threading.RLock()
        self._seq = 0
        self._bd_seq: Optional[int] = None
        self._bd_seq_used = False          # a connection was made with the current number
        self._born = False                 # the births were sent on this connection
        self._sent: Dict[str, Dict[str, Tuple[int, Any]]] = {}   # {device: {metric: (datatype, value)}} as last sent
        self._lines: Dict[str, List[Metric]] = {}                # the lines of the last publish_lines call
        self._host_online: Optional[bool] = False if self.host_id else None
        self._host_timestamp = 0
        self._host_speaks_v3 = False       # a Sparkplug 3.0 state message was seen: the old format is then ignored
        self._last_birth_at: Optional[float] = None
        self._stopped = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: set = set()

        # The channel's broker, login and TLS; nothing of its own QoS, retain and
        # lifecycle messages, which the Sparkplug specification fixes.
        connection = {k: v for k, v in channel.items() if not k.startswith(("birth_", "close_", "will_"))}
        connection["clean_session"] = True
        connection["keepalive"] = min(int(channel.get("keepalive") or 60), KEEPALIVE_SECONDS)
        base_id = channel.get("client_id") or f"vision_comm_{self.channel_id[:6]}"
        self._client = client_for_channel(connection, client_id=f"{base_id}-spb")
        self._client.before_connect = self._before_connect
        self._client.on_state_change = self._on_connection

    # ── Start and stop ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect in the background. Returns at once and never raises: a broker
        that is down is retried until it answers."""
        self._loop = asyncio.get_running_loop()
        # Kept by the client and sent after every connect, before the births.
        # A host never retains a command. One that a broker kept all the same
        # would be carried out again after every reconnect.
        await self._client.subscribe_raw(topic(self.group, "NCMD", self.node), self._on_node_command, qos=1, skip_retained=True)
        await self._client.subscribe_raw(f"{NAMESPACE}/{self.group}/DCMD/{self.node}/#", self._on_device_command, qos=1,
                                         skip_retained=True)
        if self.host_id:
            await self._client.subscribe_raw(f"{NAMESPACE}/STATE/{self.host_id}", self._on_host_state, qos=1)
            await self._client.subscribe_raw(f"STATE/{self.host_id}", self._on_host_state, qos=1)
        self._spawn(self._client.connect())

    async def stop(self) -> None:
        """Say goodbye (NDEATH) and disconnect."""
        self._stopped = True
        for task in list(self._tasks):
            task.cancel()
        # The client's close message is the NDEATH: a clean disconnect does not send the will.
        await self._client.disconnect()
        with self._lock:
            self._born = False
            self._sent = {}

    def _spawn(self, coroutine) -> None:
        if self._loop is None or self._stopped:
            coroutine.close()
            return
        task = self._loop.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── Connection ────────────────────────────────────────────────────────────

    def _before_connect(self, client) -> None:
        """Before every CONNECT: the death message of this connection, as will and as goodbye."""
        with self._lock:
            if self._bd_seq is None or self._bd_seq_used:
                self._bd_seq = self._store.next(self.channel_id)
                self._bd_seq_used = False
            death = encode_payload([Metric(BD_SEQ, DataType.Int64, self._bd_seq)], timestamp=now_ms())
        death_topic = topic(self.group, "NDEATH", self.node)
        client.will_topic, client.will_payload, client.will_qos, client.will_retain = death_topic, death, 1, False
        client.close_topic, client.close_payload, client.close_qos, client.close_retain = death_topic, death, 1, False

    def _on_connection(self, connected: bool) -> None:
        with self._lock:
            self._born = False
            self._sent = {}
            if not connected or self._stopped:
                return
            self._bd_seq_used = True
            if self.host_id:
                # Wait for the host to say it is online (a broker sends its last
                # state message again after the subscription).
                self._host_online = False
                return
            self._send_births()

    def run_soon(self, coroutine) -> None:
        """Run a coroutine on the event loop the node was started on. Callable from any thread."""
        loop = self._loop
        if loop is None or self._stopped:
            coroutine.close()
            return
        loop.call_soon_threadsafe(lambda: self._spawn(coroutine))

    def _restart(self) -> None:
        """Disconnect (with the NDEATH) and connect again, off paho's thread."""
        async def again() -> None:
            await self._client.disconnect()
            self._client.reconfigure()   # lets connect() start at once
            if not self._stopped:
                await self._client.connect()

        self.run_soon(again())

    # ── Publishing ────────────────────────────────────────────────────────────

    def _publish(self, kind: str, device: Optional[str], metrics: Sequence[Metric]) -> None:
        """One message with the next sequence number. Call with the lock held."""
        stamp = now_ms()
        seq, self._seq = self._seq, (self._seq + 1) % SEQUENCE_SIZE
        payload = encode_payload([Metric(m.name, m.datatype, m.value, stamp) for m in metrics], timestamp=stamp, seq=seq)
        self._client.publish_nowait(topic(self.group, kind, self.node, device), payload, qos=0, retain=False)

    def _send_births(self) -> None:
        """NBIRTH, then a DBIRTH for every line. Call with the lock held."""
        self._seq = 0
        self._publish("NBIRTH", None, node_metrics(self._bd_seq or 0, settings.APP_VERSION))
        self._born = True
        self._sent = {}
        self._last_birth_at = time.time()
        for device, metrics in self._lines.items():
            self._birth(device, metrics)
        logger.info("Sparkplug node %s/%s on '%s': births sent for %d line(s)", self.group, self.node, self.name, len(self._lines))

    def _birth(self, device: str, metrics: Sequence[Metric]) -> None:
        self._publish("DBIRTH", device, metrics)
        self._sent[device] = {m.name: (m.datatype, m.value) for m in metrics}

    def publish_lines(self, lines: Dict[str, List[Metric]]) -> None:
        """Send what changed since the last call. ``lines`` is {device id: every metric of that line}.

        A new line gets a DBIRTH, a line that is gone a DDEATH, a line whose
        list of metrics changed a new DBIRTH, and any other line one DDATA with
        the metrics whose value changed. Nothing is sent before the births.
        """
        with self._lock:
            self._lines = {device: list(metrics) for device, metrics in lines.items()}
            if not self._born:
                return
            for device in [d for d in self._sent if d not in self._lines]:
                self._publish("DDEATH", device, [])
                del self._sent[device]
            for device, metrics in self._lines.items():
                sent = self._sent.get(device)
                if sent is None or set(sent) != {m.name for m in metrics}:
                    self._birth(device, metrics)
                    continue
                changed = [m for m in metrics if sent[m.name] != (m.datatype, m.value)]
                if changed:
                    self._publish("DDATA", device, changed)
                    sent.update({m.name: (m.datatype, m.value) for m in changed})

    def resend(self, device: str, names: Sequence[str]) -> None:
        """Send these metrics of a line again with their last sent values (after a command)."""
        with self._lock:
            sent = self._sent.get(device) if self._born else None
            metrics = [Metric(name, *sent[name]) for name in names if name in sent] if sent else []
            if metrics:
                self._publish("DDATA", device, metrics)

    def rebirth(self) -> None:
        with self._lock:
            if self._stopped or not self._client.is_connected or self._host_online is False:
                return
            self._send_births()

    # ── Messages from the host ────────────────────────────────────────────────

    def _on_node_command(self, _topic: str, payload: bytes) -> None:
        if len(payload) > MAX_COMMAND_BYTES:
            logger.warning("Sparkplug node %s/%s: a node command of %d bytes was ignored", self.group, self.node, len(payload))
            return
        try:
            wanted = any(m.name == REBIRTH and m.datatype == DataType.Boolean and m.value is True
                         for m in decode_payload(payload).metrics)
        except ValueError as exc:
            logger.debug("Sparkplug node %s/%s: a node command could not be read: %s", self.group, self.node, exc)
            return
        if wanted:
            logger.info("Sparkplug node %s/%s on '%s': rebirth requested by the host", self.group, self.node, self.name)
            self.rebirth()

    def _on_device_command(self, text: str, payload: bytes) -> None:
        parts = parse_topic(text)
        if parts is None or parts.device is None or self._on_command is None or self._stopped:
            return
        if len(payload) > MAX_COMMAND_BYTES:
            logger.warning("Sparkplug node %s/%s: a command of %d bytes for '%s' was ignored", self.group, self.node, len(payload), parts.device)
            return
        try:
            metrics = decode_payload(payload).metrics
        except ValueError as exc:
            logger.debug("Sparkplug node %s/%s: a device command could not be read: %s", self.group, self.node, exc)
            return
        # One write per tag (the last one), and no more tags than a command has:
        # each is answered with a pass over every line.
        latest = {metric.name: metric for metric in metrics}
        if len(latest) > MAX_COMMAND_METRICS:
            logger.warning("Sparkplug node %s/%s: a command for '%s' wrote %d tags; only the first %d were looked at",
                           self.group, self.node, parts.device, len(latest), MAX_COMMAND_METRICS)
        for metric in list(latest.values())[:MAX_COMMAND_METRICS]:
            try:
                self._on_command(self, parts.device, Metric(metric.name, metric.datatype, metric.value))
            except Exception:
                logger.exception("Sparkplug command handler failed")

    def _on_host_state(self, text: str, payload: bytes) -> None:
        if text.startswith(f"{NAMESPACE}/"):
            # Sparkplug 3.0: {"online": true, "timestamp": 1668114759262}
            try:
                state = json.loads(payload)
                online, stamp = state["online"] is True, int(state.get("timestamp") or 0)
            except (ValueError, TypeError, KeyError, AttributeError):
                return
            if stamp < self._host_timestamp:
                return  # a late copy of an earlier state
            self._host_timestamp = stamp
            self._host_speaks_v3 = True
        else:
            # Sparkplug 2.2: the text ONLINE or OFFLINE. A host speaks one format:
            # once it was heard in the 3.0 format, a message in the old one is a
            # leftover on the broker, and following it would end the node again
            # after every birth.
            if self._host_speaks_v3:
                return
            word = payload.decode("utf-8", errors="replace").strip().upper()
            if word not in ("ONLINE", "OFFLINE"):
                return
            online = word == "ONLINE"
        with self._lock:
            was_born = self._born
            self._host_online = online
            if self._stopped:
                return
            if online and not self._born and self._client.is_connected:
                self._send_births()
                return
            if not online and was_born:
                # Nothing more goes out on this connection but its NDEATH, and a
                # second "offline" (the host's own and its will) starts no second reconnect.
                self._born = False
                self._sent = {}
        if not online and was_born:
            logger.info("Sparkplug node %s/%s on '%s': host '%s' is offline, waiting for it", self.group, self.node, self.name, self.host_id)
            self._restart()

    # ── State ─────────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        connected = bool(self._client.is_connected)
        with self._lock:
            return {
                "connected": connected,
                "host_online": self._host_online,
                "devices": len(self._sent),
                "last_birth_at": self._last_birth_at,
                "last_error": "" if connected else self._client.last_error,
            }


# What a node's connection and identity are made from. A change to any of these
# needs a new node; the commands switch and the interval are set on the running one.
_NODE_KEYS = (
    "host", "port", "username", "password", "client_id", "keepalive", "protocol_version",
    "tls_enabled", "sni_server_name", "ca_cert_filename", "client_cert_filename", "client_key_filename",
    "sparkplug_group_id", "sparkplug_node_id", "sparkplug_host_id",
)


@dataclass(frozen=True)
class CommandActor:
    """Who a host's command is written to the audit log as."""
    username: str
    role: str = "SPARKPLUG"
    clearance_level: int = 2


class SparkplugService:
    """A Sparkplug node for every MQTT channel that has Sparkplug on, fed with every line's metrics."""

    _nodes: Dict[str, SparkplugNode] = {}
    _signatures: Dict[str, Tuple[Any, ...]] = {}
    _channels: Dict[str, Dict[str, Any]] = {}     # the MQTT channels as last saved: what should be running
    _last: Dict[str, Dict[str, Any]] = {}         # {line id: its latest product and code}
    _devices: Dict[str, str] = {}                 # {line id: device ID} of the last pass
    _kept: Dict[str, List[Metric]] = {}           # {line id: its metrics of the last pass that could read it}
    _failed: set = set()                          # lines that could not be read in the last pass
    _due: Dict[str, float] = {}                   # {channel id: when its node is next given the lines}
    _camera_names: Dict[str, str] = {}            # {camera id: name} from the saved camera list
    _names_at: float = 0.0                        # when the names were last read
    _names_asked: set = set()                     # camera ids the list was already read for
    _tasks: set = set()
    _task: Optional["asyncio.Task"] = None
    _change: Optional[Tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = None

    # ── Nodes follow the channels ─────────────────────────────────────────────

    @staticmethod
    def _wanted(channel: Optional[Dict[str, Any]]) -> bool:
        return bool(
            channel
            and str(channel.get("protocol", "")).lower() == "mqtt"
            and channel.get("enabled", True) is True
            and channel.get("sparkplug_enabled") is True
            and channel.get("host") and channel.get("sparkplug_group_id") and channel.get("sparkplug_node_id")
        )

    @staticmethod
    def _signature(channel: Dict[str, Any]) -> Tuple[Any, ...]:
        return tuple(channel.get(key) for key in _NODE_KEYS)

    @classmethod
    async def start(cls) -> None:
        """Start a node for every saved channel that has Sparkplug on, and the publish loop."""
        from app.services.settings_persistence_service import SettingsPersistenceService

        for channel in SettingsPersistenceService.get_endpoints("mqtt"):
            if channel.get("id"):
                cls._channels[str(channel["id"])] = dict(channel)
        for channel_id in list(cls._channels):
            await cls._reconcile(channel_id)
        if cls._task is None or cls._task.done():
            cls._task = asyncio.get_running_loop().create_task(cls._run())

    @classmethod
    async def stop(cls) -> None:
        """End every node with its NDEATH, and the publish loop."""
        task, cls._task = cls._task, None
        if task is not None:
            task.cancel()
        for pending in list(cls._tasks):
            pending.cancel()
        nodes = list(cls._nodes.values())
        cls._nodes.clear()
        cls._signatures.clear()
        for node in nodes:
            try:
                await node.stop()
            except Exception:
                logger.exception("Could not stop the Sparkplug node of channel '%s'", node.name)

    @classmethod
    def apply(cls, channel: Dict[str, Any]) -> None:
        """A channel was saved: start, replace or end its node."""
        channel_id = str(channel.get("id") or "")
        if not channel_id:
            return
        cls._channels[channel_id] = dict(channel)
        cls._schedule(channel_id)

    @classmethod
    def drop(cls, channel_id: str) -> None:
        """A channel was deleted: end its node."""
        cls._channels.pop(str(channel_id), None)
        cls._schedule(str(channel_id))

    @classmethod
    def _schedule(cls, channel_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop here: start() brings the nodes in line with the channels
        task = loop.create_task(cls._reconcile(channel_id))
        cls._tasks.add(task)
        task.add_done_callback(cls._tasks.discard)

    @classmethod
    def _change_lock(cls) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if cls._change is None or cls._change[0] is not loop:
            cls._change = (loop, asyncio.Lock())
        return cls._change[1]

    @classmethod
    async def _reconcile(cls, channel_id: str) -> None:
        """Make the channel's node match the channel as last saved. One change at a time."""
        async with cls._change_lock():
            channel = cls._channels.get(channel_id)
            wanted = cls._wanted(channel)
            node = cls._nodes.get(channel_id)
            if node is not None and (not wanted or cls._signatures.get(channel_id) != cls._signature(channel)):
                cls._nodes.pop(channel_id, None)
                cls._signatures.pop(channel_id, None)
                cls._due.pop(channel_id, None)
                await node.stop()   # its NDEATH goes out before a new node's NBIRTH
                node = None
            if not wanted:
                return
            if node is not None:
                node.allow_commands = channel.get("sparkplug_allow_commands") is True
                node.interval = int(channel.get("sparkplug_interval_ms") or 1000) / 1000.0
                return
            try:
                from app.services import settings_persistence_service as persistence
                store = BdSeqStore(Path(persistence.DATA_DIR) / "sparkplug_bdseq.json")
                node = SparkplugNode(channel, store, on_command=cls._on_command)
            except (ValueError, OSError) as exc:
                logger.warning("Sparkplug on channel '%s' cannot start: %s", channel.get("name") or channel_id, exc)
                return
            cls._nodes[channel_id] = node
            cls._signatures[channel_id] = cls._signature(channel)
            await cls.refresh_camera_names()
            await node.start()
            # Its births then list every line, without waiting for the first pass.
            cls._give(node, cls._lines())
            logger.info("Sparkplug node %s/%s started on channel '%s'", node.group, node.node, node.name)

    # ── Lines are published ───────────────────────────────────────────────────

    @classmethod
    async def _run(cls) -> None:
        while True:
            await asyncio.sleep(0.1)
            try:
                if cls._nodes and cls._names_due():
                    await cls.refresh_camera_names()
                cls._tick()
            except Exception:
                logger.exception("Sparkplug publish pass failed")

    @classmethod
    def _tick(cls) -> None:
        """Give the lines to every node whose publish interval has passed."""
        now = time.monotonic()
        due = [node for channel_id, node in list(cls._nodes.items()) if now >= cls._due.get(channel_id, 0.0)]
        if not due:
            return
        lines = cls._lines()
        for node in due:
            cls._due[node.channel_id] = now + node.interval
            cls._give(node, lines)

    @classmethod
    def publish_now(cls) -> None:
        """One pass over every node, whatever its interval."""
        lines = cls._lines()
        for node in list(cls._nodes.values()):
            cls._give(node, lines)

    @staticmethod
    def _give(node: SparkplugNode, lines: Dict[str, List[Metric]]) -> None:
        try:
            node.publish_lines(lines)
        except Exception:
            logger.exception("Sparkplug node of channel '%s' could not publish", node.name)

    @classmethod
    def _lines(cls) -> Dict[str, List[Metric]]:
        """{device ID: every metric} of every production line, as it is now."""
        import app.services.line_service as line_service

        runtimes = line_service.line_manager.all()
        devices = device_ids([(runtime.id, runtime.name) for runtime in runtimes])
        lines: Dict[str, List[Metric]] = {}
        for runtime in runtimes:
            try:
                metrics = line_metrics(cls._view(runtime))
                cls._kept[runtime.id] = metrics
                cls._failed.discard(runtime.id)
            except Exception:
                # Its last values again: a line that cannot be read for a moment
                # is neither changed nor taken for deleted.
                if runtime.id not in cls._failed:
                    cls._failed.add(runtime.id)
                    logger.exception("Sparkplug could not read line '%s'; its last values are kept", runtime.name)
                metrics = cls._kept.get(runtime.id)
                if metrics is None:
                    continue
            lines[devices[runtime.id]] = metrics
        for gone in set(cls._kept) - {runtime.id for runtime in runtimes}:
            cls._kept.pop(gone, None)
            cls._last.pop(gone, None)
            cls._failed.discard(gone)
        cls._devices = devices
        return lines

    # A camera's tag is named after the camera. The name comes from the saved
    # camera list, not from the camera's driver: a camera that is not connected
    # has no driver, and its tag must not change its name when it connects.

    @staticmethod
    async def _load_camera_names() -> Dict[str, str]:
        from sqlalchemy import select

        from app.db.models.camera import Camera
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            rows = await db.execute(select(Camera.id, Camera.name))
            return {str(camera_id): str(name) for camera_id, name in rows.all() if name}

    @classmethod
    async def refresh_camera_names(cls) -> None:
        cls._names_at = time.monotonic()
        try:
            cls._camera_names = dict(await cls._load_camera_names())
        except Exception as exc:
            logger.debug("Sparkplug could not read the camera names: %s", exc)

    @classmethod
    def _names_due(cls) -> bool:
        """Read the names again every 5 seconds, and at once for a camera that is new to the lines."""
        import app.services.line_service as line_service

        if time.monotonic() - cls._names_at >= 5.0:
            return True
        on_lines = {str(camera.get("camera_id")) for runtime in line_service.line_manager.all() for camera in runtime.cameras or []}
        unasked = on_lines - set(cls._camera_names) - cls._names_asked
        cls._names_asked |= unasked
        return bool(unasked)

    @classmethod
    def _view(cls, runtime: Any) -> Dict[str, Any]:
        """A line's live figures, as sparkplug_metrics.line_metrics reads them."""
        import app.services.line_service as line_service
        from app.events.alarm_events import alarm_manager

        view = line_service.line_manager.summary(runtime)
        for camera in view.get("cameras") or []:
            camera["name"] = cls._camera_names.get(str(camera.get("camera_id"))) or camera.get("name")
        view["counts"] = dict(runtime.counter.counts_by_class)
        # As the counter names them (lower case), or a class set as "Bottle" would
        # be listed as one tag and counted on another.
        view["classes"] = [str(name).strip().lower() for camera in view.get("cameras") or []
                           for name in [*(camera.get("expected_classes") or []), *(camera.get("defect_classes") or [])]]
        view["alarms"] = [alarm.code for alarm in alarm_manager.active() if (alarm.details or {}).get("line_id") == runtime.id]
        view["last"] = dict(cls._last.get(runtime.id) or {})
        return view

    @classmethod
    def note_event(cls, payload: Any) -> None:
        """Keep the latest product and code of a line, from the message of its event.

        Called for every product, from the counting thread: it must stay cheap
        and can never raise.
        """
        try:
            line_id = payload.get("line_id")
            latest = last_from_event(payload) if line_id else None
            if latest:
                cls._last.setdefault(str(line_id), {}).update(latest)
        except Exception:
            pass

    # ── Commands from the host ────────────────────────────────────────────────

    @classmethod
    def _on_command(cls, node: SparkplugNode, device: str, metric: Metric) -> None:
        """A host wrote a tag of a line. Arrives on paho's thread; runs on the event loop."""
        node.run_soon(cls._run_command(node, device, metric))

    @classmethod
    async def _run_command(cls, node: SparkplugNode, device: str, metric: Metric) -> None:
        what = f"channel '{node.name}', line '{device}', tag '{metric.name}'"
        line_id = cls.line_of(device)
        if line_id is None:
            logger.info("Sparkplug command ignored (%s): no such line", what)
            return
        try:
            await cls._act(node, line_id, metric, what)
        except Exception as exc:
            # A refusal of the function the dashboard uses too (HTTPException), or a fault.
            logger.warning("Sparkplug command refused (%s): %s", what, getattr(exc, "detail", None) or exc)
        # The tag's real value at once, so the host never keeps showing one that was refused.
        cls._give(node, cls._lines())
        node.resend(device, [metric.name])

    @classmethod
    async def _act(cls, node: SparkplugNode, line_id: str, metric: Metric, what: str) -> None:
        if metric.name not in (RUNNING, RESET):
            logger.info("Sparkplug command ignored (%s): this tag cannot be written", what)
            return
        if not node.allow_commands:
            logger.info("Sparkplug command ignored (%s): the channel does not allow commands", what)
            return
        if metric.datatype != DataType.Boolean or not isinstance(metric.value, bool):
            logger.info("Sparkplug command ignored (%s): the value is not true or false", what)
            return
        actor = CommandActor(username=f"sparkplug:{node.name}")
        if metric.name == RUNNING:
            from app.routes.v1.lines import set_line_running
            await set_line_running(line_id, metric.value, actor)
            logger.info("Sparkplug command (%s): line %s", what, "started" if metric.value else "stopped")
        elif metric.value is True:
            import app.services.line_service as line_service
            from app.routes.v1.counting import reset_line_counters
            runtime = line_service.line_manager.get(line_id)
            if runtime is not None:
                reset_line_counters(runtime, actor)
                logger.info("Sparkplug command (%s): counters reset", what)

    # ── State ─────────────────────────────────────────────────────────────────

    @classmethod
    def status(cls) -> Dict[str, Dict[str, Any]]:
        """{channel id: its node's state} for every channel that has a node."""
        return {channel_id: node.status() for channel_id, node in list(cls._nodes.items())}

    @classmethod
    def describe(cls, channel_id: str) -> str:
        """What a channel's test adds about its node; "" for a channel without one."""
        node = cls._nodes.get(str(channel_id))
        if node is None:
            return ""
        state = node.status()
        if not state["connected"]:
            return f"; Sparkplug B: not connected ({state['last_error'] or 'connecting'})"
        if state["host_online"] is False:
            return f"; Sparkplug B: waiting for host '{node.host_id}'"
        return f"; Sparkplug B: {state['devices']} line(s) published"

    @classmethod
    def device_of(cls, line_id: str) -> Optional[str]:
        if not cls._devices:
            cls._lines()
        return cls._devices.get(line_id)

    @classmethod
    def line_of(cls, device_id: str) -> Optional[str]:
        if not cls._devices:
            cls._lines()
        return next((line_id for line_id, device in cls._devices.items() if device == device_id), None)
