"""TCP channels: how a message goes out on a TCP channel from Connections.

A TCP channel's saved settings say how its messages are sent:

- ``delimiter``: what ends each message, as escaped text (``\\n``, ``\\r\\n``,
  ``\\0``, a custom text, or ``""`` for nothing). A missing delimiter is a
  newline, as before channels had one.
- ``timeout``: seconds for connecting and for writing one message.
- ``mode`` ``"client"``: the server connects to the device at host:port. As
  before, a new connection for each message; with ``keep_open`` one
  connection is kept, made again on the next message after a failure (with a
  back-off of 1 s doubling to 30 s), and the alarm ``send.channel_down`` stays
  raised until a message goes out again.
- ``mode`` ``"server"``: the channel listens on host:port (host is the address
  to listen on, 0.0.0.0 for every network) and each message goes to every
  device that is connected. A port that cannot be opened raises
  ``send.channel_down`` and is tried again with the same back-off.

Kept connections and listeners all live on one event loop, the telemetry
dispatcher's (counting_service._telemetry_dispatcher), whose thread sends the
queued messages. Every public coroutine here can be awaited from any loop.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import re
import time
from typing import Any, Coroutine, Dict, List, Optional, Set, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)

TCP_MODES = ("client", "server")
CHANNEL_DOWN = "send.channel_down"
MAX_DELIMITER_LENGTH = 32
# The timeout of a channel saved before channels had one (the old fixed value).
_DEFAULT_TIMEOUT = 2.0

_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "\\": "\\"}
_ESCAPE_RE = re.compile(r"\\(x[0-9A-Fa-f]{2}|[nrt0\\])")


def decode_delimiter(value: Any) -> str:
    """The characters a saved delimiter stands for.

    ``\\n`` ``\\r`` ``\\t`` ``\\0`` ``\\\\`` and ``\\xHH`` are escapes; any other
    character, a real control character included, stands for itself.
    None (a channel saved without one) is a newline.
    """
    if value is None:
        return "\n"
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "x" else _ESCAPES[m.group(1)], str(value))


def validate_delimiter(value: Any) -> str:
    """A delimiter as it is saved: text of at most MAX_DELIMITER_LENGTH characters."""
    if value is None:
        return "\\n"
    if not isinstance(value, str) or len(value) > MAX_DELIMITER_LENGTH:
        raise ValueError(f"The TCP delimiter must be text of at most {MAX_DELIMITER_LENGTH} characters")
    return value


def frame(text: str, endpoint: Dict[str, Any]) -> bytes:
    """One message as it goes on the wire: its UTF-8 text and the channel's delimiter."""
    return (text + decode_delimiter(endpoint.get("delimiter"))).encode("utf-8")


def channel_timeout(endpoint: Dict[str, Any]) -> float:
    try:
        seconds = float(endpoint.get("timeout"))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    return seconds if 0 < seconds <= 60 else _DEFAULT_TIMEOUT


def channel_mode(endpoint: Dict[str, Any]) -> str:
    mode = str(endpoint.get("mode") or "client").strip().lower()
    return mode if mode in TCP_MODES else "client"


def _target(endpoint: Dict[str, Any]) -> Tuple[str, int]:
    try:
        port = int(endpoint.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return str(endpoint.get("host") or "").strip(), port


def _why(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "no answer in time"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def _close_writer(writer: Optional[asyncio.StreamWriter], timeout: float = 2.0) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout)
    except Exception as exc:
        logger.debug("A TCP connection did not close cleanly: %s", exc)


class _Kept:
    """The one connection a client channel with "Keep connection open" holds."""

    def __init__(self, target: Tuple[str, int]):
        self.target = target
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.watcher: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()
        self.failures = 0
        self.retry_at = 0.0

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def close(self) -> None:
        writer, self.writer, self.reader = self.writer, None, None
        if self.watcher is not None:
            self.watcher.cancel()
            self.watcher = None
        await _close_writer(writer)


class _Listening:
    """A server channel's listener and the devices connected to it."""

    def __init__(self, target: Tuple[str, int]):
        self.target = target
        self.server: Optional[asyncio.AbstractServer] = None
        self.devices: Set[asyncio.StreamWriter] = set()
        self.error: Optional[str] = None
        self.task: Optional[asyncio.Task] = None
        self.started = asyncio.Event()

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        server, self.server = self.server, None
        if server is not None:
            server.close()
        devices, self.devices = list(self.devices), set()
        await asyncio.gather(*(_close_writer(writer) for writer in devices), return_exceptions=True)
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), 2.0)
            except Exception as exc:
                logger.debug("A TCP listener did not close cleanly: %s", exc)


