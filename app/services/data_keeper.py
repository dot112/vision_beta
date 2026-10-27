"""The data keeper: one background task that keeps the server's data safe and the disk from filling.

It ticks every minute (the first tick a minute after the start):
  every tick                 the clock-jump check, then the free-space floor (storage_guard);
  first tick after a start   the alarms of open notices and of a clock pause come back; the records
                             come back after a rebuild of a damaged database (recovery.py); then the
                             night jobs (the nightly backup only if none was made that UTC day);
  every hour                 the age cleanup (records up to the archived days, detections, kept
                             totals, archive files, audit files), then the copy to the second folder;
  each night after 00:10 UTC the archive of the finished days, the nightly backup, the copy.
Each job runs on its own: an error is logged, raises its alarm where it has one (archive, backup,
copy) and the job is tried again later; the next good run clears the alarm.

Every delete of records happens here, one after the other on this one task, so nothing else races
the keeping of line totals (production_records_service.delete_chunk).

The clock guard pauses deleting by age when the clock looks wrong: when the cleanup would delete
every record, or when the clock jumps forward by more than a day while the server runs. An Admin
confirms the clock (Data and backups page) to go on; the free-space floor keeps working meanwhile.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, time as day_time, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import func, select

from app.config import settings
from app.db import recovery
from app.db.models.production_record import ProductRecord
from app.events.alarm_events import ALARM_CATALOG, AlarmCode, AlarmSeverity, alarm_manager, clear_alarm, raise_alarm
from app.services import (
    audit_log,
    backup_service,
    detections_cleanup,
    production_records_service,
    records_archive,
    storage_state,
)
from app.services.production_records_service import parse_time, utc
from app.utils.logger import get_logger

logger = get_logger(__name__)

SOURCE = storage_state.STORAGE_SOURCE
WOULD_DELETE_EVERYTHING = "would_delete_everything"
CLOCK_JUMPED = "clock_jumped"
_PAUSE_MESSAGES = {
    WOULD_DELETE_EVERYTHING: "The newest record is older than the records are kept, so deleting by age would remove "
                             "them all. Old data is not deleted by age until an Admin confirms the clock.",
    CLOCK_JUMPED: "The clock jumped forward by more than a day while the server was running. Old data is not "
                  "deleted by age until an Admin confirms the clock.",
}
# A jump forward bigger than this pauses deleting by age.
JUMP_LIMIT = timedelta(days=1)


def _iso(when: Optional[datetime]) -> Optional[str]:
    return utc(when).isoformat() if when is not None else None


def _severity(code: str) -> str:
    return next((entry["severity"] for entry in ALARM_CATALOG if entry["code"] == code), AlarmSeverity.WARNING.value)


def _keep_last(job: str, when: datetime) -> None:
    try:
        storage_state.update(lambda state: state.setdefault("last", {}).__setitem__(job, _iso(when)))
    except OSError as exc:
        logger.warning("Could not note the last %s run: %s", job, exc)


# ── The clock guard ───────────────────────────────────────────────────────────

class ClockGuard:
    """Pauses deleting by age when the clock looks wrong. The pause is kept in storage_state.json."""

    def __init__(self) -> None:
        self._last: Optional[tuple] = None          # (wall clock, monotonic) at the last jump check
        self._held: Optional[str] = None            # a pause the state file could not keep

    @staticmethod
    def _state() -> Dict[str, Any]:
        clock = storage_state.load().get("clock")
        return clock if isinstance(clock, dict) else {}

    def paused(self) -> bool:
        return bool(self._held) or bool(self._state().get("paused"))

    def reason(self) -> Optional[str]:
        return self._held or (self._state().get("reason") if self._state().get("paused") else None)

    def _pause(self, reason: str, now: datetime) -> str:
        self._held = reason
        try:
            def change(state: Dict[str, Any]) -> None:
                clock = state.get("clock") if isinstance(state.get("clock"), dict) else {}
                clock.update(paused=True, reason=reason, since=_iso(now))
                state["clock"] = clock
            storage_state.update(change)
        except OSError as exc:
            logger.error("Could not keep the clock pause in the state file: %s", exc)
        self.raise_alarm(reason)
        return reason

    @staticmethod
    def raise_alarm(reason: str) -> None:
        raise_alarm(AlarmCode.STORAGE_CLOCK_CHECK, SOURCE, _PAUSE_MESSAGES.get(reason, _PAUSE_MESSAGES[CLOCK_JUMPED]),
                    AlarmSeverity.WARNING, {"reason": reason})

    def check_start(self, newest: Optional[datetime], now: datetime, days: int) -> Optional[str]:
        """Pause when deleting records older than ``days`` would delete every record. Gives the new reason, or None."""
        if self.paused() or newest is None or not days or days <= 0:
            return None
        if utc(newest) >= utc(now) - timedelta(days=days):
            return None
        if self._state().get("confirmed_newest") == _iso(newest):
            return None  # an Admin said the clock is right with this same newest record
        return self._pause(WOULD_DELETE_EVERYTHING, now)

    def check_jump(self, wall: datetime, mono: float, newest: Optional[datetime]) -> Optional[str]:
        """Pause when the clock ran ahead of the server's own running time by more than a day. Gives the new reason, or None."""
        wall = utc(wall)
        last, self._last = self._last, (wall, mono)
        if last is None or self.paused():
            return None
        expected = last[0] + timedelta(seconds=mono - last[1])
        if wall - expected <= JUMP_LIMIT:
            return None
        if newest is not None and wall <= utc(newest) + JUMP_LIMIT:
            return None  # a clock that was behind, corrected: it now lands next to the newest record
        return self._pause(CLOCK_JUMPED, wall)

    def confirm(self, newest: Optional[datetime], now: datetime) -> None:
        """An Admin says the clock is right: deleting by age goes on."""
        self._held = None
        self._last = None
        try:
            def change(state: Dict[str, Any]) -> None:
                state["clock"] = {"paused": False, "reason": None, "since": None, "confirmed_newest": _iso(newest)}
            storage_state.update(change)
        except OSError as exc:
            logger.error("Could not keep the clock confirmation in the state file: %s", exc)
        clear_alarm(AlarmCode.STORAGE_CLOCK_CHECK, SOURCE, "an Admin confirmed the clock")

    def status(self) -> Dict[str, Any]:
        state = self._state()
        paused = self.paused()
        return {"paused": paused, "reason": self.reason() if paused else None,
                "since": state.get("since") if paused else None}


