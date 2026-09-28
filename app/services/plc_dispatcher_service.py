"""
PLC Dispatcher Service
======================
Runtime engine that:
  1. Holds the current set of PLC Action Cards in memory.
  2. Receives vision events (line crossings, counter increments, class detections).
  3. Evaluates each card's trigger/condition/re-arm/execution-policy.
  4. Dispatches the configured PLC operation through the appropriate hardware driver.
  5. Maintains detailed per-card execution status (idle / queued / executing / sent / acked / failed / timeout).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from app.services.plc_failsafe_service import PLCFailsafeService
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ── Card Runtime State ────────────────────────────────────────────────────────

@dataclass
class _CardState:
    card_id: str
    status: str = "idle"                # idle|queued|executing|sent|acked|failed|timeout
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
    debounce_ms, duration_ms, exec_policy) and UI-level condition strings
    (counter_reach, counter_exact, counter_modulo, class_present, and
    class-filtered line crossings).

    This function returns a *copy* with all names normalised so the rest of the
    dispatcher can work with one canonical naming convention.
    """
    c = dict(card)   # shallow copy — don't mutate the original

    # ── Trigger field: "trigger" → "trigger_type" ─────────────────────────────
    if "trigger" in c and "trigger_type" not in c:
        trigger_alias = {
            "cross_line": "line_cross",
            "line_cross": "line_cross",
            "good_counter": "good_counter",
            "reject_counter": "reject_counter",
            "class": "class_detected",
            "class_detected": "class_detected",
        }
        c["trigger_type"] = trigger_alias.get(str(c["trigger"]).lower(), c["trigger"])

    # ── Condition field: "condition" → "trigger_condition" ────────────────────
    if "condition" in c and "trigger_condition" not in c:
        raw_cond = str(c["condition"]).lower()
        set_val = str(c.get("set_value", "1") or "1").strip()
        trigger_type = c.get("trigger_type", "line_cross")

        if trigger_type in ("good_counter", "reject_counter"):
            # Map UI counter condition names to backend format strings
            n = int(set_val) if set_val.isdigit() else int(c.get("trigger_value", 1) or 1)
            c["trigger_value"] = n
            cond_map = {
                "counter_reach":  f">={n}",
                "counter_exact":  f"=={n}",
                "counter_modulo": f"%{n}==0",
            }
            c["trigger_condition"] = cond_map.get(raw_cond, raw_cond)
        elif trigger_type == "class_detected":
            class_name = set_val or "defect"
            if raw_cond in ("class_present", "class=="):
                c["trigger_condition"] = f"class=={class_name}"
            elif raw_cond == "class_absent":
                # class_absent is not directly supported — we store it for
                # future use but skip matching (returns False below)
                c["trigger_condition"] = f"class_absent=={class_name}"
            else:
                c["trigger_condition"] = raw_cond
        elif trigger_type == "line_cross" and raw_cond in ("class", "class_cross"):
            # A class-filtered crossing uses the class carried by the crossing event.
            class_filter = str(c.get("set_value", "") or "").strip()
            c["trigger_condition"] = f"class=={class_filter}" if class_filter else "class=="
        else:
            # line_cross conditions are already compatible
            c["trigger_condition"] = raw_cond

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


# ── Condition Evaluation ──────────────────────────────────────────────────────

def _eval_condition(card: dict, event: dict) -> bool:
    """
    Return True when the card's trigger + condition matches the incoming event.

    Accepts BOTH normalised field names (trigger_type, trigger_condition) and
    raw UI field names (trigger, condition) — always normalise first via
    _normalize_card() before calling this.

    Event fields:
      event_id          str   — unique per crossing / detection frame
      result            str   — "good" | "reject" | "unknown"
      good_count        int
      reject_count      int
      detected_classes  list[str]
      timestamp         float
    """
    trigger = str(card.get("trigger_type", "line_cross")).lower()
    condition = str(card.get("trigger_condition", "any")).lower()
    trigger_value = int(card.get("trigger_value", 1) or 1)

    # ── Line cross ────────────────────────────────────────────────────────────
    if trigger == "line_cross":
        result = str(event.get("result", "unknown")).lower()
        if condition == "any":
            return True
        if condition.startswith("class=="):
            target = condition.split("==", 1)[1].strip().lower()
            detected = {str(name).strip().lower() for name in event.get("detected_classes", [])}
            return bool(target) and target in detected
        return result == condition      # "good" or "reject"

    # ── Good counter ──────────────────────────────────────────────────────────
    if trigger == "good_counter":
        count = int(event.get("good_count", 0))
        return _eval_counter(condition, count, trigger_value)

    # ── Reject counter ────────────────────────────────────────────────────────
    if trigger == "reject_counter":
        count = int(event.get("reject_count", 0))
        return _eval_counter(condition, count, trigger_value)

    # ── Class detected ────────────────────────────────────────────────────────
    if trigger == "class_detected":
        detected = [c.lower() for c in event.get("detected_classes", [])]
        if condition.startswith("class=="):
            target = condition.split("==", 1)[1].strip().lower()
            return target in detected
        if condition.startswith("class_in:"):
            targets = {c.strip().lower() for c in condition[9:].split(",")}
            return bool(targets & set(detected))
        if condition.startswith("class_absent=="):
            # class absent = NOT in detected
            target = condition.split("==", 1)[1].strip().lower()
            return target not in detected

    return False


