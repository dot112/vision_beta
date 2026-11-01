"""Saved line setups: a line's setup, the connections it uses and its cameras' image settings, kept under a name.

One JSON file per save, in data/line-setups/<line id>/<save id>.json. A save holds the line as it is saved
on the server (never a page's unsaved edits), without its live state: not running, counts or batch.
"""
from __future__ import annotations

import copy
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Set, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import storage_state

SETUP_ID = re.compile(r"setup-[0-9a-f]{8}")
MAX_NAME = 64
FORMAT = 1
# The line's live state: what it is doing now, not how it is set up.
LIVE_KEYS = ("enabled", "batch", "counts_reset_at", "status", "warnings")
_LINE_ID = re.compile(r"[A-Za-z0-9_-]+")


def _svc():
    from app.services.settings_persistence_service import SettingsPersistenceService
    return SettingsPersistenceService


def folder(line_id: str) -> Path:
    """The line's save folder, under the settings' data folder as it is now (tests and scratch copies have their own)."""
    from app.services import settings_persistence_service

    if not _LINE_ID.fullmatch(str(line_id)):
        raise KeyError(line_id)
    return Path(settings_persistence_service.DATA_DIR) / "line-setups" / str(line_id)


def _path(line_id: str, setup_id: str) -> Path:
    if not SETUP_ID.fullmatch(str(setup_id)):
        raise KeyError(setup_id)
    return folder(line_id) / f"{setup_id}.json"


def clean_name(name: Any) -> str:
    """The name as saved: trimmed, 1 to 64 characters. Raises ValueError."""
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        raise ValueError("Give the setup a name.")
    if len(name) > MAX_NAME:
        raise ValueError(f"A name has at most {MAX_NAME} characters.")
    return name


def summary(save: Dict[str, Any]) -> Dict[str, Any]:
    return {key: save.get(key) for key in ("id", "name", "line_name", "saved_at", "saved_by")}


