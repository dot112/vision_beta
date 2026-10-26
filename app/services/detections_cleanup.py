"""Detections cleanup: delete old rows of the ``detections`` table, a chunk at a time.

``prune`` removes the rows older than DETECTIONS_RETENTION_DAYS (the storage
keeper calls it). ``delete_oldest`` removes the oldest rows whatever their age
(the free-space guard calls it when the disk runs short). Both go through
ix_detections_created_at, and each chunk is its own short transaction, so
inference writing detections never waits long.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import delete, select

from app.db.models.detection import DetectionLog
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Rows deleted in one transaction.
CHUNK = 5000


async def _delete_chunk(session_factory: Callable[[], Any], where: Optional[Any], limit: int) -> int:
    """Delete up to ``limit`` rows matching ``where`` (all rows if None), oldest first, in one transaction."""
    d = DetectionLog
    oldest = select(d.id).order_by(d.created_at).limit(limit)
    if where is not None:
        oldest = oldest.where(where)
    async with session_factory() as db:
        result = await db.execute(delete(d).where(d.id.in_(oldest)))
        await db.commit()
    return int(result.rowcount or 0)


async def _delete(session_factory: Callable[[], Any], where: Optional[Any], limit: Optional[int] = None) -> int:
    """Delete the rows matching ``where``, oldest first, CHUNK at a time, up to ``limit`` of them. Returns how many."""
    removed = 0
    while limit is None or removed < limit:
        size = CHUNK if limit is None else min(CHUNK, limit - removed)
        count = await _delete_chunk(session_factory, where, size)
        removed += count
        if count < size:
            break
        await asyncio.sleep(0.05)
    return removed


async def prune(session_factory: Callable[[], Any], days: int, now: datetime) -> int:
    """Delete the rows with created_at older than ``days`` before ``now``. Returns how many.

    ``days`` of 0 (or less) keeps every row. created_at is UTC without a zone
    in the database, so a ``now`` without a zone is taken as UTC too.
    """
    if not days or days <= 0:
        return 0
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(days=days)
    removed = await _delete(session_factory, DetectionLog.created_at < cutoff)
    if removed:
        logger.info("Deleted %d detection(s) older than %d day(s)", removed, days)
    return removed


async def delete_oldest(session_factory: Callable[[], Any], limit: int = CHUNK) -> int:
    """Delete the ``limit`` oldest rows, whatever their age. Returns how many."""
    if limit <= 0:
        return 0
    removed = await _delete(session_factory, None, limit)
    if removed:
        logger.info("Deleted the %d oldest detection(s) to free space", removed)
    return removed
