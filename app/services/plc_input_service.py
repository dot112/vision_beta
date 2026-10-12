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
        lock = PLCDispatcherService._endpoint_locks.setdefault(endpoint_id, asyncio.Lock())
        timeout = float(endpoint.get("timeout", 3))
        for card in cards:
            state = cls._state(card)
            # The lock is taken per read, so a PLC action never waits for more than one read.
            async with lock:
                if not driver.is_connected and not await cls._connect(endpoint, driver, now):
                    for waiting in cards:
                        cls._state(waiting).error = NOT_CONNECTED
                    return
                ok, value, message = await cls._read(driver, card, timeout)
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
    async def _read(driver: Any, card: Dict[str, Any], timeout: float) -> Tuple[bool, Any, str]:
        # A PLC counter is read as one unsigned word; the service works out its increase.
        wire_type = "uint16" if card.get("data_type") == "counter" else card.get("data_type") or "auto"
        try:
            return await asyncio.wait_for(driver.read(card["address"], wire_type), timeout=timeout + 1.0)
        except asyncio.TimeoutError:
            # A late reply could be read as the answer to the next request.
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
            f"PLC signal '{card.get('name')}' of line '{card.get('line_name')}' could not be read "
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
        if action in ("run_level", "set_batch", "alarm") or edge_matches(str(card.get("edge") or "rising"), old, new):
            cls._spawn(card, state, old, new)

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
    async def _capture(cls, card: Dict[str, Any]) -> None:
        """One picture with the card's camera: a code reader that captures when a PLC signal
        asks, or a vision camera in trigger mode, whose picture is one product."""
        from app.services.line_config import reads_codes

        runtime = cls._runtime(card)
        if runtime is None or not runtime.enabled:
            return
        camera_id = card.get("camera_id") or ""
        entry = runtime.camera_entry(camera_id)
        if entry is None:
            return
        if reads_codes(entry) and entry.get("qr_trigger") == "signal":
            from app.services.qr_service import QRReaderPipeline
            delay = int(entry.get("qr_trigger_delay_ms") or 0) / 1000.0
            QRReaderPipeline.get_worker(camera_id).request_capture(delay=delay)
        if entry.get("role", "vision") == "vision" and entry.get("count_mode") == "trigger":
            from app.services.trigger_inspection import trigger_camera
            # A camera that is not connected, or has no model, is said on the card (ValueError).
            result = await trigger_camera(runtime, camera_id)
            if result["status"] == "no_frame":
                raise ValueError(f"No new picture came from camera '{camera_id}' in time; nothing was counted")

    @staticmethod
    def _alarm(card: Dict[str, Any], on: bool) -> None:
        """An alarm with the card's own text and severity while its value is on."""
        if not on:
            alarm_manager.clear_alarm(AlarmCode.PLC_INPUT_ALARM, _alarm_source(card), "the signal went off")
            return
        alarm_manager.raise_alarm(
            AlarmCode.PLC_INPUT_ALARM, _alarm_source(card),
            str(card.get("alarm_text") or card.get("name") or "PLC signal alarm"),
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
        async with lock:
            if not driver.is_connected:
                try:
                    connected = await asyncio.wait_for(driver.connect(), timeout=timeout)
                except Exception:
                    connected = False
                if not connected:
                    detail = getattr(driver, "last_error", "") or getattr(driver, "last_connect_error", "")
                    return answer(False, None, NOT_CONNECTED + (f": {detail}" if detail else ""))
            return answer(*await cls._read(driver, card, timeout))
