"""Pydantic schemas for PLC Action Cards and execution status."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class PLCActionCard(BaseModel):
    """Full configuration for one PLC Action Trigger card."""

    id: str
    name: str = "PLC Action"
    enabled: bool = True

    # ── Communication ─────────────────────────────────────────────────────────
    plc_endpoint_id: str = ""           # ID of a PLC comm endpoint; "" = no channel

    # ── Trigger ───────────────────────────────────────────────────────────────
    trigger_type: str = "line_cross"
    # line_cross   → trigger_condition: "any" | "good" | "reject"
    # good_counter → trigger_condition: ">=N" | "==N" | "%N==0"
    # reject_counter → same pattern
    # class_detected → trigger_condition: "class==<name>"
    trigger_condition: str = "reject"
    trigger_value: int = 1              # N for counter conditions

    # ── Target ────────────────────────────────────────────────────────────────
    target_type: str = "coil"          # coil | register | digital_output | memory_bit | tag | custom_payload
    target_address: str = ""           # e.g. "10", "Q0.0", "M10.0", "ControllerTag"

    # ── Operation ─────────────────────────────────────────────────────────────
    operation: str = "PULSE"           # SET | RESET | PULSE | TOGGLE | WRITE
    write_value: float = 0.0           # used with WRITE
    pulse_duration_ms: int = 150       # used with PULSE

    # ── Timing ────────────────────────────────────────────────────────────────
    travel_delay_ms: int = 0           # delay between vision event and PLC command
    rearm_lockout_ms: int = 100        # minimum gap between successive fires

    # ── Policy ────────────────────────────────────────────────────────────────
    execution_policy: str = "once_per_event"   # once_per_event | every_frame
    ack_mode: str = "unconfirmed"              # unconfirmed | wait_ack

    # ── Failure handling ──────────────────────────────────────────────────────
    on_failure: str = "skip"           # skip | retry | error
    retry_attempts: int = 2
    retry_delay_ms: int = 100

    # ── Runtime status (not persisted) ────────────────────────────────────────
    status: Optional[str] = None
    last_result: Optional[Dict[str, Any]] = None
    last_fired_at: Optional[float] = None


class PLCActionStatus(BaseModel):
    """Live execution status for a single PLC Action card."""
    card_id: str
    name: str
    status: str                         # idle | queued | executing | sent | acked | failed | timeout
    last_result: Dict[str, Any] = Field(default_factory=dict)
    last_fired_at: Optional[float] = None


class PLCActionTestRequest(BaseModel):
    """Body for manual test execution."""
    confirm: bool = Field(False, description="Must be true to execute the physical PLC output")
    card: Optional[Dict[str, Any]] = Field(None, description="Optional card configuration to test directly")


class PLCActionTestResult(BaseModel):
    """Result of a manual PLC test execution."""
    card_id: str
    success: bool
    message: str
    status: str
    endpoint_name: Optional[str] = None
    protocol: Optional[str] = None


class OPCUAScanRequest(BaseModel):
    """Private IPv4 subnet and OPC UA port to scan for server endpoints."""

    target: str = Field(min_length=1, max_length=64)
    port: int = Field(default=4840, ge=1, le=65535)
