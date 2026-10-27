"""The records archive: every product record of one UTC day, in data/archive/records-YYYY-MM-DD.csv.gz.

Each file holds every column of the table (``id`` first) as UTF-8 CSV with a byte order mark and
``\\r\\n`` line ends, gzipped. write_day() reads the day in chunks (keyset paging, one short session
each), so memory stays flat, and writes ``<name>.partial`` that is fsynced and renamed, so a file
is whole or not there. read_day() reads a file back to the rows the table held; reimport() puts
rows back into the table after a rebuild. A text cell that starts with ``= + - @``, a tab, a
return or ``'`` gets a ``'`` in front, so a spreadsheet does not run it; reading removes it again.

The storage keeper writes the files each night, deletes the old ones and tells the age cleanup how
far the records are archived (archived_until). The Records API lists the files and sends them.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
import itertools
import json
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sqlalchemy import func, insert, select

from app.config import settings
from app.db.models.production_record import ProductRecord
from app.services import records_export, storage_state
from app.services.production_records_service import COLUMNS, RecordFilter, normalize_row, parse_time, utc
from app.utils.logger import get_logger

logger = get_logger(__name__)

HEADER = ["id", *COLUMNS]
# Rows read from the table per chunk when a day is written, and put back per insert (tests lower it).
# Rows read per chunk. Each chunk becomes rows on the event loop, so it is kept small.
CHUNK = 1000

_FILE_NAME = re.compile(r"records-([0-9]{4})-([0-9]{2})-([0-9]{2})\.csv\.gz")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_BOM = "\ufeff"
# A text cell starting with one of these would run as a formula in a spreadsheet.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "'")
_ONE_DAY = timedelta(days=1)
# A day is written once it has ended this long, so the records of its last seconds are in the table.
_GRACE = timedelta(minutes=10)


# ── Cells ─────────────────────────────────────────────────────────────────────

def cell_out(value: Any) -> str:
    """A value as the text of a CSV cell. None is empty; text that a spreadsheet would run gets a ' in front."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return utc(value).strftime(_TIME_FORMAT)
    if isinstance(value, str):
        return "'" + value if value.startswith(_FORMULA_START) else value
    return str(value)


def cell_in(text: str) -> str:
    """The text a cell was written from: the one ' that cell_out put in front is removed."""
    return text[1:] if text.startswith("'") else text


def _cells(record: ProductRecord) -> List[str]:
    cells = [cell_out(record.id)]
    for column in COLUMNS:
        value = getattr(record, column)
        if column == "details" and value is not None:
            value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        cells.append(cell_out(value))
    return cells


def _value_in(column: str, text: str) -> Any:
    """The value of a column from its cell; ValueError when the cell is not one."""
    if column == "recorded_at":
        when = parse_time(text)
        if when is None:
            raise ValueError(f"recorded_at {text!r} is not a time")
        return when
    if text == "":
        return None
    if column == "counted":
        if text not in ("true", "false"):
            raise ValueError(f"counted {text!r} is not true or false")
        return text == "true"
    if column == "confidence":
        return float(text)
    if column == "track_id":
        return int(text)
    if column == "details":
        return json.loads(cell_in(text))
    return cell_in(text)


def _row_in(cells: List[str]) -> Dict[str, Any]:
    if len(cells) != len(HEADER):
        raise ValueError(f"{len(cells)} cells instead of {len(HEADER)}")
    return normalize_row({column: _value_in(column, text) for column, text in zip(HEADER[1:], cells[1:])})


# ── The folder ────────────────────────────────────────────────────────────────

def _folder() -> Path:
    return storage_state.data_dir() / "archive"


def file_for(day: date) -> Path:
    return _folder() / f"records-{day.isoformat()}.csv.gz"


def has_file(day: date) -> bool:
    return file_for(day).is_file()


def _day_of(name: str) -> Optional[date]:
    """The day an archive file is named for; None when the name is not one of ours."""
    match = _FILE_NAME.fullmatch(name)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None  # a month 13: not one of ours


def _files() -> Dict[date, int]:
    """{day: size in bytes} of the archive files. A .partial file and anything else in the folder is left out."""
    found: Dict[date, int] = {}
    try:
        with os.scandir(_folder()) as entries:
            for entry in entries:
                day = _day_of(entry.name)
                if day is None:
                    continue
                try:
                    if entry.is_file():
                        found[day] = entry.stat().st_size
                except OSError:
                    continue  # deleted while we looked
    except FileNotFoundError:
        pass
    return found


def list_days() -> List[Dict[str, Any]]:
    """The archived days, oldest first: each {"day": "YYYY-MM-DD", "size": bytes}."""
    return [{"day": day.isoformat(), "size": size} for day, size in sorted(_files().items())]