async def _cameras(db: AsyncSession, line: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Each of the line's cameras with its image and video settings."""
    from app.services.camera_service import CameraService

    cameras = []
    for card in line.get("cameras") or []:
        camera_id = card.get("camera_id") if isinstance(card, dict) else None
        camera = await CameraService.get_camera_by_id(db, str(camera_id)) if camera_id else None
        if camera is not None:
            cameras.append({"id": camera.id, "name": camera.name, "type": camera.type,
                            "settings": copy.deepcopy(camera.settings or {})})
    return cameras


async def snapshot(db: AsyncSession, line_id: str, name: str, saved_by: str) -> Dict[str, Any]:
    """Save the line's setup as it is saved now, under a name. Returns the save's summary.

    Raises ValueError for a bad name and KeyError for a line that does not exist.
    """
    from app.services.line_config import SCHEMA_VERSION, endpoints_used_by_line

    name = clean_name(name)
    line = _svc().get_line(line_id, with_logic=True)
    if line is None:
        raise KeyError(line_id)
    for key in LIVE_KEYS:
        line.pop(key, None)
    used = endpoints_used_by_line(line)
    save = {
        "kind": "line-setup",
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "id": f"setup-{uuid.uuid4().hex[:8]}",
        "name": name,
        "line_id": line_id,
        "line_name": line.get("name") or line_id,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "saved_by": saved_by,
        "line": line,
        # As stored, passwords included: a save is kept in the same data folder as the settings file.
        "endpoints": [copy.deepcopy(e) for e in _svc().get_endpoints() if str(e.get("id")) in used],
        "cameras": await _cameras(db, line),
    }
    path = _path(line_id, save["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    storage_state.write_atomic(path, json.dumps(save, indent=2).encode("utf-8"))
    storage_state.fsync_dir(path.parent)
    return summary(save)


def read(line_id: str, setup_id: str) -> Dict[str, Any]:
    """One save of the line. Raises KeyError for a bad id, a missing save or another line's."""
    path = _path(line_id, setup_id)
    try:
        save = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KeyError(setup_id) from exc
    if not isinstance(save, dict) or save.get("kind") != "line-setup" or save.get("line_id") != line_id \
            or save.get("id") != setup_id:
        raise KeyError(setup_id)
    return save


def list_setups(line_id: str) -> List[Dict[str, Any]]:
    """The line's saves, newest first, without their contents. A file that cannot be read is left out."""
    try:
        paths = list(folder(line_id).glob("setup-*.json"))
    except KeyError:
        return []
    saves = []
    for path in paths:
        try:
            saves.append(summary(read(line_id, path.stem)))
        except KeyError:
            continue
    return sorted(saves, key=lambda s: str(s.get("saved_at") or ""), reverse=True)


def delete(line_id: str, setup_id: str) -> Dict[str, Any]:
    """Delete one save of the line. Returns its summary. Raises KeyError as read() does."""
    save = read(line_id, setup_id)
    os.remove(_path(line_id, setup_id))
    return summary(save)


# ── Load ──────────────────────────────────────────────────────────────────────

RUNNING = "Stop the line to load a setup"
NEWER = "This setup was saved by a newer version of the server"


class LoadRefused(Exception):
    """A load refused with nothing changed (409)."""


def _check(line: Dict[str, Any], save: Dict[str, Any]) -> None:
    from app.services.line_config import SCHEMA_VERSION

    if line.get("enabled"):
        raise LoadRefused(RUNNING)
    version, form = save.get("schema_version"), save.get("format")
    if not isinstance(version, int) or version > SCHEMA_VERSION or not isinstance(form, int) or form > FORMAT:
        raise LoadRefused(NEWER)


async def _add_missing_endpoints(save: Dict[str, Any], user: Any) -> List[str]:
    """Each connection of the save that no longer exists is added back with its saved id, as the
    Connections page adds one (an IP camera's record follows). One that exists is left as it is: other
    lines may use it. Gives the names of those added. Raises ValueError naming the connection."""
    have = {str(e.get("id")) for e in _svc().get_endpoints()}
    added = []
    for saved in save.get("endpoints") or []:
        if not isinstance(saved, dict) or not saved.get("id") or str(saved["id"]) in have:
            continue
        name = saved.get("name") or saved["id"]
        try:
            await _svc().add_or_update_endpoint(endpoint_data=copy.deepcopy(saved), username=user.username,
                                                role=user.role, clearance_level=user.clearance_level)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Connection '{name}' could not be added back: {exc}") from exc
        added.append(name)
    return added


def _cameras_to_keep(save: Dict[str, Any], line_id: str, existing: Set[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The saved camera cards whose camera exists (``existing``) and is on no other line, and a warning
    for each one left out."""
    names = {c.get("id"): c.get("name") for c in save.get("cameras") or [] if isinstance(c, dict)}
    kept, warnings = [], []
    for card in (save.get("line") or {}).get("cameras") or []:
        camera_id = str(card.get("camera_id") or "") if isinstance(card, dict) else ""
        name = names.get(camera_id) or camera_id
        owner = _svc().line_for_camera(camera_id) if camera_id else None
        if owner and owner != line_id:
            other = _svc().get_line(owner, with_logic=False) or {}
            warnings.append(f"Camera {name} is on {other.get('name') or owner} now: left out")
        elif camera_id not in existing:
            warnings.append(f"Camera {name} no longer exists: left out")
        else:
            kept.append(card)
    return kept, warnings


async def _put_camera_settings(db: AsyncSession, save: Dict[str, Any], kept_ids: Set[str]) -> List[str]:
    """Each kept camera gets its saved image and video settings, as its Settings would set them, except a
    Trigger that the line's card now sets. Gives a warning for a camera whose settings were refused."""
    from app.routes.v1.camera import line_trigger_mode
    from app.schemas.camera import CameraUpdate
    from app.services.camera_service import CameraService

    warnings = []
    for saved in save.get("cameras") or []:
        if not isinstance(saved, dict) or saved.get("id") not in kept_ids:
            continue
        camera = await CameraService.get_camera_by_id(db, saved["id"])
        settings = copy.deepcopy(saved.get("settings")) if isinstance(saved.get("settings"), dict) else {}
        if camera is None or not settings:
            continue
        await db.refresh(camera)    # the line's save set its trigger mode through another session
        if line_trigger_mode(camera) is not None:
            settings.pop("trigger_mode", None)
        try:
            await CameraService.update_camera(db, camera.id, CameraUpdate(settings=settings))
        except ValueError as exc:
            await db.rollback()
            warnings.append(f"Camera {saved.get('name') or camera.id}'s settings could not be put back: {exc}")
    return warnings


async def load(db: AsyncSession, line_id: str, setup_id: str, user: Any,
               update_line: Callable[[str, Dict[str, Any], Any], Awaitable[Dict[str, Any]]]) -> Dict[str, Any]:
    """Put a save back onto its line, which must be stopped.

    ``update_line`` is the line update of PUT /lines/{id} (the same checks, saving, trigger modes and
    applying). Raises KeyError (no such save of this line), LoadRefused (409), ValueError or the update's
    HTTPException (the setup is refused, with the reason). Connections already added back stay.
    """
    from app.services.camera_service import CameraService

    save = read(line_id, setup_id)
    line = _svc().get_line(line_id, with_logic=False)
    if line is None:
        raise KeyError(line_id)
    _check(line, save)
    await _add_missing_endpoints(save, user)
    ids = {str(c.get("camera_id")) for c in (save.get("line") or {}).get("cameras") or [] if isinstance(c, dict)}
    existing = {i for i in ids if await CameraService.get_camera_by_id(db, i) is not None}
    cards, warnings = _cameras_to_keep(save, line_id, existing)
    kept_ids = {str(c.get("camera_id")) for c in cards}
    body = {k: copy.deepcopy(v) for k, v in (save.get("line") or {}).items() if k not in LIVE_KEYS and k != "id"}
    body["cameras"] = cards
    # Cards that named a left-out camera keep working without it, as the line's own save requires.
    for key in ("plc_actions", "send_actions"):
        for card in body.get(key) or []:
            if isinstance(card, dict) and card.get("camera_id") and str(card["camera_id"]) not in kept_ids:
                card.pop("camera_id", None)
    view = await update_line(line_id, body, user)
    warnings += await _put_camera_settings(db, save, kept_ids)
    return {"loaded": True, "name": save.get("name"), "warnings": warnings, "line": view}
