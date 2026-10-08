from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.db.models.user import User
from app.dependencies import require_operator
from app.events.alarm_events import ALARM_CATALOG, AlarmSeverity, alarm_manager
from app.services.health_service import DOWN, health_service, to_prometheus

# Public probe for load balancers, supervisors and PLC/SCADA heartbeats.
public_router = APIRouter(tags=["Health & Alarms"])

# Mounted under the authenticated /api/v1 router.
router = APIRouter(tags=["Health & Alarms"])


def _status_code(report: Dict[str, Any]) -> int:
    # Only a dead core (database) fails the probe; a PLC or camera outage is
    # "degraded" so a supervisor does not restart the server over a cable fault.
    return status.HTTP_503_SERVICE_UNAVAILABLE if report["status"] == DOWN else status.HTTP_200_OK


@public_router.get("/health", summary="Service health: overall status plus camera, inference and PLC status")
async def health() -> JSONResponse:
    report = health_service.summary()
    return JSONResponse(report, status_code=_status_code(report), headers={"Cache-Control": "no-store"})


@router.get("/telemetry/health", summary="Detailed component health (per camera and PLC endpoint)")
async def health_detail(
    line_id: Optional[str] = Query(None, description="Only this production line in the lines section"),
) -> JSONResponse:
    report = health_service.snapshot()
    if line_id:
        lines = report["components"]["lines"]
        lines["lines"] = [ln for ln in lines["lines"] if ln["id"] == line_id]
    return JSONResponse(report, status_code=_status_code(report), headers={"Cache-Control": "no-store"})


@router.get("/telemetry/metrics", summary="Runtime metrics as JSON, or Prometheus text with ?format=prometheus")
async def metrics(
    format: str = Query("json", pattern="^(json|prometheus)$"),
    line_id: Optional[str] = Query(None, description="Only this production line's per-line metrics"),
) -> Response:
    data = health_service.metrics()
    if line_id and isinstance(data.get("lines"), list):
        data["lines"] = [row for row in data["lines"] if row.get("line") == line_id]
    if format == "prometheus":
        return PlainTextResponse(to_prometheus(data), media_type="text/plain; version=0.0.4")
    return JSONResponse(data)


ALL_LINES_HELP = "With line_id, also the alarms that concern every line (PLC connection, inference, flows)"


@router.get("/alarms", summary="List active alarms, most severe first")
async def list_alarms(
    severity: Optional[AlarmSeverity] = None,
    source: Optional[str] = Query(None, description="Only alarms whose source starts with this prefix"),
    line_id: Optional[str] = Query(None, description="Only alarms raised for this production line"),
    include_all_lines: bool = Query(False, description=ALL_LINES_HELP),
) -> Dict[str, Any]:
    alarms = alarm_manager.active(source_prefix=source)
    if severity:
        alarms = [a for a in alarms if a.severity == severity]
    if line_id:
        alarms = [a for a in alarms if a.concerns_line(line_id, include_all_lines)]
    return {"alarms": [a.to_dict() for a in alarms], "counts": alarm_manager.counts()}


@router.get("/alarms/catalog", summary="Every kind of alarm the server can raise, with a label and scope")
async def alarm_catalog() -> Dict[str, Any]:
    return {"alarms": ALARM_CATALOG}


@router.get("/alarms/history", summary="Recently cleared alarms, newest first")
async def alarm_history(
    limit: int = Query(100, ge=1, le=500),
    line_id: Optional[str] = Query(None, description="Only alarms raised for this production line"),
    include_all_lines: bool = Query(False, description=ALL_LINES_HELP),
) -> List[Dict[str, Any]]:
    alarms = alarm_manager.history(limit if not line_id else 500)
    if line_id:
        alarms = [a for a in alarms if a.concerns_line(line_id, include_all_lines)][:limit]
    return [a.to_dict() for a in alarms]


@router.post("/alarms/{alarm_id}/acknowledge", summary="Acknowledge an active alarm")
async def acknowledge_alarm(alarm_id: str, request: Request, user: User = Depends(require_operator)) -> Dict[str, Any]:
    who = getattr(user, "username", None) or str(getattr(user, "id", "unknown"))
    if getattr(request.state, "auth_method", "jwt") == "api_key":
        who = f"{who} (api key)"
    alarm = alarm_manager.acknowledge(alarm_id, who)
    if alarm is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alarm not found or already cleared")
    return alarm.to_dict()
