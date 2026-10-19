from __future__ import annotations

from typing import List, Optional
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
    DiscoveredIndustrialCamera,
    DiscoveredUSBCamera,
)
from app.services.camera_service import CameraService
from app.state.application_state import app_state

router = APIRouter(prefix="/cameras", tags=["Cameras"])


LOCKED_TRIGGER_MODE = "Set on the line's camera card (Line setup)"


def line_trigger_mode(camera) -> Optional[str]:
    """The trigger mode that the camera card of the camera's line sets: "hardware", "software" or "off".

    None when no card controls it: the camera is on no line, is not an industrial camera, is the line's
    QR reader (it keeps its own), or its card does not have Count mode One picture per trigger (a tracking
    camera or a code reader keeps the Trigger it has). It is the mode a save of the line sets
    (``trigger_mode_for_card``) and the one the camera's Settings may not change.
    """
    from app.services.line_config import locks_trigger_mode, trigger_mode_for_card
    from app.services.settings_persistence_service import SettingsPersistenceService

    if str(getattr(camera.type, "value", camera.type)) != "gige":
        return None
    line_id = SettingsPersistenceService.line_for_camera(camera.id)
    line = SettingsPersistenceService.get_line(line_id, with_logic=False) if line_id else None
    card = next((c for c in (line or {}).get("cameras", []) if c.get("camera_id") == camera.id), None)
    if card is None or not locks_trigger_mode(card):
        return None
    current = str((camera.settings or {}).get("trigger_mode") or "off").strip().lower()
    return trigger_mode_for_card(card, current)


async def _check_trigger_mode_is_free(db: AsyncSession, camera_id: str, camera_in: CameraUpdate) -> None:
    """409 when the change sets a trigger mode other than the one the camera's line card sets.

    Only the mode is locked; sending the card's own mode again (a form sends all its settings) is no change.
    """
    settings = camera_in.settings
    if not isinstance(settings, dict) or "trigger_mode" not in settings:
        return
    camera = await CameraService.get_camera_by_id(db, camera_id)
    locked = line_trigger_mode(camera) if camera is not None else None
    if locked is not None and str(settings["trigger_mode"] or "off").strip().lower() != locked:
        raise HTTPException(status_code=409, detail=LOCKED_TRIGGER_MODE)


def _public_camera(camera):
    from app.services.settings_persistence_service import SettingsPersistenceService

    result = SettingsPersistenceService.redact_secrets(CameraResponse.model_validate(camera).model_dump())
    result["connection_state"] = CameraService.connection_state(camera)
    result["kept_connected"] = CameraService.kept(camera.id)
    result["line_trigger_mode"] = line_trigger_mode(camera)
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


@router.get("/discover/gige", response_model=List[DiscoveredIndustrialCamera],
            summary="Discover industrial cameras (GigE Vision, USB3 Vision)")
async def discover_industrial_cameras(db: AsyncSession = Depends(get_db)) -> List[DiscoveredIndustrialCamera]:
    """The industrial cameras on the network and the USB3 ports. Add one as a camera of type ``gige``
    with its serial number as ``source``. 503 says what to install when the GenICam library or the
    camera maker's producer file is missing."""
    from app.hardware.camera.genicam_backend import GenICamUnavailable

    try:
        return await CameraService.discover_industrial_cameras(db)
    except GenICamUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"The scan failed: {type(exc).__name__}: {exc}") from exc


@router.get("", response_model=List[CameraResponse], summary="List configured cameras")
async def list_cameras(db: AsyncSession = Depends(get_db)) -> List[CameraResponse]:
    return [_public_camera(camera) for camera in await CameraService.list_cameras(db)]