def _eval_counter(condition: str, count: int, n: int) -> bool:
    """Evaluate counter condition string against a count value."""
    c = condition.strip().lower().replace(" ", "")
    if c.startswith(">="):
        return count >= n
    if c == f"=={n}" or c.startswith("=="):
        try:
            return count == int(c[2:])
        except ValueError:
            return count == n
    if c.startswith("%") and "==0" in c:
        # "%N==0" or "%10==0"
        try:
            divisor = int(c[1:c.index("==0")])
            return (divisor > 0) and (count % divisor == 0) and (count > 0)
        except (ValueError, AttributeError):
            return (n > 0) and (count % n == 0) and (count > 0)
    return False


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
    _dispatch_tasks: Set[asyncio.Task] = set()
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
            raw_cards = SettingsPersistenceService.get_plc_actions()
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
    def set_cards(cls, cards: List[dict]) -> None:
        """Hot-reload cards from a new list (called after API save/delete)."""
        cls._cards = [_normalize_card(c) for c in cards]
        for card in cls._cards:
            cid = card.get("id", "")
            if cid and cid not in cls._states:
                cls._states[cid] = _CardState(card_id=cid)

    @classmethod
    def get_status(cls, card_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return live status dicts for one or all cards."""
        statuses = []
        for card in cls._cards:
            cid = card.get("id", "")
            if card_id and cid != card_id:
                continue
            state = cls._states.get(cid, _CardState(card_id=cid))
            statuses.append({
                "card_id": cid,
                "name": card.get("name", ""),
                "enabled": card.get("enabled", True),
                "status": state.status,
                "last_result": state.last_result,
                "last_fired_at": state.last_fired_at or None,
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

            # ── Condition check ───────────────────────────────────────────────
            if not _eval_condition(card, event):
                continue

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

            if len(cls._dispatch_tasks) >= cls._max_dispatch_tasks:
                state.status = "failed"
                state.last_result = {"success": False, "message": "PLC dispatch capacity reached; event was dropped safely."}
                cls._notify_status_change()
                logger.error("PLC dispatch capacity reached; skipped card '%s'", cid)
                continue
            task = asyncio.create_task(
                cls._dispatch(card, state, event),
                name=f"plc_dispatch_{cid}",
            )
            cls._dispatch_tasks.add(task)
            task.add_done_callback(cls._dispatch_tasks.discard)

    @classmethod
    async def dispatch_manual(cls, card: dict) -> Dict[str, Any]:
        """
        Manually fire a card's PLC action (test button).
        Returns the execution result dict synchronously.
        """
        cid = card.get("id", "")
        state = cls._states.setdefault(cid, _CardState(card_id=cid))
        await cls._dispatch(card, state, event={"event_id": f"manual_{int(time.time())}"})
        return {**state.last_result, "status": state.status}

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
        write_val = float(card.get("write_value", 0.0) or 0.0)
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
            await asyncio.sleep(travel_ms / 1000.0)

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
            cls._notify_status_change()
            return

        if endpoint.get("enabled", True) is not True:
            state.status = "failed"
            state.last_result = {
                "success": False,
                "message": "PLC endpoint is disabled; operation was not sent.",
                "endpoint": endpoint.get("name", ep_id),
            }
            cls._notify_status_change()
            return

        from app.hardware.plc.factory import PLCDriverFactory
        try:
            driver = PLCDriverFactory.get_driver(endpoint)
        except ValueError as driver_err:
            state.status = "failed"
            state.last_result = {"success": False, "message": str(driver_err), "endpoint": endpoint.get("name", ep_id)}
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
                if ok:
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
            ack_mode = str(card.get("ack_mode", "unconfirmed")).lower()
            state.status = "unconfirmed" if ack_mode == "wait_ack" else "sent"
        else:
            state.status = "timeout" if "timed out" in msg.lower() else "failed"

        state.last_result = {
            "success": ok,
            "message": msg,
            "endpoint": endpoint.get("name", ep_id),
            "protocol": endpoint.get("plc_sub_protocol", "unknown"),
            "operation": operation,
            "address": address,
            "event_result": event.get("result"),
            "attempts": attempt,
        }

        logger.info(
            "PLCDispatcher: card '%s' → %s — %s",
            card.get("name", cid), state.status, msg,
        )
        cls._notify_status_change()

    @classmethod
    def _notify_status_change(cls) -> None:
        """Do not broadcast runtime PLC pulses as system configuration changes.

        PLC execution status is available from the PLC action status routes. The
        global settings poll is reserved for saved configuration/audit changes;
        bumping that version on every inspection pulse caused notification spam.
        """
        return
