from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class BoundingBox(BaseModel):
    x1: int = Field(..., description="Top-left X coordinate in pixels")
    y1: int = Field(..., description="Top-left Y coordinate in pixels")
    x2: int = Field(..., description="Bottom-right X coordinate in pixels")
    y2: int = Field(..., description="Bottom-right Y coordinate in pixels")
    width: int
    height: int


class DetectionItem(BaseModel):
    class_id: int
    class_name: str
    confidence: float = Field(..., description="Detection confidence score between 0.0 and 1.0")
    bbox: BoundingBox
    polygon: Optional[List[List[int]]] = Field(default=None, description="Optional segmentation polygon contour points [[x, y], ...]")
    mask_area: Optional[int] = Field(default=None, description="Optional segmented surface area in pixels")


class InspectionReadingPayload(BaseModel):
    event: str = Field(default="INSPECTION_READING")
    timestamp: str
    camera_id: Optional[str] = None
    model_name: Optional[str] = None
    track_id: Optional[int] = None
    class_name: Optional[str] = None
    is_defect: bool = False
    confidence: float = 0.0
    bbox: Optional[BoundingBox] = None
    polygon: Optional[List[List[int]]] = None
    counts: Dict[str, int] = Field(default_factory=dict)
    metrics: Dict[str, Any] = Field(default_factory=dict)


class DetectionResponse(BaseModel):
    model_name: str
    total_detections: int
    detections: List[DetectionItem]
    inference_time_ms: float
    image_width: int
    image_height: int


class LiveInspectionResponse(BaseModel):
    camera_id: str
    camera_name: str
    model_name: str
    total_detections: int
    detections: List[DetectionItem]
    inference_time_ms: float
    acquisition_time_ms: float
    total_latency_ms: float
    passed: bool = Field(..., description="Inspection pass/fail evaluation based on defect count")
