"""Production line settings: shape, validation and the version 1 upgrade.

A production line is one camera-in, decisions-out unit. Lines are saved in
``data/system_state.json`` under ``lines``. Line 1 keeps its logic in the old
top-level ``action_trigger`` and ``plc_actions`` keys, so version 1 of the
server still reads the settings file correctly; every other line keeps its
own ``action_trigger`` and ``plc_actions`` inside its entry.

This module has no runtime dependencies so it can be used from the settings
service, the line manager and the API without import cycles.
"""
from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

PRIMARY_LINE_ID = "line-1"
SCHEMA_VERSION = 2
MAX_CAMERAS_PER_LINE = 2
CAMERA_ROLES = ("vision", "qr")
QR_DISPATCH_MODES = ("off", "all", "known", "unknown")

DEFAULT_SYNC_WINDOW_MS = 500
DEFAULT_QR_HOLD_MS = 1500

_LINE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

# Fields a line entry may carry; anything else sent by a client is dropped.
_LOGIC_KEYS = ("action_trigger", "plc_actions")


def new_line_id() -> str:
    return f"line-{uuid.uuid4().hex[:8]}"


def _bool(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label} must be true or false")


def _number(value: Any, label: str, default: float, minimum: float, maximum: float) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number") from None
    if not (minimum <= number <= maximum):
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    return number


def _position(value: Any, label: str) -> Optional[float]:
    if value is None or value == "":
        return None
    return _number(value, label, 0.5, 0.0, 1.0)


def normalize_camera(raw: Any, index: int) -> Dict[str, Any]:
    label = f"Camera {index + 1}"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    camera_id = str(raw.get("camera_id") or "").strip()
    if not camera_id or len(camera_id) > 64:
        raise ValueError(f"{label} needs a camera_id")
    role = str(raw.get("role") or "vision").strip().lower()
    if role not in CAMERA_ROLES:
        raise ValueError(f"{label} role must be 'vision' or 'qr'")
    camera: Dict[str, Any] = {
        "camera_id": camera_id,
        "role": role,
        "counting": _bool(raw.get("counting"), f"{label} counting", False) if role == "vision" else False,
        "qr_hold_ms": int(_number(raw.get("qr_hold_ms"), f"{label} QR hold time", DEFAULT_QR_HOLD_MS, 100, 60000)),
    }
    # A second vision camera may use its own wirelines; blank means the line's.
    if role == "vision":
        for key in ("line1_position", "line2_position"):
            pos = _position(raw.get(key), f"{label} {key.replace('_', ' ')}")
            if pos is not None:
                camera[key] = pos
        orientation = raw.get("orientation")
        if orientation not in (None, ""):
            if orientation not in ("horizontal", "vertical"):
                raise ValueError(f"{label} orientation must be 'horizontal' or 'vertical'")
            camera["orientation"] = orientation
    return camera


