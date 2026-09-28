from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class ActionType(str, Enum):
    MODBUS_COIL = "modbus_coil"
    MODBUS_REGISTER = "modbus_register"
    MQTT_PUBLISH = "mqtt_publish"
    WEBHOOK = "webhook"
    LOG = "log"


class ActionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    action_type: ActionType
    target: str = Field(..., description="Coil index (e.g. '0'), register address (e.g. '40001'), MQTT topic, or URL")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Dynamic payload or value parameters")
    is_enabled: bool = True


class ActionUpdate(BaseModel):
    name: Optional[str] = None
    action_type: Optional[ActionType] = None
    target: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    is_enabled: Optional[bool] = None


class ActionResponse(BaseModel):
    id: str
    name: str
    action_type: ActionType
    target: str
    payload: Dict[str, Any]
    is_enabled: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ActionExecutionResult(BaseModel):
    action_id: str
    action_name: str
    success: bool
    message: str
    executed_at: datetime
