"""Send cards: what each production line reports to other systems, and when.

A send card names one channel (an MQTT, TCP or webhook channel from
Connections), a trigger and condition (the same choices as a PLC action card,
checked by the same function, card_triggers.card_fires) and the contents of
its message. A card on an MQTT channel also names the topic it publishes to:
the channel is the broker, so cards can share a broker and differ in topic. A line can have any number of cards, so it can send different
messages to different systems.

A card has no delay: the message goes out as soon as the event is known. For a
product that is when its final result is decided.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.services.card_triggers import alarm_event, card_fires, card_line, normalize_trigger
from app.services.line_config import PRIMARY_LINE_ID, SEND_PROTOCOLS
from app.utils.logger import get_logger

logger = get_logger(__name__)

# What a message can contain. A card lists the fields it sends; each field is
# one or more keys of the event's message. A name that is not listed here is
# taken as a message key itself, so field lists saved before send cards
# existed mean what they meant. The names that stand for several keys
# ("line", "qr", "alarm") are not keys of any message.
MESSAGE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "event": ("event",),
    "timestamp": ("timestamp",),
    "line": ("line_id", "line_name"),
    "line_state": ("line_state",),
    "camera_id": ("camera_id",),
    "track_id": ("track_id",),
    "class_name": ("class_name",),
    "result": ("result",),
    "is_defect": ("is_defect",),
    "reject_reason": ("reject_reason",),
    "confidence": ("confidence",),
    "bbox": ("bbox",),
    "smooth_center": ("smooth_center",),
    "velocity": ("velocity",),
    "qr": ("code", "format", "known", "paired", "qr_code", "qr_format", "qr_status", "qr_paired", "product_name"),
    "counts": ("counts",),
    "metrics": ("metrics",),
    "alarm": ("alarm_code", "alarm_message", "alarm_severity", "alarm_source", "alarm_raised"),
}


def build_message(card: dict, payload: Dict[str, Any]) -> Dict[str, Any]:
    """The message a card sends for an event: the fields it lists, or everything when it lists none."""
    fields = card.get("fields")
    if not isinstance(fields, list) or not fields:
        return payload
    message: Dict[str, Any] = {}
    for name in fields:
        for key in MESSAGE_FIELDS.get(str(name), (str(name),)):
            if key in payload:
                message[key] = payload[key]
    # A card whose fields do not occur in this kind of event sends the whole event rather than nothing.
    return message or payload


def line_state_payload(event: dict) -> Dict[str, Any]:
    """The message of a line start or stop."""
    running = bool(event.get("line_running"))
    return {
        "event": "LINE_STARTED" if running else "LINE_STOPPED",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "line_id": event.get("line_id"),
        "line_name": event.get("line_name"),
        "line_state": "started" if running else "stopped",
    }


def alarm_payload(event: dict) -> Dict[str, Any]:
    """The message of an alarm that was raised or cleared."""
    raised = bool(event.get("alarm_raised"))
    return {
        "event": "ALARM_RAISED" if raised else "ALARM_CLEARED",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "alarm_code": event.get("alarm_code"),
        "alarm_message": event.get("alarm_message"),
        "alarm_severity": event.get("alarm_severity"),
        "alarm_source": event.get("alarm_source"),
        "alarm_raised": raised,
    }


def _webhook_headers(endpoint: dict) -> Dict[str, str]:
    raw = endpoint.get("headers")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    headers: Dict[str, str] = {}
    for line in str(raw or "").splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip()] = value.strip()
    return headers


def card_topic(card: dict, endpoint: dict) -> str:
    """The MQTT topic a card publishes to: its own, else the one its channel kept before cards had topics."""
    return str(card.get("topic") or endpoint.get("topic") or "").strip()


async def deliver(endpoint: dict, message: Dict[str, Any], topic: str = "") -> Tuple[bool, str]:
    """Send one message to one channel (an MQTT channel: to this topic). Returns (sent, what happened)."""
    protocol = str(endpoint.get("protocol", "")).lower()
    try:
        if protocol == "mqtt":
            from app.services.mqtt_service import MQTTChannels
            if not topic:
                return False, "This card has no MQTT topic"
            return await MQTTChannels.publish(endpoint, topic, message)
        if protocol == "tcp":
            host, port = str(endpoint.get("host") or ""), int(endpoint.get("port") or 0)
            if not host or not port:
                return False, "The TCP channel has no host or port"
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
            try:
                writer.write((json.dumps(message) + "\n").encode("utf-8"))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
            return True, f"Sent to {host}:{port}"
        if protocol == "webhook":
            from app.services.counting_service import CountingService
            url = str(endpoint.get("url") or "")
            if not url:
                return False, "The webhook channel has no URL"
            method = str(endpoint.get("method") or "POST").upper()
            response = await CountingService.http_client().request(
                method if method in ("POST", "PUT") else "POST", url, json=message, headers=_webhook_headers(endpoint),
            )
            ok = response.status_code < 400
            return ok, f"The webhook answered HTTP {response.status_code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return False, f"'{protocol}' is not a channel a message can be sent to"


def _lane(endpoint: dict, topic: str = "") -> Tuple[Any, ...]:
    """Messages to one destination go out in order; destinations do not wait on each other."""
    protocol = str(endpoint.get("protocol", "")).lower()
    if protocol == "mqtt":
        return ("mqtt", endpoint.get("id"), topic)
    if protocol == "tcp":
        return ("tcp", endpoint.get("host"), endpoint.get("port"))
    return ("webhook", endpoint.get("url"))


class SendDispatcherService:
    """Holds every line's send cards and sends their messages."""

    _cards: List[dict] = []
    _status: Dict[str, Dict[str, Any]] = {}
    _lock = threading.Lock()

    # ── Cards ─────────────────────────────────────────────────────────────────

    @classmethod
    def load_cards(cls) -> None:
        """Load every line's send cards from the saved settings. Safe to call more than once."""
        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            cls._cards = [normalize_trigger(card) for card in SettingsPersistenceService.get_all_send_actions()]
            logger.info("SendDispatcherService: loaded %d send card(s)", len(cls._cards))
        except Exception as exc:
            logger.warning("SendDispatcherService.load_cards error: %s", exc)

    @classmethod
    def set_cards(cls, cards: List[dict], line_id: Optional[str] = None) -> None:
        """Replace one line's cards (Line 1 when no line is named); other lines keep theirs."""
        line_id = line_id or PRIMARY_LINE_ID
        fresh = []
        for raw in cards:
            card = normalize_trigger(raw)
            card["line_id"] = line_id
            fresh.append(card)
        cls._cards = [c for c in cls._cards if card_line(c) != line_id] + fresh
        live = {c.get("id") for c in cls._cards}
        with cls._lock:
            for gone in [cid for cid in cls._status if cid not in live]:
                del cls._status[gone]

    @classmethod
    def get_status(cls, line_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """{card id: {"sent_count", "last_sent_at", "last_result"}} for one line's cards, or all."""
        with cls._lock:
            return {
                card.get("id"): dict(cls._status.get(card.get("id"), {}))
                for card in cls._cards
                if not line_id or card_line(card) == line_id
            }

    # ── Sending ───────────────────────────────────────────────────────────────

    @staticmethod
    def _endpoint(endpoint_id: Any) -> Optional[dict]:
        if not endpoint_id:
            return None
        from app.services.settings_persistence_service import SettingsPersistenceService
        return next((ep for ep in SettingsPersistenceService.get_endpoints() if ep.get("id") == endpoint_id), None)

    @classmethod
    def _record(cls, card_id: Any, **changes: Any) -> None:
        with cls._lock:
            status = cls._status.setdefault(card_id, {"sent_count": 0, "last_sent_at": None, "last_result": {}})
            status.update(changes)

    @classmethod
    def evaluate(cls, event: dict, payload: Dict[str, Any]) -> int:
        """Send the message of every card that fires for this event. Never blocks: messages are
        queued on the telemetry dispatcher. Callable from any thread. Returns how many were queued."""
        queued = 0
        with_state: Optional[Dict[str, Any]] = None
        for card in list(cls._cards):
            if card.get("enabled", True) is not True:
                continue
            try:
                if not card_fires(card, event):
                    continue
                if with_state is None:
                    with_state = cls._with_line_state(payload, event)
                if cls._queue(card, build_message(card, with_state)):
                    queued += 1
            except Exception as exc:
                logger.warning("Send card '%s' could not send: %s: %s", card.get("name") or card.get("id"), type(exc).__name__, exc)
        return queued

    @staticmethod
    def _with_line_state(payload: Dict[str, Any], event: dict) -> Dict[str, Any]:
        """Every message can say whether its line is started or stopped."""
        if "line_state" in payload:
            return payload
        try:
            from app.services.line_service import line_manager
            runtime = line_manager.get(str(event.get("line_id") or payload.get("line_id") or PRIMARY_LINE_ID))
            if runtime is not None:
                return {**payload, "line_state": "started" if runtime.enabled else "stopped"}
        except Exception as exc:
            logger.debug("Could not read the line state for a message: %s", exc)
        return payload

    @classmethod
    def _queue(cls, card: dict, message: Dict[str, Any]) -> bool:
        from app.services.counting_service import _telemetry_dispatcher

        card_id = card.get("id")
        endpoint = cls._endpoint(card.get("endpoint_id"))
        problem = None
        if endpoint is None:
            problem = "No channel is chosen for this card" if not card.get("endpoint_id") else "Its channel no longer exists"
        elif str(endpoint.get("protocol", "")).lower() not in SEND_PROTOCOLS:
            problem = "Its channel is not an MQTT, TCP or webhook channel"
        elif endpoint.get("enabled", True) is not True:
            problem = "Its channel is switched off"
        topic = ""
        if not problem and str(endpoint.get("protocol", "")).lower() == "mqtt":
            topic = card_topic(card, endpoint)
            if not topic:
                problem = "This card has no MQTT topic"
        if problem:
            cls._record(card_id, last_result={"success": False, "message": problem, "at": time.time()})
            return False

        async def send(endpoint: dict = endpoint, message: Dict[str, Any] = message) -> None:
            ok, detail = await deliver(endpoint, message, topic)
            cls._record(card_id, last_result={"success": ok, "message": detail, "at": time.time()})
            if not ok:
                logger.debug("Send card '%s': %s", card.get("name") or card_id, detail)

        _telemetry_dispatcher.submit(send, key=_lane(endpoint, topic))
        with cls._lock:
            status = cls._status.setdefault(card_id, {"sent_count": 0, "last_sent_at": None, "last_result": {}})
            status["sent_count"] += 1
            status["last_sent_at"] = time.time()
        return True

    @classmethod
    async def send_test(cls, card: dict, line_name: str = "") -> Dict[str, Any]:
        """Send a card's message once now, with example values, and wait for the outcome."""
        endpoint = cls._endpoint(card.get("endpoint_id"))
        if endpoint is None:
            return {"success": False, "message": "Choose the channel this card sends to first.", "sent": None}
        if str(endpoint.get("protocol", "")).lower() not in SEND_PROTOCOLS:
            return {"success": False, "message": "The channel must be an MQTT, TCP or webhook channel.", "sent": None}
        if endpoint.get("enabled", True) is not True:
            return {"success": False, "message": f"The channel '{endpoint.get('name')}' is switched off under Connections.", "sent": None}
        message = build_message(card, sample_payload(card, line_name))
        message = {**message, "test": True}
        ok, detail = await deliver(endpoint, message, card_topic(card, endpoint))
        cls._record(card.get("id"), last_result={"success": ok, "message": f"Test: {detail}", "at": time.time()})
        return {"success": ok, "message": detail, "sent": message, "endpoint_name": endpoint.get("name")}

    # ── Line starts and stops, alarms ─────────────────────────────────────────

    @classmethod
    def line_state_changed(cls, event: dict) -> int:
        """A line was started or stopped (the event of card_triggers.line_state_event)."""
        return cls.evaluate(event, cls._with_totals(line_state_payload(event), event.get("line_id")))

    @staticmethod
    def _with_totals(payload: Dict[str, Any], line_id: Any) -> Dict[str, Any]:
        """Add the line's name and totals to a message that does not come from a product."""
        try:
            from app.services.line_service import line_manager
            runtime = line_manager.get(str(line_id)) if line_id else None
            if runtime is not None:
                reading = runtime.counter.get_current_reading_payload()
                payload = {
                    **payload,
                    "line_id": runtime.id,
                    "line_name": runtime.name,
                    "counts": reading["counts"],
                    "metrics": reading["metrics"],
                }
        except Exception as exc:
            logger.debug("Could not add line totals to a message: %s", exc)
        return payload

    @classmethod
    def watch_alarms(cls) -> None:
        """Send "Alarm" cards when an alarm is raised or cleared. Safe to call more than once."""
        from app.events.event_bus import event_bus
        from app.events.system_events import SystemEventType

        event_bus.subscribe(SystemEventType.ALARM_RAISED, cls._on_alarm_raised)
        event_bus.subscribe(SystemEventType.ALARM_CLEARED, cls._on_alarm_cleared)

    @classmethod
    async def _on_alarm_raised(cls, alarm: dict) -> None:
        cls._on_alarm(alarm, raised=True)

    @classmethod
    async def _on_alarm_cleared(cls, alarm: dict) -> None:
        cls._on_alarm(alarm, raised=False)

    @classmethod
    def _on_alarm(cls, alarm: dict, raised: bool) -> None:
        if not any(c.get("trigger_type") == "alarm" and c.get("enabled", True) is True for c in cls._cards):
            return
        from app.events.alarm_events import alarm_manager

        event = alarm_event(alarm, raised, [a.to_dict() for a in alarm_manager.active()])
        if not event["line_id"]:
            return  # a line alarm on no line (a camera no line uses) concerns no line's cards
        payload = alarm_payload(event)
        details = alarm.get("details") or {}
        cls.evaluate(event, cls._with_totals(payload, details.get("line_id")))


def sample_payload(card: dict, line_name: str = "") -> Dict[str, Any]:
    """Example values for a card's message, for its Test button and the dialog's preview."""
    trigger = str(card.get("trigger_type") or card.get("trigger") or "cross_line").lower()
    condition = str(card.get("condition") or "").lower()
    now = datetime.now(timezone.utc).isoformat()
    line = {"line_id": card_line(card), "line_name": line_name or card_line(card), "line_state": "started"}
    totals = {
        "counts": {"bottle": 142, "defect": 3},
        "metrics": {"total_inspected": 145, "good_count": 142, "rejected_count": 3,
                    "products_per_minute": 45.2, "defect_ppm": 20689.66, "yield_percentage": 97.93},
    }
    if trigger == "line_state":
        running = condition != "off"
        return {"event": "LINE_STARTED" if running else "LINE_STOPPED", "timestamp": now, **line,
                "line_state": "started" if running else "stopped", **totals}
    if trigger == "alarm":
        raised = condition != "cleared"
        return {"event": "ALARM_RAISED" if raised else "ALARM_CLEARED", "timestamp": now, **line,
                "alarm_code": "camera.disconnected", "alarm_message": "Camera 'Line camera' lost its connection",
                "alarm_severity": "critical", "alarm_source": "camera:example", "alarm_raised": raised, **totals}
    if trigger in ("qr_read", "qr_known", "qr_unknown", "qr_no_read"):
        no_read = trigger == "qr_no_read"
        known = trigger != "qr_unknown" and not no_read
        return {"event": "QR_CODE_NO_READ" if no_read else "QR_CODE_READ", "timestamp": now, **line, "camera_id": "example-camera",
                "code": None if no_read else "4006381333931", "format": None if no_read else "EAN13",
                "qr_status": "no_read" if no_read else ("known" if known else "unknown"), "known": known,
                "product_name": "Example product" if known else None, "paired": None}
    reject = trigger == "reject_counter" or condition == "reject"
    return {
        "event": "WIRELINE_OBJECT_CROSSED", "timestamp": now, **line, "camera_id": "example-camera",
        "track_id": 104, "class_name": "defect" if reject else "bottle",
        "result": "REJECTED" if reject else "PASSED", "is_defect": reject,
        "reject_reason": "vision_class" if reject else None, "vision_result": "REJECTED" if reject else "PASSED",
        "confidence": 0.94, "bbox": {"x1": 142, "y1": 88, "x2": 235, "y2": 340},
        "smooth_center": {"x": 188.5, "y": 214.0}, "velocity": {"vx": 0.0, "vy": 3.4}, **totals,
    }
