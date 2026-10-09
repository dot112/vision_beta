"""Production records: every product and code read the lines decided, for a chosen time range.

Operator level. API keys need the records:read scope.
"""
from __future__ import annotations

import re
from datetime import timedelta, timezone
from typing import Any, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.services import records_export
from app.services.production_records_service import (
    MAX_RANGE_DAYS,
    RecordFilter,
    list_records,
    parse_time,
    summary,
)

router = APIRouter(prefix="/records", tags=["Production records"])


def _sessions():
    # Looked up on each call, so a test can point it elsewhere.
    from app.db.session import AsyncSessionLocal
    return AsyncSessionLocal


def record_filter(
    start: str = Query(..., description="Range start, ISO 8601 with a time zone (e.g. 2026-10-09T06:00:00Z)"),
    end: str = Query(..., description="Range end (not included), ISO 8601 with a time zone"),
    line_id: Optional[str] = Query(default=None, max_length=48, description="One line; empty = every line"),
    result: Optional[str] = Query(default=None, pattern="^(good|reject)$"),
    kind: Optional[str] = Query(default=None, pattern="^(product|code)$"),
    batch: Optional[str] = Query(default=None, max_length=64),
    code: Optional[str] = Query(default=None, max_length=512, description="Rows whose code contains this"),
    camera_id: Optional[str] = Query(default=None, max_length=64),
    counted: Optional[bool] = Query(default=None, description="True: only products in the line totals"),
) -> RecordFilter:
    start_at, end_at = parse_time(start), parse_time(end)
    if start_at is None or end_at is None:
        raise HTTPException(status_code=422, detail="start and end must be ISO 8601 times with a time zone, e.g. 2026-10-09T06:00:00Z")
    if end_at <= start_at:
        raise HTTPException(status_code=422, detail="end must be after start")
    if end_at - start_at > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(status_code=422, detail=f"The range may be at most {MAX_RANGE_DAYS} days")
    return RecordFilter(
        start=start_at, end=end_at, line_id=(line_id or "").strip() or None, result=result or None,
        kind=kind or None, batch=(batch or "").strip() or None, code=(code or "").strip() or None,
        camera_id=(camera_id or "").strip() or None, counted=counted,
    )


def _zone(tz: Optional[str]) -> Tuple[Any, str, Optional[str]]:
    """(tzinfo, name, note): UTC with a note when the zone is unknown."""
    name = (tz or "").strip()
    if not name or name.upper() == "UTC":
        return timezone.utc, "UTC", None
    try:
        return ZoneInfo(name), name, None
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc, "UTC", f"Unknown time zone '{name[:64]}'; times are in UTC"


@router.get("", summary="Product and code records of a time range (newest first)")
async def get_records(
    flt: RecordFilter = Depends(record_filter),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await list_records(db, flt, limit=limit, offset=offset)


@router.get("/summary", summary="Totals, by class, reason, camera and per hour or day")
async def get_summary(
    flt: RecordFilter = Depends(record_filter),
    bucket: str = Query(default="hour", pattern="^(hour|day)$"),
    tz: Optional[str] = Query(default=None, max_length=64, description="IANA time zone of the hour/day buckets, e.g. Europe/Berlin"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    zone, name, note = _zone(tz)
    out = await summary(db, flt, bucket=bucket, tz=zone)
    out["tz"] = name
    if note:
        out["tz_note"] = note
    return out


def _file_name(flt: RecordFilter, extension: str, zone: Any) -> str:
    line = re.sub(r"[^A-Za-z0-9_-]+", "_", flt.line_id or "all-lines")
    stamp = "%Y%m%d-%H%M"
    return f"records_{line}_{flt.start.astimezone(zone).strftime(stamp)}_{flt.end.astimezone(zone).strftime(stamp)}.{extension}"


@router.get("/export", summary="Download the records of a time range as CSV or Excel (.xlsx)")
async def export_records(
    flt: RecordFilter = Depends(record_filter),
    format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
    tz: Optional[str] = Query(default=None, max_length=64, description="IANA time zone of the local time column, e.g. Europe/Berlin"),
    db: AsyncSession = Depends(get_db),
):
    zone, name, note = _zone(tz)
    headers = {"Cache-Control": "no-store", "X-Records-Timezone": name}
    if note:
        headers["X-Records-Timezone-Note"] = note
    if format == "csv":
        headers["Content-Disposition"] = f'attachment; filename="{_file_name(flt, "csv", zone)}"'
        return StreamingResponse(
            records_export.stream_csv(_sessions(), flt, zone, name),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    rows = await records_export.count_rows(db, flt)
    if rows > records_export.XLSX_MAX_ROWS:
        raise HTTPException(
            status_code=413,
            detail=f"{rows} records are more than an Excel sheet holds ({records_export.XLSX_MAX_ROWS}). "
                   f"Export them as CSV, or choose a shorter range.",
        )
    totals = await summary(db, flt, bucket="day" if flt.end - flt.start > timedelta(days=2) else "hour", tz=zone)
    line_label = flt.line_id or "All lines"
    if flt.line_id:
        from app.services.line_service import line_manager
        runtime = line_manager.get(flt.line_id)
        if runtime is not None:
            line_label = f"{runtime.name} ({flt.line_id})"
    spool = await records_export.build_xlsx(_sessions(), flt, totals, zone, name, line_label, note)
    headers["Content-Disposition"] = f'attachment; filename="{_file_name(flt, "xlsx", zone)}"'
    return StreamingResponse(
        records_export.iter_file(spool),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )
