"""A small MQTT broker for tests: enough to connect, publish and subscribe, in MQTT 3.1.1 and MQTT 5.

It keeps what clients did (connects with their will, subscriptions, messages)
and the order they did it in (`log`), so a test can assert on it, and delivers messages to subscribers, its own
included (`publish`). It keeps no retained messages and sends no will.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Tuple


def _varint(data, at):
    value = shift = 0
    while True:
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, at


def _text(data, at):
    size = int.from_bytes(data[at:at + 2], "big")
    return data[at + 2:at + 2 + size], at + 2 + size


def _length(size: int) -> bytes:
    out = bytearray()
    while True:
        byte, size = size & 0x7F, size >> 7
        out.append(byte | (0x80 if size else 0))
        if not size:
            return bytes(out)


def topic_matches(pattern: str, topic: str) -> bool:
    want, have = pattern.split("/"), topic.split("/")
    for index, part in enumerate(want):
        if part == "#":
            return True
        if index >= len(have) or (part != "+" and part != have[index]):
            return False
    return len(want) == len(have)


class Broker:
    def __init__(self, username: Optional[str] = None, password: Optional[str] = None, port: int = 0):
        self.username, self.password = username, password
        self.connects: List[Dict[str, Any]] = []
        self.messages: List[Dict[str, Any]] = []
        self.subscriptions: List[Tuple[str, str, int]] = []
        self.read_delay = 0.0   # seconds to wait before handling each packet: a busy or distant broker
        self.log: List[Tuple[str, str]] = []   # ("subscribe" or "publish", topic) in the order they arrived
        self._clients: Dict[socket.socket, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.server = socket.socket()
        if os.name != "nt":
            # Linux keeps a just-closed port taken for a minute (TIME_WAIT) unless this
            # is set; on Windows the option would let two brokers share a port.
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        deadline = time.time() + 5.0
        while True:
            try:
                self.server.bind(("127.0.0.1", port))
                break
            except OSError:
                # A port that was just closed can stay taken for a moment.
                if not port or time.time() > deadline:
                    raise
                time.sleep(0.1)
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    # ── What a test does ──────────────────────────────────────────────────────

    def wait(self, count: int, timeout: float = 5.0) -> List[Dict[str, Any]]:
        deadline = time.time() + timeout
        while len(self.messages) < count and time.time() < deadline:
            time.sleep(0.02)
        return self.messages

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> int:
        """Deliver a message to every subscriber whose filter matches. Returns how many got it.

        ``retain`` marks it the way a broker marks a retained message it hands
        to a new subscription.
        """
        with self._lock:
            clients = list(self._clients.items())
        sent = 0
        for conn, client in clients:
            if not any(topic_matches(pattern, topic) for pattern, _ in client["filters"]):
                continue
            body = len(topic.encode()).to_bytes(2, "big") + topic.encode()
            if client["level"] == 5:
                body += b"\x00"  # no properties
            body += bytes(payload)
            try:
                with client["send"]:
                    conn.sendall(bytes([0x31 if retain else 0x30]) + _length(len(body)) + body)
                sent += 1
            except OSError:
                pass
        return sent

    def drop_clients(self) -> None:
        """Close every client's socket without a DISCONNECT, as a network fault would."""
        with self._lock:
            clients = list(self._clients)
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def close(self) -> None:
        self._stop = True
        self.drop_clients()
        self.server.close()

    # ── The broker ────────────────────────────────────────────────────────────

    def _accept(self):
        self.server.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _packet(self, conn):
        head = conn.recv(1)
        if not head:
            return None, b""
        size = shift = 0
        while True:
            byte = conn.recv(1)[0]
            size |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        body = b""
        while len(body) < size:
            chunk = conn.recv(size - len(body))
            if not chunk:
                return None, b""
            body += chunk
        return head[0], body

    def _connect(self, conn, body) -> Optional[Dict[str, Any]]:
        _, at = _text(body, 0)
        level, flags = body[at], body[at + 1]
        keepalive = int.from_bytes(body[at + 2:at + 4], "big")
        at += 4
        if level == 5:
            size, at = _varint(body, at)
            at += size
        client_id, at = _text(body, at)
        will = None
        if flags & 0x04:  # a will: its topic and message come before the login
            if level == 5:
                size, at = _varint(body, at)
                at += size
            will_topic, at = _text(body, at)
            will_payload, at = _text(body, at)
            will = {"topic": will_topic.decode(), "payload": bytes(will_payload), "qos": (flags >> 3) & 3, "retain": bool(flags & 0x20)}
        username = password = None
        if flags & 0x80:
            username, at = _text(body, at)
        if flags & 0x40:
            password, at = _text(body, at)
        ok = self.username is None or (username, password) == (self.username.encode(), self.password.encode())
        self.connects.append({"level": level, "client_id": client_id.decode(), "ok": ok, "will": will, "keepalive": keepalive})
        if level == 5:
            conn.sendall(bytes([0x20, 3, 0, 0 if ok else 0x86, 0]))
        else:
            conn.sendall(bytes([0x20, 2, 0, 0 if ok else 4]))
        if not ok:
            return None
        client = {"level": level, "client_id": client_id.decode(), "filters": [], "send": threading.Lock()}
        with self._lock:
            self._clients[conn] = client
        return client

    def _serve(self, conn):
        client = None
        conn.settimeout(30)
        try:
            while not self._stop:
                head, body = self._packet(conn)
                if head is None:
                    return
                kind = head >> 4
                if kind == 1:  # CONNECT
                    client = self._connect(conn, body)
                    if client is None:
                        return
                elif client is None:
                    return
                if self.read_delay:
                    time.sleep(self.read_delay)
                if kind == 1:
                    continue
                elif kind == 3:  # PUBLISH
                    qos, retain = (head >> 1) & 3, bool(head & 1)
                    topic, at = _text(body, 0)
                    if qos:
                        packet_id = body[at:at + 2]
                        at += 2
                        with client["send"]:
                            conn.sendall(bytes([0x40, 2]) + packet_id)
                    if client["level"] == 5:
                        size, at = _varint(body, at)
                        at += size
                    raw = bytes(body[at:])
                    try:
                        parsed = json.loads(raw)
                    except ValueError:
                        parsed = None
                    self.log.append(("publish", topic.decode()))
                    self.messages.append({"topic": topic.decode(), "raw": raw, "payload": parsed, "qos": qos, "retain": retain})
                    self.publish(topic.decode(), raw)
                elif kind == 8:  # SUBSCRIBE
                    packet_id, at = body[0:2], 2
                    if client["level"] == 5:
                        size, at = _varint(body, at)
                        at += size
                    granted = bytearray()
                    while at < len(body):
                        pattern, at = _text(body, at)
                        qos = body[at] & 3
                        at += 1
                        client["filters"].append((pattern.decode(), qos))
                        self.subscriptions.append((client["client_id"], pattern.decode(), qos))
                        self.log.append(("subscribe", pattern.decode()))
                        granted.append(qos)
                    ack = packet_id + (b"\x00" if client["level"] == 5 else b"") + bytes(granted)
                    with client["send"]:
                        conn.sendall(bytes([0x90]) + _length(len(ack)) + ack)
                elif kind == 12:  # PINGREQ
                    with client["send"]:
                        conn.sendall(bytes([0xD0, 0]))
                elif kind == 14:  # DISCONNECT
                    return
        except (OSError, IndexError):
            pass
        finally:
            with self._lock:
                self._clients.pop(conn, None)
            try:
                conn.close()
            except OSError:
                pass
