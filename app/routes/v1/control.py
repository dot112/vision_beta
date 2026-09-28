from __future__ import annotations

from typing import Any, Dict
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.services.vision_service import VisionService

router = APIRouter(prefix="/control", tags=["Production Control"])


@router.post("/trigger/{camera_id}", summary="Simulate a production line trigger event")
async def trigger_inspection(camera_id: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Triggered by photo-eye sensor or PLC to perform immediate inline part inspection."""
    res = await VisionService.detect_live_camera(db, camera_id)
    return {
        "status": "triggered",
        "camera_id": camera_id,
        "inspection_result": res.model_dump() if hasattr(res, "model_dump") else res.dict(),
    }
