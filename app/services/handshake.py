"""The result handshake of one line: capture request -> result -> the PLC's acknowledge.

    idle --capture--> waiting_result --result--> result_ready --ack--> idle

The PLC raises its capture request (a "Take a picture" input) and holds the
product's sequence; the line answers with the product's one result (its output
actions set the PLC's result flags); the PLC acknowledges (an input goes on) and
the flags are reset by the owner's own output actions. This class keeps the state
and the timers only; the line does what each step means (``LineRuntime``).

- No result within ``result_timeout_ms`` after the capture: ``on_result_timeout``
  (request, policy). Policy "reject": the line emits a rejected product, which
  comes back here as the request's result. Policy "fault": the request ends with
  the result "fault". Never a pass. The request is closed: a result that comes
  for it later is not this request's any more (``is_closed``).
- No ack within ``ack_timeout_ms`` after the result: ``on_ack_timeout``; the
  handshake keeps waiting for the ack.
- An ack while idle or waiting for a result is stale: ignored and counted.
- A capture with the sequence that is still in the handshake is a duplicate:
  ignored and counted. A capture while a result waits for its ack (the PLC skipped
  the ack), or before the result came, is a protocol fault the line reports; the
  new capture goes ahead.

There are no threads here. ``schedule(delay_s, fn)`` returns a handle with
``cancel()``; the line schedules on its event loop. Calls may come from other
threads (a camera's inference thread finishes a product), so the state is locked.
"""
from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Optional, Set

IDLE = "idle"
WAITING = "waiting_result"
READY = "result_ready"
# Closed requests remembered, so a late result is known as late.
CLOSED_KEPT = 64


@dataclass
class Request:
    """One capture request in the handshake."""
    id: int
    sequence: Any
    at: float


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat()


