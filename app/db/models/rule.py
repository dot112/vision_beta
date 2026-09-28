from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class InspectionRule(Base):
    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    condition_field: Mapped[str] = mapped_column(String(64), nullable=False)  # total_detections, defect_count, class_name, max_confidence, qr_data
    operator: Mapped[str] = mapped_column(String(16), nullable=False)          # eq, neq, gt, gte, lt, lte, contains
    threshold_value: Mapped[str] = mapped_column(String(128), nullable=False) # e.g. "0", "0.75", "defect", "BATCH_A"
    action_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<InspectionRule id={self.id!r} name={self.name!r} field={self.condition_field!r}>"
