"""Data and backups (Admin): the storage status, the backups, the clock guard and the notices.

Signed-in Admins only; API keys cannot reach these routes, because a backup holds password
hashes and connection settings. Making or downloading a backup, confirming the clock and closing
a notice go in the audit log. None of them changes the configuration, so they work while it is locked.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.db.models.user import User
from app.dependencies import require_admin
from app.events.alarm_events import clear_alarm
from app.services import backup_service, records_export, storage_state
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/system", tags=["Data and backups"])


def _keeper():
    # Looked up on each call, so a test can give its own.
    from app.services.data_keeper import data_keeper
    return data_keeper


def _audit(user: User, action: str, details: str) -> None:
    try:
        from app.services.settings_persistence_service import SettingsPersistenceService
        SettingsPersistenceService.record_audit(
            username=user.username, role=user.role, clearance_level=user.clearance_level,
            action=action, category="System", details=details,
        )
    except Exception:
        logger.exception("Could not write the %s audit entry", action)


@router.get("/data", summary="Storage, archive, backups, clock and notices (Admin)")
async def get_data_status(user: User = Depends(require_admin)) -> Dict[str, Any]:
    return await _keeper().status()


@router.post("/backups", summary="Make a backup now (Admin)")
async def make_backup_now(user: User = Depends(require_admin)) -> Dict[str, Any]:
    try:
        entry = await _keeper().backup_now()
    except Exception as exc:
        logger.exception("The manual backup could not be made")
        raise HTTPException(status_code=503, detail=f"The backup could not be made: {exc}") from exc
    _audit(user, "BACKUP_MADE", f"Made the backup {entry['name']}")
    return entry


@router.get("/backups/{name}", summary="Download a backup as a zip (Admin)")
async def download_backup(name: str, user: User = Depends(require_admin)):
    if not backup_service.NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="No such backup")
    spool = await asyncio.to_thread(backup_service.zip_file, name)
    if spool is None:
        raise HTTPException(status_code=404, detail="No such backup")
    _audit(user, "BACKUP_DOWNLOADED", f"Downloaded the backup {name}")
    return StreamingResponse(
        records_export.iter_file(spool),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"', "Cache-Control": "no-store"},
    )


@router.post("/data/clock-ok", summary="The clock is right: deleting by age goes on (Admin)")
async def confirm_clock(user: User = Depends(require_admin)) -> Dict[str, Any]:
    keeper = _keeper()
    await keeper.confirm_clock()
    _audit(user, "CLOCK_CONFIRMED", "Confirmed the clock; deleting old data by age goes on")
    return keeper.clock_guard.status()


@router.post("/data/notices/{code}/ok", summary="Close a notice and clear its alarm (Admin)")
async def close_notice(code: str, user: User = Depends(require_admin)) -> Dict[str, Any]:
    try:
        closed = await asyncio.to_thread(storage_state.close_notice, code)
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"The notice could not be closed: {exc}") from exc
    if not closed:
        raise HTTPException(status_code=404, detail="No open notice with that code")
    clear_alarm(code, storage_state.STORAGE_SOURCE, "an Admin closed the notice")
    _audit(user, "NOTICE_CLOSED", f"Closed the notice {code}")
    return {"closed": code}
