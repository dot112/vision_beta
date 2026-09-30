"""
PLC Fail-Safe Service
=====================
Opt-in fail-safe layer for PLC line-control outputs.

Two independent, per-configuration mechanisms (both off by default):

  1. Safe state per output (PLC action card fields ``safe_state`` / ``safe_value``).
     Whenever the state of an endpoint's outputs is unknown the configured
     safe state is written before any further card is allowed to fire:
       * on server start (a crash or restart may have left outputs held),
       * after a lost PLC link (dispatch exception/timeout, dropped driver,
         failed heartbeat) once the link is back,
       * on graceful server shutdown.

  2. Heartbeat per endpoint (endpoint fields ``heartbeat_address`` and the
     existing ``heartbeat`` interval in seconds). A watchdog task writes a
     value alternating 1/0 to that address (a toggling bit or register). The PLC program must monitor it and drive its own outputs safe
     when it stops changing — that is the only protection that still works
     when the server process hangs, crashes or the network is cut, because
     the server cannot write to a PLC it cannot reach.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)

# UI/API value → driver operation
SAFE_STATE_OPERATIONS = {
    "reset": "RESET",
    "off": "RESET",
    "set": "SET",
    "on": "SET",
    "write": "WRITE",
}
SAFE_STATE_DISABLED = {"", "none", "hold"}

WATCHDOG_TICK_S = 0.5


def validate_safe_state(card: dict) -> None:
    """Raise ValueError when a card carries an unrecognised safe_state."""
    raw = card.get("safe_state")
    if raw is None:
        return
    value = str(raw).strip().lower()
    if value not in SAFE_STATE_DISABLED and value not in SAFE_STATE_OPERATIONS:
        raise ValueError(
            f"PLC action card '{card.get('id', '')}' has invalid safe_state '{raw}'; "
            "use none, reset, set or write"
        )
    if value == "write":
        try:
            float(card.get("safe_value", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"PLC action card '{card.get('id', '')}' safe_value must be a number") from exc


def safe_operation(card: dict) -> Optional[Tuple[str, float]]:
    """Return (operation, value) for a card's safe state, or None when disabled."""
    raw = str(card.get("safe_state", "none") or "none").strip().lower()
    op = SAFE_STATE_OPERATIONS.get(raw)
    if op is None:
        return None
    value = float(card.get("safe_value", 0) or 0) if op == "WRITE" else 0.0
    return op, value


async def _drop_connection(driver) -> None:
    """Close the link so a late reply cannot be read as the answer to the next request."""
    try:
        await asyncio.wait_for(driver.disconnect(), timeout=1.0)
    except Exception:
        pass
    driver.is_connected = False


@dataclass
class _EndpointWatch:
    endpoint_id: str
    was_connected: bool = False
    last_connect_attempt: float = 0.0
    last_heartbeat: float = 0.0
    heartbeat_counter: int = 0
    heartbeat_ok: Optional[bool] = None
    last_safe_state: Dict[str, Any] = field(default_factory=dict)


