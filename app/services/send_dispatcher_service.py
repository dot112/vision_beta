"""Send cards: what each production line reports to other systems, and when.

A send card names one channel (an MQTT, TCP or webhook channel from
Connections), a trigger and condition (the same choices as a PLC action card,
checked by the same function, card_triggers.card_fires) and the contents of
its message. A card on an MQTT channel also names the topic it publishes to:
the channel is the broker, so cards can share a broker and differ in topic. A line can have any number of cards, so it can send different
messages to different systems.

A card has no delay: the message goes out as soon as the event is known. For a
product that is when its final result is decided.

A card with the trigger "timer" is not sent for an event: it sends the line's
totals every N ms (its set_value), while the line runs ("while_running") or
always. Each such card has one task on the main event loop (start_timers).
"""
from __future__ import annotations

import asyncio
import functools
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from app.services.card_triggers import TIMER_TRIGGER, alarm_event, card_fires, card_line, normalize_trigger
from app.services.line_config import (
    PRIMARY_LINE_ID,
    SEND_PROTOCOLS,
    TEMPLATE_FIELDS,
    parse_template,
    timer_interval_ms,
)
from app.services.product_fields import flat_product_values
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
    # The line's batch (work order) number; a line without one sends no such key.
    "batch": ("batch",),
    "camera_id": ("camera_id",),
    "track_id": ("track_id",),
    "class_name": ("class_name",),
    "result": ("result",),
    "is_defect": ("is_defect",),
    "reject_reason": ("reject_reason",),
    # The name of the line's product check that rejected the product.
    "product_check": ("product_check",),
    # The product sequence number the PLC held when it asked for the item's pictures.
    "sequence": ("sequence",),
    # What each vision camera that joins the product result saw of the product, and which camera rejected it.
    "stations": ("stations",),
    "reject_camera_id": ("reject_camera_id",),
    "confidence": ("confidence",),
    "bbox": ("bbox",),
    "smooth_center": ("smooth_center",),
    "velocity": ("velocity",),
    "qr": ("code", "format", "known", "paired", "qr_code", "qr_format", "qr_status", "qr_paired", "product_name",
           "product_id", "code_problem"),
    "counts": ("counts",),
    "metrics": ("metrics",),
    "alarm": ("alarm_code", "alarm_message", "alarm_severity", "alarm_source", "alarm_raised"),
    # The product of a known code: its code, name, list and values ({"code", "name", "list", "list_id", "fields"}).
    "product": ("product",),
    # The input that changed (the trigger "Input changed").
    "input": ("input_id", "input_name", "value", "previous"),
}


def _local_moment(timestamp: Any) -> datetime:
    """The event's moment in the server's local time (now, when the event has no readable time)."""
    if isinstance(timestamp, str):
        try:
            moment = datetime.fromisoformat(timestamp)
            return moment.astimezone() if moment.tzinfo else moment
        except ValueError:
            pass
    return datetime.now()


def _camera_name(camera_id: Any) -> Optional[str]:
    if not camera_id:
        return None
    try:
        from app.state.application_state import app_state
        camera = app_state.cameras.get(str(camera_id))
        return getattr(camera, "name", None) if camera is not None else None
    except Exception:
        return None


