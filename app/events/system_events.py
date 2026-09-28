"""System-level event names and a thread-safe bridge onto the event bus.

Hardware drivers, inference workers and the counting tracker run on plain
threads, while the EventBus lives on the asyncio loop. ``publish_threadsafe``
lets any thread publish without knowing which one it is on.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


class SystemEventType:
    ALARM_RAISED = "alarm_raised"
    ALARM_CLEARED = "alarm_cleared"
    ALARM_ACKNOWLEDGED = "alarm_acknowledged"
    COMPONENT_STATUS = "component_status"
    DASHBOARD_ALERT = "dashboard_alert"


@dataclass
class SystemEvent:
    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "ts": self.ts.isoformat(), **self.payload}


_loop: Optional[asyncio.AbstractEventLoop] = None


def set_event_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Bind the application loop so worker threads can publish events."""
    global _loop
    _loop = loop


def publish_threadsafe(event_name: str, data: Any) -> bool:
    """Publish on the event bus from any thread. Returns False when no loop is available."""
    from app.events.event_bus import event_bus

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    try:
        if running is not None:
            running.create_task(event_bus.publish(event_name, data))
            return True
        if _loop is not None and _loop.is_running() and not _loop.is_closed():
            asyncio.run_coroutine_threadsafe(event_bus.publish(event_name, data), _loop)
            return True
    except Exception as exc:
        logger.warning("Could not publish '%s' on the event bus: %s", event_name, exc)
    return False
