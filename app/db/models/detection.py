from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class DetectionLog(Base):
    __tablename__ = "detections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    camera_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    model_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    model_name: Mapped[str] = mapped_column(String(128), default="YOLOv8")
    total_detections: Mapped[int] = mapped_column(Integer, default=0)
    detections: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    inference_time_ms: Mapped[float] = mapped_column(Float, default=0.0)
    image_saved_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<DetectionLog id={self.id!r} count={self.total_detections} time={self.inference_time_ms}ms>"
