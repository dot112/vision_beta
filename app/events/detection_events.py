"""Typed detection and counting events published on the event bus.

The event names match the strings the counting service and flow engine already
use, so these types can be adopted by publishers without changing subscribers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.events.system_events import publish_threadsafe


class DetectionEventType:
    DETECTION = "detection"
    WIRELINE_CROSS = "wireline_cross"
    READING = "reading"
    QR_CODE = "qr_code"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DetectionEvent:
    camera_id: str
    class_name: str
    confidence: float
    is_defect: bool = False
    track_id: Optional[int] = None
    bbox: Optional[list] = None
    timestamp: str = field(default_factory=_now_iso)

    event_type = DetectionEventType.DETECTION

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LineCrossEvent:
    camera_id: str
    line_index: int
    class_name: str
    track_id: Optional[int] = None
    is_defect: bool = False
    direction: Optional[str] = None
    timestamp: str = field(default_factory=_now_iso)

    event_type = DetectionEventType.WIRELINE_CROSS

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)


def publish_event(event: DetectionEvent | LineCrossEvent) -> bool:
    """Publish a typed event on the event bus from any thread."""
    return publish_threadsafe(event.event_type, event.to_payload())
