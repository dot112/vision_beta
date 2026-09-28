from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _endpoint_text(value: Any, label: str, *, maximum: int = 1024, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    text = value.strip()
    if len(text) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{label} is invalid")
    if required and not text:
        raise ValueError(f"{label} is required for an enabled endpoint")
    return text


def _endpoint_host(value: Any, label: str, *, required: bool) -> str:
    host = _endpoint_text(value, label, maximum=253, required=required)
    if any(char.isspace() for char in host):
        raise ValueError(f"{label} is invalid")
    return host


def _endpoint_port(value: Any, label: str, default: int) -> int:
    return _bounded_int(value, label, default, 1, 65535)


def _bounded_int(value: Any, label: str, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    try:
        parsed = int(default if value is None else value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be between {minimum} and {maximum}") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label} must be a whole number")
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _endpoint_bool(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _endpoint_payload(value: Any, label: str, *, maximum: int = 4096) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} must be text under {maximum} characters")
    return value


_SENSITIVE_URL_QUERY_KEYS = {"auth", "authorization", "api_key", "apikey", "key", "password", "pwd", "secret", "sig", "signature", "token", "access_token", "client_secret", "refresh_token"}


def _is_sensitive_url_query_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return normalized in _SENSITIVE_URL_QUERY_KEYS or any(
        normalized.endswith(f"_{suffix}")
        for suffix in ("auth", "key", "password", "secret", "signature", "token")
    )


def _redact_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        has_userinfo = "@" in parsed.netloc
        has_secret_query = any(_is_sensitive_url_query_key(key) for key, _ in query_pairs)
        if not has_userinfo and not has_secret_query:
            return url
        netloc = f"***:***@{parsed.netloc.rsplit('@', 1)[-1]}" if has_userinfo else parsed.netloc
        query = urlencode([
            (key, "***" if _is_sensitive_url_query_key(key) else value)
            for key, value in query_pairs
        ])
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        return "[redacted URL]"


def _restore_redacted_url(value: str, original: str) -> str:
    """Preserve credentials when a UI round-trips the redacted API view."""
    if not value or not original:
        return value
    try:
        original_view = _redact_url(original)
        if value == original_view:
            return original
        candidate = urlsplit(value)
        previous = urlsplit(original)
        netloc = candidate.netloc
        if netloc.startswith("***:***@") and "@" in previous.netloc:
            netloc = f"{previous.netloc.rsplit('@', 1)[0]}@{netloc.split('@', 1)[1]}"
        old_values: Dict[str, List[str]] = {}
        for key, old_value in parse_qsl(previous.query, keep_blank_values=True):
            if _is_sensitive_url_query_key(key):
                old_values.setdefault(key.lower(), []).append(old_value)
        restored_query = []
        for key, query_value in parse_qsl(candidate.query, keep_blank_values=True):
            values = old_values.get(key.lower(), [])
            if query_value == "***" and _is_sensitive_url_query_key(key) and values:
                query_value = values.pop(0)
            restored_query.append((key, query_value))
        return urlunsplit((candidate.scheme, netloc, candidate.path, urlencode(restored_query), candidate.fragment))
    except (TypeError, ValueError):
        return value

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
STATE_FILE = os.path.join(DATA_DIR, "system_state.json")

DEFAULT_STATE: Dict[str, Any] = {
    "version": 1,
    "last_updated": datetime.now(timezone.utc).isoformat(),
    "mqtt": {
        "host": "",
        "port": 8883,
        "keepalive": 60,
        "username": "",
        "password": "",
        "ca_cert": "",
        "auto_connect": False,
        "is_connected": False,
    },
    "tcp": {
        "host": "",
        "port": 9000,
        "delimiter": "\n",
    },
    "webhook": {
        "url": "",
        "header": "",
        "timeout": 5,
    },
    "ip_cameras": [],
    "communication_endpoints": [],
    "plc_actions": [],
    "action_trigger": {
        "line1_position": 0.35,
        "line2_position": 0.65,
        "orientation": "horizontal",
        "expected_classes": ["bottle", "can", "cup", "box"],
        "defect_classes": ["defect", "scratch", "broken"],
        "send_mqtt": False,
        "mqtt_topic": "factory/inspection/wireline",
        "send_tcp": False,
        "tcp_host": "",
        "tcp_port": 9000,
        "send_webhook": False,
        "webhook_url": "",
        "dispatch_trigger": "both",
        "dispatched_fields": None,
        "mqtt_dispatch_trigger": "both",
        "mqtt_dispatched_fields": None,
        "tcp_dispatch_trigger": "both",
        "tcp_dispatched_fields": None,
        "webhook_dispatch_trigger": "both",
        "webhook_dispatched_fields": None,
    },
    "active_camera_id": None,
    "active_model_id": None,
    "camera_auto_connect": False,
    "audit_logs": [],
}


class SettingsPersistenceService:
    _state: Dict[str, Any] = {}
    _recent_changes: List[Dict[str, Any]] = []
    _boot_id: str = str(uuid.uuid4())[:8]
    _boot_time: str = datetime.now(timezone.utc).isoformat()

    @classmethod
    def _prune_expired_audit_logs(cls) -> None:
        """Removes all audit log entries older than 24 hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        logs = cls._state.get("audit_logs", [])
        retained = []
        for entry in logs:
            ts_str = entry.get("timestamp")
            if not ts_str:
                continue
            try:
                entry_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                if entry_dt >= cutoff:
                    retained.append(entry)
            except Exception:
                continue
        cls._state["audit_logs"] = retained

    @classmethod
    def load(cls) -> Dict[str, Any]:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    # Merge with default state structure
                    merged = DEFAULT_STATE.copy()
                    merged.update(loaded)
                    cls._state = merged
                    cls._prune_expired_audit_logs()
                    cls._apply_to_runtime_services()
                    return cls._state
            except Exception as exc:
                logger.error("Error loading system_state.json: %s. Using default state.", exc)

        cls._state = DEFAULT_STATE.copy()
        cls._state["audit_logs"] = [
            {
                "id": str(uuid.uuid4())[:8],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "username": "system",
                "role": "SYSTEM",
                "clearance_level": 3,
                "action": "SYSTEM_INIT",
                "category": "System",
                "details": "Initialized factory state and persistent telemetry engine.",
            }
        ]
        cls.save()
        return cls._state

    @classmethod
    def save(cls) -> None:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            cls._prune_expired_audit_logs()
            cls._state["last_updated"] = datetime.now(timezone.utc).isoformat()
            cls._state["version"] = cls._state.get("version", 0) + 1
            temp_file = STATE_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(cls._state, f, indent=2)
            if os.path.exists(STATE_FILE):
                os.replace(temp_file, STATE_FILE)
            else:
                os.rename(temp_file, STATE_FILE)
        except Exception as exc:
            logger.error("Failed to save system state: %s", exc)

    @classmethod
    def bump_version(cls) -> None:
        """Increment the in-memory version counter without writing to disk.
        Used by PLCDispatcherService to signal status changes to poll-changes."""
        if cls._state:
            cls._state["version"] = cls._state.get("version", 0) + 1

    @classmethod
    def get_state(cls) -> Dict[str, Any]:
        if not cls._state:
            cls.load()
        state_copy = cls._state.copy()
        state_copy["boot_id"] = cls._boot_id
        state_copy["boot_time"] = cls._boot_time
        return state_copy

    @classmethod
    def get_plc_actions(cls) -> List[Dict[str, Any]]:
        """Return a detached copy of the saved PLC action-card configuration.

        ``get_state()`` intentionally returns a shallow copy for legacy callers.
        PLC cards must not be mutated through that view: assigning its
        ``plc_actions`` key only updates the copy and is lost when ``save()``
        writes the authoritative ``_state``. Keep this executable
        configuration behind explicit accessors instead.
        """
        if not cls._state:
            cls.load()

        # ``action_trigger.plc_actions`` was used by an earlier dashboard
        # implementation. Only consult it when the canonical key is absent;
        # an intentionally saved empty list must remain empty after restart.
        raw_cards = cls._state.get("plc_actions")
        if not isinstance(raw_cards, list):
            legacy = cls._state.get("action_trigger", {})
            raw_cards = legacy.get("plc_actions", []) if isinstance(legacy, dict) else []
        if not isinstance(raw_cards, list):
            return []
        return copy.deepcopy(raw_cards)

    @classmethod
    def replace_plc_actions(cls, cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Replace PLC cards in the authoritative state object.

        The return value is detached so callers cannot mutate the saved
        configuration after it has been handed to the dispatcher.
        """
        if not cls._state:
            cls.load()
        if not isinstance(cards, list) or any(not isinstance(card, dict) for card in cards):
            raise ValueError("PLC action cards must be a list of objects")

        saved_cards = copy.deepcopy(cards)
        cls._state["plc_actions"] = saved_cards

        # Prevent a stale legacy copy from resurrecting deleted cards for code
        # running against an older persisted state shape.
        action_trigger = cls._state.get("action_trigger")
        if isinstance(action_trigger, dict):
            action_trigger.pop("plc_actions", None)

        return copy.deepcopy(saved_cards)

    @classmethod
    def redact_secrets(cls, value: Any) -> Any:
        """Return a deep-copied API-safe view without credentials or auth headers."""
        sensitive = {
            "password", "headers", "header", "authorization", "token", "secret", "private_key",
            "api_key", "client_secret", "access_token", "refresh_token", "credentials",
        }
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                key_lower = str(key).lower()
                if key_lower == "password":
                    redacted["password_configured"] = bool(item)
                    continue
                if key_lower in sensitive:
                    continue
                if isinstance(item, str) and (key_lower in {"url", "source"} or key_lower.endswith("_url")):
                    redacted[key] = _redact_url(item)
                else:
                    redacted[key] = cls.redact_secrets(item)
            return redacted
        if isinstance(value, list):
            return [cls.redact_secrets(item) for item in value]
        return copy.deepcopy(value)


    @classmethod
    def update_settings(
        cls,
        updates: Dict[str, Any],
        username: str = "admin",
        role: str = "admin",
        clearance_level: int = 3,
    ) -> Dict[str, Any]:
        if not cls._state:
            cls.load()

        modified_categories = []
        change_summaries = []

        for key in ["mqtt", "tcp", "webhook", "action_trigger", "active_camera_id", "active_model_id", "camera_auto_connect"]:
            if key in updates:
                if isinstance(updates[key], dict) and isinstance(cls._state.get(key), dict):
                    safe_updates = copy.deepcopy(updates[key])
                    for field, field_value in safe_updates.items():
                        if (field == "url" or field.endswith("_url")) and isinstance(field_value, str):
                            old_url = cls._state[key].get(field)
                            if isinstance(old_url, str):
                                safe_updates[field] = _restore_redacted_url(field_value, old_url)
                    cls._state[key].update(safe_updates)
                else:
                    cls._state[key] = updates[key]
                modified_categories.append(key.upper())

                if key == "action_trigger":
                    at = updates[key]
                    parts = []
                    if "line1_position" in at or "line2_position" in at or "orientation" in at:
                        l1 = at.get("line1_position", cls._state["action_trigger"].get("line1_position"))
                        l2 = at.get("line2_position", cls._state["action_trigger"].get("line2_position"))
                        ori = at.get("orientation", cls._state["action_trigger"].get("orientation", "horizontal"))
                        parts.append(f"lines=[{l1}, {l2}], ori={ori}")
                    if "expected_classes" in at:
                        parts.append(f"expected={at['expected_classes']}")
                    if "defect_classes" in at:
                        parts.append(f"defects={at['defect_classes']}")
                    if "send_mqtt" in at:
                        parts.append(f"mqtt={'on' if at['send_mqtt'] else 'off'}")
                    if "mqtt_endpoint_id" in at:
                        parts.append(f"mqtt_target='{at['mqtt_endpoint_id']}'")
                    if "send_tcp" in at:
                        parts.append(f"tcp={'on' if at['send_tcp'] else 'off'}")
                    if "tcp_endpoint_id" in at:
                        parts.append(f"tcp_target='{at['tcp_endpoint_id']}'")
                    if "send_webhook" in at:
                        parts.append(f"webhook={'on' if at['send_webhook'] else 'off'}")
                    if "webhook_endpoint_id" in at:
                        parts.append(f"webhook_target='{at['webhook_endpoint_id']}'")
                    if "plc_actions" in at:
                        saved_cards = cls.replace_plc_actions(at["plc_actions"])
                        try:
                            from app.services.plc_dispatcher_service import PLCDispatcherService
                            PLCDispatcherService.set_cards(saved_cards)
                            logger.debug("PLCDispatcherService hot-reloaded %d card(s) from action_trigger save", len(saved_cards))
                        except Exception as plc_err:
                            logger.debug("PLC dispatcher reload error: %s", plc_err)
                    change_summaries.append(f"Action Trigger ({', '.join(parts)})")
                elif key == "mqtt":
                    h = updates[key].get("host", cls._state.get("mqtt", {}).get("host", ""))
                    p = updates[key].get("port", cls._state.get("mqtt", {}).get("port", 1883))
                    change_summaries.append(f"MQTT ({h}:{p})")
                elif key == "tcp":
                    h = updates[key].get("host", cls._state.get("tcp", {}).get("host", ""))
                    p = updates[key].get("port", cls._state.get("tcp", {}).get("port", 9000))
                    change_summaries.append(f"TCP ({h}:{p})")
                elif key == "webhook":
                    u = updates[key].get("url", "")
                    change_summaries.append(f"Webhook ({_redact_url(u) if isinstance(u, str) else 'configured'})")
                elif key == "active_camera_id":
                    change_summaries.append(f"Active Camera -> {updates[key]}")
                elif key == "active_model_id":
                    change_summaries.append(f"Active Model -> {updates[key]}")

        if "ip_cameras" in updates:
            existing_cameras = {
                camera.get("id"): camera
                for camera in cls._state.get("ip_cameras", [])
                if isinstance(camera, dict)
            }
            camera_updates = copy.deepcopy(updates["ip_cameras"])
            for camera in camera_updates:
                if not isinstance(camera, dict) or not isinstance(camera.get("source"), str):
                    continue
                old_camera = existing_cameras.get(camera.get("id"))
                if old_camera and isinstance(old_camera.get("source"), str):
                    camera["source"] = _restore_redacted_url(camera["source"], old_camera["source"])
            cls._state["ip_cameras"] = camera_updates
            modified_categories.append("IP_CAMERAS")
            change_summaries.append(f"IP Cameras list ({len(updates['ip_cameras'])} configured)")

        # Sync changes into runtime services immediately
        cls._apply_to_runtime_services()

        # Record audit log & change broadcast event with detailed summary
        cat_str = ", ".join(modified_categories) if modified_categories else "CONFIGURATION"
        summary_str = "; ".join(change_summaries) if change_summaries else f"Updated settings for: {cat_str}"

        audit_event = cls.record_audit(
            username=username,
            role=role,
            clearance_level=clearance_level,
            action="UPDATE_CONFIG",
            category=cat_str,
            details=summary_str,
        )

        cls.save()
        return cls._state

    @classmethod
    def record_audit(
        cls,
        username: str,
        role: str,
        clearance_level: int,
        action: str,
        category: str,
        details: str,
    ) -> Dict[str, Any]:
        if not cls._state:
            cls.load()

        cls._prune_expired_audit_logs()

        event = {
            "id": str(uuid.uuid4())[:8],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "username": username,
            "role": role.upper() if role else "USER",
            "clearance_level": clearance_level,
            "action": action,
            "category": category,
            "details": details,
        }

        logs = cls._state.setdefault("audit_logs", [])
        logs.insert(0, event)
        if len(logs) > 500:
            cls._state["audit_logs"] = logs[:500]

        # Record in recent changes ring for multi-client polling
        cls._recent_changes.append(event)
        if len(cls._recent_changes) > 50:
            cls._recent_changes = cls._recent_changes[-50:]

        return event

    @classmethod
    def get_audit_logs(cls, limit: int = 100) -> List[Dict[str, Any]]:
        state = cls.get_state()
        cls._prune_expired_audit_logs()
        return state.get("audit_logs", [])[:limit]

    @classmethod
    def get_changes_since(cls, since_version: int, client_boot_id: Optional[str] = None) -> Dict[str, Any]:
        state = cls.get_state()
        cur_ver = state.get("version", 1)

        # Detect if the server restarted since the client connected
        if client_boot_id and client_boot_id != cls._boot_id:
            return {
                "version": cur_ver,
                "boot_id": cls._boot_id,
                "server_restarted": True,
                "has_changes": True,
                "recent_events": [
                    {
                        "id": "reboot",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "username": "system",
                        "role": "SYSTEM",
                        "clearance_level": 3,
                        "action": "SERVER_RESTART",
                        "category": "SYSTEM",
                        "details": "Server restarted; synchronized all configuration to UI.",
                    }
                ],
                "settings": {
                    "mqtt": state.get("mqtt"),
                    "tcp": state.get("tcp"),
                    "webhook": state.get("webhook"),
                    "action_trigger": state.get("action_trigger"),
                    "ip_cameras": state.get("ip_cameras"),
                    "active_camera_id": state.get("active_camera_id"),
                    "communication_endpoints": state.get("communication_endpoints", []),
                },
            }

        if since_version >= cur_ver:
            return {
                "version": cur_ver,
                "boot_id": cls._boot_id,
                "server_restarted": False,
                "has_changes": False,
                "recent_events": [],
                "settings": None,
            }

        return {
            "version": cur_ver,
            "boot_id": cls._boot_id,
            "server_restarted": False,
            "has_changes": True,
            "recent_events": cls._recent_changes[-10:],
            "settings": {
                "mqtt": state.get("mqtt"),
                "tcp": state.get("tcp"),
                "webhook": state.get("webhook"),
                "action_trigger": state.get("action_trigger"),
                "ip_cameras": state.get("ip_cameras"),
                "active_camera_id": state.get("active_camera_id"),
                "communication_endpoints": state.get("communication_endpoints", []),
            },
        }

    @classmethod
    def add_or_update_ip_camera(
        cls,
        cam_data: Dict[str, Any],
        username: str = "admin",
        role: str = "admin",
        clearance_level: int = 3,
    ) -> Dict[str, Any]:
        if not cls._state:
            cls.load()
        cameras: List[Dict[str, Any]] = cls._state.setdefault("ip_cameras", [])

        cam_id = cam_data.get("id") or f"ip-cam-{str(uuid.uuid4())[:8]}"
        existing = next((c for c in cameras if c.get("id") == cam_id), None)

        record = {
            "id": cam_id,
            "name": cam_data.get("name") or f"IP Camera {len(cameras)+1}",
            "type": "ip",
            "source": _restore_redacted_url(
                cam_data.get("source", "").strip(),
                existing.get("source", "") if existing else "",
            ),
            "is_active": cam_data.get("is_active", False),
            "auto_connect": cam_data.get("auto_connect", True),
            "description": cam_data.get("description", "Configured IP stream"),
        }

        if existing:
            existing.update(record)
            action = "UPDATE_IP_CAMERA"
        else:
            cameras.append(record)
            action = "ADD_IP_CAMERA"

        cls.record_audit(
            username=username,
            role=role,
            clearance_level=clearance_level,
            action=action,
            category="CAMERAS",
            details=f"{action}: '{record['name']}' [source configured]",
        )
        cls.save()
        return record

    @classmethod
    def delete_ip_camera(
        cls,
        cam_id: str,
        username: str = "admin",
        role: str = "admin",
        clearance_level: int = 3,
        source: str = "",
        name: str = "",
    ) -> bool:
        """Remove an IP camera entry from persistent state (both ip_cameras and communication_endpoints)."""
        if not cls._state:
            cls.load()
        cameras: List[Dict[str, Any]] = cls._state.get("ip_cameras", [])
        original_len = len(cameras)

        # Find target camera info if not provided
        target_cam = next((c for c in cameras if c.get("id") == cam_id or c.get("name") == cam_id), None)
        target_source = (source or (target_cam.get("source") if target_cam else "") or "").strip()
        target_name = (name or (target_cam.get("name") if target_cam else "") or "").strip()

        cls._state["ip_cameras"] = [
            c for c in cameras
            if c.get("id") != cam_id
            and c.get("name") != cam_id
            and (not target_source or c.get("source") != target_source)
        ]
        removed = len(cls._state["ip_cameras"]) < original_len

        # Also remove from communication_endpoints list
        comm_endpoints = cls._state.setdefault("communication_endpoints", [])
        orig_comm_len = len(comm_endpoints)
        cls._state["communication_endpoints"] = [
            ep for ep in comm_endpoints
            if not (
                ep.get("protocol") == "ipcam"
                and (
                    ep.get("id") == cam_id
                    or ep.get("name") == cam_id
                    or (target_source and ep.get("source") == target_source)
                    or (target_name and ep.get("name") == target_name)
                )
            )
        ]
        if len(cls._state["communication_endpoints"]) < orig_comm_len:
            removed = True

        if not removed:
            return False

        # Clear active camera reference if it was this camera
        if cls._state.get("active_camera_id") == cam_id:
            cls._state["active_camera_id"] = None
            cls._state["camera_auto_connect"] = False

        # Remove runtime driver if present
        try:
            from app.state.application_state import app_state
            for cid in [cam_id, target_name]:
                if cid in app_state.cameras:
                    driver = app_state.cameras.pop(cid)
                    try:
                        driver.disconnect()
                    except Exception:
                        pass
        except Exception:
            pass

        cls.record_audit(
            username=username,
            role=role,
            clearance_level=clearance_level,
            action="DELETE_IP_CAMERA",
            category="CAMERAS",
            details=f"Deleted IP camera id='{cam_id}'",
        )
        cls.save()
        return True

    @classmethod
    def get_endpoints(cls, protocol: Optional[str] = None) -> List[Dict[str, Any]]:
        state = cls.get_state()
        endpoints: List[Dict[str, Any]] = state.setdefault("communication_endpoints", [])
        if protocol:
            return [ep for ep in endpoints if ep.get("protocol", "").lower() == protocol.lower()]
        return endpoints

    @classmethod
    async def add_or_update_endpoint(
        cls,
        endpoint_data: Dict[str, Any],
        username: str = "admin",
        role: str = "admin",
        clearance_level: int = 3,
    ) -> Dict[str, Any]:
        if not cls._state:
            cls.load()
        state = cls._state
        endpoints: List[Dict[str, Any]] = state.setdefault("communication_endpoints", [])

        raw_protocol = endpoint_data.get("protocol", "tcp")
        if not isinstance(raw_protocol, str):
            raise ValueError("Protocol must be text")
        proto = raw_protocol.strip().lower()
        if proto not in {"tcp", "mqtt", "ipcam", "webhook", "plc"}:
            raise ValueError(f"Unsupported endpoint protocol: {proto or '(empty)'}")

        requested_id = endpoint_data.get("id")
        ep_id = requested_id or f"{proto}-{str(uuid.uuid4())[:8]}"
        if not isinstance(ep_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", ep_id):
            raise ValueError("Endpoint ID contains invalid characters")
        existing = next((ep for ep in endpoints if ep.get("id") == ep_id), None)
        previous_protocol = existing.get("protocol") if existing else None
        enabled = endpoint_data.get("enabled", (existing or {}).get("enabled", True))
        if not isinstance(enabled, bool):
            raise ValueError("Endpoint enabled must be true or false")
        name = endpoint_data.get("name", (existing or {}).get("name", f"Channel {len(endpoints)+1}"))
        description = endpoint_data.get("description", (existing or {}).get("description", ""))
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError("Channel name must contain 1 to 128 characters")
        if not isinstance(description, str) or len(description) > 1024:
            raise ValueError("Channel description must be at most 1024 characters")

        record: Dict[str, Any] = {
            "id": ep_id,
            "name": name.strip(),
            "protocol": proto,
            "enabled": enabled,
            "description": description,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        # Protocol-specific field mappings
        if proto == "tcp":
            record.update({
                "host": _endpoint_host(endpoint_data.get("host", (existing or {}).get("host", "")), "TCP host", required=record["enabled"]),
                "port": _endpoint_port(endpoint_data.get("port", (existing or {}).get("port")), "TCP port", 9000),
                "delimiter": endpoint_data.get("delimiter", "\\n"),
                "mode": endpoint_data.get("mode", "client"),
                "timeout": _bounded_int(endpoint_data.get("timeout"), "TCP timeout", 5, 1, 30),
            })
            # Sync to global tcp state if enabled
            if record["enabled"]:
                state["tcp"] = {"host": record["host"], "port": record["port"], "delimiter": record["delimiter"]}
        elif proto == "mqtt":
            mqtt_host = _endpoint_host(endpoint_data.get("host", (existing or {}).get("host", "")), "MQTT host", required=record["enabled"])
            tls_enabled = _endpoint_bool(endpoint_data.get("tls_enabled"), "MQTT TLS", False)
            if _endpoint_bool(endpoint_data.get("tls_insecure"), "Insecure MQTT TLS", False):
                raise ValueError("MQTT certificate verification cannot be disabled")
            from app.services.mqtt_service import _resolve_cert

            cert_refs: Dict[str, str] = {}
            for field in ("ca_cert_filename", "client_cert_filename", "client_key_filename"):
                value = endpoint_data.get(field, (existing or {}).get(field, "")) or ""
                if not isinstance(value, str):
                    raise ValueError(f"{field} must be a filename")
                value = value.strip()
                if value:
                    _resolve_cert(value)
                cert_refs[field] = value
            record.update({
                "host": mqtt_host,
                "port": _endpoint_port(endpoint_data.get("port", (existing or {}).get("port")), "MQTT port", 8883),
                "tls_enabled": tls_enabled,
                "tls_insecure": False,
                "sni_server_name": _endpoint_host(endpoint_data.get("sni_server_name", (existing or {}).get("sni_server_name", "")), "MQTT SNI server name", required=False),
                "username": _endpoint_text(endpoint_data.get("username", (existing or {}).get("username", "")), "MQTT username", maximum=256),
                "password": _endpoint_text(endpoint_data.get("password") or (existing or {}).get("password", ""), "MQTT password", maximum=512),
                **cert_refs,
                "client_id": _endpoint_text(endpoint_data.get("client_id", (existing or {}).get("client_id", f"vision_comm_{ep_id[:6]}")), "MQTT client ID", maximum=128),
                "keepalive": _bounded_int(endpoint_data.get("keepalive"), "MQTT keepalive", 60, 5, 3600),
                "protocol_version": _endpoint_text(endpoint_data.get("protocol_version", (existing or {}).get("protocol_version", "3.1.1")), "MQTT protocol version", maximum=8),
                "clean_session": _endpoint_bool(endpoint_data.get("clean_session", (existing or {}).get("clean_session")), "MQTT clean session", True),
                "qos": _bounded_int(endpoint_data.get("qos"), "MQTT QoS", 1, 0, 2),
                "retain": _endpoint_bool(endpoint_data.get("retain", (existing or {}).get("retain")), "MQTT retain", False),
                "topic": _endpoint_text(endpoint_data.get("topic", (existing or {}).get("topic", "factory/inspection/wireline")), "MQTT topic", maximum=512, required=True),
                "birth_topic": _endpoint_text(endpoint_data.get("birth_topic", (existing or {}).get("birth_topic", "")), "MQTT birth topic", maximum=512),
                "birth_payload": _endpoint_payload(endpoint_data.get("birth_payload", (existing or {}).get("birth_payload", "")), "MQTT birth payload"),
                "birth_qos": _bounded_int(endpoint_data.get("birth_qos"), "MQTT birth QoS", 0, 0, 2),
                "birth_retain": _endpoint_bool(endpoint_data.get("birth_retain", (existing or {}).get("birth_retain")), "MQTT birth retain", False),
                "close_topic": _endpoint_text(endpoint_data.get("close_topic", (existing or {}).get("close_topic", "")), "MQTT close topic", maximum=512),
                "close_payload": _endpoint_payload(endpoint_data.get("close_payload", (existing or {}).get("close_payload", "")), "MQTT close payload"),
                "close_qos": _bounded_int(endpoint_data.get("close_qos"), "MQTT close QoS", 0, 0, 2),
                "close_retain": _endpoint_bool(endpoint_data.get("close_retain", (existing or {}).get("close_retain")), "MQTT close retain", False),
                "will_topic": _endpoint_text(endpoint_data.get("will_topic", (existing or {}).get("will_topic", "")), "MQTT will topic", maximum=512),
                "will_payload": _endpoint_payload(endpoint_data.get("will_payload", (existing or {}).get("will_payload", "")), "MQTT will payload"),
                "will_qos": _bounded_int(endpoint_data.get("will_qos"), "MQTT will QoS", 0, 0, 2),
                "will_retain": _endpoint_bool(endpoint_data.get("will_retain", (existing or {}).get("will_retain")), "MQTT will retain", False),
            })
            if record["protocol_version"].lower() not in {"3.1.1", "5.0"}:
                raise ValueError("MQTT protocol version must be 3.1.1 or 5.0")
            state["mqtt"] = {
                "host": record["host"],
                "port": record["port"],
                "keepalive": record["keepalive"],
                "username": record["username"],
                "password": record["password"],
                "ca_cert": record["ca_cert_filename"],
                "client_cert": record["client_cert_filename"],
                "client_key": record["client_key_filename"],
                "tls_enabled": record["tls_enabled"],
                "auto_connect": bool(record["enabled"] and record["host"]),
                "is_connected": state.get("mqtt", {}).get("is_connected", False),
            }
        elif proto == "ipcam":
            source = _endpoint_text(
                endpoint_data.get("source", (existing or {}).get("source", "")),
                "IP camera source",
                maximum=2048,
                required=record["enabled"],
            )
            if existing and isinstance(existing.get("source"), str):
                source = _restore_redacted_url(source, existing["source"])
            if source:
                try:
                    parsed_source = urlsplit(source)
                except ValueError as exc:
                    raise ValueError("IP camera source URL is invalid") from exc
                if parsed_source.scheme.lower() not in {"rtsp", "rtsps", "http", "https"} or not parsed_source.hostname:
                    raise ValueError("IP camera source must be an RTSP, RTSPS, HTTP, or HTTPS URL")
            record.update({
                "source": source,
                "transport": _endpoint_text(endpoint_data.get("transport", (existing or {}).get("transport", "tcp")), "IP camera transport", maximum=16),
                "resolution": _endpoint_text(endpoint_data.get("resolution", (existing or {}).get("resolution", "640x480")), "IP camera resolution", maximum=32),
                "buffer_size": _bounded_int(endpoint_data.get("buffer_size"), "IP camera buffer size", 1, 1, 16),
            })
            # Sync into ip_cameras list
            cls.add_or_update_ip_camera({
                "id": record["id"],
                "name": record["name"],
                "source": record["source"],
                "is_active": False,
                "auto_connect": record["enabled"],
                "description": record["description"],
            }, username=username, role=role, clearance_level=clearance_level)
            if not record["enabled"] and state.get("active_camera_id") == record["id"]:
                state["camera_auto_connect"] = False
        elif proto == "webhook":
            url = _endpoint_text(endpoint_data.get("url", (existing or {}).get("url", "")), "Webhook URL", maximum=2048, required=record["enabled"])
            if existing and isinstance(existing.get("url"), str):
                url = _restore_redacted_url(url, existing["url"])
            method = _endpoint_text(endpoint_data.get("method", (existing or {}).get("method", "POST")), "Webhook method", maximum=8).upper()
            if method not in {"POST", "PUT"}:
                raise ValueError("Webhook method must be POST or PUT")
            if url:
                try:
                    parsed_url = urlsplit(url)
                except ValueError as exc:
                    raise ValueError("Webhook URL is invalid") from exc
                if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
                    raise ValueError("Webhook URL must be HTTP(S) and cannot include embedded credentials")
            headers = endpoint_data.get("headers") or (existing or {}).get("headers", "Content-Type: application/json")
            if not isinstance(headers, str) or len(headers) > 8192 or "\r" in headers:
                raise ValueError("Webhook headers must be text under 8192 characters")
            record.update({
                "url": url,
                "method": method,
                "headers": headers,
                "timeout": _bounded_int(endpoint_data.get("timeout"), "Webhook timeout", 5, 1, 30),
                "retry_count": _bounded_int(endpoint_data.get("retry_count"), "Webhook retry count", 2, 0, 5),
            })
            if record["enabled"]:
                state["webhook"] = {
                    "url": record["url"],
                    "header": record["headers"],
                    "timeout": record["timeout"],
                }
        elif proto == "plc":
            # Accept plc_protocol (UI name) as alias for plc_sub_protocol
            raw_sub_proto = (
                endpoint_data.get("plc_sub_protocol")
                or endpoint_data.get("plc_protocol")
                or "modbus_tcp"
            )
            if not isinstance(raw_sub_proto, str):
                raise ValueError("PLC protocol must be text")
            sub_proto = raw_sub_proto.strip().lower()
            if sub_proto not in {"modbus_tcp", "modbus", "s7", "siemens_s7", "siemens", "ethernet_ip", "ethernetip", "eip", "ab", "opcua", "opc_ua", "generic_tcp", "generic", "tcp"}:
                raise ValueError(f"Unsupported PLC protocol: {sub_proto}")
            opcua_path = _endpoint_text(
                endpoint_data.get("opcua_path", (existing or {}).get("opcua_path", "")),
                "OPC UA endpoint path",
                maximum=256,
            )
            if any(char in opcua_path for char in ("://", "?", "#")):
                raise ValueError("OPC UA endpoint path must be a URL path, not a full URL or query")
            if opcua_path and not opcua_path.startswith("/"):
                opcua_path = f"/{opcua_path}"
            opcua_security = _endpoint_text(
                endpoint_data.get("opcua_security", (existing or {}).get("opcua_security", "None")),
                "OPC UA security policy",
                maximum=32,
            ) or "None"
            if opcua_security not in {"None", "Basic256Sha256_Sign", "Basic256Sha256_SignAndEncrypt"}:
                raise ValueError("Unsupported OPC UA security policy")
            default_plc_port = 4840 if sub_proto in {"opcua", "opc_ua"} else 502
            record.update({
                "host": _endpoint_host(endpoint_data.get("host", (existing or {}).get("host", "")), "PLC host", required=record["enabled"]),
                "port": _endpoint_port(endpoint_data.get("port", (existing or {}).get("port")), "PLC port", default_plc_port),
                "plc_sub_protocol": sub_proto,
                "plc_protocol": sub_proto,  # keep UI alias in sync
                "plc_slot": _bounded_int(endpoint_data.get("plc_slot", endpoint_data.get("s7_slot", endpoint_data.get("eip_slot"))), "PLC slot", 0, 0, 255),
                "plc_rack": _bounded_int(endpoint_data.get("plc_rack", endpoint_data.get("s7_rack")), "PLC rack", 0, 0, 7),
                "timeout": _bounded_int(endpoint_data.get("timeout"), "PLC timeout", 3, 1, 30),
                "heartbeat": _bounded_int(endpoint_data.get("heartbeat"), "PLC heartbeat", 10, 1, 3600),
                "reconnect_interval": _bounded_int(endpoint_data.get("reconnect_interval"), "PLC reconnect interval", 5, 1, 300),
                # Modbus TCP
                "modbus_unit_id": _bounded_int(endpoint_data.get("modbus_unit_id"), "Modbus unit ID", 1, 1, 247),
                "modbus_zero_base": _endpoint_bool(endpoint_data.get("modbus_zero_base"), "Modbus zero-based addressing", False),
                "simulation_mode": _endpoint_bool(endpoint_data.get("simulation_mode", (existing or {}).get("simulation_mode", False)), "PLC simulation mode", False),
                # Siemens S7
                "s7_model": _endpoint_text(endpoint_data.get("s7_model", (existing or {}).get("s7_model", "s7-1200")), "S7 model", maximum=32),
                "s7_rack": _bounded_int(endpoint_data.get("s7_rack"), "S7 rack", 0, 0, 7),
                "s7_slot": _bounded_int(endpoint_data.get("s7_slot"), "S7 slot", 1, 0, 31),
                # EtherNet/IP
                "eip_slot": _bounded_int(endpoint_data.get("eip_slot"), "EtherNet/IP slot", 0, 0, 255),
                "eip_path": _endpoint_text(endpoint_data.get("eip_path", (existing or {}).get("eip_path", "1,0")), "EtherNet/IP path", maximum=128),
                # OPC UA
                "opcua_path": opcua_path,
                "opcua_security": opcua_security,
                # MELSEC SLMP
                "melsec_network": _bounded_int(endpoint_data.get("melsec_network"), "MELSEC network", 0, 0, 255),
                "melsec_station": _bounded_int(endpoint_data.get("melsec_station"), "MELSEC station", 255, 0, 255),
                # Omron FINS
                "fins_network": _bounded_int(endpoint_data.get("fins_network"), "FINS network", 0, 0, 127),
                "fins_node": _bounded_int(endpoint_data.get("fins_node"), "FINS node", 1, 0, 255),
                "fins_unit": _bounded_int(endpoint_data.get("fins_unit"), "FINS unit", 0, 0, 31),
                # Optional raw frame templates for GenericTCP
                "set_template": _endpoint_text(endpoint_data.get("set_template", (existing or {}).get("set_template", "")), "PLC set template", maximum=2048),
                "reset_template": _endpoint_text(endpoint_data.get("reset_template", (existing or {}).get("reset_template", "")), "PLC reset template", maximum=2048),
                "toggle_template": _endpoint_text(endpoint_data.get("toggle_template", (existing or {}).get("toggle_template", "")), "PLC toggle template", maximum=2048),
                "write_template": _endpoint_text(endpoint_data.get("write_template", (existing or {}).get("write_template", "")), "PLC write template", maximum=2048),
            })

        if existing:
            existing.update(record)
            existing.pop("last_test_result", None)
            action = "UPDATE_COMM_ENDPOINT"
        else:
            endpoints.append(record)
            action = "ADD_COMM_ENDPOINT"

        cls.record_audit(
            username=username,
            role=role,
            clearance_level=clearance_level,
            action=action,
            category="COMMUNICATIONS",
            details=f"{action}: '{record['name']}' [{record['protocol'].upper()}]",
        )
        cls.save()
        if proto == "plc" or previous_protocol == "plc":
            try:
                from app.hardware.plc.factory import PLCDriverFactory
                PLCDriverFactory.invalidate(ep_id)
            except Exception as exc:
                logger.warning("Could not invalidate cached PLC driver for endpoint %s: %s", ep_id, exc)
        return record

    @classmethod
    def delete_endpoint(
        cls,
        endpoint_id: str,
        username: str = "admin",
        role: str = "admin",
        clearance_level: int = 3,
    ) -> bool:
        if not cls._state:
            cls.load()
        endpoints: List[Dict[str, Any]] = cls._state.setdefault("communication_endpoints", [])
        initial_len = len(endpoints)
        target = next((ep for ep in endpoints if ep.get("id") == endpoint_id), None)
        cls._state["communication_endpoints"] = [ep for ep in endpoints if ep.get("id") != endpoint_id]

        if len(cls._state["communication_endpoints"]) < initial_len:
            if target and target.get("protocol") == "ipcam":
                cls.delete_ip_camera(
                    endpoint_id,
                    username=username,
                    role=role,
                    clearance_level=clearance_level,
                    source=target.get("source", ""),
                    name=target.get("name", ""),
                )
            if target and target.get("protocol") == "plc":
                try:
                    from app.hardware.plc.factory import PLCDriverFactory
                    PLCDriverFactory.invalidate(endpoint_id)
                except Exception as exc:
                    logger.warning("Could not invalidate cached PLC driver for endpoint %s: %s", endpoint_id, exc)
            cls.record_audit(
                username=username,
                role=role,
                clearance_level=clearance_level,
                action="DELETE_COMM_ENDPOINT",
                category="COMMUNICATIONS",
                details=f"Removed Communication Channel '{endpoint_id}'",
            )
            cls.save()
            return True
        return False

    @classmethod
    async def test_endpoint(cls, endpoint_id: str) -> Dict[str, Any]:
        """Test an endpoint and persist its latest visible status."""
        try:
            result = await cls._probe_endpoint(endpoint_id)
        except Exception as exc:
            logger.exception("Communication endpoint test failed unexpectedly for %s", endpoint_id)
            result = {"success": False, "message": f"Connection test failed ({type(exc).__name__})"}

        state = cls.get_state()
        endpoint = next(
            (item for item in state.setdefault("communication_endpoints", []) if item.get("id") == endpoint_id),
            None,
        )
        if endpoint:
            endpoint["last_test_result"] = {
                "success": bool(result.get("success")),
                "message": str(result.get("message", "Connection test failed")),
                "tested_at": datetime.now(timezone.utc).isoformat(),
            }
            cls.save()
        return result

    @classmethod
    async def _probe_endpoint(cls, endpoint_id: str) -> Dict[str, Any]:
        """Perform an async live connection/handshake test without changing state."""
        state = cls.get_state()
        endpoints: List[Dict[str, Any]] = state.setdefault("communication_endpoints", [])
        ep = next((e for e in endpoints if e.get("id") == endpoint_id), None)
        if not ep:
            return {"success": False, "message": f"Endpoint '{endpoint_id}' not found."}

        proto = ep.get("protocol", "tcp").lower()
        if proto == "tcp":
            host = ep.get("host", "127.0.0.1")
            port = int(ep.get("port", 9000))
            timeout = float(ep.get("timeout", 3.0))
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
                return {"success": True, "message": f"Successfully connected to TCP socket {host}:{port}"}
            except Exception as exc:
                return {"success": False, "message": f"TCP connection failed to {host}:{port}: {exc}"}
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

        elif proto == "mqtt":
            host = ep.get("host", "")
            port = int(ep.get("port", 1883))
            if not host:
                return {"success": False, "message": "MQTT host is empty."}
            test_client = None
            try:
                from app.hardware.mqtt.client import MQTTClient
                from app.services.mqtt_service import _resolve_cert
                test_client = MQTTClient(
                    host=host,
                    port=port,
                    username=ep.get("username") or None,
                    password=ep.get("password") or None,
                    client_id=ep.get("client_id") or "test_probe",
                    keepalive=int(ep.get("keepalive", 60)),
                    clean_session=bool(ep.get("clean_session", True)),
                    protocol_version=str(ep.get("protocol_version", "3.1.1")),
                    tls_enabled=bool(ep.get("tls_enabled")),
                    tls_insecure=False,
                    sni_server_name=ep.get("sni_server_name") or None,
                    ca_cert_path=_resolve_cert(ep.get("ca_cert_filename")),
                    client_cert_path=_resolve_cert(ep.get("client_cert_filename")),
                    client_key_path=_resolve_cert(ep.get("client_key_filename")),
                )
                res = await test_client.connect()
                if res:
                    return {"success": True, "message": f"Successfully authenticated with MQTT broker {host}:{port}"}
                return {"success": False, "message": f"Could not connect to MQTT broker {host}:{port}"}
            except Exception as exc:
                return {"success": False, "message": f"MQTT test error: {exc}"}
            finally:
                if test_client is not None:
                    try:
                        await asyncio.wait_for(test_client.disconnect(), timeout=2.0)
                    except Exception:
                        pass

        elif proto == "ipcam":
            source = ep.get("source", "")
            if not source:
                return {"success": False, "message": "IP stream source URL is empty."}

            # Check 1: If this camera is already connected and streaming in app_state, succeed immediately!
            try:
                from app.state.application_state import app_state
                clean_src = str(source).strip()
                clean_id = str(endpoint_id).strip()
                for cid, driver in list(app_state.cameras.items()):
                    drv_src = str(getattr(driver, "source", "") or getattr(driver, "url", "") or "").strip()
                    drv_id = str(getattr(driver, "camera_id", "") or cid).strip()
                    if (cid == clean_id) or (drv_id == clean_id) or (drv_src and clean_src and drv_src == clean_src):
                        if getattr(driver, "is_connected", False):
                            return {
                                "success": True,
                                "message": f"Camera is connected and actively streaming ({cid})",
                            }
            except Exception as active_chk_err:
                logger.debug("Active camera check error: %s", active_chk_err)

            # Check 2: Direct probe if not already connected
            try:
                import os
                import cv2

                def _probe_cam():
                    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
                    cam_src = int(source) if str(source).isdigit() else source
                    backend = cv2.CAP_FFMPEG if str(source).startswith(("rtsp://", "rtsps://", "http://", "https://")) else cv2.CAP_ANY
                    cap = cv2.VideoCapture(cam_src, backend)
                    try:
                        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 4000)
                        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 4000)
                    except Exception:
                        pass
                    try:
                        if not cap.isOpened():
                            return False
                        ret, frame = cap.read()
                        return bool(ret and frame is not None and getattr(frame, "size", 0) > 0)
                    finally:
                        cap.release()

                loop = asyncio.get_running_loop()
                ok = await asyncio.wait_for(loop.run_in_executor(None, _probe_cam), timeout=6.0)
                if ok:
                    return {"success": True, "message": "Successfully connected and decoded a frame from the configured IP stream"}
                return {"success": False, "message": "Could not read video frames from the configured IP stream"}
            except asyncio.TimeoutError:
                return {"success": False, "message": "Timed out connecting to the configured IP stream"}
            except Exception:
                return {"success": False, "message": "IP camera connection test failed"}

        elif proto == "webhook":
            url = ep.get("url", "")
            if not url:
                return {"success": False, "message": "Webhook URL is empty."}
            try:
                import httpx
                headers = {"Content-Type": "application/json", "User-Agent": "VisionServer/1.0"}
                for h_line in ep.get("headers", "").splitlines():
                    if ":" in h_line:
                        hk, hv = h_line.split(":", 1)
                        headers[hk.strip()] = hv.strip()
                data = {"event": "TEST_WEBHOOK_PING", "timestamp": datetime.now(timezone.utc).isoformat()}
                timeout = float(ep.get("timeout", 5.0))
                method = (ep.get("method") or "POST").upper()
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                    resp = await client.request(method, url, json=data, headers=headers)
                    return {"success": True, "message": f"Webhook responded with HTTP status {resp.status_code}"}
            except Exception as exc:
                return {"success": False, "message": f"Webhook delivery test failed ({type(exc).__name__})"}

        elif proto == "plc":
            host = ep.get("host", "")
            port = int(ep.get("port", 502))
            if str(ep.get("plc_sub_protocol") or ep.get("plc_protocol") or "").lower() in {"s7", "siemens", "siemens_s7"} and port == 502:
                port = 102
            if not host:
                return {"success": False, "message": "PLC host is empty."}
            driver = None
            try:
                from app.hardware.plc.factory import PLCDriverFactory
                driver = PLCDriverFactory.get_driver(ep, fresh=True)
                ok = await asyncio.wait_for(driver.connect(), timeout=float(ep.get("timeout", 3)))
                if ok:
                    sub = (ep.get("plc_protocol") or ep.get("plc_sub_protocol") or "modbus_tcp").upper()
                    return {"success": True, "message": f"PLC ({sub}) connected OK at {host}:{port}"}
                detail = getattr(driver, "last_error", "")
                suffix = f": {detail}" if detail else ""
                return {"success": False, "message": f"PLC connection failed at {host}:{port}{suffix}"}
            except asyncio.TimeoutError:
                return {"success": False, "message": f"PLC connection timed out at {host}:{port}"}
            except Exception as exc:
                return {"success": False, "message": f"PLC test error: {exc}"}
            finally:
                if driver is not None:
                    try:
                        await asyncio.wait_for(driver.disconnect(), timeout=2.0)
                    except Exception:
                        pass

        return {"success": False, "message": f"Unknown protocol '{proto}'"}


    @classmethod
    def _apply_to_runtime_services(cls) -> None:
        """Applies loaded state to runtime singletons."""
        try:
            from app.services.counting_service import counting_service
            from app.schemas.counting import CountingConfig
            cfg_data = cls._state.get("action_trigger", {})
            if cfg_data:
                config = CountingConfig(
                    line1_position=cfg_data.get("line1_position", 0.35),
                    line2_position=cfg_data.get("line2_position", 0.65),
                    orientation=cfg_data.get("orientation", "horizontal"),
                    expected_classes=cfg_data.get("expected_classes", ["bottle", "can", "cup"]),
                    defect_classes=cfg_data.get("defect_classes", ["defect", "scratch", "broken"]),
                    send_mqtt=cfg_data.get("send_mqtt", False),
                    mqtt_topic=cfg_data.get("mqtt_topic", "factory/inspection/wireline"),
                    send_tcp=cfg_data.get("send_tcp", False),
                    tcp_host=cfg_data.get("tcp_host", "127.0.0.1"),
                    tcp_port=cfg_data.get("tcp_port", 9000),
                    send_webhook=cfg_data.get("send_webhook", False),
                    webhook_url=cfg_data.get("webhook_url", ""),
                    mqtt_endpoint_id=cfg_data.get("mqtt_endpoint_id"),
                    tcp_endpoint_id=cfg_data.get("tcp_endpoint_id"),
                    webhook_endpoint_id=cfg_data.get("webhook_endpoint_id"),
                    dispatch_trigger=cfg_data.get("dispatch_trigger", "both"),
                    dispatched_fields=cfg_data.get("dispatched_fields"),
                    mqtt_dispatch_trigger=cfg_data.get("mqtt_dispatch_trigger", cfg_data.get("dispatch_trigger", "both")),
                    mqtt_dispatched_fields=cfg_data.get("mqtt_dispatched_fields", cfg_data.get("dispatched_fields")),
                    tcp_dispatch_trigger=cfg_data.get("tcp_dispatch_trigger", cfg_data.get("dispatch_trigger", "both")),
                    tcp_dispatched_fields=cfg_data.get("tcp_dispatched_fields", cfg_data.get("dispatched_fields")),
                    webhook_dispatch_trigger=cfg_data.get("webhook_dispatch_trigger", cfg_data.get("dispatch_trigger", "both")),
                    webhook_dispatched_fields=cfg_data.get("webhook_dispatched_fields", cfg_data.get("dispatched_fields")),
                )
                counting_service.update_config(config)
        except Exception as exc:
            logger.debug("Runtime counting_service sync: %s", exc)

    @classmethod
    async def apply_on_startup(cls) -> None:
        """Called during FastAPI lifespan startup to restore all saved state and auto-connect comms."""
        state = cls.load()
        cls._apply_to_runtime_services()
        logger.info("Restored persistent system state (version %d).", state.get("version", 1))

        if state.get("camera_auto_connect", False) is True:
            try:
                from app.db.session import AsyncSessionLocal
                from app.services.camera_service import CameraService
                from app.db.models.camera import Camera
                from sqlalchemy import select

                async with AsyncSessionLocal() as db:
                    active_cam_id = state.get("active_camera_id")
                    target_cam = None
                    if active_cam_id:
                        target_cam = await CameraService.get_camera_by_id(db, str(active_cam_id))
                    if not target_cam:
                        stmt = select(Camera).where(Camera.is_active == True).order_by(Camera.updated_at.desc()).limit(1)
                        res = await db.execute(stmt)
                        target_cam = res.scalar_one_or_none()
                    if not target_cam:
                        stmt = select(Camera).order_by(Camera.updated_at.desc()).limit(1)
                        res = await db.execute(stmt)
                        target_cam = res.scalar_one_or_none()

                    if target_cam:
                        success, err = await CameraService.connect_camera(db, target_cam.id)
                        if success:
                            cls._state["active_camera_id"] = target_cam.id
                            cls.save()
                            logger.info("Auto-connected last active camera '%s' [%s] on server startup", target_cam.name, target_cam.id)
                        else:
                            logger.warning("Failed to auto-connect last active camera '%s' on startup: %s", target_cam.name, err)
                    else:
                        logger.info("No configured camera found in database to auto-connect on startup")
            except Exception as cam_err:
                logger.warning("Camera auto-connect error on startup: %s", cam_err)


        # 1. Auto-connect MQTT from system config if enabled
        mqtt_cfg = state.get("mqtt", {})
        if mqtt_cfg.get("auto_connect", False) is True and mqtt_cfg.get("host"):
            try:
                from app.services.mqtt_service import MQTTService
                logger.info("Auto-connecting to saved MQTT Broker: %s:%s", mqtt_cfg.get("host"), mqtt_cfg.get("port"))
                await MQTTService.connect(
                    host=mqtt_cfg.get("host"),
                    port=mqtt_cfg.get("port", 8883),
                    username=mqtt_cfg.get("username"),
                    password=mqtt_cfg.get("password"),
                    tls_enabled=bool(mqtt_cfg.get("tls_enabled", True)),
                    ca_cert_filename=mqtt_cfg.get("ca_cert") or mqtt_cfg.get("ca_cert_filename") or None,
                    client_cert_filename=mqtt_cfg.get("client_cert") or mqtt_cfg.get("client_cert_filename") or None,
                    client_key_filename=mqtt_cfg.get("client_key") or mqtt_cfg.get("client_key_filename") or None,
                )
                state["mqtt"]["is_connected"] = True
            except Exception as e:
                logger.warning("Startup MQTT auto-connect failed: %s", e)

        # Load explicitly configured PLC action cards and pre-connect their drivers.
        try:
            from app.services.plc_dispatcher_service import PLCDispatcherService
            PLCDispatcherService.load_cards()
            asyncio.create_task(PLCDispatcherService.autoconnect_drivers())
            logger.info("PLCDispatcherService initialised with %d card(s)", len(PLCDispatcherService._cards))
        except Exception as plc_err:
            logger.warning("PLC dispatcher startup error: %s", plc_err)

        # Refresh the persisted connection indicators on every server boot.
        endpoints = list(state.get("communication_endpoints", []))
        test_slots = asyncio.Semaphore(5)

        async def _startup_endpoint_check(endpoint: Dict[str, Any]) -> None:
            endpoint_id = endpoint.get("id")
            if not endpoint_id:
                return
            async with test_slots:
                try:
                    result = await cls.test_endpoint(str(endpoint_id))
                    status = "All Good" if result.get("success") else "Failed"
                    logger.info("Startup test for endpoint '%s' [%s] -> %s: %s", endpoint.get("name", endpoint_id), endpoint.get("protocol", "unknown"), status, result.get("message", ""))
                except Exception:
                    logger.exception("Startup communication check failed for endpoint %s", endpoint_id)

        if endpoints:
            await asyncio.gather(*(_startup_endpoint_check(endpoint) for endpoint in endpoints))