class Handshake:
    def __init__(self, schedule: Optional[Callable[[float, Callable[[], None]], Any]] = None,
                 on_result_timeout: Optional[Callable[[Request, str], None]] = None,
                 on_ack_timeout: Optional[Callable[[Request], None]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.schedule = schedule
        self.on_result_timeout = on_result_timeout
        self.on_ack_timeout = on_ack_timeout
        self._clock = clock
        self._lock = threading.RLock()
        self.settings: Dict[str, Any] = {}
        self.state = IDLE
        self.current: Optional[Request] = None
        self.since = clock()
        self.last_result: Optional[str] = None
        self.last_error: Optional[str] = None
        self.stale_acks = 0
        self.timeouts = 0
        self.ack_timeouts = 0
        self.duplicates = 0
        self.late = 0
        self._next_id = 0
        self._timer: Any = None
        self._closed: Deque[int] = collections.deque(maxlen=CLOSED_KEPT)
        self._closed_set: Set[int] = set()

    # ── Settings ──────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled"))

    @property
    def capture_input_id(self) -> Optional[str]:
        return self.settings.get("capture_input_id") if self.enabled else None

    @property
    def ack_input_id(self) -> Optional[str]:
        return self.settings.get("ack_input_id") if self.enabled else None

    def configure(self, settings: Optional[Dict[str, Any]]) -> None:
        """The line's ``handshake`` settings (normalize_handshake). Switched off, or with other
        inputs, the handshake starts again from idle; a new timeout applies from the next step."""
        settings = dict(settings or {})
        with self._lock:
            old = self.settings
            same = bool(settings.get("enabled")) and bool(old.get("enabled")) and all(
                settings.get(key) == old.get(key) for key in ("capture_input_id", "ack_input_id"))
            self.settings = settings
            if not same and self.state != IDLE:
                self._cancel_timer()
                self.current = None
                self._move(IDLE)

    # ── Steps ─────────────────────────────────────────────────────────────────

    def capture(self, sequence: Any) -> Optional[Dict[str, Any]]:
        """The capture input fired with this sequence. None when the handshake is off.

        Returns {"action": "accepted" | "duplicate", "request_id", "fault"}: ``fault`` is
        "skipped_ack" (a result was waiting for its ack) or "no_result" (the last request had
        no result yet), else None.
        """
        with self._lock:
            if not self.enabled:
                return None
            current = self.current
            if self.state != IDLE and current is not None and sequence is not None and sequence == current.sequence:
                self.duplicates += 1
                return {"action": "duplicate", "request_id": current.id, "fault": None}
            fault = {READY: "skipped_ack", WAITING: "no_result"}.get(self.state)
            self._cancel_timer()
            self._next_id += 1
            self.current = Request(self._next_id, sequence, self._clock())
            if fault is not None:
                self.last_error = fault
            self._move(WAITING)
            self._start_timer(self.settings.get("result_timeout_ms"), self._result_timer, self.current.id)
            return {"action": "accepted", "request_id": self.current.id, "fault": fault}

    def result(self, request_id: Optional[int], result: str, timeout: bool = False) -> bool:
        """A product's one result went out. True when it was the result the handshake waited for.

        ``request_id``: the request the product answers; None (a product made without one) answers
        the waiting request. ``timeout``: the line's own reject for a request that timed out.
        """
        with self._lock:
            current = self.current
            if not self.enabled or self.state != WAITING or current is None:
                return False
            if request_id is not None and request_id != current.id:
                return False
            if self.is_closed(current.id) and not timeout:
                return False
            self._ready(result)
            return True

    def ack(self) -> str:
        """The PLC's acknowledge went on: "done" (back to idle) or "stale" (nothing to acknowledge)."""
        with self._lock:
            if self.state != READY:
                self.stale_acks += 1
                return "stale"
            self._cancel_timer()
            self._move(IDLE)
            return "done"

    def is_closed(self, request_id: Optional[int]) -> bool:
        """True for a request that timed out: its one result was given already."""
        return request_id is not None and request_id in self._closed_set

    def note_late(self) -> None:
        with self._lock:
            self.late += 1

    def reset_counts(self) -> None:
        with self._lock:
            self.stale_acks = self.timeouts = self.ack_timeouts = self.duplicates = self.late = 0
            self.last_error = None

    def stop(self) -> None:
        """Forget the request under way (the line is gone); the next capture starts from idle."""
        with self._lock:
            self._cancel_timer()
            self.current = None
            self._move(IDLE)

    # ── Inside ────────────────────────────────────────────────────────────────

    def _move(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.since = self._clock()

    def _ready(self, result: str) -> None:
        self._cancel_timer()
        self.last_result = result
        self._move(READY)
        self._start_timer(self.settings.get("ack_timeout_ms"), self._ack_timer, self.current.id if self.current else 0)

    def _close(self, request_id: int) -> None:
        if len(self._closed) == self._closed.maxlen:
            self._closed_set.discard(self._closed[0])
        self._closed.append(request_id)
        self._closed_set.add(request_id)

    def _start_timer(self, ms: Any, fn: Callable[[int], None], request_id: int) -> None:
        if self.schedule is None or not ms:
            return
        self._timer = self.schedule(int(ms) / 1000.0, lambda: fn(request_id))

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _result_timer(self, request_id: int) -> None:
        with self._lock:
            request = self.current
            if self.state != WAITING or request is None or request.id != request_id:
                return
            self._timer = None
            self.timeouts += 1
            self._close(request_id)
            policy = self.settings.get("on_timeout") or "reject"
            self.last_error = "inspection_timeout"
        callback = self.on_result_timeout
        if callback is not None:
            try:
                callback(request, policy)
            finally:
                self._end_timed_out(request, policy)
        else:
            self._end_timed_out(request, policy)

    def _end_timed_out(self, request: Request, policy: str) -> None:
        """A timed-out request that the line's reject did not answer (or "fault") ends here."""
        with self._lock:
            if self.state == WAITING and self.current is request:
                self._ready("fault" if policy == "fault" else "reject")

    def _ack_timer(self, request_id: int) -> None:
        with self._lock:
            request = self.current
            if self.state != READY or request is None or request.id != request_id:
                return
            self._timer = None
            self.ack_timeouts += 1
            self.last_error = "ack_timeout"
        if self.on_ack_timeout is not None:
            self.on_ack_timeout(request)

    # ── View ──────────────────────────────────────────────────────────────────

    def view(self) -> Dict[str, Any]:
        with self._lock:
            current = self.current if self.state != IDLE else None
            return {
                "enabled": self.enabled,
                "state": self.state,
                "sequence": current.sequence if current is not None else None,
                "request_id": current.id if current is not None else None,
                "since": _iso(self.since),
                "last_result": self.last_result,
                "stale_acks": self.stale_acks,
                "timeouts": self.timeouts,
                "ack_timeouts": self.ack_timeouts,
                "duplicates": self.duplicates,
                "late": self.late,
                "last_error": self.last_error,
                "settings": dict(self.settings),
            }
