"""Line setup's text reader: a picture held still to set the fields on, the reading of unsaved
settings (the preview), and the reader models on this server.

Supervisor (Level 2) and up, like Line setup. They save nothing, so they work while the
configuration is locked; API keys cannot call them (JWT only).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.dependencies import get_db, require_supervisor
from app.engines import ocr_engine, ocr_models
from app.engines.text_reading import read_picture
from app.services import held_pictures
from app.services.line_config import normalize_camera

router = APIRouter(prefix="/ocr", tags=["Text reader (OCR)"])

@router.post("/picture/camera/{camera_id}", summary="Keep the camera's newest picture still for setting up its text fields (JPEG)")
async def hold_picture(camera_id: str, user: User = Depends(require_supervisor),
                       db: AsyncSession = Depends(get_db)) -> Response:
    mat = await held_pictures.frame(db, camera_id)
    held_pictures.hold(camera_id, mat)
    return held_pictures.jpeg(mat)


@router.get("/picture/camera/{camera_id}", summary="The held picture (held=1) or a new one (JPEG)")
async def get_picture(camera_id: str, held: bool = Query(default=False),
                      user: User = Depends(require_supervisor), db: AsyncSession = Depends(get_db)) -> Response:
    if held:
        mat = held_pictures.held(camera_id)
        if mat is None:
            raise HTTPException(status_code=404, detail=held_pictures.NOT_HELD)
        return held_pictures.jpeg(mat)
    return held_pictures.jpeg(await held_pictures.frame(db, camera_id))


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
        mat = held_pictures.held(camera_id)
        if mat is None:
            raise HTTPException(status_code=404, detail=held_pictures.NOT_HELD)
    else:
        mat = await held_pictures.frame(db, camera_id)
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
