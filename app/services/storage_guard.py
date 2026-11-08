"""The free-space floor: keeps the disk that holds data/ from filling up.

The data keeper calls ``check`` every minute. Free space is the disk's free
space plus the room inside the database: deleting rows does not shrink the
SQLite file, it frees pages that new rows use first.

storage.low is raised while the disk is STORAGE_WARN_PERCENT % used or more,
and cleared 2 points below. storage.full is raised while free space is under
the floor (STORAGE_MIN_FREE_MB; 0 turns it off). The oldest data is then
deleted, one kind after the other, until free space is back to 1.25 x the floor:
  1. detections;
  2. records of days that have an archive file;
  3. archive files;
  4. records that are in no archive file, but never those of the newest
     24 hours (storage.records_lost and a notice say so).
Records go through ProductionRecorder.delete_chunk, so the line totals stay
exact. storage.full clears once free space is back to the floor.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import psutil
from sqlalchemy import and_, func, select, text

from app.config import settings
from app.db.models.production_record import ProductRecord
from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager, clear_alarm, raise_alarm
from app.services import detections_cleanup, records_archive, storage_state
from app.services.production_records_service import saved_lines, utc
from app.utils.logger import get_logger

logger = get_logger(__name__)

MB = 1024 * 1024
# storage.low clears this many points under the warning level, so it does not flicker.
LOW_CLEARS_BELOW = 2.0
# Freeing space goes on until this many times the floor is free.
TARGET_FACTOR = 1.25
# Records this new are never deleted for space.
KEEP_NEWEST = timedelta(hours=24)
NOTHING_LEFT = "nothing more the server can delete"


@dataclass
class DiskReading:
    total: int
    free: int
    used_percent: float


def read_disk(path: Optional[str] = None) -> DiskReading:
    """The disk that holds the data folder (or ``path``)."""
    usage = psutil.disk_usage(path or str(storage_state.data_dir()))
    return DiskReading(total=int(usage.total), free=int(usage.free), used_percent=float(usage.percent))


async def room_in_database(session_factory: Callable[[], Any]) -> int:
    """Bytes of the pages SQLite freed inside the database file (new rows use them first)."""
    async with session_factory() as db:
        pages = (await db.execute(text("PRAGMA freelist_count"))).scalar_one()
        size = (await db.execute(text("PRAGMA page_size"))).scalar_one()
    return int(pages) * int(size)


def _mb(value: int) -> int:
    return int(value // MB)


def _active(code: str) -> bool:
    return alarm_manager.get(code, storage_state.STORAGE_SOURCE) is not None


class StorageGuard:
    """Measures the free space, raises and clears the storage alarms, and deletes the oldest data when it runs short."""

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None, recorder: Any = None,
                 disk: Callable[[], DiskReading] = read_disk,
                 room: Optional[Callable[[], Awaitable[int]]] = None):
        self._session_factory = session_factory
        self._recorder = recorder
        self._disk = disk
        self._room = room

    def _sessions(self) -> Callable[[], Any]:
        if self._session_factory is None:
            from app.db.session import AsyncSessionLocal
            return AsyncSessionLocal
        return self._session_factory

    def _records(self) -> Any:
        if self._recorder is None:
            from app.services.production_records_service import production_recorder
            return production_recorder
        return self._recorder

    @staticmethod
    def floor() -> int:
        return max(0, int(settings.STORAGE_MIN_FREE_MB)) * MB

    async def _measure(self) -> Tuple[DiskReading, int]:
        """(the disk, the room inside the database)."""
        reading = await asyncio.to_thread(self._disk)
        room = await (self._room() if self._room is not None else room_in_database(self._sessions()))
        return reading, int(room)

    async def _free(self) -> int:
        reading, room = await self._measure()
        return reading.free + room

    # ── The check ─────────────────────────────────────────────────────────────

    async def check(self, now: datetime) -> Dict[str, Any]:
        """Measure, raise or clear storage.low and storage.full, and free space when under the floor."""
        reading, room = await self._measure()
        warn = float(settings.STORAGE_WARN_PERCENT)
        if reading.used_percent >= warn:
            raise_alarm(AlarmCode.STORAGE_LOW, storage_state.STORAGE_SOURCE,
                        f"The data disk is {reading.used_percent:.0f}% used (warning at {warn:.0f}%)",
                        AlarmSeverity.WARNING, {"used_percent": reading.used_percent})
        elif reading.used_percent < warn - LOW_CLEARS_BELOW:
            clear_alarm(AlarmCode.STORAGE_LOW, storage_state.STORAGE_SOURCE, "the data disk has room again")

        floor = self.floor()
        free = reading.free + room
        if floor and free < floor:
            raise_alarm(AlarmCode.STORAGE_FULL, storage_state.STORAGE_SOURCE,
                        f"Only {_mb(free)} MB free, under the floor of {_mb(floor)} MB: deleting the oldest data",
                        AlarmSeverity.CRITICAL, {"free_mb": _mb(free), "floor_mb": _mb(floor)})
            await self.free_space(int(floor * TARGET_FACTOR), now)
            reading, room = await self._measure()
            free = reading.free + room
            if free < floor:
                raise_alarm(AlarmCode.STORAGE_FULL, storage_state.STORAGE_SOURCE,
                            f"Only {_mb(free)} MB free, under the floor of {_mb(floor)} MB: {NOTHING_LEFT}",
                            AlarmSeverity.CRITICAL, {"free_mb": _mb(free), "floor_mb": _mb(floor)})
        if not floor or free >= floor:
            clear_alarm(AlarmCode.STORAGE_FULL, storage_state.STORAGE_SOURCE, "free space is back above the floor")

        return self._report(reading, room)

    async def status(self) -> Dict[str, Any]:
        """The same reading as ``check`` gives, without raising, clearing or deleting anything."""
        reading, room = await self._measure()
        return self._report(reading, room)

    def _report(self, reading: DiskReading, room: int) -> Dict[str, Any]:
        state = "full" if _active(AlarmCode.STORAGE_FULL) else "low" if _active(AlarmCode.STORAGE_LOW) else "ok"
        return {"total": reading.total, "free": reading.free, "room": room, "used_percent": reading.used_percent,
                "floor": self.floor(), "warn_percent": float(settings.STORAGE_WARN_PERCENT), "state": state}

    # ── Freeing space ─────────────────────────────────────────────────────────

    async def free_space(self, target: int, now: datetime) -> List[Dict[str, Any]]:
        """Delete the oldest data, in the order of the module doc, until ``target`` bytes are free. Gives the steps taken."""
        now = utc(now)
        lines = saved_lines()
        free = await self._free()
        steps: List[Dict[str, Any]] = []
        # Oldest first. Every archived day counts, also after an older day whose archive failed.
        archived_days = sorted(date.fromisoformat(entry["day"]) for entry in await asyncio.to_thread(records_archive.list_days))
        lost: Dict[str, Any] = {}

        async def detections() -> int:
            return await detections_cleanup.delete_oldest(self._sessions(), detections_cleanup.CHUNK)

        async def archived_records() -> int:
            while archived_days:
                start = datetime(archived_days[0].year, archived_days[0].month, archived_days[0].day, tzinfo=timezone.utc)
                count, _ = await self._records().delete_chunk(
                    and_(ProductRecord.recorded_at >= start, ProductRecord.recorded_at < start + timedelta(days=1)), lines)
                if count:
                    return count
                archived_days.pop(0)  # that day's records are gone: the next archived day
            return 0

        async def archive_files() -> int:
            return 1 if await asyncio.to_thread(records_archive.delete_oldest_file) else 0

        async def records() -> int:
            cutoff = now - KEEP_NEWEST
            if "oldest" not in lost:
                lost["oldest"] = await self._oldest_record(cutoff)
            count, newest = await self._records().delete_chunk(ProductRecord.recorded_at < cutoff, lines)
            if count:
                lost["newest"] = newest
            return count

        for name, delete_one in (("detections", detections), ("archived_records", archived_records),
                                 ("archive_files", archive_files), ("records", records)):
            deleted = 0
            while free < target:
                count = await delete_one()
                if not count:
                    break
                deleted += count
                free = await self._free()
            if deleted:
                steps.append({"step": name, "deleted": deleted})
                logger.warning("Free space: deleted %d %s", deleted, name.replace("_", " "))
            if free >= target:
                break

        lost_count = next((step["deleted"] for step in steps if step["step"] == "records"), 0)
        if lost_count:
            self._report_lost(lost_count, lost.get("oldest"), lost.get("newest"))
        return steps

    async def _oldest_record(self, cutoff: datetime) -> Optional[datetime]:
        async with self._sessions()() as db:
            oldest = (await db.execute(select(func.min(ProductRecord.recorded_at))
                                       .where(ProductRecord.recorded_at < cutoff))).scalar_one()
        return utc(oldest) if oldest is not None else None

    @staticmethod
    def _report_lost(count: int, oldest: Optional[datetime], newest: Optional[datetime]) -> None:
        span = f" ({oldest:%Y-%m-%d %H:%M} to {newest:%Y-%m-%d %H:%M} UTC)" if oldest and newest else ""
        message = f"{count} record(s) that were in no archive file were deleted to keep the disk from filling{span}"
        details = {"deleted": count, "oldest": oldest.isoformat() if oldest else None,
                   "newest": newest.isoformat() if newest else None}
        raise_alarm(AlarmCode.STORAGE_RECORDS_LOST, storage_state.STORAGE_SOURCE, message, AlarmSeverity.CRITICAL, details)
        try:
            storage_state.add_notice(AlarmCode.STORAGE_RECORDS_LOST, message, details,
                                     now=datetime.now(timezone.utc))
        except OSError as exc:
            logger.error("Could not keep the records-lost notice: %s", exc)
