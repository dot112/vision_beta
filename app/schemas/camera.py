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
    """The settings of an industrial camera (GigE Vision or USB3 Vision); its ``source`` is its serial number."""

    trigger_mode: Literal["off", "hardware", "software"] = Field(
        default="off", description="off: runs freely. hardware: a signal on the camera's own input takes each "
                                   "picture. software: the server takes each picture.")
    trigger_line: str = Field(default="Line0", max_length=32, description="Hardware trigger: the camera's input line")
    trigger_edge: Literal["rising", "falling"] = Field(default="rising", description="Hardware trigger: the edge")
    trigger_delay_us: float = Field(default=0, ge=0, le=10_000_000, description="Microseconds from the trigger to the exposure")
    exposure_us: Optional[float] = Field(default=None, ge=10, le=10_000_000, description="Exposure time in microseconds; not set = the camera's own")
    gain_db: Optional[float] = Field(default=None, ge=0, le=48, description="Gain in dB; not set = the camera's own")
    strobe_line: str = Field(default="", max_length=32, description="An output line that is on while the sensor is exposed, for a light; empty = none")


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
    # connected, reconnecting (lost or being retried; nobody disconnected it), failed or disconnected
    connection_state: str = "disconnected"
    # The camera's Connect switch (Line setup camera card) is on: it is connected at start and retried until off.
    kept_connected: bool = False
    # The trigger mode (off, hardware or software) that the camera card of the line this industrial
    # camera is on sets and locks: a vision card with Count mode One picture per trigger. Null when no
    # card does (no line, another kind of camera, a QR reader, a tracking camera or a code reader).
    line_trigger_mode: Optional[str] = None
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
    connection_state: str = "disconnected"
    kept_connected: bool = False
    is_streaming: bool
    source: str
    properties: Dict[str, Any]
    last_error: Optional[str] = None


class DiscoveredIndustrialCamera(BaseModel):
    serial: str
    model: str = ""
    vendor: str = ""
    interface: str = ""       # GigE or USB3
    name: str = ""            # the name saved in the camera itself, if any
    in_use: bool = False      # a saved camera already has this serial number
    channel: Optional[str] = None   # the network camera channel (Connections) that has it, if any


class DiscoveredUSBCamera(BaseModel):
    device_index: int
    name: str
    is_available: bool
    suggested_source: str
