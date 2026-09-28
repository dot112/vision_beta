from __future__ import annotations

from typing import Any, Dict
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.services.telemetry_service import TelemetryService

router = APIRouter(prefix="/telemetry", tags=["Telemetry & Metrics"])


@router.get("/stats", summary="Get system & inspection telemetry stats")
async def get_telemetry_stats(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    return await TelemetryService.get_stats(db)
