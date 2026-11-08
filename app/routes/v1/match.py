"""Line setup's pattern check: a picture held still to train on, the draft (the reference and check boxes, the
search area, the mask, the good samples), Train, Edit training, Start over, and the check of unsaved settings
(the preview).

Supervisor (Level 2) and up, like Line setup; API keys cannot call them (JWT only). The picture and the
preview save nothing, so they work while the configuration is locked; the draft's changes and Train are
refused then, as every other change is.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.dependencies import get_db, require_supervisor
from app.engines.pattern_check import check_picture, error_result
from app.engines.pattern_training import TrainingRefused, find_sample, train
from app.services import held_pictures, match_references
from app.services.line_config import DEFAULT_MATCH_ANGLE, MATCH_ANGLE_LIMITS, normalize_camera

router = APIRouter(prefix="/match", tags=["Pattern check"])

NO_DRAFT = "No draft for this camera"
TAKE_A_PICTURE = "No picture is held for this camera: take a picture first"
NOT_FOUND = "Reference not found in this picture"
NO_ROOM = ("The disk is below its free-space floor (Data and backups): there is no room for training files. "
           "Free some room first.")


def _draft_dir_ok(camera_id: str) -> None:
    try:
        match_references._draft_dir(camera_id)
    except KeyError:
        raise HTTPException(status_code=400, detail="The camera id is not valid") from None


def _held_or_404(camera_id: str) -> Any:
    mat = held_pictures.held(camera_id)
    if mat is None:
        raise HTTPException(status_code=404, detail=TAKE_A_PICTURE)
    return mat


def _angle(body: Dict[str, Any]) -> int:
    value = body.get("angle", DEFAULT_MATCH_ANGLE)
    low, high = MATCH_ANGLE_LIMITS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or not low <= value <= high:
        raise HTTPException(status_code=400, detail=f"The angle must be a whole number from {low} to {high}")
    return int(value)


def _room() -> None:
    if not match_references.room_ok():
        raise HTTPException(status_code=507, detail=NO_ROOM)


def _draft_or_404(camera_id: str) -> Dict[str, Any]:
    draft = match_references.draft(camera_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=NO_DRAFT)
    return draft


@router.post("/picture/camera/{camera_id}", summary="Keep the camera's newest picture still for training its pattern check (JPEG)")
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


@router.post("/preview/camera/{camera_id}", summary="Check the held or a new picture with unsaved pattern check settings")
async def preview(camera_id: str, body: Dict[str, Any] = Body(...),
                  user: User = Depends(require_supervisor), db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    settings = body.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="settings must be an object: the camera's card")
    try:
        camera = normalize_camera({**settings, "camera_id": camera_id, "role": "match"}, 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if body.get("held"):
        mat = held_pictures.held(camera_id)
        if mat is None:
            raise HTTPException(status_code=404, detail=held_pictures.NOT_HELD)
    else:
        mat = await held_pictures.frame(db, camera_id)
    match = camera["match"]
    try:
        ref = await asyncio.to_thread(match_references.load_set, match["reference_id"])
    except match_references.ReferenceMissing as missing:
        return error_result(str(missing), int(mat.shape[1]), int(mat.shape[0])).to_dict()
    result = await asyncio.to_thread(check_picture, ref, match, mat)
    return result.to_dict()


@router.get("/draft/camera/{camera_id}", summary="The camera's training draft: boxes, search area, samples, last trained set")
async def get_draft(camera_id: str, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    _draft_dir_ok(camera_id)
    return _draft_or_404(camera_id)


@router.get("/draft/camera/{camera_id}/picture", summary="The draft's training picture (JPEG)")
async def draft_picture(camera_id: str, user: User = Depends(require_supervisor)) -> Response:
    _draft_dir_ok(camera_id)
    path = match_references.draft_file(camera_id, "picture")
    if path is None:
        raise HTTPException(status_code=404, detail=NO_DRAFT)
    # Read as bytes and decoded (cv2.imread cannot open a path with other than ASCII letters on Windows).
    return held_pictures.jpeg(await asyncio.to_thread(match_references._read_image, path))


@router.get("/draft/camera/{camera_id}/mask", summary="The draft's mask (PNG, white = ignore)")
async def draft_mask(camera_id: str, user: User = Depends(require_supervisor)) -> Response:
    _draft_dir_ok(camera_id)
    path = match_references.draft_file(camera_id, "mask")
    if path is None:
        raise HTTPException(status_code=404, detail="No mask is painted on this draft")
    return Response(content=path.read_bytes(), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/draft/camera/{camera_id}/samples/{n}", summary="One of the draft's good samples (JPEG)")
async def draft_sample(camera_id: str, n: int, user: User = Depends(require_supervisor)) -> Response:
    _draft_dir_ok(camera_id)
    path = match_references.draft_file(camera_id, n)
    if path is None:
        raise HTTPException(status_code=404, detail=f"There is no sample {n}")
    return Response(content=path.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.put("/draft/camera/{camera_id}", summary="Save the draft's boxes and search area (a new draft starts from the held picture)")
async def put_draft(camera_id: str, body: Dict[str, Any] = Body(...),
                    user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    _draft_dir_ok(camera_id)
    if match_references.draft(camera_id) is None:
        await asyncio.to_thread(match_references.start_draft, camera_id, _held_or_404(camera_id))
    try:
        await asyncio.to_thread(match_references.save_boxes, camera_id, body.get("reference"),
                                body.get("checks") or [], body.get("search_area"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _draft_or_404(camera_id)


@router.put("/draft/camera/{camera_id}/mask", summary="Save the draft's mask (a PNG of the training picture's size)")
async def put_mask(camera_id: str, request: Request, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    _draft_dir_ok(camera_id)
    png = await request.body()
    try:
        await asyncio.to_thread(match_references.save_mask, camera_id, png)
    except LookupError:
        raise HTTPException(status_code=404, detail=NO_DRAFT) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _draft_or_404(camera_id)


@router.post("/draft/camera/{camera_id}/samples", summary="Add the held picture to the draft as a good sample")
async def add_sample(camera_id: str, body: Dict[str, Any] = Body(default={}),
                     user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    _draft_dir_ok(camera_id)
    _room()
    angle = _angle(body)
    data = await asyncio.to_thread(match_references.draft_data, camera_id)
    if data is None:
        raise HTTPException(status_code=404, detail=NO_DRAFT)
    if not data.reference:
        raise HTTPException(status_code=400, detail="Draw the reference box first")
    mat = _held_or_404(camera_id)
    found = await asyncio.to_thread(find_sample, data, mat, angle)
    if found is None:
        raise HTTPException(status_code=400, detail=NOT_FOUND)
    try:
        n = await asyncio.to_thread(match_references.add_sample, camera_id, mat, int(round(found.score * 100)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"n": n, "found": {"x": round(found.x, 1), "y": round(found.y, 1), "angle": round(found.angle, 2),
                              "score": round(found.score, 4)}}


@router.delete("/draft/camera/{camera_id}/samples/{n}", summary="Remove a good sample from the draft (not sample 1)")
async def remove_sample(camera_id: str, n: int, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    _draft_dir_ok(camera_id)
    try:
        await asyncio.to_thread(match_references.remove_sample, camera_id, n)
    except LookupError:
        raise HTTPException(status_code=404, detail=NO_DRAFT) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _draft_or_404(camera_id)


@router.post("/draft/camera/{camera_id}/train", summary="Train a new reference set from the draft; gives its id and suggested thresholds")
async def train_draft(camera_id: str, body: Dict[str, Any] = Body(default={}),
                      user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    from app.services.settings_persistence_service import SettingsPersistenceService

    _draft_dir_ok(camera_id)
    _room()
    angle = _angle(body)
    checks = body.get("checks") if isinstance(body.get("checks"), list) else []
    data = await asyncio.to_thread(match_references.draft_data, camera_id)
    if data is None:
        raise HTTPException(status_code=404, detail=NO_DRAFT)
    set_id = match_references.new_id()
    try:
        ref, info = await asyncio.to_thread(train, data, angle, checks, set_id)
    except TrainingRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await asyncio.to_thread(match_references.write_set, ref, data, getattr(user, "username", None) or "", camera_id)
    match_references.set_trained(camera_id, set_id)
    SettingsPersistenceService.clean_up_soon()
    return {"reference_id": set_id, **info}


@router.post("/draft/camera/{camera_id}/edit", summary="Edit training: a draft made from a reference set")
async def edit_set(camera_id: str, body: Dict[str, Any] = Body(...),
                   user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    _draft_dir_ok(camera_id)
    try:
        await asyncio.to_thread(match_references.draft_from_set, camera_id, str(body.get("reference_id") or ""))
    except match_references.ReferenceMissing as missing:
        raise HTTPException(status_code=404, detail=str(missing)) from None
    return _draft_or_404(camera_id)


@router.delete("/draft/camera/{camera_id}", summary="Start over: remove the camera's training draft")
async def delete_draft(camera_id: str, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    _draft_dir_ok(camera_id)
    await asyncio.to_thread(match_references.delete_draft, camera_id)
    return {"deleted": True}
