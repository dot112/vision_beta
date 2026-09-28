from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field


class CameraType(str, Enum):
    USB = "usb"
    IP = "ip"
    GIGE = "gige"


class USBSettings(BaseModel):
    device_index: int = Field(default=0, ge=0, description="USB Video device index (0, 1, 2...)")
    width: int = Field(default=1280, ge=160, le=7680, description="Capture frame width")
    height: int = Field(default=720, ge=120, le=4320, description="Capture frame height")
    fps: int = Field(default=30, ge=1, le=240, description="Frames per second")
    brightness: Optional[float] = Field(default=None, description="Brightness value")
    contrast: Optional[float] = Field(default=None, description="Contrast value")
    saturation: Optional[float] = Field(default=None, description="Saturation value")
    exposure: Optional[float] = Field(default=None, description="Exposure value")
    auto_exposure: Optional[bool] = Field(default=True, description="Enable automatic exposure")


class IPSettings(BaseModel):
    url: str = Field(..., description="RTSP/HTTP stream URL (e.g. rtsp://192.168.1.100:554/stream1)")
    username: Optional[str] = Field(default=None, description="Optional stream auth username")
    password: Optional[str] = Field(default=None, description="Optional stream auth password")
    width: Optional[int] = Field(default=None, description="Desired output width (resizing)")
    height: Optional[int] = Field(default=None, description="Desired output height (resizing)")
    fps: Optional[int] = Field(default=25, description="Expected stream frame rate")
    transport: Literal["tcp", "udp"] = Field(default="tcp", description="RTSP transport protocol")
    buffer_size: int = Field(default=1, ge=1, le=10, description="OpenCV buffer size (1 for minimal latency)")


class GigESettings(BaseModel):
    serial_number: Optional[str] = Field(default=None, description="GigE Vision camera serial number")
    ip_address: Optional[str] = Field(default=None, description="GigE Vision camera IP address")
    width: int = Field(default=1920, description="Capture width")
    height: int = Field(default=1080, description="Capture height")
    fps: int = Field(default=30, description="Frames per second")
    packet_size: int = Field(default=9000, description="Jumbo frame packet size")
    exposure_time_us: Optional[float] = Field(default=10000.0, description="Exposure time in microseconds")
    gain_db: Optional[float] = Field(default=0.0, description="Sensor gain in dB")


class CameraCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Human readable camera name")
    type: CameraType = Field(..., description="Type of camera: usb, ip, or gige")
    source: str = Field(..., description="Device index (e.g., '0') or RTSP URL or Serial Number")
    settings: Dict[str, Any] = Field(default_factory=dict, description="Camera specific settings")


class CameraUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    source: Optional[str] = Field(default=None)
    settings: Optional[Dict[str, Any]] = Field(default=None)


class CameraResponse(BaseModel):
    id: str
    name: str
    type: CameraType
    source: str
    settings: Dict[str, Any]
    is_active: bool
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CameraStatusResponse(BaseModel):
    id: str
    name: str
    type: CameraType
    is_active: bool
    is_streaming: bool
    source: str
    properties: Dict[str, Any]
    last_error: Optional[str] = None


class DiscoveredUSBCamera(BaseModel):
    device_index: int
    name: str
    is_available: bool
    suggested_source: str