def normalize_line(raw: Any, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate a line sent by a client, filling gaps from ``existing``.

    Returns a new dict. The line's ``action_trigger`` and ``plc_actions`` are
    passed through as given (validated elsewhere).
    """
    if not isinstance(raw, dict):
        raise ValueError("A production line must be an object")
    base = copy.deepcopy(existing) if existing else {}
    merged = {**base, **raw}

    line_id = str(merged.get("id") or "").strip().lower() or new_line_id()
    if not _LINE_ID_RE.match(line_id):
        raise ValueError("Line id may use lowercase letters, digits, '-' and '_' (up to 48 characters)")

    name = str(merged.get("name") or "").strip()
    if not name:
        raise ValueError("Line name is required")
    if len(name) > 64:
        raise ValueError("Line name must be 64 characters or fewer")

    raw_cameras = merged.get("cameras") or []
    if not isinstance(raw_cameras, list):
        raise ValueError("cameras must be a list")
    if len(raw_cameras) > MAX_CAMERAS_PER_LINE:
        raise ValueError(f"A line can have at most {MAX_CAMERAS_PER_LINE} cameras")
    cameras = [normalize_camera(c, i) for i, c in enumerate(raw_cameras)]
    ids = [c["camera_id"] for c in cameras]
    if len(set(ids)) != len(ids):
        raise ValueError("The same camera is listed twice on this line")

    vision = [c for c in cameras if c["role"] == "vision"]
    counting = [c for c in vision if c["counting"]]
    if len(counting) > 1:
        raise ValueError("Only one vision camera can be the counting camera")
    if vision and not counting:
        vision[0]["counting"] = True

    raw_sync = merged.get("sync") or {}
    if not isinstance(raw_sync, dict):
        raise ValueError("sync must be an object")
    sync = {
        "enabled": _bool(raw_sync.get("enabled"), "Sync", False),
        "window_ms": int(_number(raw_sync.get("window_ms"), "Sync window", DEFAULT_SYNC_WINDOW_MS, 50, 10000)),
    }
    if sync["enabled"] and not (vision and any(c["role"] == "qr" for c in cameras)):
        raise ValueError("Sync needs one vision camera and one QR reader on the line")

    model_id = merged.get("model_id")
    model_id = str(model_id).strip() if model_id not in (None, "") else None

    line: Dict[str, Any] = {
        "id": line_id,
        "name": name,
        "enabled": _bool(merged.get("enabled"), "enabled", True),
        "auto_connect": _bool(merged.get("auto_connect"), "auto_connect", True),
        "cameras": cameras,
        "model_id": model_id,
        "sync": sync,
        "min_fps": _number(merged.get("min_fps"), "Minimum processed frame rate", 0.0, 0.0, 240.0),
    }
    for key in _LOGIC_KEYS:
        if key in merged:
            line[key] = copy.deepcopy(merged[key])
    if "action_trigger" in line and not isinstance(line["action_trigger"], dict):
        raise ValueError("action_trigger must be an object")
    if "plc_actions" in line and not isinstance(line["plc_actions"], list):
        raise ValueError("plc_actions must be a list")
    return line


def check_camera_ownership(lines: Iterable[Dict[str, Any]]) -> None:
    """A camera belongs to one line only."""
    owner: Dict[str, str] = {}
    for line in lines:
        for cam in line.get("cameras", []):
            cid = cam.get("camera_id")
            if cid in owner and owner[cid] != line.get("id"):
                raise ValueError(f"Camera '{cid}' already belongs to line '{owner[cid]}'")
            owner[cid] = line.get("id")


def upgrade_state_to_v2(state: Dict[str, Any]) -> bool:
    """Create Line 1 from a version 1 settings file. Returns True when it changed anything.

    Nothing existing is removed or renamed: Line 1 reads its logic from the old
    ``action_trigger`` and ``plc_actions`` keys, so version 1 keeps working
    against the upgraded file.
    """
    lines = state.get("lines")
    if isinstance(lines, list) and any(isinstance(ln, dict) and ln.get("id") == PRIMARY_LINE_ID for ln in lines):
        if state.get("schema_version") != SCHEMA_VERSION:
            state["schema_version"] = SCHEMA_VERSION
            return True
        return False

    cameras = []
    active_camera = state.get("active_camera_id")
    if active_camera:
        cameras.append({"camera_id": str(active_camera), "role": "vision", "counting": True})
    line1 = normalize_line({
        "id": PRIMARY_LINE_ID,
        "name": "Line 1",
        "enabled": True,
        # Version 1 connected its camera on startup only when asked to.
        "auto_connect": bool(state.get("camera_auto_connect", False)),
        "cameras": cameras,
        # None: follow the model activated on the Models page, as version 1 did.
        "model_id": None,
    })
    others = [ln for ln in lines if isinstance(ln, dict)] if isinstance(lines, list) else []
    state["lines"] = [line1] + others
    state["schema_version"] = SCHEMA_VERSION
    return True


def plc_address_clashes(cards_by_line: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """PLC addresses written by cards on more than one line (a warning, not an error)."""
    users: Dict[Tuple[str, str], Dict[str, List[str]]] = {}
    for line_id, cards in cards_by_line.items():
        for card in cards or []:
            if card.get("enabled", True) is not True:
                continue
            endpoint = str(card.get("plc_endpoint_id") or "").strip()
            address = str(card.get("target_address") or "").strip()
            if not endpoint or not address:
                continue
            users.setdefault((endpoint, address.lower()), {}).setdefault(line_id, []).append(
                str(card.get("name") or card.get("id") or "")
            )
    clashes = []
    for (endpoint, address), by_line in sorted(users.items()):
        if len(by_line) > 1:
            clashes.append({
                "plc_endpoint_id": endpoint,
                "address": address,
                "lines": sorted(by_line),
                "cards": {line_id: names for line_id, names in sorted(by_line.items())},
            })
    return clashes


def endpoints_used_by_line(line: Dict[str, Any]) -> Dict[str, str]:
    """{endpoint_id: what uses it} for one line's dispatch settings and PLC cards."""
    used: Dict[str, str] = {}
    trigger = line.get("action_trigger") or {}
    for protocol in ("mqtt", "tcp", "webhook"):
        endpoint = trigger.get(f"{protocol}_endpoint_id")
        if endpoint and endpoint != "all":
            used[str(endpoint)] = f"{protocol.upper()} dispatch"
    for card in line.get("plc_actions") or []:
        endpoint = card.get("plc_endpoint_id")
        if endpoint:
            used.setdefault(str(endpoint), f"PLC card '{card.get('name') or card.get('id')}'")
    for cam in line.get("cameras") or []:
        if cam.get("camera_id"):
            used.setdefault(str(cam["camera_id"]), "camera")
    return used