class PLCFailsafeService:
    """Singleton service. ``start()`` on boot, ``shutdown()`` before drivers close."""

    _needs_safe: Set[str] = set()           # endpoint IDs whose outputs are in an unknown state
    _watch: Dict[str, _EndpointWatch] = {}
    _task: Optional[asyncio.Task] = None

    # ── Configuration views ───────────────────────────────────────────────────

    @classmethod
    def _cards(cls) -> List[dict]:
        from app.services.plc_dispatcher_service import PLCDispatcherService
        return list(PLCDispatcherService._cards)

    @classmethod
    def safe_targets(cls, endpoint_id: str) -> List[Tuple[str, str, float]]:
        """(address, operation, value) for every enabled card on this endpoint with a safe state."""
        targets: Dict[str, Tuple[str, str, float]] = {}
        for card in cls._cards():
            if card.get("enabled", True) is not True or card.get("plc_endpoint_id", "") != endpoint_id:
                continue
            safe = safe_operation(card)
            address = str(card.get("target_address", "")).strip()
            if safe is None or not address:
                continue
            existing = targets.get(address)
            if existing and existing[1:] != safe:
                logger.warning(
                    "PLC fail-safe: conflicting safe states for %s on endpoint %s; keeping %s",
                    address, endpoint_id, existing[1],
                )
                continue
            targets[address] = (address, safe[0], safe[1])
        return list(targets.values())

    @classmethod
    def _endpoints(cls) -> Dict[str, dict]:
        from app.services.settings_persistence_service import SettingsPersistenceService
        return {
            ep["id"]: ep
            for ep in SettingsPersistenceService.get_endpoints(protocol="plc")
            if ep.get("id") and ep.get("enabled", True) is True
        }

    @classmethod
    def _watched_endpoints(cls) -> Dict[str, dict]:
        """Enabled endpoints that have a heartbeat or at least one safe-state output."""
        watched = {}
        for ep_id, ep in cls._endpoints().items():
            if str(ep.get("heartbeat_address", "") or "").strip() or cls.safe_targets(ep_id):
                watched[ep_id] = ep
        return watched

    # ── Link-state flags ──────────────────────────────────────────────────────

    @classmethod
    def needs_safe_state(cls, endpoint_id: str) -> bool:
        return endpoint_id in cls._needs_safe

    @classmethod
    def mark_link_lost(cls, endpoint_id: str) -> None:
        """Outputs on this endpoint are now in an unknown state (only tracked when opted in)."""
        if endpoint_id and endpoint_id not in cls._needs_safe and cls.safe_targets(endpoint_id):
            logger.warning("PLC fail-safe: link to endpoint %s lost; safe state pending", endpoint_id)
            cls._needs_safe.add(endpoint_id)

    # ── Safe-state application ────────────────────────────────────────────────

    @classmethod
    async def apply_locked(cls, endpoint: dict, driver, reason: str) -> Tuple[bool, str]:
        """
        Write every safe-state target on a connected driver.
        Caller must hold the endpoint lock and have connected the driver.
        """
        ep_id = endpoint.get("id", "")
        targets = cls.safe_targets(ep_id)
        if not targets:
            cls._needs_safe.discard(ep_id)
            return True, "no safe-state outputs configured"

        op_timeout = float(endpoint.get("timeout", 3)) + 1.0
        failures = []
        for address, operation, value in targets:
            try:
                ok, msg = await asyncio.wait_for(
                    driver.execute_operation(operation=operation, address=address, write_value=value),
                    timeout=op_timeout,
                )
            except Exception as exc:  # includes asyncio.TimeoutError
                ok, msg = False, f"{type(exc).__name__}: {exc}"
                await _drop_connection(driver)
            if not ok:
                failures.append(f"{address}: {msg}")

        ok = not failures
        summary = (
            f"safe state applied to {len(targets)} output(s) ({reason})"
            if ok else f"safe state failed ({reason}): " + "; ".join(failures)
        )
        watch = cls._watch.setdefault(ep_id, _EndpointWatch(endpoint_id=ep_id))
        watch.last_safe_state = {"success": ok, "reason": reason, "message": summary, "at": time.time()}
        if ok:
            cls._needs_safe.discard(ep_id)
            logger.info("PLC fail-safe: endpoint %s — %s", endpoint.get("name", ep_id), summary)
        else:
            cls._needs_safe.add(ep_id)
            logger.error("PLC fail-safe: endpoint %s — %s", endpoint.get("name", ep_id), summary)
        return ok, summary

    @classmethod
    async def apply(cls, endpoint: dict, reason: str) -> Tuple[bool, str]:
        """Connect if needed and apply the safe state, serialised with dispatches."""
        from app.hardware.plc.factory import PLCDriverFactory
        from app.services.plc_dispatcher_service import PLCDispatcherService

        ep_id = endpoint.get("id", "")
        driver = PLCDriverFactory.get_driver(endpoint)
        lock = PLCDispatcherService._endpoint_locks.setdefault(ep_id, asyncio.Lock())
        async with lock:
            if not driver.is_connected:
                try:
                    connected = await asyncio.wait_for(driver.connect(), timeout=float(endpoint.get("timeout", 3)))
                except Exception:
                    connected = False
                if not connected:
                    cls._needs_safe.add(ep_id)
                    return False, "PLC not reachable; safe state pending"
            return await cls.apply_locked(endpoint, driver, reason)

    # ── Watchdog ──────────────────────────────────────────────────────────────

    @classmethod
    def start(cls) -> None:
        """Flag every opted-in endpoint as unknown and start the watchdog loop."""
        try:
            for ep_id in cls._watched_endpoints():
                if cls.safe_targets(ep_id):
                    cls._needs_safe.add(ep_id)
        except Exception as exc:
            logger.warning("PLC fail-safe start: could not read endpoints: %s", exc)
        if cls._task is None or cls._task.done():
            cls._task = asyncio.create_task(cls._run(), name="plc_failsafe_watchdog")

    @classmethod
    async def _run(cls) -> None:
        while True:
            try:
                await cls.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("PLC fail-safe watchdog error: %s", exc)
            await asyncio.sleep(WATCHDOG_TICK_S)

    @classmethod
    async def tick(cls, now: Optional[float] = None) -> None:
        """One watchdog pass over every opted-in endpoint."""
        now = time.monotonic() if now is None else now
        watched = cls._watched_endpoints()
        for ep_id in list(cls._watch):
            if ep_id not in watched:
                cls._watch.pop(ep_id, None)
        for ep_id, endpoint in watched.items():
            try:
                await cls._tick_endpoint(endpoint, now)
            except Exception as exc:
                logger.warning("PLC fail-safe watchdog error on %s: %s", ep_id, exc)

    @classmethod
    async def _tick_endpoint(cls, endpoint: dict, now: float) -> None:
        from app.hardware.plc.factory import PLCDriverFactory
        from app.services.plc_dispatcher_service import PLCDispatcherService

        ep_id = endpoint["id"]
        watch = cls._watch.setdefault(ep_id, _EndpointWatch(endpoint_id=ep_id))
        driver = PLCDriverFactory.get_driver(endpoint)

        # A driver that dropped since the last pass means outputs are unknown.
        if not driver.is_connected and watch.was_connected:
            cls.mark_link_lost(ep_id)
        watch.was_connected = driver.is_connected

        hb_address = str(endpoint.get("heartbeat_address", "") or "").strip()
        hb_interval = max(1.0, float(endpoint.get("heartbeat", 10) or 10))
        hb_due = bool(hb_address) and (now - watch.last_heartbeat) >= hb_interval
        reconnect_due = (now - watch.last_connect_attempt) >= max(1.0, float(endpoint.get("reconnect_interval", 5) or 5))
        if not (hb_due or cls.needs_safe_state(ep_id)):
            return
        if not driver.is_connected and not reconnect_due:
            return

        lock = PLCDispatcherService._endpoint_locks.setdefault(ep_id, asyncio.Lock())
        async with lock:
            if not driver.is_connected:
                watch.last_connect_attempt = now
                try:
                    connected = await asyncio.wait_for(driver.connect(), timeout=float(endpoint.get("timeout", 3)))
                except Exception:
                    connected = False
                if not connected:
                    watch.heartbeat_ok = False if hb_address else None
                    return
                watch.was_connected = True

            if cls.needs_safe_state(ep_id):
                await cls.apply_locked(endpoint, driver, reason="link restored" if watch.last_safe_state else "startup")

            if hb_due and driver.is_connected:
                watch.last_heartbeat = now
                watch.heartbeat_counter = (watch.heartbeat_counter + 1) % 0x10000
                try:
                    ok, msg = await asyncio.wait_for(
                        driver.execute_operation(operation="WRITE", address=hb_address,
                                                 write_value=float(watch.heartbeat_counter % 2)),
                        timeout=float(endpoint.get("timeout", 3)) + 1.0,
                    )
                except Exception as exc:
                    ok, msg = False, f"{type(exc).__name__}: {exc}"
                watch.heartbeat_ok = ok
                if not ok:
                    logger.warning("PLC heartbeat to %s failed: %s", endpoint.get("name", ep_id), msg)
                    await _drop_connection(driver)
                    watch.was_connected = False
                    cls.mark_link_lost(ep_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @classmethod
    async def shutdown(cls) -> None:
        """Stop the watchdog and drive every opted-in output to its safe state."""
        task, cls._task = cls._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            endpoints = cls._endpoints()
        except Exception as exc:
            logger.warning("PLC fail-safe shutdown: could not read endpoints: %s", exc)
            return
        async def make_safe(ep_id: str, endpoint: dict) -> None:
            try:
                await asyncio.wait_for(cls.apply(endpoint, reason="shutdown"),
                                       timeout=2 * float(endpoint.get("timeout", 3)) + 2.0)
            except Exception as exc:
                logger.error("PLC fail-safe shutdown: endpoint %s not made safe: %s", ep_id, exc)

        # All endpoints at once: an unreachable PLC must not use up the time
        # the others need before the container is killed.
        await asyncio.gather(*(
            make_safe(ep_id, endpoint) for ep_id, endpoint in endpoints.items() if cls.safe_targets(ep_id)
        ))

    @classmethod
    def get_status(cls) -> List[Dict[str, Any]]:
        statuses = []
        try:
            watched = cls._watched_endpoints()
        except Exception:
            watched = {}
        for ep_id, endpoint in watched.items():
            watch = cls._watch.get(ep_id, _EndpointWatch(endpoint_id=ep_id))
            statuses.append({
                "endpoint_id": ep_id,
                "name": endpoint.get("name", ep_id),
                "safe_outputs": [
                    {"address": a, "operation": op, "value": v} for a, op, v in cls.safe_targets(ep_id)
                ],
                "safe_state_pending": ep_id in cls._needs_safe,
                "last_safe_state": watch.last_safe_state or None,
                "heartbeat_address": endpoint.get("heartbeat_address") or None,
                "heartbeat_ok": watch.heartbeat_ok,
                "heartbeat_counter": watch.heartbeat_counter,
            })
        return statuses

    @classmethod
    def reset(cls) -> None:
        """Test helper."""
        cls._needs_safe = set()
        cls._watch = {}
        cls._task = None