def retention_days() -> int:
    """Days an archive file is kept: ARCHIVE_RETENTION_DAYS, but not fewer than RECORDS_RETENTION_DAYS.

    A file deleted while its records are still in the database would hold their age cleanup back for
    good (it deletes only archived days). 0 = until the disk needs the space.
    """
    archive = max(0, int(settings.ARCHIVE_RETENTION_DAYS))
    records = max(0, int(settings.RECORDS_RETENTION_DAYS))
    if not archive:
        return 0
    return max(archive, records) if records else archive


def prune_files(days: int, now: datetime) -> int:
    """Delete the files whose day ended more than ``days`` days before ``now``. Returns how many.

    ``days`` of 0 (or less) keeps every file.
    """
    if not days or days <= 0:
        return 0
    cutoff = utc(now) - timedelta(days=days)
    deleted = 0
    for day in sorted(_files()):
        if _start(day) + _ONE_DAY >= cutoff:
            continue
        try:
            file_for(day).unlink()
            deleted += 1
        except OSError as exc:
            logger.warning("Could not delete the old archive file %s: %s", file_for(day).name, exc)
    if deleted:
        logger.info("Deleted %d archive file(s) older than %d day(s)", deleted, days)
    return deleted


def delete_oldest_file() -> Optional[str]:
    """Delete the oldest archive file whatever its age. Returns its name; None when there is none to delete."""
    for day in sorted(_files()):
        path = file_for(day)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Could not delete the archive file %s: %s", path.name, exc)
            continue
        logger.info("Deleted the oldest archive file %s to free space", path.name)
        return path.name
    return None


# ── Writing a day ─────────────────────────────────────────────────────────────

def _start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _csv_bytes(rows: List[List[str]], bom: bool = False) -> bytes:
    buf = io.StringIO()
    if bom:
        # A byte order mark, so spreadsheet programs read the file as UTF-8.
        buf.write(_BOM)
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    return buf.getvalue().encode("utf-8")