class TcpChannels:
    """Sends on TCP channels and holds their kept connections and listeners.

    The dictionaries are only touched on the dispatcher loop, so they need no lock.
    """

    _kept: Dict[str, _Kept] = {}
    _listening: Dict[str, _Listening] = {}
    # The back-off after a failure: the first wait, and the longest.
    BACKOFF_FIRST = 1.0
    BACKOFF_MAX = 30.0

    # ── The dispatcher loop ───────────────────────────────────────────────────

    @staticmethod
    def _loop() -> asyncio.AbstractEventLoop:
        from app.services.counting_service import _telemetry_dispatcher
        return _telemetry_dispatcher._loop

    @classmethod
    def _schedule(cls, coro: Coroutine[Any, Any, Any]) -> Optional[concurrent.futures.Future]:
        """Run coro on the dispatcher loop without waiting for it. None when the dispatcher has stopped."""
        loop = cls._loop()
        if loop.is_closed():
            coro.close()
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            return None

    @classmethod
    async def _on_loop(cls, coro: Coroutine[Any, Any, Any]) -> Any:
        """Await coro on the dispatcher loop, from whatever loop this is."""
        loop = cls._loop()
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is loop:
            return await coro
        future = cls._schedule(coro)
        if future is None:
            raise RuntimeError("the message dispatcher has stopped")
        return await asyncio.wrap_future(future)

    # ── Sending ───────────────────────────────────────────────────────────────

    @classmethod
    async def send(cls, endpoint: Dict[str, Any], data: bytes) -> Tuple[bool, str]:
        """Send one framed message on a TCP channel. Returns (sent, what happened)."""
        try:
            return await cls._on_loop(cls._send(dict(endpoint), data))
        except Exception as exc:
            return False, _why(exc)

    @classmethod
    async def _send(cls, endpoint: Dict[str, Any], data: bytes) -> Tuple[bool, str]:
        host, port = _target(endpoint)
        if not host or not port:
            return False, "The TCP channel has no host or port"
        timeout = channel_timeout(endpoint)
        if channel_mode(endpoint) == "server":
            return await cls._send_to_devices(endpoint, data, timeout)
        if endpoint.get("keep_open") is True:
            return await cls._send_kept(endpoint, data, timeout)
        return await cls._send_once(host, port, data, timeout)

    @staticmethod
    async def _send_once(host: str, port: int, data: bytes, timeout: float) -> Tuple[bool, str]:
        """Connect, send, close: a new connection for each message."""
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except Exception as exc:
            return False, f"Cannot connect to {host}:{port}: {_why(exc)}"
        try:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout)
        except Exception as exc:
            return False, f"Sending to {host}:{port} failed: {_why(exc)}"
        finally:
            await _close_writer(writer, timeout)
        return True, f"Sent to {host}:{port}"

    @classmethod
    async def _send_kept(cls, endpoint: Dict[str, Any], data: bytes, timeout: float) -> Tuple[bool, str]:
        channel_id = str(endpoint.get("id") or "")
        target = _target(endpoint)
        host, port = target
        kept = cls._kept.get(channel_id)
        if kept is None or kept.target != target:
            if kept is not None:
                await kept.close()
            kept = cls._kept[channel_id] = _Kept(target)
        async with kept.lock:
            if not kept.connected:
                wait = kept.retry_at - time.monotonic()
                if wait > 0:
                    return False, f"Not connected to {host}:{port}; the next try is in {wait:.0f} s"
                try:
                    kept.reader, kept.writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
                except Exception as exc:
                    return cls._kept_failed(endpoint, kept, f"Cannot connect to {host}:{port}: {_why(exc)}")
                kept.watcher = asyncio.get_running_loop().create_task(cls._watch(kept, kept.reader, kept.writer))
            try:
                kept.writer.write(data)
                await asyncio.wait_for(kept.writer.drain(), timeout)
            except Exception as exc:
                await kept.close()
                return cls._kept_failed(endpoint, kept, f"Sending to {host}:{port} failed: {_why(exc)}")
            kept.failures, kept.retry_at = 0, 0.0
        cls._channel_up(endpoint)
        return True, f"Sent to {host}:{port}"

    @staticmethod
    async def _watch(kept: _Kept, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read what the device sends back (and drop it) to notice when it closes the connection."""
        try:
            while await reader.read(4096):
                pass
        except Exception as exc:
            logger.debug("A kept TCP connection broke: %s", exc)
        if kept.writer is writer:
            kept.writer = kept.reader = None
            kept.watcher = None
            await _close_writer(writer)

    @classmethod
    def _kept_failed(cls, endpoint: Dict[str, Any], kept: _Kept, detail: str) -> Tuple[bool, str]:
        kept.failures += 1
        kept.retry_at = time.monotonic() + cls._backoff(kept.failures)
        cls._channel_down(endpoint, detail)
        return False, detail

    @classmethod
    def _backoff(cls, failures: int) -> float:
        return min(cls.BACKOFF_MAX, cls.BACKOFF_FIRST * (2 ** max(0, failures - 1)))

    @classmethod
    async def _send_to_devices(cls, endpoint: Dict[str, Any], data: bytes, timeout: float) -> Tuple[bool, str]:
        listening = await cls._listen(endpoint)
        port = listening.target[1]
        if listening.server is None:
            return False, listening.error or f"Not listening on port {port} yet"
        devices = [writer for writer in listening.devices if not writer.is_closing()]
        if not devices:
            return False, f"No device is connected to port {port}"

        async def to(writer: asyncio.StreamWriter) -> bool:
            try:
                writer.write(data)
                await asyncio.wait_for(writer.drain(), timeout)
                return True
            except Exception as exc:
                logger.debug("A device on TCP port %d did not take a message: %s", port, exc)
                listening.devices.discard(writer)
                await _close_writer(writer)
                return False

        sent = sum(await asyncio.gather(*(to(writer) for writer in devices)))
        if not sent:
            return False, f"No device is connected to port {port}"
        return True, f"Sent to {sent} device{'' if sent == 1 else 's'} on port {port}"

    # ── Server mode ───────────────────────────────────────────────────────────

    @classmethod
    async def _listen(cls, endpoint: Dict[str, Any]) -> _Listening:
        """The channel's listener, started (or moved to a new address) when needed."""
        channel_id = str(endpoint.get("id") or "")
        target = _target(endpoint)
        listening = cls._listening.get(channel_id)
        if listening is not None and listening.target == target:
            if not listening.started.is_set():
                await listening.started.wait()
            return listening
        if listening is not None:
            await listening.close()
        listening = cls._listening[channel_id] = _Listening(target)
        listening.task = asyncio.get_running_loop().create_task(cls._keep_listening(endpoint, listening))
        await listening.started.wait()
        return listening

    @classmethod
    async def _keep_listening(cls, endpoint: Dict[str, Any], listening: _Listening) -> None:
        """Open the port; while it cannot be opened, raise the alarm and try again with the back-off."""
        host, port = listening.target
        failures = 0
        while True:
            try:
                listening.server = await asyncio.start_server(
                    lambda reader, writer: cls._device(listening, reader, writer), host, port)
            except Exception as exc:
                failures += 1
                listening.error = f"Cannot listen on {host}:{port}: {_why(exc)}"
                cls._channel_down(endpoint, listening.error)
                listening.started.set()
                await asyncio.sleep(cls._backoff(failures))
                continue
            listening.error = None
            listening.started.set()
            cls._channel_up(endpoint)
            logger.info("TCP channel '%s' is listening on %s:%d", endpoint.get("name") or endpoint.get("id"), host, port)
            return

    @staticmethod
    async def _device(listening: _Listening, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """A device connected: it gets every message until it disconnects. What it sends is dropped."""
        listening.devices.add(writer)
        try:
            while await reader.read(4096):
                pass
        except Exception as exc:
            logger.debug("A device on a TCP channel disconnected: %s", exc)
        finally:
            listening.devices.discard(writer)
            await _close_writer(writer)

    # ── Channels saved, deleted, and shutdown ─────────────────────────────────

    @classmethod
    def apply(cls, endpoint: Dict[str, Any]) -> Optional[concurrent.futures.Future]:
        """A channel was saved: start, move or stop its listener and drop a connection it no longer keeps."""
        return cls._schedule(cls._apply(dict(endpoint)))

    @classmethod
    async def _apply(cls, endpoint: Dict[str, Any]) -> None:
        channel_id = str(endpoint.get("id") or "")
        host, port = target = _target(endpoint)
        live = (str(endpoint.get("protocol", "")).lower() == "tcp" and endpoint.get("enabled", True) is True
                and bool(host) and bool(port))
        mode = channel_mode(endpoint)
        listening = cls._listening.get(channel_id)
        if listening is not None and not (live and mode == "server" and listening.target == target):
            await cls._listening.pop(channel_id).close()
        kept = cls._kept.get(channel_id)
        if kept is not None and not (live and mode == "client" and endpoint.get("keep_open") is True and kept.target == target):
            await cls._kept.pop(channel_id).close()
        if live and mode == "server":
            await cls._listen(endpoint)
        elif not (live and mode == "client" and endpoint.get("keep_open") is True):
            cls._channel_up(endpoint, reason="the channel no longer keeps a connection or listens")

    @classmethod
    def drop(cls, channel_id: str) -> Optional[concurrent.futures.Future]:
        """A channel was deleted: close its connection and listener."""
        return cls._schedule(cls._drop(str(channel_id)))

    @classmethod
    async def _drop(cls, channel_id: str) -> None:
        listening = cls._listening.pop(channel_id, None)
        if listening is not None:
            await listening.close()
        kept = cls._kept.pop(channel_id, None)
        if kept is not None:
            await kept.close()
        from app.events.alarm_events import alarm_manager
        alarm_manager.clear_alarm(CHANNEL_DOWN, cls._source(channel_id), "channel deleted")

    @classmethod
    def sync(cls, endpoints: List[Dict[str, Any]]) -> Optional[concurrent.futures.Future]:
        """Start the listener of every server channel and drop what deleted channels held (at startup)."""
        return cls._schedule(cls._sync([dict(ep) for ep in endpoints if isinstance(ep, dict)]))

    @classmethod
    async def _sync(cls, endpoints: List[Dict[str, Any]]) -> None:
        live = {str(ep.get("id")) for ep in endpoints}
        for channel_id in [cid for cid in {*cls._listening, *cls._kept} if cid not in live]:
            await cls._drop(channel_id)
        for endpoint in endpoints:
            if str(endpoint.get("protocol", "")).lower() == "tcp":
                await cls._apply(endpoint)

    @classmethod
    async def close_all(cls) -> None:
        """Shutdown: close every kept connection and listener."""
        try:
            await cls._on_loop(cls._close_all())
        except RuntimeError:
            pass  # the dispatcher has stopped: its loop closed them

    @classmethod
    async def _close_all(cls) -> None:
        held = [*cls._listening.values(), *cls._kept.values()]
        cls._listening, cls._kept = {}, {}
        await asyncio.gather(*(item.close() for item in held), return_exceptions=True)

    @classmethod
    async def status(cls, endpoint: Dict[str, Any]) -> Dict[str, Any]:
        """A server channel's listener: {"listening", "devices", "message"} (started when it is not yet)."""
        async def read() -> Dict[str, Any]:
            listening = await cls._listen(endpoint)
            host, port = listening.target
            if listening.server is None:
                return {"listening": False, "devices": 0, "message": listening.error or f"Not listening on port {port}"}
            devices = sum(1 for writer in listening.devices if not writer.is_closing())
            return {"listening": True, "devices": devices,
                    "message": f"Listening on {host}:{port}; {devices} device{'' if devices == 1 else 's'} connected"}
        return await cls._on_loop(read())

    # ── The alarm ─────────────────────────────────────────────────────────────

    @staticmethod
    def _source(channel_id: Any) -> str:
        return f"channel:{channel_id}"

    @classmethod
    def _channel_down(cls, endpoint: Dict[str, Any], detail: str) -> None:
        from app.events.alarm_events import AlarmSeverity, alarm_manager
        name = endpoint.get("name") or endpoint.get("id")
        alarm_manager.raise_alarm(
            CHANNEL_DOWN, cls._source(endpoint.get("id")), f"TCP channel '{name}': {detail}", AlarmSeverity.WARNING,
            {"endpoint_id": endpoint.get("id"), "endpoint_name": name},
        )

    @classmethod
    def _channel_up(cls, endpoint: Dict[str, Any], reason: str = "a message went out again") -> None:
        from app.events.alarm_events import alarm_manager
        if alarm_manager.get(CHANNEL_DOWN, cls._source(endpoint.get("id"))) is not None:
            alarm_manager.clear_alarm(CHANNEL_DOWN, cls._source(endpoint.get("id")), reason)
