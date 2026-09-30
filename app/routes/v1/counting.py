from __future__ import annotations

from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.db.models.user import User
from app.dependencies import require_supervisor
from app.schemas.counting import CountingConfig, CountingStatsResponse, ResetCountsRequest, TrackInfo

router = APIRouter(prefix="/counting", tags=["Wireline Object Counting & PPM"])

LINE_QUERY = Query(default=None, description="Production line id; Line 1 when omitted")


def _line(line_id: Optional[str]):
    from app.services.line_service import line_manager
    runtime = line_manager.get(line_id if isinstance(line_id, str) else None)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    return runtime


@router.get("/stats", response_model=CountingStatsResponse, summary="Get live object counts, per-class stats & Defect PPM")
async def get_counting_stats(response: Response, line_id: Optional[str] = LINE_QUERY) -> CountingStatsResponse:
    """
    [Level 1+ Operator Clearance]
    Returns real-time 2-wireline crossing counts, breakdown by object class,
    total inspected, rejected parts count, and Defect Rate in PPM (Parts Per Million).
    """
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _line(line_id).counter.get_stats()


@router.post("/config", response_model=CountingConfig, summary="Configure wirelines & expected classes (Level 2+ Supervisor)")
async def update_counting_config(
    config: CountingConfig,
    user: Optional[User] = Depends(require_supervisor),
    line_id: Optional[str] = LINE_QUERY,
) -> CountingConfig:
    """
    [Level 2+ Supervisor Clearance]
    Update 2-wireline positions (0.0 to 1.0), orientation (horizontal/vertical),
    and expected object classes (e.g. ['bottle', 'can', 'cup']) to filter counting.
    """
    return _line(line_id).counter.update_config(config)


@router.post("/reset", response_model=CountingStatsResponse, summary="Reset object counts & PPM statistics (Level 2+ Supervisor)")
async def reset_counts(
    req: ResetCountsRequest = ResetCountsRequest(),
    user: Optional[User] = Depends(require_supervisor),
    line_id: Optional[str] = LINE_QUERY,
) -> CountingStatsResponse:
    """
    [Level 2+ Supervisor Clearance]
    Resets total counts, per-class numbers, and PPM calculation to 0.
    """
    runtime = _line(line_id)
    if req.reset_all:
        runtime.reset()
        res = runtime.counter.get_stats()
    else:
        res = runtime.counter.reset_counts(reset_all=False, classes_to_reset=req.classes_to_reset)
    if user:
        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            SettingsPersistenceService.record_audit(
                username=user.username,
                role=user.role,
                clearance_level=user.clearance_level,
                action="RESET_COUNTERS",
                category="PRODUCTION",
                details=f"Reset production inspection counters of line '{runtime.name}' to 0",
                line_id=runtime.id,
            )
            SettingsPersistenceService.save()
        except Exception:
            pass
    return res


@router.get("/tracks", response_model=List[TrackInfo], summary="Get live per-track Kalman state & telemetry")
async def get_active_tracks(response: Response, line_id: Optional[str] = LINE_QUERY) -> List[TrackInfo]:
    """
    [Level 1+ Operator Clearance]
    Returns real-time per-track telemetry for all objects currently tracked in frame:
    Track ID, class, smoothed Kalman center (x/y), velocity (vx/vy), confirmed state, and counted flag.
    """
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _line(line_id).counter.get_active_tracks()
