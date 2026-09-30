from __future__ import annotations

from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class CountingConfig(BaseModel):
    line1_position: float = Field(
        default=0.35, ge=0.0, le=1.0, description="Position of Line 1 / Entry line (0.0 to 1.0 relative to frame)"
    )
    line2_position: float = Field(
        default=0.65, ge=0.0, le=1.0, description="Position of Line 2 / Exit line (0.0 to 1.0 relative to frame)"
    )
    orientation: str = Field(
        default="horizontal", description="'horizontal' (horizontal wirelines for vertical flow) or 'vertical' (vertical wirelines for horizontal flow)"
    )
    direction: str = Field(
        default="forward", description="'forward' (Line1 -> Line2), 'backward' (Line2 -> Line1), or 'both'"
    )
    expected_classes: List[str] = Field(
        default=["bottle", "cup", "can", "person", "cell phone"],
        description="List of class names to count as valid production units. If empty, all non-defect classes are counted.",
    )
    defect_classes: List[str] = Field(
        default=["defect", "defect_candidate", "scratch", "dent", "missing_cap", "broken"],
        description="List of class names considered defective (increments rejected count and calculates Defect PPM)",
    )
    # ── Action Trigger Protocol Destination Toggles & Selected Endpoints ──
    send_mqtt: bool = Field(default=True, description="Enable dispatching count JSON event to MQTT Broker")
    mqtt_topic: str = Field(default="factory/inspection/wireline", description="MQTT topic to publish count events to")
    mqtt_endpoint_id: Optional[str] = Field(default=None, description="Selected MQTT comms endpoint ID or 'all'")

    send_tcp: bool = Field(default=False, description="Enable dispatching count JSON event to TCP Socket")
    tcp_host: str = Field(default="127.0.0.1", description="Target TCP Socket Host IP")
    tcp_port: int = Field(default=9000, description="Target TCP Socket Port")
    tcp_endpoint_id: Optional[str] = Field(default=None, description="Selected TCP comms endpoint ID or 'all'")

    send_webhook: bool = Field(default=False, description="Enable dispatching count JSON event to HTTP Webhook API")
    webhook_url: str = Field(default="", description="Target HTTP/HTTPS API URL endpoint")
    webhook_headers: Optional[Dict[str, str]] = Field(default={}, description="Optional custom HTTP request headers")
    webhook_endpoint_id: Optional[str] = Field(default=None, description="Selected Webhook comms endpoint ID or 'all'")
    dispatch_trigger: str = Field(
        default="both",
        description="Default condition triggering protocol dispatch: 'both' (all crossed), 'passed' (good units only), or 'rejected' (defects only)"
    )
    dispatched_fields: Optional[List[str]] = Field(
        default=None,
        description="Default custom list of payload field keys to include in external dispatch JSON. None/empty includes all fields."
    )
    mqtt_dispatch_trigger: str = Field(
        default="both",
        description="Condition triggering MQTT dispatch: 'both', 'passed', or 'rejected'"
    )
    mqtt_dispatched_fields: Optional[List[str]] = Field(
        default=None,
        description="Custom list of payload field keys for MQTT dispatch"
    )
    tcp_dispatch_trigger: str = Field(
        default="both",
        description="Condition triggering TCP dispatch: 'both', 'passed', or 'rejected'"
    )
    tcp_dispatched_fields: Optional[List[str]] = Field(
        default=None,
        description="Custom list of payload field keys for TCP dispatch"
    )
    webhook_dispatch_trigger: str = Field(
        default="both",
        description="Condition triggering Webhook dispatch: 'both', 'passed', or 'rejected'"
    )
    webhook_dispatched_fields: Optional[List[str]] = Field(
        default=None,
        description="Custom list of payload field keys for Webhook dispatch"
    )

    # ── QR reads sent through the same channels: 'off', 'all', 'known' or 'unknown' ──
    mqtt_qr_dispatch: str = Field(default="off", description="Which QR reads go to MQTT: 'off', 'all', 'known' or 'unknown'")
    tcp_qr_dispatch: str = Field(default="off", description="Which QR reads go to TCP: 'off', 'all', 'known' or 'unknown'")
    webhook_qr_dispatch: str = Field(default="off", description="Which QR reads go to the webhook: 'off', 'all', 'known' or 'unknown'")

    # ── Conveyor Tracking & ByteTrack Parameters ──
    track_high_thresh: float = Field(
        default=0.50, ge=0.05, le=1.0, description="ByteTrack high-confidence threshold for stage 1 association"
    )
    track_low_thresh: float = Field(
        default=0.15, ge=0.01, le=0.9, description="ByteTrack low-confidence threshold for stage 2 recovery"
    )
    match_threshold: float = Field(
        default=0.70, ge=0.1, le=2.0, description="Cost threshold for association (IoU/distance)"
    )
    max_missed_frames: int = Field(
        default=15, ge=1, le=120, description="Maximum missed frames before a track is pruned"
    )
    max_speed_pixels: float = Field(
        default=120.0, ge=10.0, le=1000.0, description="Maximum allowed movement/speed in pixels per frame"
    )
    min_hits: int = Field(
        default=2, ge=1, le=10, description="Consecutive detections required to confirm a new track"
    )
    position_tolerance: float = Field(
        default=180.0, ge=20.0, le=600.0, description="Max spatial association tolerance radius in pixels"
    )


