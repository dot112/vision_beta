"""Line setup's text reader: a picture held still to set the fields on, the reading of unsaved
settings (the preview), and the reader models on this server.

Supervisor (Level 2) and up, like Line setup. They save nothing, so they work while the
configuration is locked; API keys cannot call them (JWT only).
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.dependencies import get_db, require_supervisor
from app.engines import ocr_engine, ocr_models
from app.engines.text_reading import read_picture
from app.services.line_config import normalize_camera

router = APIRouter(prefix="/ocr", tags=["Text reader (OCR)"])

# A held picture is dropped after this long unused.
HELD_TTL_S = 600.0
# A camera switched off is not connected for a picture (CameraService.grab_raw_frame): its card says how.
NOT_CONNECTED = "The camera is not connected: switch on Connect under Camera"
_held: Dict[str, Tuple[np.ndarray, float]] = {}
_held_lock = threading.Lock()
_now: Callable[[], float] = time.monotonic


async def _frame(db: AsyncSession, camera_id: str) -> np.ndarray:
    """The camera's newest picture (a copy: it is kept or read while the camera goes on). A camera
    that is not connected is connected first, as for the count lines' picture: a reader is set up
    on a stopped line."""
    from app.services.camera_service import CameraService
    from app.state.application_state import app_state

    driver = app_state.cameras.get(camera_id)
    if driver is not None and getattr(driver, "is_connected", False) and hasattr(driver, "get_latest_raw_mat"):
        ok, mat, _ = driver.get_latest_raw_mat(copy=True)
        error = "The camera has no picture yet"
    else:
        ok, mat, _, error = await CameraService.grab_raw_frame(db, camera_id)
        if error == "Camera is offline or not found":
            error = NOT_CONNECTED
    if not ok or mat is None:
        raise HTTPException(status_code=404, detail=error or "The camera has no picture yet")
    return np.array(mat, copy=True)


def _held_picture(camera_id: str) -> Optional[np.ndarray]:
    """The camera's held picture, still in use (a use keeps it another HELD_TTL_S), or None."""
    now = _now()
    with _held_lock:
        for gone in [cid for cid, (_, used) in _held.items() if now - used > HELD_TTL_S]:
            del _held[gone]
        held = _held.get(camera_id)
        if held is None:
            return None
        _held[camera_id] = (held[0], now)
        return held[0]


def _jpeg(mat: np.ndarray) -> Response:
    import cv2

    ok, buf = cv2.imencode(".jpg", mat, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise HTTPException(status_code=500, detail="The picture could not be encoded")
    return Response(content=buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/picture/camera/{camera_id}", summary="Keep the camera's newest picture still for setting up its text fields (JPEG)")
async def hold_picture(camera_id: str, user: User = Depends(require_supervisor),
                       db: AsyncSession = Depends(get_db)) -> Response:
    mat = await _frame(db, camera_id)
    with _held_lock:
        _held[camera_id] = (mat, _now())
    return _jpeg(mat)


@router.get("/picture/camera/{camera_id}", summary="The held picture (held=1) or a new one (JPEG)")
async def get_picture(camera_id: str, held: bool = Query(default=False),
                      user: User = Depends(require_supervisor), db: AsyncSession = Depends(get_db)) -> Response:
    if held:
        mat = _held_picture(camera_id)
        if mat is None:
            raise HTTPException(status_code=404, detail="No picture is held for this camera")
        return _jpeg(mat)
    return _jpeg(await _frame(db, camera_id))


@router.post("/preview/camera/{camera_id}", summary="Read the held or a new picture with unsaved text reader settings")
async def preview(camera_id: str, body: Dict[str, Any] = Body(...),
                  user: User = Depends(require_supervisor), db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    from app.services.line_service import line_manager

    settings = body.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="settings must be an object: the camera's card")
    try:
        camera = normalize_camera({**settings, "camera_id": camera_id, "role": "ocr"}, 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if body.get("held"):
        mat = _held_picture(camera_id)
        if mat is None:
            raise HTTPException(status_code=404, detail="No picture is held for this camera")
    else:
        mat = await _frame(db, camera_id)
    ocr = camera["ocr"]
    locate = None
    if ocr.get("locator_model_id"):
        try:
            yolo, _, _ = await line_manager.api_engine(model_id=ocr["locator_model_id"])
            locate = lambda img: yolo.predict_mat(img).detections  # noqa: E731
        except ValueError:
            locate = None     # the read says the YOLO model is not loaded
    result = await asyncio.to_thread(read_picture, mat, ocr, ocr_engine.engine_for(ocr["model"]), locate)
    return {
        "width": int(mat.shape[1]), "height": int(mat.shape[0]), "ms": result.ms,
        "lines": [{"text": line.text, "confidence": round(line.confidence, 4), "box": line.box} for line in result.lines],
        "fields": [{"name": f.name, "text": f.text, "raw": f.raw, "fits": f.fits, "box": f.box, "area": f.area}
                   for f in result.fields],
        "result": result.result, "reason": result.reason, "error": result.error,
    }


@router.get("/models", summary="The text reader models on this server, and the one a new reader gets")
async def models(user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    new = ocr_models.new_reader_model()
    engine = ocr_engine.engine_for(new)
    ready = await asyncio.to_thread(lambda: engine.ready)
    return {"models": ocr_models.available(), "new": new, "ready": bool(ready), "error": engine.error}
