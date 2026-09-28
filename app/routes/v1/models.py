from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_supervisor
from app.db.models.user import User
from app.schemas.model import ModelCreate, ModelResponse
from app.services.model_service import ModelService

router = APIRouter(prefix="/models", tags=["Vision Models"])


@router.get("", response_model=List[ModelResponse], summary="List all vision models")
async def list_models(db: AsyncSession = Depends(get_db)) -> List[ModelResponse]:
    """List all registered neural network models in model_store."""
    return await ModelService.list_models(db)


@router.get("/active", response_model=ModelResponse, summary="Get currently active model")
async def get_active_model(db: AsyncSession = Depends(get_db)) -> ModelResponse:
    """Retrieve the model currently executing inference in runtime memory."""
    model = await ModelService.get_active_model(db)
    if not model:
        raise HTTPException(status_code=404, detail="No active model set")
    return model


@router.post("/register", response_model=ModelResponse, status_code=status.HTTP_201_CREATED, summary="Register existing model file")
async def register_model(model_in: ModelCreate, db: AsyncSession = Depends(get_db), _user: User = Depends(require_supervisor)) -> ModelResponse:
    """Register an existing .onnx / .engine model file on disk."""
    try:
        return await ModelService.create_model(db, model_in)
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
        return await ModelService.upload_model_file(db, name, version, classes, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{model_id}/activate", response_model=ModelResponse, summary="Activate model for live inference")
async def activate_model(model_id: str, db: AsyncSession = Depends(get_db), _user: User = Depends(require_supervisor)) -> ModelResponse:
    """Hot-swaps the active inference engine with the specified model."""
    model = await ModelService.activate_model(db, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


@router.patch("/{model_id}/mode", response_model=ModelResponse, summary="Switch model task mode (detect / segment)")
async def set_model_mode(
    model_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_supervisor),
) -> ModelResponse:
    """Sets metadata_json.task to 'detect' or 'segment' without reloading the model."""
    from sqlalchemy import select as sa_select
    from app.db.models.model import VisionModel
    task = (body.get("task") or "detect").strip().lower()
    if task not in ("detect", "segment"):
        raise HTTPException(status_code=422, detail="task must be 'detect' or 'segment'")
    stmt = sa_select(VisionModel).where(VisionModel.id == model_id)
    result = await db.execute(stmt)
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    meta = dict(model.metadata_json or {})
    meta["task"] = task
    model.metadata_json = meta
    # If this is the active engine, update its task flag live
    from app.state.application_state import app_state
    if app_state.active_model and app_state.active_model.get("id") == model_id:
        engine = app_state.active_model.get("engine")
        if engine:
            engine.task = task
    await db.commit()
    await db.refresh(model)
    return model


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a model from registry and disk (Level 2+)")
async def delete_model(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    user: Optional[User] = Depends(require_supervisor),
) -> None:
    """Permanently removes the model weights file and database record."""
    try:
        deleted = await ModelService.delete_model(db, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Model not found")
