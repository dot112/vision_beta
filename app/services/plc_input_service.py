"""
PLC Input Service
=================
PLC signals (inputs): the PLC sends signals into the software, per line.

Each production line has signal cards (``plc_inputs`` in its settings). A card
names a PLC channel and an address; this service reads it every poll and, when
the value changes the way the card says, the line does something: it starts
or stops, counts a product, takes a picture, resets its counters, sets its
batch number, confirms a PLC action, or raises an alarm.

One task per PLC channel reads all of that channel's cards, at the smallest
poll time among them. Every read takes the same per-channel lock the PLC
action cards write under, so a read never comes between a card's write and
its strobe. Everything here runs on the server's main event loop.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager
from app.utils.logger import get_logger

logger = get_logger(__name__)

# A PLC counter is one 16-bit register that starts again at 0 after 65535.
COUNTER_MODULUS = 65536
# More products than this between two reads is not a count: the PLC reset its
# counter (or was restarted). Nothing is counted and the new value is the
# starting point.
COUNTER_MAX_STEP = 1000

NOT_CONNECTED = "PLC not connected"

_UNSET = object()


def truthy(value: Any) -> bool:
    """Whether a value read from the PLC is "on": a set bit, a number that is not 0, text that says so."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "off", "no")
    return bool(value)


def counter_delta(previous: int, current: int) -> int:
    """How many products a PLC counter counted between two reads; 0 when it was reset."""
    delta = (int(current) - int(previous)) % COUNTER_MODULUS
    return delta if delta <= COUNTER_MAX_STEP else 0


def edge_matches(edge: str, old: Any, new: Any) -> bool:
    """Whether a change of a signal's value is the edge a card reacts to."""
    if edge == "change":
        return old != new
    was, now = truthy(old), truthy(new)
    if edge == "falling":
        return was and not now
    return now and not was


class SignalFilter:
    """The debounced value of one signal.

    The first value read is accepted at once: it is the starting point, not a
    change. After that a new value is accepted once a read at least
    ``debounce_s`` after it was first seen still shows it.
    """

    def __init__(self, debounce_s: float) -> None:
        self.debounce = max(0.0, float(debounce_s))
        self.value: Any = _UNSET
        self._candidate: Any = _UNSET
        self._since = 0.0

    @property
    def has_value(self) -> bool:
        return self.value is not _UNSET

    def update(self, value: Any, now: float) -> Optional[Tuple[Any, Any]]:
        """Take one read. Returns (old, new) when the accepted value changed, else None."""
        if self.value is _UNSET:
            self.value = value
            return None
        if value == self.value:
            self._candidate = _UNSET
            return None
        if self._candidate is _UNSET or value != self._candidate:
            self._candidate, self._since = value, now
        if now - self._since < self.debounce:
            return None
        old, self.value = self.value, value
        self._candidate = _UNSET
        return old, value


@dataclass(frozen=True)
class PLCActor:
    """Who a signal's start, stop, reset or batch change is written to the audit log as."""
    username: str                # "plc:<channel name>"
    role: str = "PLC"
    clearance_level: int = 2


@dataclass
class _InputState:
    """What is known of one card while it is being read."""
    signature: Tuple[Any, ...]
    filter: SignalFilter
    value: Any = None
    has_value: bool = False
    changed_at: Optional[float] = None       # wall-clock time the value read last changed
    last_read_at: Optional[float] = None
    read_count: int = 0
    failures: int = 0                        # reads that failed in a row
    error: Optional[str] = None              # why the last read failed
    action_error: Optional[str] = None       # why the card's last action failed; kept until one succeeds
    counter_base: Optional[int] = None


def _signature(card: Dict[str, Any]) -> Tuple[Any, ...]:
    """What makes a card's reads mean what they mean: a card changed in one of these starts again."""
    return tuple(card.get(key) for key in ("plc_endpoint_id", "address", "data_type", "edge", "debounce_ms", "action"))