@router.post("", response_model=CameraResponse, status_code=status.HTTP_201_CREATED, summary="Create/Register Camera")
async def create_camera(camera_in: CameraCreate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> CameraResponse:
    try:
        return _public_camera(await CameraService.create_camera(db, camera_in))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{camera_id}", response_model=CameraResponse, summary="Get camera details")
async def get_camera(camera_id: str, db: AsyncSession = Depends(get_db)) -> CameraResponse:
    camera = await CameraService.get_camera_by_id(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return _public_camera(camera)


@router.put("/{camera_id}", response_model=CameraResponse, summary="Update camera settings")
async def update_camera(camera_id: str, camera_in: CameraUpdate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> CameraResponse:
    await _check_trigger_mode_is_free(db, camera_id, camera_in)
    try:
        updated = await CameraService.update_camera(db, camera_id, camera_in)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Camera not found")
    return _public_camera(updated)


@router.patch("/{camera_id}", response_model=CameraResponse, summary="Patch camera settings (partial update)")
async def patch_camera(camera_id: str, camera_in: CameraUpdate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> CameraResponse:
    """Partial update — applies only supplied fields (resolution, ROI, rotation, flip, bandwidth params)."""
    await _check_trigger_mode_is_free(db, camera_id, camera_in)
    try:
        updated = await CameraService.update_camera(db, camera_id, camera_in)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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


RUNNING_LINE_KEEPS_IT = "The running line keeps this camera connected. Stop the line first."


@router.post("/{camera_id}/connect", summary="Switch the camera's Connect switch on: keep it connected")
async def connect_camera(camera_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """The camera is put on the kept-connected list (saved) and connected.

    "status" is "connected", or "retrying" when it did not connect: it is then retried in the
    background until it is switched off. "disconnected" when it was switched off while it connected
    (the last choice wins). 409 when the server already has the most cameras connected (nothing saved).
    """
    from app.services.settings_persistence_service import SettingsPersistenceService

    if await CameraService.get_camera_by_id(db, camera_id) is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    limit = CameraService.camera_limit_error(camera_id)
    if limit is not None:
        raise HTTPException(status_code=409, detail=limit)
    SettingsPersistenceService.set_camera_kept(camera_id, True)
    success, _error = await CameraService.connect_camera(db, camera_id)
    if not CameraService.wanted(camera_id):
        await CameraService.disconnect_camera(db, camera_id)
        return {"status": "disconnected", "camera_id": camera_id}
    return {"status": "connected" if success else "retrying", "camera_id": camera_id}


@router.post("/{camera_id}/disconnect", summary="Switch the camera's Connect switch off and disconnect it")
async def disconnect_camera(camera_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """
    Takes the camera off the kept-connected list, stops its background retries, and releases the device.
    409 while the camera's line is running: the line keeps it connected.
    """
    from app.services.line_service import line_manager
    from app.services.settings_persistence_service import SettingsPersistenceService

    if line_manager.holds_camera(camera_id):
        raise HTTPException(status_code=409, detail=RUNNING_LINE_KEEPS_IT)
    SettingsPersistenceService.set_camera_kept(camera_id, False)
    success = await CameraService.disconnect_camera(db, camera_id)
    if not success:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"status": "disconnected", "camera_id": camera_id}


@router.post("/{camera_id}/trigger", summary="Take one test picture by software trigger; nothing is counted (Level 2+ Supervisor)")
async def test_trigger_camera(camera_id: str, _user=Depends(require_supervisor)) -> dict:
    """An industrial camera on Software trigger takes one picture now, to see that it answers.

    The picture becomes the camera's latest one (video, snapshot) and is not inspected,
    counted or sent. 409 says why the camera cannot be triggered; a running line's
    camera is left to its line, whose triggers take its pictures.
    """
    import asyncio
    import time

    from app.services.line_service import line_manager
    from app.services.settings_persistence_service import SettingsPersistenceService

    driver = app_state.cameras.get(camera_id)
    if driver is None or not getattr(driver, "is_connected", False):
        raise HTTPException(status_code=409, detail="The camera is not connected: connect it first")
    if not hasattr(driver, "take_picture"):
        raise HTTPException(status_code=409, detail="Only an industrial camera (GigE / USB3 Vision) has a software trigger")
    line_id = SettingsPersistenceService.line_for_camera(camera_id)
    runtime = line_manager.get(line_id) if line_id else None
    if runtime is not None and runtime.enabled:
        raise HTTPException(status_code=409, detail=f"Line '{runtime.name}' is running and its triggers take this camera's "
                                                    "pictures. Stop the line to take a test picture.")
    started = time.monotonic()
    try:
        shot = await asyncio.to_thread(driver.take_picture)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if shot is None:
        raise HTTPException(status_code=504, detail="The camera took the trigger, but no picture came within "
                                                    f"{driver.TRIGGER_WAIT_S:g} s")
    _, frame_id, arrived = shot
    return {"status": "picture", "camera_id": camera_id, "frame_id": frame_id, "arrived_at": arrived,
            "waited_ms": round((time.monotonic() - started) * 1000.0, 1)}


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

