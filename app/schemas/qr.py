from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from app.schemas.vision import BoundingBox


class BarcodeItem(BaseModel):
    code_type: str = Field(..., description="Barcode type: QR_CODE, EAN_13, CODE_128, etc.")
    data: str = Field(..., description="Decoded string content")
    polygon: List[List[int]] = Field(default_factory=list, description="Polygon corner points [ [x,y], ... ]")
    bbox: Optional[BoundingBox] = None


class BarcodeDecodeResponse(BaseModel):
    total_found: int
    codes: List[BarcodeItem]
    decode_time_ms: float
    image_width: int
    image_height: int


class LiveCameraBarcodeResponse(BaseModel):
    camera_id: str
    camera_name: str
    total_found: int
    codes: List[BarcodeItem]
    decode_time_ms: float
    acquisition_time_ms: float
    total_latency_ms: float
