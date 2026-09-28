"""Scope definitions and route policy for integration API keys."""

from __future__ import annotations

from typing import Dict


API_KEY_SCOPES: Dict[str, dict] = {
    "monitor:read": {"label": "Monitoring", "description": "Read live status, counts, tracks, camera status and system telemetry.", "min_clearance": 1},
    "inspection:read": {"label": "Inspection history", "description": "Read detection history and inspection results.", "min_clearance": 1},
    "inspection:trigger": {"label": "Run inspections", "description": "Trigger image, camera and QR inspections. May fire configured production actions.", "min_clearance": 2},
    "production:reset": {"label": "Reset production counts", "description": "Reset production counters; this changes recorded production totals.", "min_clearance": 2},
    "configuration:read": {"label": "Read configuration", "description": "Read settings, communication endpoints, models, rules, actions and flows.", "min_clearance": 1},
    "configuration:write": {"label": "Change configuration", "description": "Change general settings, cameras, models, rules and non-PLC configuration.", "min_clearance": 2},
    "camera:control": {"label": "Camera control", "description": "Connect and disconnect configured cameras.", "min_clearance": 2},
    "plc:configure": {"label": "Configure PLC actions", "description": "Create, edit, reload or remove PLC action cards and PLC endpoints; discover OPC UA endpoints.", "min_clearance": 2},
    "plc:operate": {"label": "Operate PLC actions", "description": "Execute or test PLC actions; may actuate production equipment.", "min_clearance": 2},
    "integrations:operate": {"label": "Operate integrations", "description": "Connect MQTT, publish/subscribe, and test configured external communication endpoints.", "min_clearance": 2},
    "alarms:acknowledge": {"label": "Acknowledge alarms", "description": "Acknowledge active alarms (for SCADA/HMI integration).", "min_clearance": 1},
}


def required_api_key_scopes(method: str, path: str) -> set[str] | None:
    """Map API requests to required scopes. None means API keys are not allowed."""
    method = method.upper()
    path = path.rstrip("/") or "/"

    if path == "/api/v1/auth/me" and method == "GET":
        return {"monitor:read"}
    if path.startswith("/api/v1/auth/"):
        return None  # login, password, logout, users, and key administration are JWT-only

    if path.startswith("/api/v1/cameras"):
        if method in {"GET", "HEAD"}:
            return {"monitor:read"}
        if path.endswith(("/connect", "/disconnect")) and method == "POST":
            return {"camera:control"}
        return {"configuration:write"}

    if path.startswith("/api/v1/vision/detections"):
        return {"inspection:read"} if method in {"GET", "HEAD"} else {"inspection:trigger"}
    if path.startswith("/api/v1/vision/annotated") or path.startswith("/api/v1/vision/stream"):
        return {"monitor:read"}
    if path.startswith("/api/v1/vision/"):
        return {"inspection:trigger"}
    if path.startswith("/api/v1/qr/annotated"):
        return {"monitor:read"}
    if path.startswith("/api/v1/qr/") or path.startswith("/api/v1/control/trigger"):
        return {"inspection:trigger"}

    if path.startswith("/api/v1/counting/"):
        if method in {"GET", "HEAD"}:
            return {"monitor:read"}
        if path.endswith("/reset"):
            return {"production:reset"}
        return {"configuration:write"}
    if path.startswith("/api/v1/telemetry/"):
        return {"monitor:read"}
    if path == "/api/v1/alarms" or path.startswith("/api/v1/alarms/"):
        if method in {"GET", "HEAD"}:
            return {"monitor:read"}
        if path.endswith("/acknowledge") and method == "POST":
            return {"alarms:acknowledge"}
        return None

    if path == "/api/v1/plc/opcua/scan" and method == "POST":
        return {"plc:configure"}

    if path.startswith("/api/v1/plc/actions"):
        if path.endswith("/test"):
            return {"plc:operate"}
        if method in {"GET", "HEAD"}:
            return {"monitor:read"} if path.endswith("/status") else {"configuration:read"}
        return {"plc:configure"}
    if path.startswith("/api/v1/plc/"):
        return {"monitor:read"} if method in {"GET", "HEAD"} else {"plc:operate"}

    if path.startswith("/api/v1/mqtt/status") and method in {"GET", "HEAD"}:
        return {"monitor:read"}
    if path.startswith("/api/v1/mqtt/certs"):
        return {"configuration:read"} if method in {"GET", "HEAD"} else {"configuration:write"}
    if path.startswith("/api/v1/mqtt/"):
        return {"integrations:operate"}

    if path.startswith("/api/v1/comms/"):
        return {"integrations:operate"}

    if path.startswith("/api/v1/system/settings"):
        return {"configuration:read"} if method in {"GET", "HEAD"} else {"configuration:write"}
    if path.startswith("/api/v1/system/audit-logs") or path.startswith("/api/v1/system/poll-changes"):
        return {"configuration:read"}
    if path.startswith("/api/v1/system/endpoints/") and path.endswith("/test"):
        return {"integrations:operate"}
    if path.startswith("/api/v1/system/endpoints"):
        if method in {"GET", "HEAD"}:
            return {"configuration:read"}
        return {"configuration:write"}
    if path.startswith("/api/v1/system/ip-cameras"):
        return {"configuration:write"}
    if path.startswith("/api/v1/system/"):
        return {"configuration:read"} if method in {"GET", "HEAD"} else {"configuration:write"}

    if path.startswith("/api/v1/models/") or path == "/api/v1/models":
        return {"configuration:read"} if method in {"GET", "HEAD"} else {"configuration:write"}
    if path.startswith("/api/v1/actions/") and path.endswith("/execute"):
        return {"plc:operate"}
    if path.startswith("/api/v1/actions/") or path == "/api/v1/actions":
        return {"configuration:read"} if method in {"GET", "HEAD"} else {"plc:configure"}
    if path.startswith("/api/v1/flows/") or path == "/api/v1/flows":
        if method in {"GET", "HEAD"}:
            return {"configuration:read"}
        if path.endswith("/test"):
            return {"plc:operate", "integrations:operate"}
        return {"configuration:write", "plc:configure", "plc:operate", "integrations:operate"}
    if path.startswith("/api/v1/rules/") or path == "/api/v1/rules":
        return {"configuration:read"} if method in {"GET", "HEAD"} else {"configuration:write"}

    # Fail closed for new/unclassified endpoints until their scope policy is added.
    return None