class _DayFile:
    """One day's file while it is written: a gzip stream on ``<name>.partial``, renamed by commit().

    Every method blocks (the caller runs them in a thread). The lock makes abort() wait for a write
    that is still running when the task that started it was cancelled.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._partial = path.with_name(path.name + ".partial")
        self._lock = threading.Lock()
        self._file: Optional[Any] = None
        self._gzip: Optional[gzip.GzipFile] = None

    def write_rows(self, rows: List[ProductRecord]) -> None:
        """Add the rows to the file, opening it (with the header) at the first call."""
        with self._lock:
            if self._gzip is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._file = open(self._partial, "wb")
                # No file name in the gzip header: it would say ".partial".
                self._gzip = gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=self._file)
                self._gzip.write(_csv_bytes([HEADER], bom=True))
            self._gzip.write(_csv_bytes([_cells(record) for record in rows]))

    def commit(self) -> None:
        """Finish the stream, make it survive a power cut and give the file its name."""
        with self._lock:
            self._gzip.close()
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            os.replace(self._partial, self._path)
        storage_state.fsync_dir(self._path.parent)

    def abort(self) -> None:
        """Close the stream and delete the .partial file. Never raises."""
        with self._lock:
            for stream in (self._gzip, self._file):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            try:
                self._partial.unlink()
            except OSError:
                pass


async def write_day(session_factory: Any, day: date) -> int:
    """Write the records of a UTC day to its file (an old file of the day is replaced). Returns the rows written.

    A day without records writes no file. On any failure the .partial file is deleted and the
    error raised.
    """
    start = _start(day)
    flt = RecordFilter(start=start, end=start + _ONE_DAY)
    out = _DayFile(file_for(day))
    written = 0
    try:
        async for rows in records_export.iter_chunks(session_factory, flt, CHUNK):
            # Formatting and compressing are the slow part: off the event loop.
            await asyncio.to_thread(out.write_rows, rows)
            written += len(rows)
        if written:
            await asyncio.to_thread(out.commit)
    except BaseException:
        await asyncio.to_thread(out.abort)
        raise
    if written:
        logger.info("Archived %d product record(s) of %s", written, day.isoformat())
    return written


async def _next_record(session_factory: Any, since: Optional[datetime] = None) -> Optional[datetime]:
    """The time of the oldest record at or after ``since`` (of the whole table without it); None when there is none."""
    stmt = select(func.min(ProductRecord.recorded_at))
    if since is not None:
        stmt = stmt.where(ProductRecord.recorded_at >= since)
    async with session_factory() as db:
        found = (await db.execute(stmt)).scalar_one_or_none()
    return utc(found) if found is not None else None


async def days_to_archive(session_factory: Any, now: datetime) -> List[date]:
    """The days that have records, ended at least 10 minutes before ``now`` and have no file yet. Oldest first."""
    done = utc(now) - _GRACE
    have = await asyncio.to_thread(_files)
    days: List[date] = []
    # Hop from the day of one record to the day of the next: days without records cost nothing.
    found = await _next_record(session_factory)
    while found is not None:
        end = _start(found.date()) + _ONE_DAY
        if end > done:
            break
        if found.date() not in have:
            days.append(found.date())
        found = await _next_record(session_factory, end)
    return days


async def archive_pending(session_factory: Any, now: datetime) -> List[date]:
    """Write every day days_to_archive names, oldest first. Returns the days written; raises on the first failure."""
    written: List[date] = []
    for day in await days_to_archive(session_factory, now):
        if await write_day(session_factory, day):
            written.append(day)
    return written


async def archived_until(session_factory: Any) -> Optional[datetime]:
    """The time before which every record sits in a day that has a file.

    That is the start of the oldest UTC day that has records and no file; when every day with
    records has its file, the end of the newest of them. None when there are no records.
    """
    have = await asyncio.to_thread(_files)
    found = await _next_record(session_factory)
    while found is not None:
        if found.date() not in have:
            return _start(found.date())
        end = _start(found.date()) + _ONE_DAY
        found = await _next_record(session_factory, end)
        if found is None:
            return end
    return None


# ── Reading and putting back ──────────────────────────────────────────────────

def read_day(day: date) -> Iterator[Dict[str, Any]]:
    """The rows of a day's file as normalize_row gives them (without the id), oldest first.

    Blocks while it reads: run it in a thread. Raises when the file cannot be read or does not hold
    what write_day wrote (a ValueError names the row).
    """
    path = file_for(day)
    with io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8-sig", newline="") as text:
        reader = csv.reader(text)
        if next(reader, None) != HEADER:
            raise ValueError(f"{path.name} does not start with the records header")
        for number, cells in enumerate(reader, start=2):
            try:
                row = _row_in(cells)
            except ValueError as exc:
                raise ValueError(f"{path.name}, row {number}: {exc}") from exc
            yield row


def _take(rows: Iterator[Dict[str, Any]], size: int, since: Optional[datetime]) -> List[Dict[str, Any]]:
    """The next ``size`` rows at or after ``since``."""
    return list(itertools.islice((row for row in rows if since is None or row["recorded_at"] >= since), size))


def _same(row: Dict[str, Any]) -> Tuple[Any, ...]:
    """Every column of a row but its id, to tell whether the table already has it."""
    return tuple(json.dumps(row[c], sort_keys=True) if c == "details" else row[c] for c in COLUMNS)


async def _insert_new(session_factory: Any, rows: List[Dict[str, Any]], old_ids: int) -> int:
    """Insert the rows the table does not have, as new rows. ``old_ids`` is the newest id from before the import."""
    rec = ProductRecord
    lo, hi = min(r["recorded_at"] for r in rows), max(r["recorded_at"] for r in rows)
    async with session_factory() as db:
        stored = (await db.execute(
            select(rec).where(rec.recorded_at >= lo, rec.recorded_at <= hi, rec.id <= old_ids))).scalars().all()
    have = {_same(normalize_row({c: getattr(r, c) for c in COLUMNS})) for r in stored}
    new = [row for row in rows if _same(row) not in have]
    if new:
        async with session_factory() as db:
            await db.execute(insert(rec), new)
            await db.commit()
    return len(new)


async def reimport(session_factory: Any, days: List[date], after: Optional[datetime]) -> int:
    """Put the rows of the days' files back into the table, CHUNK at a time. Returns how many were added.

    With ``after`` only the rows recorded at or after it come back. A row the table already has (all
    columns but the id alike, as it was before this call) is not added again, so the call can be
    repeated and can follow a partly restored table. The rows get new ids. Raises when a file
    cannot be read; the days before it stay imported.
    """
    since = utc(after) if after is not None else None
    async with session_factory() as db:
        old_ids = (await db.execute(select(func.max(ProductRecord.id)))).scalar_one() or 0
    added = 0
    for day in sorted(days):
        if since is not None and _start(day) + _ONE_DAY <= since:
            continue  # the whole day is older
        rows = read_day(day)
        try:
            while True:
                chunk = await asyncio.to_thread(_take, rows, CHUNK, since)
                if chunk:
                    added += await _insert_new(session_factory, chunk, old_ids)
                if len(chunk) < CHUNK:
                    break
        finally:
            rows.close()
    if added:
        logger.info("Put %d product record(s) back from the archive", added)
    return added