def template_values(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The value of every placeholder (TEMPLATE_FIELDS) for an event's message; None where it has none."""
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}

    def pick(*keys: str) -> Any:
        return next((payload[key] for key in keys if payload.get(key) is not None), None)

    def metric(key: str) -> Any:
        return payload[key] if payload.get(key) is not None else metrics.get(key)

    moment = _local_moment(payload.get("timestamp"))
    result = pick("result")
    camera_id = pick("camera_id")
    # The camera whose result rejected the product: named by a line with joined
    # cameras, else the vision camera when it found the defect.
    reject_camera = pick("reject_camera_id")
    if reject_camera is None and pick("reject_reason") == "vision_class":
        reject_camera = camera_id
    camera_name = pick("camera_name") or _camera_name(camera_id)
    if reject_camera is not None:
        reject_camera = (camera_name if reject_camera == camera_id else _camera_name(reject_camera)) or reject_camera
    values = {
        "line_id": pick("line_id"),
        "line_name": pick("line_name"),
        "line_state": pick("line_state"),
        "event": pick("event"),
        "timestamp": pick("timestamp"),
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H:%M:%S"),
        "camera_id": camera_id,
        "camera_name": camera_name,
        "track_id": pick("track_id"),
        "class_name": pick("class_name"),
        "confidence": pick("confidence"),
        "result": result,
        "result_code": {"PASSED": 1, "REJECTED": 2}.get(str(result).upper()) if result is not None else None,
        "reject_reason": pick("reject_reason"),
        "reject_camera": reject_camera,
        "code": pick("code", "qr_code"),
        "code_format": pick("format", "qr_format"),
        "code_status": pick("qr_status"),
        "product_name": pick("product_name"),
        # A message has it where the code holds more than the product; otherwise it is the code.
        "product_id": pick("product_id") or (None if payload.get("code_problem") else pick("code", "qr_code")),
        "good_count": metric("good_count"),
        "rejected_count": metric("rejected_count"),
        "total_inspected": metric("total_inspected"),
        "yield_percentage": metric("yield_percentage"),
        "products_per_minute": metric("products_per_minute"),
        "batch": pick("batch"),
        "alarm_code": pick("alarm_code"),
        "alarm_message": pick("alarm_message"),
        "input_name": pick("input_name"),
        "product_check": pick("product_check"),
        "sequence": pick("sequence"),
    }
    found = {name: values.get(name) for name in TEMPLATE_FIELDS}
    # {product.name}, {product.lane}...: the product of a known code and its values.
    found.update(flat_product_values(payload.get("product") if isinstance(payload.get("product"), dict) else None))
    # {input.<value name>}: the last value of the line's inputs; {input.value}, {input.previous}: the input that changed.
    if payload.get("line_id"):
        from app.services.plc_input_service import PLCInputService
        found.update({f"input.{name}": value for name, value in PLCInputService.values(str(payload["line_id"])).items()})
    if payload.get("event") == "INPUT_CHANGED":
        found["input.value"], found["input.previous"] = payload.get("value"), payload.get("previous")
    return found


@functools.lru_cache(maxsize=256)
def _template_parts(template: str) -> Tuple[Tuple[bool, str], ...]:
    return parse_template(template)


def render_template(template: str, payload: Dict[str, Any]) -> str:
    """A text message: the template with each {placeholder} replaced by its value ("" when it has none)."""
    values = template_values(payload)
    return "".join(
        ("" if values.get(part) is None else str(values[part])) if is_field else part
        for is_field, part in _template_parts(template)
    )


def build_message(card: dict, payload: Dict[str, Any]) -> Union[Dict[str, Any], str]:
    """The message a card sends for an event: its text, for a text card; else the fields it
    lists, or everything when it lists none."""
    if card.get("format") == "text":
        return render_template(str(card.get("template") or ""), payload)
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


def input_payload(event: dict) -> Dict[str, Any]:
    """The message of an input change: which input, its new and its old value, and the line's totals."""
    payload = {
        "event": "INPUT_CHANGED",
        "timestamp": datetime.fromtimestamp(float(event.get("timestamp") or time.time()), timezone.utc).isoformat(),
        "line_id": event.get("line_id"),
        "input_id": event.get("input_id"),
        "input_name": event.get("input_name"),
        "value": event.get("value"),
        "previous": event.get("previous"),
    }
    return SendDispatcherService._with_totals(payload, event.get("line_id"))


def timer_payload(line_id: str) -> Dict[str, Any]:
    """The message of a timer card, before the line's name, state and totals are added."""
    return {"event": "TIMER", "timestamp": datetime.now(timezone.utc).isoformat(), "line_id": line_id}


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


