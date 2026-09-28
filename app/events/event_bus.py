from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List
from app.utils.logger import get_logger

logger = get_logger(__name__)


class EventBus:
    """
    In-Memory Asynchronous Pub/Sub Event Bus for decoupled real-time vision pipelines.
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}

    def subscribe(self, event_name: str, callback: Callable) -> None:
        subscribers = self._subscribers.setdefault(event_name, [])
        if callback not in subscribers:
            subscribers.append(callback)

    async def publish(self, event_name: str, data: Any) -> None:
        if event_name in self._subscribers:
            for callback in self._subscribers[event_name]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        asyncio.create_task(callback(data))
                    else:
                        callback(data)
                except Exception as exc:
                    logger.error("EventBus subscriber error on '%s': %s", event_name, exc)


event_bus = EventBus()