def _alarm_source(card: Dict[str, Any]) -> str:
    return f"plc_input:{card.get('id', '')}"


class PLCInputService:
    """Singleton service. ``start()`` on boot, ``reload()`` when a line or a channel is saved."""

    _running: bool = False
    _all: List[Dict[str, Any]] = []                      # every line's cards, each with its line_id
    _cards: Dict[str, List[Dict[str, Any]]] = {}         # channel id -> the cards that are read
    _states: Dict[Tuple[str, str], _InputState] = {}     # (line id, card id) -> state
    _tasks: Dict[str, "asyncio.Task[None]"] = {}         # channel id -> its polling task
    _actions: Set["asyncio.Task[None]"] = set()
    _connect_tried: Dict[str, float] = {}                # channel id -> when a connect last failed
    _line_locks: Dict[str, asyncio.Lock] = {}            # line id -> one start or stop at a time

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @classmethod
    def is_running(cls) -> bool:
        return cls._running

    @classmethod
    def start(cls) -> None:
        """Start polling. Safe to call more than once."""
        cls._running = True
        cls.reload()

    @classmethod
    async def stop(cls) -> None:
        cls._running = False
        tasks, cls._tasks = list(cls._tasks.values()), {}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await cls.drain(timeout=2.0)

    @classmethod
    async def drain(cls, timeout: float = 5.0) -> None:
        """Wait until the actions that are under way are done (cancel what is left after the timeout)."""
        pending = [task for task in cls._actions if not task.done()]
        if not pending:
            return
        _, late = await asyncio.wait(pending, timeout=timeout)
        for task in late:
            task.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)

    @classmethod
    def reset(cls) -> None:
        """Test helper: forget every card, state and task."""
        for task in list(cls._tasks.values()) + list(cls._actions):
            if not task.done():
                try:
                    task.cancel()
                except RuntimeError:
                    pass        # its loop is closed already
        cls._running = False
        cls._all, cls._cards, cls._states, cls._tasks = [], {}, {}, {}
        cls._actions, cls._connect_tried, cls._line_locks = set(), {}, {}

    # ── Configuration ─────────────────────────────────────────────────────────

    @classmethod
    def reload(cls) -> None:
        """Read every line's signal cards again; start and stop the polling tasks to match.

        A card that was changed, removed or switched off loses what was known
        of it, and its alarms are cleared: nothing would clear them later.
        """
        from app.services.settings_persistence_service import SettingsPersistenceService

        cards: List[Dict[str, Any]] = []
        try:
            for line in SettingsPersistenceService.get_lines():
                for card in line.get("plc_inputs") or []:
                    cards.append({**card, "line_id": line["id"], "line_name": line.get("name") or line["id"]})
        except Exception:
            logger.exception("Could not read the lines' PLC signal cards")
            return
        by_channel: Dict[str, List[Dict[str, Any]]] = {}
        live: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
        for card in cards:
            if cls._config_problem(card) is None:
                by_channel.setdefault(card["plc_endpoint_id"], []).append(card)
                live[(card["line_id"], card["id"])] = _signature(card)
        cls._all, cls._cards = cards, by_channel
        for key in list(cls._states):
            if live.get(key) != cls._states[key].signature:
                del cls._states[key]
        for alarm in alarm_manager.active("plc_input:"):
            details = alarm.details or {}
            if (details.get("line_id"), details.get("input_id")) not in cls._states:
                alarm_manager.clear_alarm(alarm.code, alarm.source, "the signal card was changed, removed or switched off")
        for channel in list(cls._connect_tried):
            if channel not in by_channel:
                del cls._connect_tried[channel]
        cls._sync_tasks()

    @staticmethod
    def _config_problem(card: Dict[str, Any]) -> Optional[str]:
        """Why a card is not read at all, or None when it is."""
        if card.get("enabled", True) is not True:
            return "switched off"
        if not card.get("plc_endpoint_id"):
            return "No PLC channel is chosen for this signal"
        if not card.get("address"):
            return "This signal has no address"
        return None

    @classmethod
    def _sync_tasks(cls) -> None:
        if not cls._running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for channel in list(cls._tasks):
            if channel not in cls._cards or cls._tasks[channel].done():
                cls._tasks.pop(channel).cancel()
        for channel in cls._cards:
            if channel not in cls._tasks:
                cls._tasks[channel] = loop.create_task(cls._run(channel), name=f"plc_inputs_{channel}")

    @classmethod
    def poll_interval(cls, endpoint_id: str) -> float:
        """Seconds between two passes over a channel: the smallest poll time of its cards."""
        times = [int(card.get("poll_ms") or 100) for card in cls._cards.get(endpoint_id) or []]
        return (min(times) if times else 100) / 1000.0

    # ── Polling ───────────────────────────────────────────────────────────────

    @classmethod
    async def _run(cls, endpoint_id: str) -> None:
        while cls._cards.get(endpoint_id):
            started = time.monotonic()
            try:
                await cls.poll_once(endpoint_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("PLC signals: a pass over channel %s failed", endpoint_id)
            await asyncio.sleep(max(0.005, cls.poll_interval(endpoint_id) - (time.monotonic() - started)))

    @staticmethod
    def _endpoint(endpoint_id: str) -> Optional[Dict[str, Any]]:
        from app.services.settings_persistence_service import SettingsPersistenceService
        return next((ep for ep in SettingsPersistenceService.get_endpoints(protocol="plc")
                     if ep.get("id") == endpoint_id), None)

    @classmethod
    def _state(cls, card: Dict[str, Any]) -> _InputState:
        key = (card["line_id"], card["id"])
        state = cls._states.get(key)
        if state is None:
            state = cls._states[key] = _InputState(
                signature=_signature(card),
                filter=SignalFilter(int(card.get("debounce_ms") or 0) / 1000.0),
            )
        return state

    @classmethod
    def _channel_problem(cls, endpoint_id: str) -> Tuple[Optional[Dict[str, Any]], Any, Optional[str]]:
        """(endpoint, driver, None) for a channel that can be read, else (.., .., why not)."""
        from app.hardware.plc.factory import PLCDriverFactory

        endpoint = cls._endpoint(endpoint_id)
        if endpoint is None:
            return None, None, "Its PLC channel no longer exists"
        if endpoint.get("enabled", True) is not True:
            return endpoint, None, "Its PLC channel is switched off"
        try:
            return endpoint, PLCDriverFactory.get_driver(endpoint), None
        except ValueError as exc:
            return endpoint, None, str(exc)

    @classmethod
    async def poll_once(cls, endpoint_id: str, now: Optional[float] = None) -> None:
        """Read every card of one channel once and act on what changed."""
        from app.services.plc_dispatcher_service import PLCDispatcherService

        cards = list(cls._cards.get(endpoint_id) or [])
        if not cards:
            return
        now = time.monotonic() if now is None else now
        endpoint, driver, problem = cls._channel_problem(endpoint_id)
        if problem is not None:
            for card in cards:
                cls._state(card).error = problem
            return
        # The channel's lock keeps one output action's writes together (data, strobe, pulse).
        # A read does not need it: the driver sends one request at a time, and a read
        # must see what a pulse switches on. Connecting and disconnecting take it, so
        # neither cuts an output action in half.
        lock = PLCDispatcherService._endpoint_locks.setdefault(endpoint_id, asyncio.Lock())
        timeout = float(endpoint.get("timeout", 3))
        for card in cards:
            state = cls._state(card)
            if not driver.is_connected:
                async with lock:
                    connected = driver.is_connected or await cls._connect(endpoint, driver, now)
                if not connected:
                    for waiting in cards:
                        cls._state(waiting).error = NOT_CONNECTED
                    return
            ok, value, message = await cls._read(driver, card, timeout, lock)
            if ok:
                cls._took(card, state, value, now)
            else:
                cls._failed(card, state, message, endpoint, driver, now)

    @classmethod
    async def _connect(cls, endpoint: Dict[str, Any], driver: Any, now: float) -> bool:
        """Connect, but not more often than the channel's reconnect interval: a poll must not hammer a dead PLC."""
        endpoint_id = endpoint.get("id", "")
        tried = cls._connect_tried.get(endpoint_id)
        if tried is not None and now - tried < max(1.0, float(endpoint.get("reconnect_interval", 5) or 5)):
            return False
        try:
            connected = await asyncio.wait_for(driver.connect(), timeout=float(endpoint.get("timeout", 3)))
        except Exception:
            connected = False
        if connected:
            cls._connect_tried.pop(endpoint_id, None)
        else:
            cls._connect_tried[endpoint_id] = now
        return bool(connected)

    @staticmethod
    async def _read(driver: Any, card: Dict[str, Any], timeout: float,
                    lock: asyncio.Lock) -> Tuple[bool, Any, str]:
        # A PLC counter is read as one unsigned word; the service works out its increase.
        wire_type = "uint16" if card.get("data_type") == "counter" else card.get("data_type") or "auto"
        try:
            return await asyncio.wait_for(driver.read(card["address"], wire_type), timeout=timeout + 1.0)
        except asyncio.TimeoutError:
            # A late reply could be read as the answer to the next request. The
            # connection is dropped once no output action is half done.
            async with lock:
                try:
                    await asyncio.wait_for(driver.disconnect(), timeout=1.0)
                except Exception:
                    pass
                driver.is_connected = False
            return False, None, "The PLC did not answer the read in time"
        except Exception as exc:
            return False, None, f"{type(exc).__name__}: {exc}"

    @classmethod
    def _failed(cls, card: Dict[str, Any], state: _InputState, message: str,
                endpoint: Dict[str, Any], driver: Any, now: float) -> None:
        state.last_read_at = time.time()
        state.error = message
        state.failures += 1
        if not driver.is_connected:
            # The link went down with this read: outputs are in an unknown state,
            # and the reconnect waits like any other.
            from app.services.plc_failsafe_service import PLCFailsafeService
            PLCFailsafeService.mark_link_lost(endpoint.get("id", ""))
            cls._connect_tried[endpoint.get("id", "")] = now
            if state.failures < 2:
                return      # one lost link is the PLC connection's own alarm
        alarm_manager.raise_alarm(
            AlarmCode.PLC_INPUT_FAILED, _alarm_source(card),
            f"Input '{card.get('name')}' of line '{card.get('line_name')}' could not be read "
            f"from {card.get('address')} on {endpoint.get('name') or endpoint.get('id')}: {message}",
            AlarmSeverity.WARNING,
            {"line_id": card["line_id"], "input_id": card["id"], "endpoint_id": endpoint.get("id"),
             "address": card.get("address")},
        )

    @classmethod
    def _took(cls, card: Dict[str, Any], state: _InputState, value: Any, now: float) -> None:
        """One good read: keep it for the status and act on what it changed."""
        stamp = time.time()
        if state.error is not None or state.failures:
            alarm_manager.clear_alarm(AlarmCode.PLC_INPUT_FAILED, _alarm_source(card), "the signal was read again")
        state.error, state.failures = None, 0
        state.last_read_at = stamp
        state.read_count += 1
        if state.has_value and value != state.value:
            state.changed_at = stamp
        state.value, state.has_value = value, True

        action = card.get("action")
        if card.get("data_type") == "counter":
            try:
                current = int(value)
            except (TypeError, ValueError):
                state.error = f"The PLC counter read '{value}', which is not a number"
                return
            base, state.counter_base = state.counter_base, current
            if base is None or current == base:
                return
            count = counter_delta(base, current)
            if count:
                cls._spawn(card, state, base, current, count)
            else:
                logger.warning(
                    "PLC signal '%s' of line '%s': the PLC counter went from %d to %d, more than %d in one read; "
                    "taken as a reset of the counter, nothing is counted",
                    card.get("name"), card.get("line_name"), base, current, COUNTER_MAX_STEP,
                )
            return

        first = not state.filter.has_value
        change = state.filter.update(value, now)
        if first:
            # The starting point runs no action: a start bit that is already on must not
            # start a line when the server boots. A level is different: an alarm that is
            # on is raised at once, and the batch the PLC holds is the line's batch.
            if (action == "alarm" and truthy(value)) or action == "set_batch":
                cls._spawn(card, state, None, value)
            return
        if change is None:
            return
        old, new = change
        # Every accepted change goes to the cards with the trigger "Input changed", whatever the input's own action.
        cls._publish_input(card, old, new)
        # And to the line itself (its result handshake's acknowledge).
        runtime = cls._runtime(card)
        if runtime is not None and hasattr(runtime, "on_input_change"):
            try:
                runtime.on_input_change(card.get("id"), old, new, str(card.get("edge") or "rising"))
            except Exception:
                logger.exception("Input '%s' of line '%s': the line could not take its change",
                                 card.get("name"), card.get("line_name"))
        if action == "none":
            return
        if action in ("run_level", "set_batch", "alarm") or edge_matches(str(card.get("edge") or "rising"), old, new):
            cls._spawn(card, state, old, new)

    @classmethod
    def _publish_input(cls, card: Dict[str, Any], old: Any, new: Any) -> None:
        """Send an input's change to its line's output actions and send cards (called on the event loop)."""
        from app.services.card_triggers import input_event
        from app.services.plc_dispatcher_service import PLCDispatcherService
        from app.services.send_dispatcher_service import SendDispatcherService, input_payload

        event = input_event(card, old, new)
        try:
            task = asyncio.get_running_loop().create_task(PLCDispatcherService.evaluate(event), name=f"plc_input_event_{card.get('id')}")
            cls._actions.add(task)
            task.add_done_callback(cls._actions.discard)
        except RuntimeError:
            pass        # no loop: nothing to fire the output actions on
        try:
            SendDispatcherService.evaluate(event, input_payload(event))
        except Exception as exc:
            logger.warning("PLC signal '%s' of line '%s': its change could not be sent: %s",
                           card.get("name"), card.get("line_name"), exc)

    @classmethod
    def _spawn(cls, card: Dict[str, Any], state: _InputState, old: Any, new: Any, count: int = 1) -> None:
        """Run a card's action on the loop without holding up the poll."""
        async def guarded() -> None:
            try:
                await cls._act(card, old, new, count)
                state.action_error = None
            except asyncio.CancelledError:
                raise
            except ValueError as exc:
                # The value cannot be used (a batch that is too long, for one): said on the card.
                state.action_error = str(exc)
                logger.warning("PLC signal '%s' of line '%s': %s", card.get("name"), card.get("line_name"), exc)
            except Exception as exc:
                state.action_error = f"Its action failed: {getattr(exc, 'detail', None) or exc}"
                logger.exception("PLC signal '%s' of line '%s': the action '%s' failed",
                                 card.get("name"), card.get("line_name"), card.get("action"))

        task = asyncio.get_running_loop().create_task(guarded(), name=f"plc_input_{card.get('id')}")
        cls._actions.add(task)
        task.add_done_callback(cls._actions.discard)

    # ── Actions ───────────────────────────────────────────────────────────────

    @classmethod
    async def _act(cls, card: Dict[str, Any], old: Any, new: Any, count: int = 1) -> None:
        """What the line does for one change of a card's value (``count``: products, for a PLC counter)."""
        action = card.get("action")
        logger.debug("PLC signal '%s': %r -> %r (action %s)", card.get("name"), old, new, action)
        if action in ("start_line", "stop_line", "run_level"):
            await cls._set_running(card, truthy(new) if action == "run_level" else action == "start_line")
        elif action == "reset_counters":
            cls._reset_counters(card)
        elif action == "product_sensor":
            runtime = cls._runtime(card)
            for _ in range(max(1, int(count))) if runtime is not None else ():
                runtime.sensor_product()
        elif action == "capture":
            await cls._capture(card)
        elif action == "alarm":
            cls._alarm(card, truthy(new))
        elif action == "confirm_card":
            # The PLC says the output really happened (a reject gate's sensor, for one): the
            # oldest waiting fire of every PLC action that names this signal is confirmed.
            from app.services.plc_dispatcher_service import PLCDispatcherService
            PLCDispatcherService.confirm_signal(card["line_id"], card["id"])
        elif action == "set_batch":
            # The value the PLC holds is the line's batch number. One that cannot be a
            # batch (too long, a control character, a bit) is refused: the line keeps its own.
            from app.routes.v1.lines import set_line_batch
            set_line_batch(card["line_id"], new, cls._actor(card))

    @staticmethod
    def _runtime(card: Dict[str, Any]) -> Any:
        import app.services.line_service as line_service
        return line_service.line_manager.get(card["line_id"])

    @classmethod
    def _actor(cls, card: Dict[str, Any]) -> PLCActor:
        """Who the audit log names for what a signal did: the PLC channel it came from."""
        endpoint = cls._endpoint(card.get("plc_endpoint_id") or "") or {}
        return PLCActor(username=f"plc:{endpoint.get('name') or card.get('plc_endpoint_id')}")

    @classmethod
    async def _set_running(cls, card: Dict[str, Any], running: bool) -> None:
        """Start or stop the card's line, unless it is in that state already."""
        from app.routes.v1.lines import set_line_running
        from app.services.settings_persistence_service import SettingsPersistenceService

        line_id = card["line_id"]
        # One at a time per line: a start connects cameras, and a stop must not run into it.
        async with cls._line_locks.setdefault(line_id, asyncio.Lock()):
            line = SettingsPersistenceService.get_line(line_id, with_logic=False)
            if line is None or bool(line.get("enabled")) == bool(running):
                return
            await set_line_running(line_id, bool(running), cls._actor(card))
            logger.info("PLC signal '%s': line '%s' %s", card.get("name"), card.get("line_name"),
                        "started" if running else "stopped")

    @classmethod
    def _reset_counters(cls, card: Dict[str, Any]) -> None:
        from app.routes.v1.counting import reset_line_counters

        runtime = cls._runtime(card)
        if runtime is None:
            return
        with runtime._lock:
            codes = any(runtime.qr_stats.values())
        if runtime.counter.total_inspected == 0 and not codes \
                and all(aux.total_inspected == 0 for aux in runtime.aux_counters.values()):
            return      # nothing was counted since the last reset
        reset_line_counters(runtime, cls._actor(card))
        logger.info("PLC signal '%s': counters of line '%s' reset", card.get("name"), card.get("line_name"))

    @classmethod
    async def _capture(cls, card: Dict[str, Any]) -> List[Dict[str, Any]]:
        """One picture with every camera of the line that ticked the card's trigger number, all at once.

        Returns what each camera did. A camera that cannot take its picture does not stop the
        others; their reasons are raised together as one ValueError, which the card shows.
        """
        from app.services.line_config import triggered_cameras

        runtime = cls._runtime(card)
        if runtime is None or not runtime.enabled:
            return []
        number = card.get("trigger_no")
        cameras = triggered_cameras(runtime.cameras, card)
        if not cameras:
            raise ValueError(f"No camera of this line uses trigger {number}: tick it on a camera's card, "
                             "under Counting or Read codes")
        # What travels with this request into the product it makes: the carton's sequence number.
        context: Dict[str, Any] = {}
        sequence = await cls.read_sequence(runtime)
        if sequence is not None:
            context["sequence"] = sequence
        # The line's result handshake, when this is its capture input.
        request = runtime.capture_request(card.get("id"), sequence) if hasattr(runtime, "capture_request") else None
        if request is not None:
            if request["action"] == "duplicate":
                return [{"status": "duplicate", "trigger_no": number, "sequence": sequence}]
            context["request_id"] = request["request_id"]
        done = await asyncio.gather(
            *(cls._capture_camera(runtime, camera_id, number, context) for camera_id in cameras), return_exceptions=True,
        )
        for result in done:
            if isinstance(result, BaseException) and not isinstance(result, ValueError):
                raise result
        problems = [str(result) for result in done if isinstance(result, ValueError)]
        if problems:
            raise ValueError("; ".join(problems))
        return [result for result in done if result is not None]

    @classmethod
    async def read_sequence(cls, runtime: Any) -> Any:
        """The carton's sequence number when a "Take a picture" input fires: the line's sequence input,
        read once, now. None when the line has none, or it cannot be read.

        A fresh read, not the last polled value: the PLC sets the sequence and the capture request in
        the same scan, and the poll may not have read the sequence input of that pass yet. When the
        read fails, the polled value stands in only while the input reads fine.
        """
        input_id = getattr(runtime, "sequence_input_id", None)
        if not input_id:
            return None
        card = next((c for c in cls._all if c.get("line_id") == runtime.id and c.get("id") == input_id), None)
        if card is None or cls._config_problem(card) is not None:
            return None
        try:
            answer = await cls.read_now(card)
        except Exception as exc:
            answer = {"ok": False, "message": str(exc)}
        if answer.get("ok"):
            return answer.get("value")
        logger.warning("Line '%s': the product sequence could not be read now (%s); the last value read is used, if any",
                       card.get("line_name"), answer.get("message"))
        return cls.value_of(runtime.id, input_id)

    @classmethod
    def value_of(cls, line_id: str, input_id: Optional[str]) -> Any:
        """The last accepted value of one input of a line; None when it has none, or its last read failed."""
        state = cls._states.get((line_id, input_id or ""))
        if state is None or not state.filter.has_value or state.error is not None:
            return None
        return state.filter.value

    @classmethod
    async def _capture_camera(cls, runtime: Any, camera_id: str, trigger_no: Optional[int],
                              context: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """One picture with one camera: a code reader that captures when an input asks, or a
        vision camera in trigger mode, whose picture is one product.

        For a camera that takes its own pictures with a trigger notice, the input with the notice's
        number only says a picture is coming: the line pairs it with the camera's picture by time
        (LineRuntime.trigger_notice), and the answer is ``status`` "expecting".

        ``context``: what travels with the request into the product (``sequence``, ``request_id``)."""
        context = context or {}
        import app.services.line_service as line_service
        import app.services.trigger_inspection as trigger_inspection
        from app.services.line_config import reads_codes
        from app.state.application_state import app_state

        entry = runtime.camera_entry(camera_id)
        if entry is None:
            return None
        answer: Dict[str, Any] = {"camera_id": camera_id, "status": "requested", "trigger_no": trigger_no}
        if trigger_no is not None and trigger_no == entry.get("notice_trigger") and runtime.has_notice(camera_id):
            engine = line_service.line_manager.engine_for_camera(camera_id)[0]
            if not getattr(engine, "is_loaded", True):
                raise ValueError(f"Camera '{camera_id}' has no vision model loaded, so its picture cannot be inspected")
            runtime.trigger_notice(camera_id, trigger_no, context)
            return {"camera_id": camera_id, "status": "expecting", "trigger_no": trigger_no}
        if getattr(app_state.cameras.get(camera_id), "trigger_mode", "off") == "hardware":
            # Its pictures are used as they come; reading the last one again would count it twice.
            raise ValueError(f"Camera '{camera_id}' is triggered by its own input, so this input is not obeyed: "
                             "every picture the camera takes is used already")
        if reads_codes(entry) and entry.get("qr_trigger") == "signal":
            from app.services.qr_service import QRReaderPipeline
            delay = int(entry.get("qr_trigger_delay_ms") or 0) / 1000.0
            QRReaderPipeline.get_worker(camera_id).request_capture(delay=delay, trigger_no=trigger_no, **context)
        if entry.get("role", "vision") == "vision" and entry.get("count_mode") == "trigger":
            # A camera without a model is said on the card (ValueError). A picture that does not come
            # (no frame in time, the camera not connected) is an answer too: a "no picture" reject.
            answer = await trigger_inspection.trigger_camera(runtime, camera_id, trigger_no=trigger_no, **context)
        return answer

    @staticmethod
    def _alarm(card: Dict[str, Any], on: bool) -> None:
        """An alarm with the card's own text and severity while its value is on."""
        if not on:
            alarm_manager.clear_alarm(AlarmCode.PLC_INPUT_ALARM, _alarm_source(card), "the signal went off")
            return
        alarm_manager.raise_alarm(
            AlarmCode.PLC_INPUT_ALARM, _alarm_source(card),
            str(card.get("alarm_text") or card.get("name") or "Input alarm"),
            AlarmSeverity(card.get("alarm_severity") or "warning"),
            {"line_id": card["line_id"], "input_id": card["id"], "endpoint_id": card.get("plc_endpoint_id"),
             "address": card.get("address")},
        )

    # ── Views ─────────────────────────────────────────────────────────────────

    @classmethod
    def get_status(cls, line_id: str) -> List[Dict[str, Any]]:
        """What each of a line's cards last read: its value, when it changed, how often it was read, its error."""
        rows = []
        for card in cls._all:
            if card.get("line_id") != line_id:
                continue
            state = cls._states.get((line_id, card["id"]))
            problem = cls._config_problem(card)
            error = (state.error or state.action_error) if state is not None else None
            if state is None and problem is not None and card.get("enabled", True) is True:
                error = problem
            rows.append({
                "id": card["id"],
                "name": card.get("name"),
                "enabled": card.get("enabled", True) is True,
                "action": card.get("action"),
                "value": state.value if state is not None and state.has_value else None,
                "changed_at": state.changed_at if state is not None else None,
                "last_read_at": state.last_read_at if state is not None else None,
                "read_count": state.read_count if state is not None else 0,
                "error": error,
            })
        return rows

    @classmethod
    def values(cls, line_id: str) -> Dict[str, Any]:
        """{value name: last accepted value} of a line's inputs that have a value name and have been read.

        An input whose last read failed (its PLC is down, the address cannot be read) has no
        value until it is read again: an old value must not stand in for one nobody can see.
        """
        found: Dict[str, Any] = {}
        for card in cls._all:
            name = card.get("value_name")
            if card.get("line_id") != line_id or not name:
                continue
            state = cls._states.get((line_id, card["id"]))
            if state is not None and state.filter.has_value and state.error is None:
                found[name] = state.filter.value
        return found

    @classmethod
    async def read_now(cls, card: Dict[str, Any]) -> Dict[str, Any]:
        """Read a card's address once, now (the dialog's "Read now"; the card need not be saved)."""
        from app.services.plc_dispatcher_service import PLCDispatcherService

        def answer(ok: bool, value: Any, message: str) -> Dict[str, Any]:
            return {"ok": ok, "value": value, "message": message}

        if not card.get("plc_endpoint_id"):
            return answer(False, None, "Choose the PLC channel of this signal first.")
        if not card.get("address"):
            return answer(False, None, "Enter the address of this signal first.")
        endpoint, driver, problem = cls._channel_problem(card["plc_endpoint_id"])
        if problem is not None:
            return answer(False, None, problem)
        timeout = float(endpoint.get("timeout", 3))
        lock = PLCDispatcherService._endpoint_locks.setdefault(endpoint.get("id", ""), asyncio.Lock())
        if not driver.is_connected:
            async with lock:
                try:
                    connected = driver.is_connected or await asyncio.wait_for(driver.connect(), timeout=timeout)
                except Exception:
                    connected = False
            if not connected:
                detail = getattr(driver, "last_error", "") or getattr(driver, "last_connect_error", "")
                return answer(False, None, NOT_CONNECTED + (f": {detail}" if detail else ""))
        return answer(*await cls._read(driver, card, timeout, lock))