class TrackInfo(BaseModel):
    track_id: int = Field(..., description="Unique track identifier")
    class_id: int = Field(default=0, description="Class ID from detector")
    class_name: str = Field(..., description="Detected object class name")
    confidence: float = Field(..., description="Detection or track confidence score")
    bbox: Dict[str, int] = Field(..., description="Raw bounding box {'x1', 'y1', 'x2', 'y2'}")
    center: Dict[str, float] = Field(default_factory=dict, description="Raw bounding box center {'cx', 'cy'}")
    smooth_center: Dict[str, float] = Field(default_factory=dict, description="EMA smoothed tracking center {'cx', 'cy'}")
    smooth_center_x: float = Field(..., description="Smoothed center X in pixels")
    smooth_center_y: float = Field(..., description="Smoothed center Y in pixels")
    velocity_x: float = Field(default=0.0, description="Estimated velocity in X (pixels/frame)")
    velocity_y: float = Field(default=0.0, description="Estimated velocity in Y (pixels/frame)")
    age: int = Field(default=1, description="Total frames track has been alive")
    missed_frames: int = Field(default=0, description="Frames since last detection")
    confirmed: bool = Field(default=True, description="Whether track has reached confirmed hit threshold")
    counted: bool = Field(default=False, description="Whether this track has already been counted")


class CountingStatsResponse(BaseModel):
    total_inspected: int = Field(..., description="Total units inspected (Good + Rejected)")
    good_count: int = Field(..., description="Total good / passed units")
    rejected_count: int = Field(..., description="Total rejected / defective units")
    defect_ppm: float = Field(..., description="Defect Rate in Parts Per Million (PPM)")
    products_per_minute: float = Field(default=0.0, description="Throughput Rate in Products Per Minute (PPM)")
    yield_percentage: float = Field(..., description="Yield percentage (0.0 to 100.0%)")
    counts_by_class: Dict[str, int] = Field(..., description="Breakdown of counts per object class")
    expected_classes: List[str] = Field(..., description="Configured expected classes")
    defect_classes: List[str] = Field(..., description="Configured defect classes")
    active_tracks_count: int = Field(..., description="Currently tracked objects in frame")
    tracks: List[TrackInfo] = Field(default_factory=list, description="Real-time list of active tracked objects")
    config: CountingConfig


class ResetCountsRequest(BaseModel):
    reset_all: bool = Field(default=True, description="Reset all counters to 0")
    classes_to_reset: Optional[List[str]] = Field(default=None, description="Optional specific classes to reset")
