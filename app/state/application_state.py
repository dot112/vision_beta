from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class ApplicationState:
    """Singleton-style container for shared runtime state."""

    # Connection/service readiness flags
    db_ready: bool = False
    # Set when SIGTERM/SIGINT arrives, so open video streams end at once and
    # the shutdown (PLC safe states) is not held up by viewers.
    shutting_down: bool = False
    mqtt_connected: bool = False
    modbus_connected: bool = False

    # Active camera registry  {camera_id: camera_instance}
    cameras: Dict[str, Any] = field(default_factory=dict)

    # System metrics
    processed_frames: int = 0
    detection_count: int = 0


# Module-level singleton
app_state = ApplicationState()
