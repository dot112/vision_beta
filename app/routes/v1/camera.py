from __future__ import annotations

from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.dependencies import require_supervisor
from app.schemas.camera import (
    CameraCreate,
    CameraResponse,
    CameraStatusResponse,
    CameraUpdate,
    DiscoveredUSBCamera,
)
from app.services.camera_service import CameraService
from app.state.application_state import app_state

router = APIRouter(prefix="/cameras", tags=["Cameras"])


def _public_camera(camera):
    from app.services.settings_persistence_service import SettingsPersistenceService

    result = SettingsPersistenceService.redact_secrets(CameraResponse.model_validate(camera).model_dump())
    result["connection_state"] = CameraService.connection_state(camera)
    if result.get("last_error"):
        result["last_error"] = "Camera connection failed; consult server logs for details."
    return result


def _public_camera_status(status_data):
    from app.services.settings_persistence_service import SettingsPersistenceService

    result = SettingsPersistenceService.redact_secrets(CameraStatusResponse.model_validate(status_data).model_dump())
    if result.get("last_error"):
        result["last_error"] = "Camera connection failed; consult server logs for details."
    return result


@router.get("/discover/usb", response_model=List[DiscoveredUSBCamera], summary="Discover Hardware Cameras")
async def discover_usb_cameras(db: AsyncSession = Depends(get_db)) -> List[DiscoveredUSBCamera]:
    return await CameraService.discover_hardware_cameras(db)


@router.get("", response_model=List[CameraResponse], summary="List configured cameras")
async def list_cameras(db: AsyncSession = Depends(get_db)) -> List[CameraResponse]:
    return [_public_camera(camera) for camera in await CameraService.list_cameras(db)]


@router.post("", response_model=CameraResponse, status_code=status.HTTP_201_CREATED, summary="Create/Register Camera")
async def create_camera(camera_in: CameraCreate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> CameraResponse:
    return _public_camera(await CameraService.create_camera(db, camera_in))


@router.get("/{camera_id}", response_model=CameraResponse, summary="Get camera details")
async def get_camera(camera_id: str, db: AsyncSession = Depends(get_db)) -> CameraResponse:
    camera = await CameraService.get_camera_by_id(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return _public_camera(camera)


@router.put("/{camera_id}", response_model=CameraResponse, summary="Update camera settings")
async def update_camera(camera_id: str, camera_in: CameraUpdate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> CameraResponse:
    updated = await CameraService.update_camera(db, camera_id, camera_in)
    if not updated:
        raise HTTPException(status_code=404, detail="Camera not found")
    return _public_camera(updated)


@router.patch("/{camera_id}", response_model=CameraResponse, summary="Patch camera settings (partial update)")
async def patch_camera(camera_id: str, camera_in: CameraUpdate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> CameraResponse:
    """Partial update — applies only supplied fields (resolution, ROI, rotation, flip, bandwidth params)."""
    updated = await CameraService.update_camera(db, camera_id, camera_in)
    if not updated:
        raise HTTPException(status_code=404, detail="Camera not found")
    return _public_camera(updated)


@router.delete("/{camera_id}", summary="Delete camera")
async def delete_camera(camera_id: str, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> dict:
    from app.services.settings_persistence_service import SettingsPersistenceService
    using = SettingsPersistenceService.lines_using_endpoint(camera_id)
    if using:
        raise HTTPException(
            status_code=409,
            detail=f"Camera is used by production line(s): {', '.join(using)}. Remove it from the line first.",
        )
    success = await CameraService.delete_camera(db, camera_id)
    if not success:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"detail": f"Camera {camera_id} deleted successfully"}


@router.post("/{camera_id}/connect", summary="Connect to camera hardware/stream")
async def connect_camera(camera_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    success, error = await CameraService.connect_camera(db, camera_id)
    if not success:
        raise HTTPException(status_code=400, detail="Camera connection failed; verify the configured source and consult server logs.")
    return {"status": "connected", "camera_id": camera_id}


@router.post("/{camera_id}/disconnect", summary="Disconnect from camera")
async def disconnect_camera(camera_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """
    Disconnects hardware capture, turns off camera LED, and releases device handle.
    """
    success = await CameraService.disconnect_camera(db, camera_id)
    if not success:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"status": "disconnected", "camera_id": camera_id}


@router.get("/{camera_id}/status", response_model=CameraStatusResponse, summary="Get live camera status")
async def get_camera_status(camera_id: str, db: AsyncSession = Depends(get_db)) -> CameraStatusResponse:
    status_resp = await CameraService.get_camera_status(db, camera_id)
    if not status_resp:
        raise HTTPException(status_code=404, detail="Camera not found")
    return _public_camera_status(status_resp)


@router.get("/{camera_id}/frame", summary="Grab instantaneous JPEG snapshot")
async def grab_frame(
    camera_id: str,
    full: bool = Query(False, description="The whole picture before the ROI crop (to draw the ROI on)"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if full:
        jpeg_bytes = await CameraService.grab_full_view(camera_id)
        if not jpeg_bytes:
            raise HTTPException(status_code=400, detail="The camera is not connected or has no picture yet")
        return Response(content=jpeg_bytes, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    success, jpeg_bytes, error = await CameraService.grab_frame(db, camera_id)
    if not success or not jpeg_bytes:
        raise HTTPException(status_code=400, detail="Failed to grab a frame from the configured camera")
    return Response(content=jpeg_bytes, media_type="image/jpeg")


async def _mjpeg_generator(
    camera_id: str,
    max_width: int = 640,
    jpeg_quality: int = 70,
):
    """Lock-free 30 FPS raw MJPEG stream via publisher layer (zero OpenCV on event loop)."""
    from app.services.vision_service import CameraStreamPipeline

    async for chunk in CameraStreamPipeline.stream_raw_mjpeg(camera_id):
        yield chunk


@router.get("/{camera_id}/stream", summary="Live Continuous MJPEG Video Feed (Raw)")
async def live_mjpeg_stream(
    camera_id: str,
    max_width: int = Query(default=640, ge=160, le=3840),
    quality: int = Query(default=65, ge=20, le=95),
) -> StreamingResponse:
    driver = app_state.cameras.get(camera_id)
    if not driver or not driver.is_connected:
        raise HTTPException(status_code=404, detail="Camera stream is offline or disconnected")
    return StreamingResponse(
        _mjpeg_generator(camera_id, max_width=max_width, jpeg_quality=quality),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )

