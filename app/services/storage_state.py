"""What the server remembers about its own storage, in data/storage_state.json.

The clock guard, the open notices (damage the server repaired, records it had to delete),
when each job last ran, and rebuild work still to do. The file is written whole through
a temp file (fsync, rename) under one lock, so a power cut leaves the old file or the new
one. It holds nothing the server cannot do without: a file that does not parse is logged
and started again empty.

Also the small file helpers the other storage code shares: the data folder, the UTC time
stamp used in file and folder names, and an atomic write.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.utils.logger import LogThrottle, get_logger

logger = get_logger(__name__)
_damage_reports = LogThrottle(60.0)

# The source of every storage alarm.
STORAGE_SOURCE = "storage"
STATE_FILE_NAME = "storage_state.json"

_lock = threading.Lock()


def data_dir() -> Path:
    # Imported here because the settings service uses this module too. Read on every
    # call, so a test that points DATA_DIR at a temp folder is followed.
    from app.services import settings_persistence_service

    return Path(settings_persistence_service.DATA_DIR)


def stamp(now: datetime, micro: bool = False) -> str:
    """UTC time for a file or folder name: 20261026T001000Z (20261026T001000123456Z with micro)."""
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ" if micro else "%Y%m%dT%H%M%SZ")


def fsync_dir(path: Path) -> None:
    """Make a new file or rename in a folder survive a power cut. Windows cannot open a folder to sync it."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # A share that cannot sync a folder: the file itself was synced.
        pass


def write_atomic(path: Path, data: bytes) -> None:
    """Write a file so that it is either the old one or the whole new one, never half."""
    path = Path(path)
    partial = path.with_name(path.name + ".partial")
    try:
        with open(partial, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(partial, path)
    except BaseException:
        # No half-written file left taking space on a disk that may be full.
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    fsync_dir(path.parent)


def _read() -> Dict[str, Any]:
    path = data_dir() / STATE_FILE_NAME
    try:
        state = json.loads(path.read_bytes())
        if not isinstance(state, dict):
            raise ValueError("it does not hold a JSON object")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _damage_reports.log(logger, logging.WARNING, str(path),
                            "%s could not be read, starting it again empty: %s", path, exc)
        return {}
    return state


def load() -> Dict[str, Any]:
    """The saved state; {} when there is none or the file is damaged."""
    with _lock:
        return _read()


def update(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """Load the state, let change() edit it in place, save it, and give it back.

    The whole round trip is under one lock, so two threads cannot overwrite each other's
    edit. Nothing is written when change() left the state as it was. Raises OSError when
    the file cannot be written.
    """
    with _lock:
        state = _read()
        before = json.dumps(state, sort_keys=True)
        change(state)
        if json.dumps(state, sort_keys=True) != before:
            folder = data_dir()
            folder.mkdir(parents=True, exist_ok=True)
            write_atomic(folder / STATE_FILE_NAME, json.dumps(state, indent=2).encode("utf-8"))
        return state


def add_notice(code: str, message: str, details: Optional[Dict[str, Any]] = None,
               now: Optional[datetime] = None) -> None:
    """Keep a notice until an Admin closes it. One per code: a new one replaces the open one."""
    notice = {
        "code": code,
        "at": (now or datetime.now(timezone.utc)).isoformat(),
        "message": message,
        "details": details or {},
    }

    def change(state: Dict[str, Any]) -> None:
        state["notices"] = [n for n in state.get("notices", []) if n.get("code") != code] + [notice]

    update(change)


def close_notice(code: str) -> bool:
    """Drop the open notice with this code. False when there was none."""
    closed: List[bool] = []

    def change(state: Dict[str, Any]) -> None:
        notices = state.get("notices", [])
        kept = [n for n in notices if n.get("code") != code]
        closed.append(len(kept) != len(notices))
        if closed[0]:
            state["notices"] = kept

    update(change)
    return closed[0]


def notices() -> List[Dict[str, Any]]:
    """The open notices, oldest first: each {"code", "at", "message", "details"}."""
    return list(load().get("notices", []))
