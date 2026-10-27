"""Start-up recovery: the server starts even when its database file is damaged.

A marker file, data/.server_running, is there while the server runs; finding it at a start means
the last run did not end cleanly (power cut, crash, kill), and the database then gets a full
``PRAGMA quick_check`` before anything opens it (otherwise only a cheap open). A damaged database,
or an upgrade that fails because the file is damaged, is moved to data/broken/<time>/ (kept, with
its -wal and -shm files) and replaced by the newest backup's config.db; with no backup the upgrade
builds an empty database. The upgrade runs, the server starts and the lines run.

The records come back in the background, at the data keeper's first run (``finish_rebuild``): the
newest records are read out of the damaged file as far as they read, the archived days inside the
kept days are put back, and the line counters are restored again. A notice and the critical alarm
storage.database_restored say what was used and how many records came back.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional

from sqlalchemy import func, select

from app.config import settings
from app.db.models.production_record import ProductRecord
from app.events.alarm_events import AlarmCode, AlarmSeverity, raise_alarm
from app.services import backup_service, production_records_service, records_archive, storage_state
from app.services.production_records_service import COLUMNS, normalize_row, parse_time, utc
from app.utils.logger import get_logger

logger = get_logger(__name__)

MARKER = ".server_running"
_DAMAGE_WORDS = ("malformed", "not a database")
# Rows read from a damaged file, and put back, at a time.
_PAGE = 5000


def _marker() -> Path:
    return storage_state.data_dir() / MARKER


def unclean_end() -> bool:
    """True when the last run of the server did not end cleanly."""
    return _marker().exists()


def mark_running() -> None:
    try:
        storage_state.write_atomic(_marker(), datetime.now(timezone.utc).isoformat().encode("utf-8"))
    except OSError as exc:
        logger.warning("Could not write %s: %s", MARKER, exc)


def mark_stopped() -> None:
    try:
        _marker().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove %s: %s", MARKER, exc)


def check(db_path: str, full: bool) -> bool:
    """True when the database opens (and, ``full``, passes PRAGMA quick_check), or is not there yet."""
    if not os.path.exists(db_path):
        return True
    try:
        with closing(sqlite3.connect(backup_service._read_only_uri(db_path), uri=True)) as db:
            if full:
                row = db.execute("PRAGMA quick_check").fetchone()
                return bool(row) and str(row[0]).lower() == "ok"
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return True
    except sqlite3.DatabaseError as exc:
        logger.error("The database %s does not open cleanly: %s", db_path, exc)
        return False


def is_damage(exc: BaseException) -> bool:
    """True for a SQLite error that says the file is damaged, however it was wrapped."""
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for error in (current, getattr(current, "orig", None)):
            if isinstance(error, sqlite3.DatabaseError) and any(word in str(error).lower() for word in _DAMAGE_WORDS):
                return True
        current = current.__cause__ or current.__context__
    return False


def rebuild(db_path: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Move the damaged database aside and put the newest backup's config.db in its place.

    Gives {"broken_dir", "backup"} (backup: the backup's name, None when there is none).
    """
    now = utc(now or datetime.now(timezone.utc))
    broken_dir = storage_state.data_dir() / "broken" / storage_state.stamp(now)
    broken_dir.mkdir(parents=True, exist_ok=True)
    name = os.path.basename(db_path)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            shutil.move(db_path + suffix, str(broken_dir / (name + suffix)))
    backup = backup_service.newest_config_db()
    if backup is not None:
        shutil.copy2(backup, db_path)
        with open(db_path, "r+b") as f:
            os.fsync(f.fileno())
    storage_state.fsync_dir(Path(db_path).parent)
    info = {"broken_dir": str(broken_dir), "backup": backup.parent.name if backup is not None else None}
    logger.error("The damaged database was moved to %s and rebuilt from %s", broken_dir, info["backup"] or "nothing (no backup)")
    try:
        storage_state.update(lambda state: state.__setitem__("rebuild", {
            **info, "database": str(broken_dir / name), "pending": True, "at": now.isoformat()}))
    except OSError as exc:
        logger.error("Could not note the rebuild in the state file; the records are not put back: %s", exc)
    return info


def _report(message: str, details: Dict[str, Any]) -> None:
    raise_alarm(AlarmCode.STORAGE_DATABASE_RESTORED, storage_state.STORAGE_SOURCE, message, AlarmSeverity.CRITICAL, details)
    try:
        storage_state.add_notice(AlarmCode.STORAGE_DATABASE_RESTORED, message, details)
    except OSError as exc:
        logger.error("Could not keep the database-restored notice: %s", exc)


def _used(info: Dict[str, Any]) -> str:
    return f"the backup {info['backup']}" if info.get("backup") else "no backup (an empty database)"


async def open_database(db_path: str, upgrade: Callable[[], Awaitable[None]],
                        dispose: Callable[[], Awaitable[None]]) -> Optional[Dict[str, Any]]:
    """Check, rebuild when damaged, and upgrade the database. Gives the rebuild info, or None when it was fine.

    An upgrade that fails because the file is damaged leads to one rebuild and one more upgrade;
    any other error, or a second failure, is raised (the start fails, as before).
    """
    info: Optional[Dict[str, Any]] = None
    full = unclean_end()
    if full:
        logger.warning("The last run did not end cleanly: checking the database")
    if not await asyncio.to_thread(check, db_path, full):
        await dispose()
        info = await asyncio.to_thread(rebuild, db_path)
    try:
        await upgrade()
    except Exception as exc:
        if not is_damage(exc):
            raise
        logger.error("The database upgrade found a damaged file: %s", exc)
        await dispose()
        info = await asyncio.to_thread(rebuild, db_path)
        await upgrade()
    mark_running()
    if info is not None:
        _report(f"The database was damaged and was rebuilt from {_used(info)}; the records are being put back "
                f"from the archive. The damaged file is kept in {info['broken_dir']}.", dict(info))
    return info


