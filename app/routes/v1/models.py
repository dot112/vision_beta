from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_supervisor
from app.db.models.model import VisionModel
from app.db.models.user import User
from app.schemas.model import ModelCreate, ModelResponse
from app.services.line_config import PRIMARY_LINE_ID, cameras_using_model, counting_camera
from app.services.model_service import ModelService

router = APIRouter(prefix="/models", tags=["Vision Models"])


def _svc():
    from app.services.settings_persistence_service import SettingsPersistenceService
    return SettingsPersistenceService


def _manager():
    from app.services.line_service import line_manager
    return line_manager


async def _camera_names(db: AsyncSession) -> Dict[str, str]:
    from app.services.camera_service import CameraService
    return {camera.id: camera.name for camera in await CameraService.list_cameras(db)}


def _users(model_id: str, names: Dict[str, str]) -> List[Dict[str, Any]]:
    """The vision cameras that run a model, each with its line and camera name."""
    return [
        {
            "line_id": line["id"],
            "line_name": line["name"],
            "camera_id": camera["camera_id"],
            "camera_name": names.get(camera["camera_id"], camera["camera_id"]),
        }
        for line, camera in cameras_using_model(_svc().get_lines(), model_id)
    ]


def _view(model: VisionModel, names: Dict[str, str]) -> ModelResponse:
    """A model as the API shows it: active while a camera runs it, with where it is used."""
    view = ModelResponse.model_validate(model)
    view.used_by = _users(model.id, names)
    view.is_active = bool(view.used_by)
    state = _manager().model_state(model.id)
    view.loaded, view.load_error = state["loaded"], state["error"]
    return view


@router.get("", response_model=List[ModelResponse], summary="List all vision models, with the cameras that run each")
async def list_models(db: AsyncSession = Depends(get_db)) -> List[ModelResponse]:
    """List all registered neural network models in model_store."""
    names = await _camera_names(db)
    return [_view(model, names) for model in await ModelService.list_models(db)]


@router.get("/active", response_model=ModelResponse, summary="The model of Line 1's counting camera")
async def get_active_model(db: AsyncSession = Depends(get_db)) -> ModelResponse:
    """What version 1 called the active model: the model its one camera ran.
    That camera is Line 1's counting camera now."""
    model_id = _manager().default_model_id()
    model = await ModelService.get_model_by_id(db, model_id) if model_id else None
    if not model:
        raise HTTPException(status_code=404, detail="No active model set: Line 1's vision camera has no model")
    return _view(model, await _camera_names(db))


@router.post("/register", response_model=ModelResponse, status_code=status.HTTP_201_CREATED, summary="Register existing model file")
async def register_model(model_in: ModelCreate, db: AsyncSession = Depends(get_db), _user: User = Depends(require_supervisor)) -> ModelResponse:
    """Register an existing .onnx / .engine model file on disk."""
    try:
        return _view(await ModelService.create_model(db, model_in), {})
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/upload", response_model=ModelResponse, status_code=status.HTTP_201_CREATED, summary="Upload new model weights (.onnx)")
async def upload_model(
    name: str = Form(..., description="Model name (e.g. YOLOv8_BottleDefects)"),
    version: str = Form(default="v1.0", description="Version string"),
    classes_comma_separated: str = Form(default="defect,scratch,dent,ok", description="Comma separated class labels"),
    file: UploadFile = File(..., description=".onnx or .engine model weights file"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_supervisor),
) -> ModelResponse:
    """Upload model weights file directly to model_store/ and register in database."""
    classes = [c.strip() for c in classes_comma_separated.split(",") if c.strip()]
    try:
        return _view(await ModelService.upload_model_file(db, name, version, classes, file), {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _default_camera(db: AsyncSession) -> Optional[str]:
    """The camera that feeds Line 1 while it has none of its own (see pick_default_camera)."""
    from sqlalchemy import select

    from app.db.models.camera import Camera
    from app.services.line_service import pick_default_camera

    owned = {cam["camera_id"] for line in _svc().get_lines() for cam in line.get("cameras", [])}
    result = await db.execute(select(Camera).order_by(Camera.updated_at.desc()))
    target = pick_default_camera(list(result.scalars().all()), owned, _svc().get_active_camera_id())
    return target.id if target else None


@router.post("/{model_id}/activate", response_model=ModelResponse, summary="Pick the model for Line 1's counting camera")
async def activate_model(model_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_supervisor)) -> ModelResponse:
    """The call of version 1, which had one model for its one camera. That camera
    is Line 1's counting camera now, so this picks the model for it, the same
    as choosing it on Line setup. To pick a model for another camera, save the
    line (PUT /lines/{id}) with that camera's model_id."""
    from app.routes.v1.lines import apply_line

    model = await ModelService.get_model_by_id(db, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    line = _svc().get_line(PRIMARY_LINE_ID, with_logic=False)
    cameras = line.get("cameras", [])
    target = counting_camera(line)
    if target is None:
        if cameras:
            raise HTTPException(
                status_code=409,
                detail="Line 1 has no vision camera to run the model on. Add one on Line setup and pick the model there.",
            )
        # Line 1 without cameras is fed by one camera that belongs to no line:
        # that camera becomes its counting camera, with this model.
        feed = await _default_camera(db)
        if feed is None:
            raise HTTPException(
                status_code=409,
                detail="There is no camera to run the model on yet. Add a camera, then pick the model for it on Line setup.",
            )
        target = {"camera_id": feed, "role": "vision", "counting": True}
        cameras = [target]
    # Loaded first, so a model that cannot be loaded changes nothing.
    try:
        await _manager().api_engine(model_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    target["model_id"] = model_id
    try:
        _svc().save_line({"id": PRIMARY_LINE_ID, "cameras": cameras})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _svc().record_audit(
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
        action="PICK_MODEL",
        category="LINES",
        details=f"Picked model '{model.name}' for the counting camera of '{line['name']}' (activate call)",
        line_id=PRIMARY_LINE_ID,
    )
    await apply_line(PRIMARY_LINE_ID)
    await db.refresh(model)
    return _view(model, await _camera_names(db))


@router.patch("/{model_id}/mode", response_model=ModelResponse, summary="Switch model task mode (detect / segment)")
async def set_model_mode(
    model_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_supervisor),
) -> ModelResponse:
    """Sets metadata_json.task to 'detect' or 'segment' without reloading the model."""
    task = (body.get("task") or "detect").strip().lower()
    if task not in ("detect", "segment"):
        raise HTTPException(status_code=422, detail="task must be 'detect' or 'segment'")
    model = await ModelService.get_model_by_id(db, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    meta = dict(model.metadata_json or {})
    meta["task"] = task
    model.metadata_json = meta
    # A loaded copy of the model changes at once.
    _manager().set_model_task(model_id, task)
    await db.commit()
    await db.refresh(model)
    return _view(model, await _camera_names(db))


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a model from registry and disk (Level 2+)")
async def delete_model(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    user: Optional[User] = Depends(require_supervisor),
) -> None:
    """Permanently removes the model weights file and database record.
    Refused (409) while a vision camera runs the model."""
    used_by = [f"{user_['line_name']} ({user_['camera_name']})" for user_ in _users(model_id, await _camera_names(db))]
    try:
        deleted = await ModelService.delete_model(db, model_id, used_by=used_by)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Model not found")
    _manager().forget_model(model_id)
