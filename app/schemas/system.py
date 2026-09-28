from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    app_name: str
    version: str
    environment: str
    timestamp: datetime


class SystemInfoResponse(BaseModel):
    app_name: str
    version: str
    environment: str
    debug: bool
    db_ready: bool
    mqtt_connected: bool
    modbus_connected: bool
    processed_frames: int
    detection_count: int
    active_model: Optional[Dict[str, Any]] = None
