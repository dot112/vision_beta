from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import JSON, Boolean, DateTime, Enum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CameraType(str):
    USB = "usb"
    IP = "ip"
    GIGE = "gige"


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(
        Enum("usb", "ip", "gige", name="camera_type"), nullable=False
    )
    # For USB: "0", "1", ... device index as string
    # For IP:  full URL "rtsp://..." or "http://..."
    # For GigE: serial number
    source: Mapped[str] = mapped_column(String(512), nullable=False)

    # JSON blob — contents depend on camera type
    settings: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Camera id={self.id!r} name={self.name!r} type={self.type!r}>"
