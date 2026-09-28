from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class DetectionLogResponse(BaseModel):
    id: str
    camera_id: Optional[str] = None
    model_id: Optional[str] = None
    model_name: str
    total_detections: int
    detections: List[Dict[str, Any]]
    inference_time_ms: float
    image_saved_path: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
