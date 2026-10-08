from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ModelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    version: str = Field(default="v1.0", max_length=32)
    framework: str = Field(default="onnx", description="Framework type: onnx, tensorrt, torch")
    file_path: str = Field(..., description="Local path to model weights file (.onnx / .engine)")
    classes: List[str] = Field(default_factory=list, description="Class label list in index order")
    input_width: int = Field(default=640, ge=32, le=4096)
    input_height: int = Field(default=640, ge=32, le=4096)
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    nms_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    metadata_json: Optional[Dict[str, Any]] = Field(default_factory=dict)


class ModelUpdate(BaseModel):
    name: Optional[str] = None
    version: Optional[str] = None
    confidence_threshold: Optional[float] = None
    nms_threshold: Optional[float] = None
    classes: Optional[List[str]] = None


class ModelResponse(BaseModel):
    id: str
    name: str
    version: str
    framework: str
    file_path: str
    classes: List[str]
    input_width: int
    input_height: int
    confidence_threshold: float
    nms_threshold: float
    # True while at least one vision camera runs the model (picked on Line setup).
    is_active: bool
    # The cameras that run it: [{"line_id", "line_name", "camera_id", "camera_name"}].
    used_by: List[Dict[str, Any]] = Field(default_factory=list)
    # Whether it is in memory and, when a camera runs it and it is not, why.
    loaded: bool = False
    load_error: Optional[str] = None
    metadata_json: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
