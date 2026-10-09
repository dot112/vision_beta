"""
PLC Dispatcher Service
======================
Runtime engine that:
  1. Holds the current set of PLC Action Cards in memory.
  2. Receives vision events (line crossings, counter increments, class detections).
  3. Evaluates each card's trigger/condition/re-arm/execution-policy.
  4. Dispatches the configured PLC operation through the appropriate hardware driver.
  5. Maintains detailed per-card execution status (idle / queued / executing / sent / failed / timeout).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

# When a card fires is decided in card_triggers, the same for PLC cards and
# send cards. The names below are what this module called them before that.
from app.services.card_triggers import (  # noqa: F401  (re-exported)
    ALARM_EVENT,
    ANY_LINE,
    LINE_STATE_EVENT,
    alarm_event,
    card_alarm_codes,
    card_fires,
    card_line as _card_line,
    camera_matches as _camera_matches,
    eval_counter as _eval_counter,
    line_state_event,
    normalize_trigger,
    trigger_matches as _eval_condition,
)
from app.services.line_config import OTHER_REJECT_REASON_CODE, PRIMARY_LINE_ID, REJECT_REASON_CODES
from app.services.plc_failsafe_service import PLCFailsafeService
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ── Card Runtime State ────────────────────────────────────────────────────────

@dataclass
class _CardState:
    card_id: str
    status: str = "idle"                # idle|queued|executing|sent|failed|timeout
    last_fired_at: float = 0.0          # epoch seconds
    # Insertion-ordered so the oldest IDs are evicted first (a set would drop an arbitrary one,
    # possibly the event just fired, and let once_per_event fire it twice).
    fired_event_ids: Dict[str, None] = field(default_factory=dict)
    last_result: Dict[str, Any] = field(default_factory=dict)


# ── Field-Name Normalization (UI → Backend) ───────────────────────────────────

def _normalize_card(card: dict) -> dict:
    """
    Translate frontend field names to backend field names.

    The dashboard stores cards with short names (trigger, condition, delay_ms,
    debounce_ms, duration_ms, exec_policy). The trigger and condition are
    translated by card_triggers.normalize_trigger, shared with send cards;
    the PLC-only fields are translated here.

    This function returns a *copy* with all names normalised so the rest of the
    dispatcher can work with one canonical naming convention.
    """
    c = normalize_trigger(card)

    # ── Timing fields ─────────────────────────────────────────────────────────
    if "duration_ms" in c and "pulse_duration_ms" not in c:
        c["pulse_duration_ms"] = c["duration_ms"]
    if "delay_ms" in c and "travel_delay_ms" not in c:
        c["travel_delay_ms"] = c["delay_ms"]
    if "debounce_ms" in c and "rearm_lockout_ms" not in c:
        c["rearm_lockout_ms"] = c["debounce_ms"]

    # ── Execution policy ──────────────────────────────────────────────────────
    if "exec_policy" in c and "execution_policy" not in c:
        c["execution_policy"] = c["exec_policy"]

    # ── Operation normalisation (UI uses lowercase) ────────────────────────────
    if "operation" in c:
        c["operation"] = str(c["operation"]).upper()

    return c


# ── Dispatcher ────────────────────────────────────────────────────────────────

class PLCDispatcherService:
    """
    Singleton service.  Call `load_cards()` on startup and
    `evaluate(event)` from counting_service on every trigger event.
    """

    _cards: List[dict] = []                  # raw card dicts from persistence
    _states: Dict[str, _CardState] = {}      # keyed by card["id"]
    _lock: asyncio.Lock = None               # type: ignore[assignment]
    _endpoint_locks: Dict[str, asyncio.Lock] = {}
    _dispatch_tasks: Set[asyncio.Task] = set()     # every line's in-flight dispatches
    _line_tasks: Dict[str, Set[asyncio.Task]] = {}  # the same tasks, per line
    # Per line, so a busy line cannot crowd out the others.
    _max_dispatch_tasks = 256

    # ── Initialisation ────────────────────────────────────────────────────────

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    def load_cards(cls) -> None:
        """Load PLC action cards from persistence. Safe to call multiple times."""
        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            raw_cards = SettingsPersistenceService.get_all_plc_actions()
            cls._cards = [_normalize_card(c) for c in raw_cards]
            # Init state for any new card IDs
            for card in cls._cards:
                cid = card.get("id", "")
                if cid and cid not in cls._states:
                    cls._states[cid] = _CardState(card_id=cid)
            logger.info("PLCDispatcherService: loaded %d PLC action card(s)", len(cls._cards))
        except Exception as exc:
            logger.warning("PLCDispatcherService.load_cards error: %s", exc)

    @classmethod
    async def autoconnect_drivers(cls) -> None:
        """
        Background task: attempt to pre-connect PLC drivers for all enabled cards
        so that the first real event fires without connection latency.
        """
        await asyncio.sleep(3.0)  # let other startup tasks settle first
        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            endpoints = {
                ep["id"]: ep
                for ep in SettingsPersistenceService.get_endpoints(protocol="plc")
                if ep.get("enabled", True) is True
            }
            from app.hardware.plc.factory import PLCDriverFactory
            for card in cls._cards:
                if card.get("enabled", True) is not True:
                    continue
                ep_id = card.get("plc_endpoint_id", "")
                if ep_id and ep_id in endpoints:
                    driver = PLCDriverFactory.get_driver(endpoints[ep_id])
                    if not driver.is_connected:
                        ok = await driver.connect()
                        logger.info(
                            "PLCDispatcher pre-connect '%s' → %s",
                            endpoints[ep_id].get("name", ep_id),
                            "OK" if ok else "FAILED",
                        )
        except Exception as exc:
            logger.debug("PLCDispatcher autoconnect error: %s", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    @classmethod
    def set_cards(cls, cards: List[dict], line_id: Optional[str] = None) -> None:
        """Hot-reload one line's cards (Line 1 when no line is named); other lines keep theirs."""
        line_id = line_id or PRIMARY_LINE_ID
        fresh = []
        for c in cards:
            card = _normalize_card(c)
            card["line_id"] = line_id
            fresh.append(card)
        before = {c.get("id") for c in cls._cards if _card_line(c) == line_id}
        cls._cards = [c for c in cls._cards if _card_line(c) != line_id] + fresh
        # A deleted or switched-off action never fires again, so its failure alarm could never clear.
        live = {c.get("id") for c in fresh if c.get("enabled", True) is True}
        from app.events.alarm_events import alarm_manager
        for cid in (before | {c.get("id") for c in fresh}) - live:
            if cid:
                alarm_manager.clear_source(f"plc_card:{cid}", "action deleted or switched off")
        for card in cls._cards:
            cid = card.get("id", "")
            if cid and cid not in cls._states:
                cls._states[cid] = _CardState(card_id=cid)

    @classmethod
    def get_status(cls, card_id: Optional[str] = None, line_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return live status dicts for one or all cards, optionally of one line."""
        statuses = []
        for card in cls._cards:
            cid = card.get("id", "")
            if card_id and cid != card_id:
                continue
            if line_id and _card_line(card) != line_id:
                continue
            state = cls._states.get(cid, _CardState(card_id=cid))
            statuses.append({
                "card_id": cid,
                "name": card.get("name", ""),
                "enabled": card.get("enabled", True),
                "status": state.status,
                "last_result": state.last_result,
                "last_fired_at": state.last_fired_at or None,
                "line_id": _card_line(card),
            })
        return statuses

    @classmethod
    async def evaluate(cls, event: dict) -> None:
        """
        Entry point called by counting_service for every trigger event.
        Fires matching card dispatches as background tasks (non-blocking).
        """
        if not cls._cards:
            return
        now = time.monotonic()
        event_id = str(event.get("event_id", ""))

        for card in cls._cards:
            if card.get("enabled", True) is not True:
                continue
            cid = card.get("id", "")
            if not cid:
                continue
            # ── Does the card fire for this event? (shared with send cards) ────
            if not card_fires(card, event):
                continue
            card_line = _card_line(card)

            # ── Re-arm lockout ────────────────────────────────────────────────
            state = cls._states.setdefault(cid, _CardState(card_id=cid))
            rearm_ms = int(card.get("rearm_lockout_ms", 100) or 0)
            if rearm_ms > 0 and (now - state.last_fired_at) < (rearm_ms / 1000.0):
                logger.debug("PLCDispatcher: card '%s' re-arm lockout active (%.0f ms remaining)",
                             card.get("name", cid),
                             rearm_ms - (now - state.last_fired_at) * 1000)
                continue

            # ── Execution policy ──────────────────────────────────────────────
            policy = str(card.get("execution_policy", "once_per_event")).lower()
            if policy == "once_per_event" and event_id and event_id in state.fired_event_ids:
                continue

            # ── Dispatch ──────────────────────────────────────────────────────
            state.last_fired_at = now
            if event_id:
                state.fired_event_ids[event_id] = None
                # Keep bounded (last 500 event IDs), evicting the oldest
                while len(state.fired_event_ids) > 500:
                    state.fired_event_ids.pop(next(iter(state.fired_event_ids)))

            line_tasks = cls._line_tasks.setdefault(card_line, set())
            if len(line_tasks) >= cls._max_dispatch_tasks:
                state.status = "failed"
                state.last_result = {"success": False, "message": "PLC dispatch capacity reached for this line; event was dropped safely."}
                cls._report_alarm(card, {"id": card.get("plc_endpoint_id")}, "failed", state.last_result["message"])
                cls._notify_status_change()
                continue
            task = asyncio.create_task(
                cls._dispatch(card, state, event),
                name=f"plc_dispatch_{cid}",
            )
            cls._dispatch_tasks.add(task)
            line_tasks.add(task)
            task.add_done_callback(cls._dispatch_tasks.discard)
            task.add_done_callback(line_tasks.discard)

    @classmethod
    def watch_alarms(cls) -> None:
        """Run "Alarm" cards when an alarm is raised or cleared. Safe to call more than once."""
        from app.events.event_bus import event_bus
        from app.events.system_events import SystemEventType

        event_bus.subscribe(SystemEventType.ALARM_RAISED, cls._on_alarm_raised)
        event_bus.subscribe(SystemEventType.ALARM_CLEARED, cls._on_alarm_cleared)

    @classmethod
    async def _on_alarm_raised(cls, alarm: dict) -> None:
        await cls._on_alarm(alarm, raised=True)

    @classmethod
    async def _on_alarm_cleared(cls, alarm: dict) -> None:
        await cls._on_alarm(alarm, raised=False)

    @classmethod
    async def _on_alarm(cls, alarm: dict, raised: bool) -> None:
        if not any(c.get("trigger_type") == "alarm" and c.get("enabled", True) is True for c in cls._cards):
            return
        from app.events.alarm_events import alarm_manager

        event = alarm_event(alarm, raised, [a.to_dict() for a in alarm_manager.active()])
        if not event["line_id"]:
            return  # a line alarm on no line (a camera no line uses) concerns no line's cards
        await cls.evaluate(event)

    @classmethod
    async def dispatch_manual(cls, card: dict) -> Dict[str, Any]:
        """
        Manually fire a card's PLC action (test button).
        Returns the execution result dict synchronously.

        A card that writes a value of the product writes example values:
        a rejected product, counts of 1, class number 1.
        """
        cid = card.get("id", "")
        state = cls._states.setdefault(cid, _CardState(card_id=cid))
        event = {"event_id": f"manual_{int(time.time())}", "manual": True, "result": "reject", "reject_reason": "vision_class",
                 "good_count": 1, "reject_count": 1, "total_count": 1, "class_index": 1}
        await cls._dispatch(card, state, event=event)
        return {**state.last_result, "status": state.status}

    @staticmethod
    def write_value(card: dict, event: dict) -> float:
        """What a card writes for an event (its value_source). Raises ValueError when the event has no such value."""
        source = str(card.get("value_source") or "fixed").strip().lower()
        if source == "fixed":
            return float(card.get("write_value", 0.0) or 0.0)
        result = str(event.get("result") or "").lower()
        if source == "result_code":
            if result not in ("good", "reject"):
                raise ValueError("this event has no product result to write")
            return 1.0 if result == "good" else 2.0
        if source in ("good_count", "reject_count", "total_count"):
            good, rejected = event.get("good_count"), event.get("reject_count")
            if good is None or rejected is None:
                # An event that is not a product (a line start, an alarm): the line's totals now.
                from app.services.line_service import line_manager
                runtime = line_manager.get(_card_line(card))
                if runtime is None:
                    raise ValueError("the line's counts are not available")
                good, rejected = runtime.counter.good_count, runtime.counter.rejected_count
            if source == "total_count":
                total = event.get("total_count")
                return float(total if total is not None else int(good) + int(rejected))
            return float(good if source == "good_count" else rejected)
        if source == "class_index":
            if event.get("class_index") is not None:
                return float(event["class_index"])
            return float(PLCDispatcherService._class_index(card, event))
        if source == "reject_reason_code":
            if result != "reject":
                return 0.0
            return float(REJECT_REASON_CODES.get(event.get("reject_reason"), OTHER_REJECT_REASON_CODE))
        if source == "batch":
            batch = event.get("batch")
            if batch in (None, ""):
                raise ValueError("the line has no batch number to write")
            try:
                value = float(str(batch).strip())
            except ValueError:
                raise ValueError(f"the batch number '{batch}' is not a number, so it cannot be written") from None
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"the batch number '{batch}' is not a number, so it cannot be written")
            return value
        raise ValueError(f"'{source}' is not a value a card can write")

    @staticmethod
    def _class_index(card: dict, event: dict) -> int:
        """The product's class as a number: its place (from 1) in its camera's "Products to count"
        followed by "Defects to reject"; 0 when it is in neither."""
        classes = event.get("detected_classes") or []
        name = str(classes[0]).strip().lower() if classes else ""
        if not name:
            return 0
        from app.services.line_service import line_manager
        runtime = line_manager.get(str(event.get("line_id") or _card_line(card)))
        if runtime is None:
            return 0
        camera_id = event.get("camera_id") or runtime.counting_camera_id()
        entry = (runtime.camera_entry(camera_id) if camera_id else None) or runtime.camera_entry(runtime.counting_camera_id() or "")
        if not entry:
            return 0
        listed = [str(c).strip().lower() for c in (entry.get("expected_classes") or []) + (entry.get("defect_classes") or [])]
        return listed.index(name) + 1 if name in listed else 0

    @classmethod
    async def shutdown(cls) -> None:
        tasks = list(cls._dispatch_tasks)
        if not tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Internal Dispatch ─────────────────────────────────────────────────────

    @classmethod
    async def _dispatch(cls, card: dict, state: _CardState, event: dict) -> None:
        """Execute the PLC operation defined by a card."""
        cid = card.get("id", "")
        ep_id = card.get("plc_endpoint_id", "")
        operation = str(card.get("operation", "PULSE")).upper()
        address = str(card.get("target_address", ""))
        travel_ms = int(card.get("travel_delay_ms", 0) or 0)
        pulse_ms = int(card.get("pulse_duration_ms", 150) or 150)
        strobe = str(card.get("strobe_address") or "").strip()
        strobe_ms = int(card.get("strobe_pulse_ms") or 100)
        on_failure = str(card.get("on_failure", "skip")).lower()
        retry_attempts = int(card.get("retry_attempts", 2) or 0)
        retry_delay_ms = max(0, int(card.get("retry_delay_ms", 100) if card.get("retry_delay_ms") is not None else 100))

        state.status = "queued"
        logger.info(
            "PLCDispatcher: card '%s' queued — op=%s addr=%s ep=%s travel=%dms",
            card.get("name", cid), operation, address, ep_id, travel_ms,
        )

        # ── Travel delay ──────────────────────────────────────────────────────
        if travel_ms > 0:
            wait = travel_ms / 1000.0
            crossed_at = event.get("crossed_at")
            if event.get("delay_from_crossing") and crossed_at is not None:
                # The product's result was decided some time after it crossed
                # the line (it waited for its code). The product has travelled
                # on meanwhile, so that time is part of the travel delay.
                wait -= time.monotonic() - float(crossed_at)
                if wait < 0:
                    logger.warning(
                        "PLCDispatcher: card '%s' fires %.0f ms late: the product's result came after the card's "
                        "travel delay of %d ms. Make the travel delay longer than the line's Sync window.",
                        card.get("name", cid), -wait * 1000.0, travel_ms,
                    )
            if wait > 0:
                await asyncio.sleep(wait)

        # ── Resolve endpoint + driver ─────────────────────────────────────────
        if not ep_id:
            state.status = "failed"
            state.last_result = {
                "success": False,
                "message": "No PLC endpoint is configured; operation was not sent.",
                "endpoint": None,
                "protocol": None,
            }
            logger.warning("PLCDispatcher card '%s' skipped: no endpoint configured", card.get("name", cid))
            cls._report_alarm(card, {}, "failed", state.last_result["message"])
            cls._notify_status_change()
            return

        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            endpoints = SettingsPersistenceService.get_endpoints(protocol="plc")
            endpoint = next((e for e in endpoints if e.get("id") == ep_id), None)
        except Exception as ep_err:
            endpoint = None
            logger.warning("PLCDispatcher: could not resolve endpoint '%s': %s", ep_id, ep_err)

        if not endpoint:
            state.status = "failed"
            state.last_result = {
                "success": False,
                "message": f"PLC endpoint '{ep_id}' not found in configuration.",
                "endpoint": ep_id,
            }
            cls._report_alarm(card, {"id": ep_id}, "failed", state.last_result["message"])
            cls._notify_status_change()
            return

        if endpoint.get("enabled", True) is not True:
            state.status = "failed"
            state.last_result = {
                "success": False,
                "message": "PLC endpoint is disabled; operation was not sent.",
                "endpoint": endpoint.get("name", ep_id),
            }
            cls._report_alarm(card, endpoint, "failed", state.last_result["message"])
            cls._notify_status_change()
            return

        # The value is worked out now, after the travel delay: what the event says at the moment of the write.
        try:
            write_val = cls.write_value(card, event) if operation == "WRITE" else 0.0
        except ValueError as value_err:
            state.status = "failed"
            state.last_result = {"success": False, "message": f"Nothing was written: {value_err}", "endpoint": endpoint.get("name", ep_id)}
            cls._report_alarm(card, endpoint, "failed", state.last_result["message"])
            cls._notify_status_change()
            return

        from app.hardware.plc.factory import PLCDriverFactory
        try:
            driver = PLCDriverFactory.get_driver(endpoint)
        except ValueError as driver_err:
            state.status = "failed"
            state.last_result = {"success": False, "message": str(driver_err), "endpoint": endpoint.get("name", ep_id)}
            cls._report_alarm(card, endpoint, "failed", str(driver_err))
            cls._notify_status_change()
            return

        state.status = "executing"

        # ── Execute with retry ────────────────────────────────────────────────
        # TOGGLE is non-idempotent: if the PLC applied it but its reply was lost,
        # retrying would toggle the output back to its original state.
        attempts = retry_attempts + 1 if on_failure == "retry" and operation != "TOGGLE" else 1
        if on_failure == "retry" and operation == "TOGGLE":
            logger.warning("PLCDispatcher: automatic retry suppressed for TOGGLE card '%s'", card.get("name", cid))
        ok = False
        msg = ""
        strobe_failed = False

        timeout_s = float(endpoint.get("timeout", 3))
        for attempt in range(1, attempts + 1):
            try:
                endpoint_lock = cls._endpoint_locks.setdefault(ep_id, asyncio.Lock())
                async with endpoint_lock:
                    if not driver.is_connected:
                        try:
                            conn_ok = await asyncio.wait_for(driver.connect(), timeout=timeout_s)
                        except asyncio.TimeoutError:
                            conn_ok = False
                        if not conn_ok:
                            # Nothing was sent, so a retry is safe.
                            detail = getattr(driver, "last_error", "")
                            raise ConnectionError(
                                f"Cannot connect to PLC at {endpoint.get('host')}:{endpoint.get('port')}"
                                + (f": {detail}" if detail else "")
                            )
                    if PLCFailsafeService.needs_safe_state(ep_id):
                        # Outputs are in an unknown state after a lost link; restore
                        # the configured safe state before firing anything new.
                        safe_ok, safe_msg = await PLCFailsafeService.apply_locked(endpoint, driver, reason="before dispatch")
                        if not safe_ok:
                            raise ConnectionError(safe_msg)
                    try:
                        ok, msg = await asyncio.wait_for(
                            driver.execute_operation(
                                operation=operation,
                                address=address,
                                write_value=write_val,
                                pulse_duration_ms=pulse_ms,
                            ),
                            timeout=timeout_s + (pulse_ms / 1000.0) + 1,
                        )
                    except asyncio.TimeoutError:
                        # A late reply could be read as the answer to the next
                        # request, so drop the connection and start clean.
                        await driver.disconnect()
                        raise
                    if ok and strobe:
                        # Still inside the endpoint lock: no other card's write
                        # comes between the data and its strobe.
                        try:
                            strobe_ok, strobe_msg = await asyncio.wait_for(
                                driver.execute_operation(operation="PULSE", address=strobe, pulse_duration_ms=strobe_ms),
                                timeout=timeout_s + (strobe_ms / 1000.0) + 1,
                            )
                        except asyncio.TimeoutError:
                            await driver.disconnect()
                            raise
                        if strobe_ok:
                            msg = f"{msg}; strobe {strobe} pulsed {strobe_ms} ms"
                        else:
                            # The data is written; a retry would write it and strobe again.
                            ok, strobe_failed = False, True
                            msg = f"{msg}, but the strobe {strobe} failed: {strobe_msg}"
                if ok or strobe_failed:
                    break
            except asyncio.TimeoutError:
                msg = f"Attempt {attempt}: PLC operation timed out; actuation state is unknown and was not retried"
                ok = False
                PLCFailsafeService.mark_link_lost(ep_id)
                break
            except ValueError as exc:
                # Bad address or value in the card: retrying cannot help and the link is fine.
                msg = f"Attempt {attempt}: invalid PLC target or value: {exc}"
                ok = False
                break
            except Exception as exc:
                msg = f"Attempt {attempt}: {exc}"
                ok = False
                driver.is_connected = False  # force reconnect on next attempt
                PLCFailsafeService.mark_link_lost(ep_id)

            if not ok and attempt < attempts:
                await asyncio.sleep(retry_delay_ms / 1000.0)

        # ── Update status ─────────────────────────────────────────────────────
        if ok:
            # A card saved with the old "Wait for PLC ACK" choice did not wait for
            # anything either: it is "sent" like every other.
            state.status = "sent"
        else:
            state.status = "timeout" if "timed out" in msg.lower() else "failed"

        state.last_result = {
            "success": ok,
            "message": msg,
            "endpoint": endpoint.get("name", ep_id),
            "protocol": endpoint.get("plc_sub_protocol", "unknown"),
            "operation": operation,
            "address": address,
            "value": write_val if operation == "WRITE" else None,
            "strobe": strobe or None,
            "event_result": event.get("result"),
            "attempts": attempt,
        }

        logger.log(
            logging.INFO if ok else logging.WARNING,
            "PLCDispatcher: card '%s' → %s — %s",
            card.get("name", cid), state.status, msg,
        )
        cls._report_alarm(card, endpoint, state.status, msg)
        cls._notify_status_change()

    @staticmethod
    def _report_alarm(card: dict, endpoint: dict, status: str, msg: str) -> None:
        """Raise an alarm for a failed actuation, clear it once the card succeeds again."""
        from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager

        source = f"plc_card:{card.get('id', '')}"
        name = card.get("name") or card.get("id", "")
        details = {
            "card_id": card.get("id"),
            "line_id": _card_line(card),
            "endpoint_id": endpoint.get("id"),
            "operation": str(card.get("operation", "")).upper(),
            "address": str(card.get("target_address", "")),
        }
        if status == "timeout":
            # Actuation state is unknown: the reject gate may or may not have fired.
            alarm_manager.raise_alarm(
                AlarmCode.PLC_ACTION_TIMEOUT, source,
                f"PLC action '{name}' timed out; actuation state is unknown: {msg}",
                AlarmSeverity.CRITICAL, details,
            )
        elif status == "failed":
            alarm_manager.raise_alarm(
                AlarmCode.PLC_ACTION_FAILED, source,
                f"PLC action '{name}' failed: {msg}",
                AlarmSeverity.CRITICAL, details,
            )
        else:
            alarm_manager.clear_alarm(AlarmCode.PLC_ACTION_FAILED, source, "action succeeded")
            alarm_manager.clear_alarm(AlarmCode.PLC_ACTION_TIMEOUT, source, "action succeeded")

    @classmethod
    def _notify_status_change(cls) -> None:
        """Do not broadcast runtime PLC pulses as system configuration changes.

        PLC execution status is available from the PLC action status routes. The
        global settings poll is reserved for saved configuration/audit changes;
        bumping that version on every inspection pulse caused notification spam.
        """
        return
