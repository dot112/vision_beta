"""The configuration lock: read it, lock, unlock, and the Admin's lock code.

Signed-in users only (an API key may read the state). The rules are in
app/services/config_lock_service.py; which requests the lock blocks, in
app/security/config_lock.py. Each answer carries the caller's ``level``: the dashboard keeps
its sign-in per tab but the level in storage shared by every tab of the browser, so its lock
dialog takes this one.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db.models.user import User
from app.dependencies import require_admin, require_operator, require_supervisor
from app.services.config_lock_service import ConfigLockService, LockError

router = APIRouter(prefix="/system", tags=["Configuration lock"])


class UnlockBody(BaseModel):
    code: Optional[str] = None


class LockCodeBody(BaseModel):
    code: str


def _answer(user: User, call: Callable[..., Dict[str, Any]], *args: Any) -> Dict[str, Any]:
    try:
        return {**call(*args), "level": user.clearance_level}
    except LockError as error:
        headers = {"Retry-After": str(error.retry_after)} if error.status_code == 429 else None
        raise HTTPException(status_code=error.status_code, detail=error.detail, headers=headers) from None


@router.get("/lock", summary="Whether the configuration is locked")
async def lock_status(user: User = Depends(require_operator)) -> Dict[str, Any]:
    return _answer(user, ConfigLockService.status)


@router.post("/lock", summary="Lock the configuration (Level 2+)")
async def lock_configuration(user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    return _answer(user, ConfigLockService.lock, user)


@router.post("/unlock", summary="Unlock the configuration (Admin, or Level 2 with the lock code)")
async def unlock_configuration(body: Optional[UnlockBody] = None, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    return _answer(user, ConfigLockService.unlock, user, body.code if body else None)


@router.put("/lock/code", summary="Set or change the lock code (Admin)")
async def set_lock_code(body: LockCodeBody, user: User = Depends(require_admin)) -> Dict[str, Any]:
    return _answer(user, ConfigLockService.set_code, user, body.code)


@router.delete("/lock/code", summary="Remove the lock code: only an Admin can unlock then (Admin)")
async def remove_lock_code(user: User = Depends(require_admin)) -> Dict[str, Any]:
    return _answer(user, ConfigLockService.remove_code, user)
