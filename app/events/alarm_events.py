"""Alarm model and in-memory alarm manager.

An alarm is identified by ``(code, source)``: raising the same code for the same
source again updates the existing active alarm (bumping ``count``) instead of
creating a duplicate, so a PLC that fails to connect every few seconds shows up
as one alarm, not hundreds. Alarms move through ``active -> acknowledged ->
cleared``; a cleared alarm moves to the bounded history.

The manager is thread-safe because camera and inference workers run on plain
threads, and it publishes ``alarm_raised`` / ``alarm_cleared`` /
``alarm_acknowledged`` on the event bus when an event loop is available.
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple

from app.events.system_events import SystemEventType, publish_threadsafe
from app.utils.logger import get_logger

logger = get_logger(__name__)


class AlarmSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def log_level(self) -> int:
        return {
            AlarmSeverity.INFO: logging.INFO,
            AlarmSeverity.WARNING: logging.WARNING,
            AlarmSeverity.CRITICAL: logging.ERROR,
        }[self]


class AlarmState(str, Enum):
    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    CLEARED = "cleared"


class AlarmCode:
    """Well-known alarm codes. Free-form codes are allowed; these keep callers consistent."""

    PLC_CONNECT_FAILED = "plc.connect_failed"
    PLC_DISCONNECT_ERROR = "plc.disconnect_error"
    PLC_ACTION_FAILED = "plc.action_failed"
    PLC_ACTION_TIMEOUT = "plc.action_timeout"
    FLOW_ACTION_FAILED = "flow.action_failed"
    FLOW_NODE_ERROR = "flow.node_error"
    FLOW_RUN_FAILED = "flow.run_failed"
    FLOW_OVERLOAD = "flow.overload"
    FLOW_BOOTSTRAP_FAILED = "flow.bootstrap_failed"
    FLOW_ENDPOINT_UNRESOLVED = "flow.endpoint_unresolved"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Alarm:
    code: str
    source: str
    severity: AlarmSeverity
    message: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: AlarmState = AlarmState.ACTIVE
    count: int = 1
    details: Dict[str, Any] = field(default_factory=dict)
    raised_at: datetime = field(default_factory=_now)
    last_raised_at: datetime = field(default_factory=_now)
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    cleared_at: Optional[datetime] = None

    @property
    def key(self) -> Tuple[str, str]:
        return self.code, self.source

    def to_dict(self) -> Dict[str, Any]:
        def iso(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt else None

        return {
            "id": self.id,
            "code": self.code,
            "source": self.source,
            "severity": self.severity.value,
            "state": self.state.value,
            "message": self.message,
            "count": self.count,
            "details": dict(self.details),
            "raised_at": iso(self.raised_at),
            "last_raised_at": iso(self.last_raised_at),
            "acknowledged_at": iso(self.acknowledged_at),
            "acknowledged_by": self.acknowledged_by,
            "cleared_at": iso(self.cleared_at),
        }


class AlarmManager:
    def __init__(self, history_size: int = 500):
        self._lock = threading.RLock()
        self._active: Dict[Tuple[str, str], Alarm] = {}
        self._history: Deque[Alarm] = deque(maxlen=history_size)
        self.total_raised = 0

    # ── Raising / clearing ────────────────────────────────────────────────────

    def raise_alarm(
        self,
        code: str,
        source: str,
        message: str,
        severity: AlarmSeverity | str = AlarmSeverity.WARNING,
        details: Optional[Dict[str, Any]] = None,
    ) -> Alarm:
        """Raise (or re-raise) an alarm and log it. Returns the live alarm."""
        severity = AlarmSeverity(severity)
        with self._lock:
            alarm = self._active.get((code, source))
            is_new = alarm is None
            if is_new:
                alarm = Alarm(code=code, source=source, severity=severity, message=message,
                              details=dict(details or {}))
                self._active[alarm.key] = alarm
                self.total_raised += 1
            else:
                alarm.count += 1
                alarm.message = message
                alarm.last_raised_at = _now()
                if details:
                    alarm.details.update(details)
                # Escalate, never silently downgrade, a live alarm's severity.
                if _severity_rank(severity) > _severity_rank(alarm.severity):
                    alarm.severity = severity
                    is_new = True  # an escalation is worth announcing again
            snapshot = alarm.to_dict()

        # Log every new alarm; repeats are logged at debug to avoid flooding the log.
        level = severity.log_level if is_new else logging.DEBUG
        logger.log(level, "ALARM %s [%s] %s: %s (x%d)", severity.value.upper(), code, source, message, snapshot["count"])
        if is_new:
            publish_threadsafe(SystemEventType.ALARM_RAISED, snapshot)
        return alarm

    def clear_alarm(self, code: str, source: str, reason: str = "condition cleared") -> Optional[Alarm]:
        """Clear the active alarm for (code, source), if any."""
        with self._lock:
            alarm = self._active.pop((code, source), None)
            if alarm is None:
                return None
            alarm.state = AlarmState.CLEARED
            alarm.cleared_at = _now()
            alarm.details["clear_reason"] = reason
            self._history.append(alarm)
            snapshot = alarm.to_dict()
        logger.info("ALARM CLEARED [%s] %s: %s", code, source, reason)
        publish_threadsafe(SystemEventType.ALARM_CLEARED, snapshot)
        return alarm

    def clear_source(self, source: str, reason: str = "source removed") -> int:
        """Clear every active alarm for a source (e.g. an endpoint that was deleted)."""
        with self._lock:
            keys = [key for key in self._active if key[1] == source]
        for code, src in keys:
            self.clear_alarm(code, src, reason)
        return len(keys)

    def acknowledge(self, alarm_id: str, user: Optional[str] = None) -> Optional[Alarm]:
        """Acknowledge an active alarm. It stays listed until its condition clears."""
        with self._lock:
            alarm = next((a for a in self._active.values() if a.id == alarm_id), None)
            if alarm is None:
                return None
            if alarm.state != AlarmState.ACKNOWLEDGED:
                alarm.state = AlarmState.ACKNOWLEDGED
                alarm.acknowledged_at = _now()
                alarm.acknowledged_by = user
            snapshot = alarm.to_dict()
        logger.info("ALARM ACKNOWLEDGED [%s] %s by %s", alarm.code, alarm.source, user or "unknown")
        publish_threadsafe(SystemEventType.ALARM_ACKNOWLEDGED, snapshot)
        return alarm

    # ── Queries ───────────────────────────────────────────────────────────────

    def active(self, source_prefix: Optional[str] = None) -> List[Alarm]:
        with self._lock:
            alarms = list(self._active.values())
        if source_prefix:
            alarms = [a for a in alarms if a.source.startswith(source_prefix)]
        return sorted(alarms, key=lambda a: (-_severity_rank(a.severity), a.raised_at))

    def history(self, limit: int = 100) -> List[Alarm]:
        with self._lock:
            items = list(self._history)
        return list(reversed(items))[: max(0, limit)]

    def get(self, code: str, source: str) -> Optional[Alarm]:
        with self._lock:
            return self._active.get((code, source))

    def counts(self) -> Dict[str, int]:
        counts = {s.value: 0 for s in AlarmSeverity}
        with self._lock:
            for alarm in self._active.values():
                counts[alarm.severity.value] += 1
            counts["unacknowledged"] = sum(1 for a in self._active.values() if a.state == AlarmState.ACTIVE)
        return counts

    def reset(self) -> None:
        """Drop all alarms (tests and full restarts only)."""
        with self._lock:
            self._active.clear()
            self._history.clear()
            self.total_raised = 0


def _severity_rank(severity: AlarmSeverity) -> int:
    return {AlarmSeverity.INFO: 0, AlarmSeverity.WARNING: 1, AlarmSeverity.CRITICAL: 2}[severity]


alarm_manager = AlarmManager()


def raise_alarm(
    code: str,
    source: str,
    message: str,
    severity: AlarmSeverity | str = AlarmSeverity.WARNING,
    details: Optional[Dict[str, Any]] = None,
) -> Alarm:
    return alarm_manager.raise_alarm(code, source, message, severity, details)


def clear_alarm(code: str, source: str, reason: str = "condition cleared") -> Optional[Alarm]:
    return alarm_manager.clear_alarm(code, source, reason)


__all__ = [
    "Alarm",
    "AlarmCode",
    "AlarmManager",
    "AlarmSeverity",
    "AlarmState",
    "alarm_manager",
    "clear_alarm",
    "raise_alarm",
]
