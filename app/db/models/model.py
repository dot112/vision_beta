from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class VisionModel(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="v1.0")
    framework: Mapped[str] = mapped_column(String(32), default="onnx")  # onnx, tensorrt, torch
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    classes: Mapped[List[str]] = mapped_column(JSON, default=list)
    input_width: Mapped[int] = mapped_column(Integer, default=640)
    input_height: Mapped[int] = mapped_column(Integer, default=640)
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.5)
    nms_threshold: Mapped[float] = mapped_column(Float, default=0.45)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<VisionModel id={self.id!r} name={self.name!r} version={self.version!r} active={self.is_active}>"
