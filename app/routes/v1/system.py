from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from app.db.models.user import User
from app.dependencies import get_current_user, get_db, require_operator, require_supervisor
from app.services.settings_persistence_service import SettingsPersistenceService
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/system", tags=["System Settings & Real-Time Sync"])


@router.get("/settings", summary="Get current persistent server settings")
async def get_system_settings(
    user: User = Depends(require_operator),
) -> Dict[str, Any]:
    """Returns all saved server configurations, comms protocols, and IP camera list."""
    return SettingsPersistenceService.redact_secrets(SettingsPersistenceService.get_state())


@router.post("/settings", summary="Update persistent server settings (Level 2+ Supervisor)")
@router.patch("/settings", summary="Partial-update persistent server settings (Level 2+ Supervisor)")
async def update_system_settings(
    settings_in: Dict[str, Any],
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    """
    [Level 2+ Supervisor Clearance Required]
    Saves MQTT, TCP, Webhook, and Action Trigger parameters to disk and broadcasts to all clients.
    """
    result = SettingsPersistenceService.update_settings(
        updates=settings_in,
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
    )
    return SettingsPersistenceService.redact_secrets(result)


@router.get("/audit-logs", summary="Get system audit change logs (Level 2+ Supervisor)")
async def get_audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    line_id: Optional[str] = Query(default=None, description="Only entries for this production line"),
    user: User = Depends(require_supervisor),
) -> List[Dict[str, Any]]:
    """Returns chronological audit log of configuration updates, logins, and hardware actions."""
    return SettingsPersistenceService.get_audit_logs(limit=limit, line_id=line_id)


@router.get("/poll-changes", summary="Poll for server configuration changes across connected clients")
async def poll_changes(
    since_version: int = Query(default=0, ge=0),
    boot_id: Optional[str] = Query(default=None, description="Client's last known server boot ID to detect restarts"),
    user: User = Depends(require_operator),
) -> Dict[str, Any]:
    """
    Used by connected dashboards and mobile apps to detect when another user has changed
    system settings or the server was restarted, and automatically sync the interface in real time.
    """
    result = SettingsPersistenceService.get_changes_since(since_version=since_version, client_boot_id=boot_id)
    return SettingsPersistenceService.redact_secrets(result)


@router.get("/endpoints", summary="List all communication endpoints (TCP, MQTT, IP Camera, Webhook)")
async def list_communication_endpoints(
    protocol: Optional[str] = Query(default=None, description="Optional protocol filter: tcp, mqtt, ipcam, webhook"),
    user: User = Depends(require_operator),
) -> List[Dict[str, Any]]:
    endpoints = SettingsPersistenceService.redact_secrets(SettingsPersistenceService.get_endpoints(protocol=protocol))
    # Which production lines use each channel (dispatch targets, PLC cards, cameras).
    from app.services.line_config import endpoints_used_by_line
    usage = [(line["name"], endpoints_used_by_line(line)) for line in SettingsPersistenceService.get_lines(with_logic=True)]
    for endpoint in endpoints:
        endpoint["used_by"] = [name for name, used in usage if endpoint.get("id") in used]
    return endpoints


@router.post("/endpoints", summary="Create or update communication endpoint (Level 2+ Supervisor)")
async def create_communication_endpoint(
    endpoint_data: Dict[str, Any],
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    if not endpoint_data.get("name"):
        raise HTTPException(status_code=400, detail="Channel name is required")
    if not endpoint_data.get("protocol"):
        raise HTTPException(status_code=400, detail="Protocol (tcp, mqtt, ipcam, webhook) is required")
    try:
        result = await SettingsPersistenceService.add_or_update_endpoint(
            endpoint_data=endpoint_data,
            username=user.username,
            role=user.role,
            clearance_level=user.clearance_level,
        )
    except (ValueError, TypeError, AttributeError, OverflowError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SettingsPersistenceService.redact_secrets(result)


@router.put("/endpoints/{endpoint_id}", summary="Update communication endpoint (Level 2+ Supervisor)")
async def update_communication_endpoint(
    endpoint_id: str,
    endpoint_data: Dict[str, Any],
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    endpoint_data["id"] = endpoint_id
    try:
        result = await SettingsPersistenceService.add_or_update_endpoint(
            endpoint_data=endpoint_data,
            username=user.username,
            role=user.role,
            clearance_level=user.clearance_level,
        )
    except (ValueError, TypeError, AttributeError, OverflowError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SettingsPersistenceService.redact_secrets(result)


@router.delete("/endpoints/{endpoint_id}", summary="Delete communication endpoint (Level 1+ Operator)")
async def delete_communication_endpoint(
    endpoint_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    # Look up target endpoint before deletion to extract source URL if it was an IP camera
    state = SettingsPersistenceService.get_state()
    endpoints = state.get("communication_endpoints", [])
    target = next((ep for ep in endpoints if ep.get("id") == endpoint_id), None)
    target_src = (target.get("source") or "").strip() if target else ""

    using = SettingsPersistenceService.lines_using_endpoint(endpoint_id)
    if using:
        raise HTTPException(
            status_code=409,
            detail=f"Channel is used by production line(s): {', '.join(using)}. Change those lines first.",
        )

    deleted = SettingsPersistenceService.delete_endpoint(
        endpoint_id=endpoint_id,
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    # Also delete from Camera DB if this was an IP camera or has matching DB entry
    try:
        from app.services.camera_service import CameraService
        from app.db.models.camera import Camera
        from sqlalchemy import select as sa_select

        camera = await CameraService.get_camera_by_id(db, endpoint_id)
        if not camera and target_src:
            stmt = sa_select(Camera).where(Camera.source == target_src)
            res = await db.execute(stmt)
            camera = res.scalar_one_or_none()
        if camera:
            await db.delete(camera)
            await db.commit()
    except Exception:
        pass

    return {"status": "deleted", "id": endpoint_id}


@router.post("/endpoints/{endpoint_id}/test", summary="Test live connectivity of a communication endpoint")
async def test_communication_endpoint(
    endpoint_id: str,
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    return await SettingsPersistenceService.test_endpoint(endpoint_id)


@router.post("/ip-cameras", summary="Register or update an IP Camera (Level 2+ Supervisor)")
async def add_or_update_ip_camera(
    camera_data: Dict[str, Any],
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    """Registers an RTSP / HTTP IP Camera into persistent storage and camera pool."""
    if not camera_data.get("source"):
        raise HTTPException(status_code=400, detail="IP Camera stream URL (source) is required")
    return SettingsPersistenceService.add_or_update_ip_camera(
        cam_data=camera_data,
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
    )


@router.delete("/ip-cameras/{cam_id}", summary="Remove an IP Camera (Level 2+ Supervisor)")
async def delete_ip_camera(
    cam_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    deleted = SettingsPersistenceService.delete_ip_camera(
        cam_id=cam_id,
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
    )
    # Also delete from Camera DB
    try:
        from app.services.camera_service import CameraService
        camera = await CameraService.get_camera_by_id(db, cam_id)
        if camera:
            await db.delete(camera)
            await db.commit()
    except Exception:
        pass

    if not deleted:
        raise HTTPException(status_code=404, detail="IP Camera not found")
    return {"status": "deleted", "cam_id": cam_id}