async def deliver(endpoint: dict, message: Union[Dict[str, Any], str], topic: str = "") -> Tuple[bool, str]:
    """Send one message to one channel (an MQTT channel: to this topic). Returns (sent, what happened).

    A dict goes out as JSON; text (a text card's message) goes out as it is.
    """
    protocol = str(endpoint.get("protocol", "")).lower()
    try:
        if protocol == "mqtt":
            from app.services.mqtt_service import MQTTChannels
            if not topic:
                return False, "This card has no MQTT topic"
            return await MQTTChannels.publish(endpoint, topic, message)
        if protocol == "tcp":
            from app.services.tcp_channels import TcpChannels, frame
            text = message if isinstance(message, str) else json.dumps(message)
            return await TcpChannels.send(endpoint, frame(text, endpoint))
        if protocol == "webhook":
            from app.services.counting_service import CountingService
            url = str(endpoint.get("url") or "")
            if not url:
                return False, "The webhook channel has no URL"
            method = str(endpoint.get("method") or "POST").upper()
            method = method if method in ("POST", "PUT") else "POST"
            headers = _webhook_headers(endpoint)
            if isinstance(message, str):
                if not any(key.lower() == "content-type" for key in headers):
                    headers["Content-Type"] = "text/plain; charset=utf-8"
                response = await CountingService.http_client().request(method, url, content=message.encode("utf-8"), headers=headers)
            else:
                response = await CountingService.http_client().request(method, url, json=message, headers=headers)
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
    # Timer cards: {(line id, card id): (what its clock depends on, its task)}, on _timer_loop.
    _timers: Dict[Tuple[str, str], Tuple[Tuple[Any, ...], "asyncio.Task[None]"]] = {}
    _timer_loop: Optional[asyncio.AbstractEventLoop] = None
    # How often a "while the line runs" timer looks whether its stopped line was started
    # by a way that does not announce it (the server starting with the line running).
    TIMER_IDLE_CHECK_S = 0.25

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
        cls._request_timer_sync()

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
        cls._request_timer_sync()

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
    def _queue(cls, card: dict, message: Union[Dict[str, Any], str]) -> bool:
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

        async def send(endpoint: dict = endpoint, message: Union[Dict[str, Any], str] = message) -> None:
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
        if isinstance(message, dict):
            message = {**message, "test": True}
        ok, detail = await deliver(endpoint, message, card_topic(card, endpoint))
        cls._record(card.get("id"), last_result={"success": ok, "message": f"Test: {detail}", "at": time.time()})
        return {"success": ok, "message": detail, "sent": message, "endpoint_name": endpoint.get("name")}

    # ── Line starts and stops, alarms ─────────────────────────────────────────

    @classmethod
    def line_state_changed(cls, event: dict) -> int:
        """A line was started or stopped (the event of card_triggers.line_state_event)."""
        # Its "while the line runs" timers start again: the first message one interval after a start.
        cls._request_timer_sync(restart_line=str(event.get("line_id") or PRIMARY_LINE_ID))
        return cls.evaluate(event, cls._with_totals(line_state_payload(event), event.get("line_id")))

    # ── Timer cards ───────────────────────────────────────────────────────────

    @classmethod
    def start_timers(cls) -> None:
        """Run the timer cards on the running event loop from now on (server start)."""
        cls._timer_loop = asyncio.get_running_loop()
        cls._sync_timers()

    @classmethod
    async def stop_timers(cls) -> None:
        """Stop every timer card (server shutdown)."""
        cls._timer_loop = None
        tasks = [task for _, task in cls._timers.values()]
        cls._timers = {}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    def _request_timer_sync(cls, restart_line: Optional[str] = None) -> None:
        """Bring the timer tasks in line with the cards, from any thread. Nothing before start_timers()."""
        loop = cls._timer_loop
        if loop is None or loop.is_closed():
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            cls._sync_timers(restart_line)
        else:
            loop.call_soon_threadsafe(cls._sync_timers, restart_line)

    @classmethod
    def _sync_timers(cls, restart_line: Optional[str] = None) -> None:
        """One task per switched-on timer card. A card whose interval, condition or line changed gets a
        new one; so do the "while the line runs" cards of ``restart_line``. Runs on _timer_loop."""
        loop = cls._timer_loop
        if loop is None:
            return
        wanted: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
        for card in cls._cards:
            if card.get("trigger_type") == TIMER_TRIGGER and card.get("enabled", True) is True and card.get("id"):
                wanted[(card_line(card), str(card["id"]))] = (timer_interval_ms(card), _timer_always(card))
        for key, (clock, task) in list(cls._timers.items()):
            restart = restart_line is not None and key[0] == restart_line and not clock[1]
            if wanted.get(key) != clock or task.done() or restart:
                task.cancel()
                del cls._timers[key]
        for key, clock in wanted.items():
            if key not in cls._timers:
                task = loop.create_task(cls._run_timer(key[0], key[1], clock[0] / 1000.0, clock[1]),
                                        name=f"send_timer_{key[1]}")
                cls._timers[key] = (clock, task)

    @staticmethod
    def _line_running(line_id: str) -> bool:
        from app.services.line_service import line_manager

        runtime = line_manager.get(line_id)
        return bool(runtime is not None and runtime.enabled)

    @classmethod
    async def _run_timer(cls, line_id: str, card_id: str, every_s: float, always: bool) -> None:
        """Send the card every ``every_s`` seconds, keeping to its schedule. Missed sends
        (a busy server) are skipped, not sent in a burst."""
        loop = asyncio.get_running_loop()
        next_at: Optional[float] = None
        while True:
            now = loop.time()
            if not always and not cls._line_running(line_id):
                next_at = None
                await asyncio.sleep(cls.TIMER_IDLE_CHECK_S)
                continue
            if next_at is None:
                next_at = now + every_s
            if now < next_at:
                await asyncio.sleep(next_at - now)
                continue
            card = next((c for c in cls._cards if card_line(c) == line_id and c.get("id") == card_id), None)
            if card is not None:
                cls.send_timer(card)
            next_at += every_s * (int((now - next_at) // every_s) + 1)

    @classmethod
    def send_timer(cls, card: dict) -> bool:
        """Queue one message of a timer card now: the line's state and totals."""
        line_id = card_line(card)
        try:
            if card.get("only_when"):
                from app.services.event_values import values_for
                from app.services.rules import rules_pass
                if not rules_pass(card["only_when"], values_for({"event_type": "timer", "line_id": line_id})):
                    return False
            payload = cls._with_line_state(cls._with_totals(timer_payload(line_id), line_id), {"line_id": line_id})
            return cls._queue(card, build_message(card, payload))
        except Exception as exc:
            logger.warning("Send card '%s' could not send: %s: %s", card.get("name") or card.get("id"), type(exc).__name__, exc)
            return False

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
                batch = getattr(runtime, "batch", "")
                if batch:
                    payload["batch"] = batch
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


def _timer_always(card: dict) -> bool:
    """A timer card that sends also while its line is stopped."""
    return str(card.get("condition") or card.get("trigger_condition") or "").lower() == "always"


def sample_payload(card: dict, line_name: str = "") -> Dict[str, Any]:
    """Example values for a card's message, for its Test button and the dialog's preview."""
    trigger = str(card.get("trigger_type") or card.get("trigger") or "cross_line").lower()
    condition = str(card.get("condition") or "").lower()
    now = datetime.now(timezone.utc).isoformat()
    line = {"line_id": card_line(card), "line_name": line_name or card_line(card), "line_state": "started",
            "batch": "B-1001"}
    totals = {
        "counts": {"bottle": 142, "defect": 3},
        "metrics": {"total_inspected": 145, "good_count": 142, "rejected_count": 3,
                    "products_per_minute": 45.2, "defect_ppm": 20689.66, "yield_percentage": 97.93},
    }
    if trigger == "line_state":
        running = condition != "off"
        return {"event": "LINE_STARTED" if running else "LINE_STOPPED", "timestamp": now, **line,
                "line_state": "started" if running else "stopped", **totals}
    if trigger == TIMER_TRIGGER:
        return {"event": "TIMER", "timestamp": now, **line, **totals}
    if trigger == "alarm":
        raised = condition != "cleared"
        return {"event": "ALARM_RAISED" if raised else "ALARM_CLEARED", "timestamp": now, **line,
                "alarm_code": "camera.disconnected", "alarm_message": "Camera 'Line camera' lost its connection",
                "alarm_severity": "critical", "alarm_source": "camera:example", "alarm_raised": raised, **totals}
    if trigger in ("qr_read", "qr_known", "qr_unknown", "qr_no_read"):
        no_read = trigger == "qr_no_read"
        known = trigger != "qr_unknown" and not no_read
        return {"event": "QR_CODE_NO_READ" if no_read else "QR_CODE_READ", "timestamp": now, **line, "camera_id": "example-camera",
                "camera_name": "Line camera",
                "code": None if no_read else "4006381333931", "format": None if no_read else "EAN13",
                "qr_status": "no_read" if no_read else ("known" if known else "unknown"), "known": known,
                "product_name": "Example product" if known else None, "paired": None}
    reject = trigger == "reject_counter" or condition == "reject"
    return {
        "event": "WIRELINE_OBJECT_CROSSED", "timestamp": now, **line, "camera_id": "example-camera", "camera_name": "Line camera",
        "track_id": 104, "class_name": "defect" if reject else "bottle",
        "result": "REJECTED" if reject else "PASSED", "is_defect": reject,
        "reject_reason": "vision_class" if reject else None, "vision_result": "REJECTED" if reject else "PASSED",
        "confidence": 0.94, "bbox": {"x1": 142, "y1": 88, "x2": 235, "y2": 340},
        "smooth_center": {"x": 188.5, "y": 214.0}, "velocity": {"vx": 0.0, "vy": 3.4}, **totals,
    }