# ── Getting the records back ──────────────────────────────────────────────────

def _row_from(values: tuple) -> Optional[Dict[str, Any]]:
    """A product_records row read with plain sqlite3, as the table's insert takes it."""
    row = dict(zip(COLUMNS, values))
    try:
        when = row["recorded_at"]
        if isinstance(when, str):
            when = datetime.fromisoformat(when)
        row["recorded_at"] = utc(when)
        if isinstance(row.get("details"), str):
            row["details"] = json.loads(row["details"])
        row["counted"] = bool(row.get("counted"))
        return normalize_row(row)
    except (TypeError, ValueError, KeyError):
        return None


def _salvage(broken_db: str, after: Optional[datetime]) -> Iterator[List[Dict[str, Any]]]:
    """Pages of the rows recorded at or after ``after``, newest first, until the file stops reading."""
    if not os.path.exists(broken_db):
        return
    columns = ", ".join(COLUMNS)
    try:
        db = sqlite3.connect(backup_service._read_only_uri(broken_db), uri=True)
    except sqlite3.Error:
        return
    with closing(db):
        last_id: Optional[int] = None
        while True:
            try:
                if last_id is None:
                    page = db.execute(f"SELECT id, {columns} FROM product_records ORDER BY id DESC LIMIT ?", (_PAGE,)).fetchall()
                else:
                    page = db.execute(f"SELECT id, {columns} FROM product_records WHERE id < ? ORDER BY id DESC LIMIT ?",
                                      (last_id, _PAGE)).fetchall()
            except sqlite3.Error as exc:
                logger.warning("Stopped reading the damaged database at id %s: %s", last_id, exc)
                return
            if not page:
                return
            last_id = page[-1][0]
            rows = [row for row in (_row_from(values[1:]) for values in page) if row is not None]
            wanted = [row for row in rows if after is None or row["recorded_at"] >= after]
            if wanted:
                yield wanted
            if after is not None and not wanted:
                return  # a whole page older than wanted: the rest is older still


def salvage_rows(broken_db: str, after: Optional[datetime]) -> List[Dict[str, Any]]:
    """The records of a damaged database recorded at or after ``after``, as far as they read. Never raises."""
    found: List[Dict[str, Any]] = []
    try:
        for page in _salvage(broken_db, utc(after) if after is not None else None):
            found.extend(page)
    except Exception as exc:
        logger.warning("Could not read records from %s: %s", broken_db, exc)
    return found


async def _put_back(session_factory: Any, rows: List[Dict[str, Any]]) -> int:
    async with session_factory() as db:
        newest_id = (await db.execute(select(func.max(ProductRecord.id)))).scalar_one() or 0
    added = 0
    for start in range(0, len(rows), _PAGE):
        # Rows the table already has (a second run) are not added again.
        added += await records_archive._insert_new(session_factory, rows[start:start + _PAGE], newest_id)
    return added


async def finish_rebuild(session_factory: Any) -> Dict[str, int]:
    """After a rebuild: salvage, put the archived days back, restore the counters. Gives {"salvaged", "reimported"}."""
    state = storage_state.load().get("rebuild")
    if not isinstance(state, dict) or not state.get("pending"):
        return {"salvaged": 0, "reimported": 0}
    days = [date.fromisoformat(entry["day"]) for entry in await asyncio.to_thread(records_archive.list_days)]

    # The newest records are in no archive file yet: read them out of the damaged file.
    after = records_archive._start(max(days)) + timedelta(days=1) if days else None
    rows = await asyncio.to_thread(salvage_rows, state.get("database") or "", after)
    salvaged = await _put_back(session_factory, rows) if rows else 0

    # The archived days inside the kept days, from the newest record ever deleted on (inclusive).
    keep = settings.RECORDS_RETENTION_DAYS
    first = (datetime.now(timezone.utc) - timedelta(days=keep)).date() if keep and keep > 0 else None
    deleted_through = parse_time(storage_state.load().get("deleted_through"))
    reimported = 0
    for day in sorted(d for d in days if first is None or d >= first):
        try:
            reimported += await records_archive.reimport(session_factory, [day], deleted_through)
        except Exception as exc:
            logger.error("Could not put the archived day %s back: %s", day, exc)

    await production_records_service.production_recorder.flush()
    await production_records_service.restore_line_counts(session_factory)

    def done(current: Dict[str, Any]) -> None:
        rebuilt = current.get("rebuild") if isinstance(current.get("rebuild"), dict) else {}
        rebuilt.update(pending=False, salvaged=salvaged, reimported=reimported)
        current["rebuild"] = rebuilt
    try:
        storage_state.update(done)
    except OSError as exc:
        logger.error("Could not note the end of the rebuild: %s", exc)
    _report(f"The database was damaged and was rebuilt from {_used(state)}: {salvaged} record(s) salvaged from the "
            f"damaged file and {reimported} put back from the archive. The damaged file is kept in {state.get('broken_dir')}.",
            {"backup": state.get("backup"), "broken_dir": state.get("broken_dir"),
             "salvaged": salvaged, "reimported": reimported})
    return {"salvaged": salvaged, "reimported": reimported}
