from __future__ import annotations

from typing import Any, List, Optional
from pydantic import BaseModel, Field


class ModbusCoilWrite(BaseModel):
    address: int = Field(..., ge=0, le=65535, description="Coil address (0-indexed)")
    value: bool = Field(..., description="Boolean coil state: true (ON) or false (OFF)")


class ModbusRegisterWrite(BaseModel):
    address: int = Field(..., ge=0, le=65535, description="Holding register address (0-indexed)")
    value: int = Field(..., ge=0, le=65535, description="16-bit unsigned integer value (0-65535)")


class ModbusReadResponse(BaseModel):
    address: int
    count: int
    values: List[Any]
    success: bool
    error: Optional[str] = None
