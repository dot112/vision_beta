from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from sqlalchemy import JSON, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Action(Base):
    __tablename__ = "actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)  # modbus_coil, modbus_register, mqtt_publish, webhook, log
    target: Mapped[str] = mapped_column(String(256), default="")         # coil number, register address, MQTT topic, or URL
    payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Action id={self.id!r} name={self.name!r} type={self.action_type!r}>"