# ── The keeper ────────────────────────────────────────────────────────────────

class DataKeeper:
    TICK_SECONDS = 60
    HOUR_SECONDS = 3600
    NIGHT_AFTER = day_time(0, 10)

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None, recorder: Any = None, guard: Any = None,
                 clock: Optional[Callable[[], datetime]] = None, monotonic: Callable[[], float] = time.monotonic):
        self._session_factory = session_factory
        self._recorder = recorder
        self._guard = guard
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self.clock_guard = ClockGuard()
        self._task: Optional[asyncio.Task] = None
        self._first_done = False
        self._next_hour: Optional[float] = None
        self._night_done: Optional[date] = None

    # The real database, recorder and guard, looked up on use so tests can give their own.
    def _sessions(self) -> Callable[[], Any]:
        if self._session_factory is None:
            from app.db.session import AsyncSessionLocal
            return AsyncSessionLocal
        return self._session_factory

    def _records(self) -> Any:
        return self._recorder if self._recorder is not None else production_records_service.production_recorder

    @property
    def guard(self) -> Any:
        if self._guard is None:
            from app.services.storage_guard import StorageGuard
            self._guard = StorageGuard(self._session_factory, self._recorder)
        return self._guard

    def _now(self) -> datetime:
        return utc(self._clock())

    # ── Life cycle ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start ticking on the running loop; the first tick comes TICK_SECONDS from now."""
        if self.running:
            return
        self._task = asyncio.get_running_loop().create_task(self._run(), name="data_keeper")
        logger.info("Data keeper started")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.TICK_SECONDS)
            try:
                await self.tick()
            except Exception:
                logger.exception("Data keeper error")

    # ── The schedule ──────────────────────────────────────────────────────────

    async def tick(self) -> None:
        if not self._first_done:
            self._first_done = True
            await self.first_run()
        now = self._now()
        self.clock_guard.check_jump(now, self._monotonic(), await self._attempt("newest record", self.newest_record))
        await self._attempt("free-space check", lambda: self.guard.check(now))
        mono = self._monotonic()
        if self._next_hour is None or mono >= self._next_hour:
            self._next_hour = mono + self.HOUR_SECONDS
            await self.hourly(now)
        if now.time() >= self.NIGHT_AFTER and self._night_done != now.date():
            await self.nightly(now)

    async def first_run(self) -> None:
        """At the first tick after a start: the alarms come back, then the night jobs that are due."""
        self._first_done = True
        now = self._now()
        for notice in storage_state.notices():
            raise_alarm(notice["code"], SOURCE, notice.get("message") or notice["code"], _severity(notice["code"]),
                        notice.get("details") or {})
        if self.clock_guard.paused():
            self.clock_guard.raise_alarm(self.clock_guard.reason() or CLOCK_JUMPED)
        # After a rebuild of a damaged database: the records come back, then the counters.
        await self._attempt("putting the records back after a rebuild", lambda: recovery.finish_rebuild(self._sessions()))
        await self.nightly(now)

    async def hourly(self, now: datetime) -> None:
        if not self.clock_guard.paused():
            newest = await self._attempt("newest record", self.newest_record)
            self.clock_guard.check_start(newest, now, settings.RECORDS_RETENTION_DAYS)
        if self.clock_guard.paused():
            logger.info("Deleting by age is paused until an Admin confirms the clock")
        else:
            await self._age_cleanup(now)
        await self._copy(now)

    async def nightly(self, now: datetime, force_backup: bool = False) -> None:
        """Archive the finished days (when ARCHIVE_RECORDS is on), make the nightly backup (unless one was made that UTC day), copy."""
        if settings.ARCHIVE_RECORDS:
            try:
                days = await records_archive.archive_pending(self._sessions(), now)
                clear_alarm(AlarmCode.STORAGE_ARCHIVE_FAILED, SOURCE, "the archive run worked")
                _keep_last("archive", now)
                if days:
                    logger.info("Archived %d day(s) of records", len(days))
            except Exception as exc:
                logger.exception("Could not archive the records")
                raise_alarm(AlarmCode.STORAGE_ARCHIVE_FAILED, SOURCE, f"Records could not be archived: {exc}",
                            AlarmSeverity.WARNING, {"error": str(exc)})
        else:
            clear_alarm(AlarmCode.STORAGE_ARCHIVE_FAILED, SOURCE, "the archive is switched off")
        if force_backup or not await self._backed_up_on(now.date()):
            await self._backup("nightly", now)
        await self._copy(now)
        if now.time() >= self.NIGHT_AFTER:
            self._night_done = now.date()

    # ── The jobs ──────────────────────────────────────────────────────────────

    async def _attempt(self, what: str, job: Callable[[], Awaitable[Any]]) -> Any:
        """Run one job; an error is logged and gives None, so the next job still runs."""
        try:
            return await job()
        except Exception:
            logger.exception("Data keeper: %s failed", what)
            return None

    async def _age_cleanup(self, now: datetime) -> None:
        sessions = self._sessions()
        # After a settings restore, lines missing from the restored settings would count as gone:
        # their records would be deleted without being kept in the totals. That waits for an Admin.
        held = any(notice["code"] == AlarmCode.STORAGE_SETTINGS_RESTORED for notice in storage_state.notices())
        if held:
            logger.info("Old records are kept while the settings-restored notice is open")
        else:
            async def records() -> None:
                before = None
                if settings.ARCHIVE_RECORDS:
                    before = await records_archive.archived_until(sessions)
                    if before is None:
                        return  # no records at all
                await self._records().prune(now=now, before=before)
            await self._attempt("record cleanup", records)
            await self._attempt("kept totals cleanup", lambda: production_records_service.prune_kept_totals(sessions))
        await self._attempt("detections cleanup",
                            lambda: detections_cleanup.prune(sessions, settings.DETECTIONS_RETENTION_DAYS, now))
        await self._attempt("archive files cleanup",
                            lambda: asyncio.to_thread(records_archive.prune_files, settings.ARCHIVE_RETENTION_DAYS, now))
        await self._attempt("audit files cleanup",
                            lambda: asyncio.to_thread(audit_log.prune, settings.AUDIT_RETENTION_DAYS, now))
        _keep_last("cleanup", now)

    async def _backed_up_on(self, day: date) -> bool:
        try:
            found = await asyncio.to_thread(backup_service.list_backups)
        except Exception:
            logger.exception("Could not list the backups")
            return False
        for entry in found:
            made = parse_time(entry.get("made_at"))
            if entry.get("kind") == "nightly" and made is not None and made.date() == day:
                return True
        return False

    async def _backup(self, kind: str, now: datetime) -> Optional[Dict[str, Any]]:
        try:
            entry = await asyncio.to_thread(backup_service.make_backup, kind, None, now)
        except Exception as exc:
            logger.exception("Could not make the %s backup", kind)
            raise_alarm(AlarmCode.STORAGE_BACKUP_FAILED, SOURCE, f"The {kind} backup could not be made: {exc}",
                        AlarmSeverity.WARNING, {"kind": kind, "error": str(exc)})
            return None
        clear_alarm(AlarmCode.STORAGE_BACKUP_FAILED, SOURCE, "a backup was made")
        _keep_last("backup", now)
        return entry

    async def _copy(self, now: datetime) -> None:
        folder = (settings.BACKUP_COPY_DIR or "").strip()
        if not folder:
            clear_alarm(AlarmCode.STORAGE_COPY_FAILED, SOURCE, "no second folder is set")
            return
        try:
            await asyncio.to_thread(backup_service.mirror, folder)
        except Exception as exc:
            logger.warning("Could not copy to the second folder %s: %s", folder, exc)
            raise_alarm(AlarmCode.STORAGE_COPY_FAILED, SOURCE,
                        f"Backups and archive files could not be copied to {folder}: {exc}",
                        AlarmSeverity.WARNING, {"folder": folder, "error": str(exc)})
            return
        clear_alarm(AlarmCode.STORAGE_COPY_FAILED, SOURCE, "the second folder was written")
        _keep_last("copy", now)

    # ── What the Data and backups page asks for ───────────────────────────────

    async def backup_now(self) -> Dict[str, Any]:
        """A manual backup now, then the copy to the second folder. Raises when the backup cannot be made."""
        now = self._now()
        entry = await asyncio.to_thread(backup_service.make_backup, "manual", None, now)
        clear_alarm(AlarmCode.STORAGE_BACKUP_FAILED, SOURCE, "a backup was made")
        _keep_last("backup", now)
        await self._copy(now)
        return entry

    async def confirm_clock(self) -> None:
        self.clock_guard.confirm(await self.newest_record(), self._now())

    async def newest_record(self) -> Optional[datetime]:
        async with self._sessions()() as db:
            newest = (await db.execute(select(func.max(ProductRecord.recorded_at)))).scalar_one()
        return utc(newest) if newest is not None else None

    async def _oldest_record(self) -> Optional[datetime]:
        async with self._sessions()() as db:
            oldest = (await db.execute(select(func.min(ProductRecord.recorded_at)))).scalar_one()
        return utc(oldest) if oldest is not None else None

    @staticmethod
    def _database_size() -> int:
        from app.services.settings_persistence_service import _sqlite_path
        path = _sqlite_path(settings.DATABASE_URL)
        if not path:
            return 0
        return sum(os.path.getsize(name) for name in (path, path + "-wal") if os.path.isfile(name))

    async def status(self) -> Dict[str, Any]:
        """Everything the Data and backups page shows."""
        disk = await self._attempt("disk reading", self.guard.status) or {}
        oldest = await self._attempt("oldest record", self._oldest_record)
        newest = await self._attempt("newest record", self.newest_record)
        days: List[Dict[str, Any]] = await asyncio.to_thread(records_archive.list_days)
        backups: List[Dict[str, Any]] = await asyncio.to_thread(backup_service.list_backups)
        last = storage_state.load().get("last")
        last = last if isinstance(last, dict) else {}
        folder = (settings.BACKUP_COPY_DIR or "").strip()
        copy_ok = None
        if folder:
            if alarm_manager.get(AlarmCode.STORAGE_COPY_FAILED, SOURCE) is not None:
                copy_ok = False
            elif last.get("copy"):
                copy_ok = True
        return {
            "disk": disk,
            "database": {"size": await asyncio.to_thread(self._database_size), "oldest": _iso(oldest), "newest": _iso(newest)},
            "archive": {"enabled": bool(settings.ARCHIVE_RECORDS), "days": len(days), "size": sum(d["size"] for d in days),
                        "oldest": days[0]["day"] if days else None, "newest": days[-1]["day"] if days else None,
                        "retention_days": settings.ARCHIVE_RETENTION_DAYS},
            "backups": backups,
            "copy": {"dir": folder, "ok": copy_ok, "last": last.get("copy")},
            "clock": self.clock_guard.status(),
            "notices": storage_state.notices(),
            "last": {job: last.get(job) for job in ("cleanup", "archive", "backup", "copy")},
        }


data_keeper = DataKeeper()
