"""The configuration lock: whether it is on, who changed it, and the lock code.

The state is `config_lock` in system_state.json, so it survives a restart. Level 2 and Admin
lock; an Admin unlocks with no code, a Level 2 user only with the lock code an Admin set.
After MAX_WRONG_CODES wrong codes in a row, codes are refused for WAIT_SECONDS (kept in
memory: a restart clears it). Which requests the lock blocks is app/security/config_lock.py.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.security import hash_password, verify_password
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService

CATEGORY = "Configuration lock"


class LockError(Exception):
    """A lock request that is refused: the HTTP status and the message for the user."""

    def __init__(self, status_code: int, detail: str, retry_after: int = 0):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


class ConfigLockService:
    MAX_WRONG_CODES = 5
    WAIT_SECONDS = 300
    CODE_MIN = 6
    CODE_MAX = 128
    clock = time.monotonic

    _lock = threading.Lock()
    _wrong_codes = 0
    _blocked_until = 0.0

    @classmethod
    def _state(cls) -> Dict[str, Any]:
        SettingsPersistenceService.get_state()  # loads the file on first use
        state = SettingsPersistenceService._state.get("config_lock")
        if not isinstance(state, dict):
            state = {}
        merged = {**DEFAULT_STATE["config_lock"], **state}
        SettingsPersistenceService._state["config_lock"] = merged
        return merged

    @classmethod
    def _retry_after(cls) -> int:
        left = cls._blocked_until - cls.clock()
        return int(left + 0.999) if left > 0 else 0

    @classmethod
    def status(cls) -> Dict[str, Any]:
        state = cls._state()
        return {
            "locked": bool(state["locked"]),
            "changed_by": state["changed_by"],
            "changed_at": state["changed_at"],
            "code_set": bool(state["code_hash"]),
            "retry_after": cls._retry_after(),
        }

    @classmethod
    def is_locked(cls) -> bool:
        return bool(cls._state()["locked"])

    @classmethod
    def reset_attempts(cls) -> None:
        with cls._lock:
            cls._wrong_codes = 0
            cls._blocked_until = 0.0

    @classmethod
    def _audit(cls, user: Any, action: str, details: str) -> None:
        SettingsPersistenceService.record_audit(
            username=user.username, role=user.role, clearance_level=user.clearance_level,
            action=action, category=CATEGORY, details=details,
        )

    @classmethod
    def _set_locked(cls, user: Any, locked: bool, details: str) -> Dict[str, Any]:
        state = cls._state()
        state["locked"] = locked
        state["changed_by"] = user.username
        state["changed_at"] = datetime.now(timezone.utc).isoformat()
        cls._audit(user, "CONFIG_LOCKED" if locked else "CONFIG_UNLOCKED", details)
        SettingsPersistenceService.save()
        return cls.status()

    @classmethod
    def lock(cls, user: Any) -> Dict[str, Any]:
        if user.clearance_level < 2:
            raise LockError(403, "Only a Level 2 user or an Admin can lock the configuration.")
        if cls.is_locked():
            return cls.status()
        return cls._set_locked(user, True, "Locked the configuration.")

    @classmethod
    def unlock(cls, user: Any, code: Optional[str]) -> Dict[str, Any]:
        if user.clearance_level < 2:
            raise LockError(403, "Only an Admin, or a Level 2 user with the lock code, can unlock the configuration.")
        if user.clearance_level >= 3:
            cls.reset_attempts()
            if not cls.is_locked():
                return cls.status()
            return cls._set_locked(user, False, "Unlocked the configuration.")
        code_hash = cls._state()["code_hash"]
        if not code_hash:
            raise LockError(403, "No lock code is set: only an Admin can unlock.")
        # Nothing typed is not a guess: it is not counted as a wrong code.
        if not (code or "").strip():
            raise LockError(403, "Type the lock code to unlock.")
        with cls._lock:
            wait = cls._retry_after()
            if wait:
                raise LockError(429, f"Too many wrong codes. Try again in {wait} s.", retry_after=wait)
            if not verify_password((code or "").strip(), code_hash):
                cls._wrong_codes += 1
                blocked = cls._wrong_codes >= cls.MAX_WRONG_CODES
                if blocked:
                    cls._wrong_codes = 0
                    cls._blocked_until = cls.clock() + cls.WAIT_SECONDS
            else:
                cls._wrong_codes = 0
                blocked = None
        if blocked is not None:
            cls._audit(user, "UNLOCK_WRONG_CODE", "A wrong lock code was typed.")
            if blocked:
                cls._audit(user, "UNLOCK_BLOCKED",
                           f"{cls.MAX_WRONG_CODES} wrong lock codes: unlocking with a code waits {cls.WAIT_SECONDS} s.")
            SettingsPersistenceService.save()
            raise LockError(403, "Wrong lock code.")
        if not cls.is_locked():
            return cls.status()
        return cls._set_locked(user, False, "Unlocked the configuration with the lock code.")

    @classmethod
    def set_code(cls, user: Any, code: str) -> Dict[str, Any]:
        if user.clearance_level < 3:
            raise LockError(403, "Only an Admin can set the lock code.")
        code = (code or "").strip()
        if not cls.CODE_MIN <= len(code) <= cls.CODE_MAX:
            raise LockError(422, f"The lock code needs {cls.CODE_MIN} to {cls.CODE_MAX} characters.")
        state = cls._state()
        state["code_hash"] = hash_password(code)
        state["code_set_by"] = user.username
        state["code_set_at"] = datetime.now(timezone.utc).isoformat()
        cls.reset_attempts()
        cls._audit(user, "LOCK_CODE_SET", "Set a new lock code.")
        SettingsPersistenceService.save()
        return cls.status()

    @classmethod
    def remove_code(cls, user: Any) -> Dict[str, Any]:
        if user.clearance_level < 3:
            raise LockError(403, "Only an Admin can remove the lock code.")
        state = cls._state()
        if state["code_hash"]:
            state["code_hash"] = None
            state["code_set_by"] = user.username
            state["code_set_at"] = datetime.now(timezone.utc).isoformat()
            cls._audit(user, "LOCK_CODE_REMOVED", "Removed the lock code: only an Admin can unlock now.")
            SettingsPersistenceService.save()
        return cls.status()
