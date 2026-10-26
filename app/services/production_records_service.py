"""Product records: every product a line decides and every code read, kept in the database.

``ProductionRecorder.record()`` is what the counters and the lines call. It
only queues the row (inference threads call it, so it never waits on the
database); a task on the main loop writes the queue in one insert per second,
or as soon as FLUSH_ROWS rows wait. The same task deletes rows older than
RECORDS_RETENTION_DAYS once an hour. The products a counter still holds are
counted into kept_totals as they are deleted, so the line totals stay exact.

The rest of the module reads the table: the filters of the Records API, the
summary, and the line totals a counter gets back after a restart.
"""
from __future__ import annotations

import asyncio
import collections
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import settings
from app.db.models.kept_total import KeptTotal
from app.db.models.production_record import ProductRecord
from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager, raise_alarm
from app.services import storage_state
from app.utils.logger import get_logger

logger = get_logger(__name__)

SOURCE = "production_records"

# The text columns and their lengths: longer values are cut to fit.
_TEXT_COLUMNS = {
    "line_id": 48, "line_name": 64, "camera_id": 64, "camera_name": 128, "kind": 16, "result": 8,
    "reject_reason": 32, "class_name": 128, "code": 512, "code_format": 32, "code_status": 16,
    "product_name": 128, "product_list_id": 36, "batch": 64,
}
COLUMNS = (
    "recorded_at", "line_id", "line_name", "camera_id", "camera_name", "kind", "counted", "result",
    "reject_reason", "class_name", "confidence", "track_id", "code", "code_format", "code_status",
    "product_name", "product_list_id", "batch", "details",
)


def utc(value: datetime) -> datetime:
    """An aware UTC time. A time without a zone is taken as UTC (the database keeps UTC without one)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_time(value: Any) -> Optional[datetime]:
    """An ISO time with its zone ("Z" or an offset) as UTC; None when it is not one."""
    if isinstance(value, datetime):
        return utc(value)
    text = str(value or "").strip()
    if not text:
        return None
    # A "+" left unencoded in a query string arrives as a space.
    if len(text) > 6 and text[-6] == " " and text[-3] == ":":
        text = text[:-6] + "+" + text[-5:]
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _text(value: Any, column: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text[:_TEXT_COLUMNS[column]] if text else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def build_row(payload: Dict[str, Any], kind: str, counted: bool, **extra: Any) -> Dict[str, Any]:
    """One row of the table from an event's message payload.

    The payloads name their code fields differently (a vision product's are
    ``qr_code`` / ``qr_format``, a read's ``code`` / ``format``);
    send_dispatcher_service.template_values maps both to the names used here,
    and looks the camera's name up. ``extra`` sets or overrides columns.
    """
    from app.services.send_dispatcher_service import template_values

    values = template_values(payload)
    result = None
    if kind == "product":
        result = "reject" if payload.get("is_defect") else "good"
    details = {}
    vision_result = payload.get("vision_result")
    if vision_result is not None:
        details["vision_result"] = "reject" if str(vision_result).upper() == "REJECTED" else "good"
    for key in ("stations", "bbox", "reject_camera_id", "product", "product_check", "code_problem", "sequence",
                "expected_lane", "seen_lane"):
        if payload.get(key) is not None:
            details[key] = payload[key]
    # The product ID a code holds, where it is not the whole code (a list with a code format).
    if payload.get("product_id") is not None and payload.get("product_id") != values.get("code"):
        details["product_id"] = payload["product_id"]
    row = {
        "recorded_at": datetime.now(timezone.utc),
        "line_id": values.get("line_id"),
        "line_name": values.get("line_name"),
        "camera_id": values.get("camera_id"),
        "camera_name": values.get("camera_name"),
        "kind": kind,
        "counted": bool(counted),
        "result": result,
        "reject_reason": payload.get("reject_reason") if result == "reject" else None,
        "class_name": payload.get("class_name"),
        "confidence": payload.get("confidence") if kind == "product" else None,
        "track_id": payload.get("track_id"),
        "code": values.get("code"),
        "code_format": values.get("code_format"),
        "code_status": values.get("code_status"),
        "product_name": values.get("product_name"),
        "product_list_id": payload.get("product_list_id"),
        "batch": values.get("batch"),
        "details": details or None,
    }
    row.update(extra)
    return normalize_row(row)


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Every column, each value of its column's type (one insert takes many rows of one shape)."""
    out: Dict[str, Any] = {}
    for column in COLUMNS:
        value = row.get(column)
        if column in _TEXT_COLUMNS:
            value = _text(value, column)
        elif column == "recorded_at":
            value = utc(value) if isinstance(value, datetime) else datetime.now(timezone.utc)
        elif column == "counted":
            value = bool(value)
        elif column == "confidence":
            value = _number(value)
        elif column == "track_id":
            value = _integer(value)
        out[column] = value
    out["kind"] = out["kind"] or "product"
    return out


# ── The writer ────────────────────────────────────────────────────────────────

class ProductionRecorder:
    """Queues rows from any thread and writes them in batches on the main loop."""

    FLUSH_SECONDS = 1.0
    FLUSH_ROWS = 500
    RETENTION_SECONDS = 3600.0
    # The first clean-up runs this long after the start, not during it.
    FIRST_RETENTION_SECONDS = 60.0
    DELETE_CHUNK = 5000
    FAILURES_BEFORE_ALARM = 3

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None, limit: Optional[int] = None):
        self._session_factory = session_factory
        self._limit = limit
        self._rows: Deque[Dict[str, Any]] = collections.deque()
        self._lock = threading.Lock()
        # Rows dropped since the last write, and whether records.dropped was raised for them.
        self._dropped = 0
        self._drop_alarmed = False
        self.dropped_total = 0
        self.failures = 0
        # Inserts that succeeded, and the rows they wrote (for the health view and tests).
        self.transactions = 0
        self.written = 0
        self.running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._wake_pending = False
        self._task: Optional[asyncio.Task] = None
        self._next_retention = 0.0

    @property
    def limit(self) -> int:
        return max(1, int(self._limit if self._limit is not None else settings.RECORDS_QUEUE_LIMIT))

    def _sessions(self) -> Callable[[], Any]:
        if self._session_factory is None:
            from app.db.session import AsyncSessionLocal
            return AsyncSessionLocal
        return self._session_factory

    def waiting(self) -> int:
        with self._lock:
            return len(self._rows)

    # ── Queue ─────────────────────────────────────────────────────────────────

    def record(self, row: Dict[str, Any]) -> bool:
        """Queue one row (any thread; never blocks). False when the recorder is not running."""
        if not self.running:
            return False
        with self._lock:
            dropped = self._trim(1)
            self._rows.append(row)
            wake = len(self._rows) >= self.FLUSH_ROWS and not self._wake_pending
            if wake:
                self._wake_pending = True
        if dropped:
            self._report_dropped()
        if wake:
            self._wake_soon()
        return True

    def _trim(self, room: int) -> int:
        """Drop the oldest rows until ``room`` more fit. Call with the lock held. Returns how many went."""
        dropped = 0
        while self._rows and len(self._rows) + room > self.limit:
            self._rows.popleft()
            dropped += 1
        self._dropped += dropped
        self.dropped_total += dropped
        return dropped

    def _report_dropped(self) -> None:
        with self._lock:
            if self._drop_alarmed:
                return
            self._drop_alarmed = True
            dropped = self._dropped
        logger.error("Product records queue full (%d rows): dropping the oldest", self.limit)
        try:
            raise_alarm(
                AlarmCode.RECORDS_DROPPED, SOURCE,
                f"Product records were dropped: more than {self.limit} waited to be written",
                AlarmSeverity.WARNING, {"dropped": dropped, "queue_limit": self.limit},
            )
        except Exception:
            logger.debug("Could not raise the records.dropped alarm", exc_info=True)

    def _wake_soon(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass

    # ── Writing ───────────────────────────────────────────────────────────────

    async def flush(self) -> int:
        """Write every waiting row in one insert. Returns the rows written (0 on a database error)."""
        with self._lock:
            batch = list(self._rows)
            self._rows.clear()
            dropped = self._dropped
            self._dropped = 0
            self._drop_alarmed = False
        if batch:
            try:
                async with self._sessions()() as db:
                    await db.execute(insert(ProductRecord), batch)
                    await db.commit()
            except Exception as exc:
                with self._lock:
                    # Still dropped since the last write that went through.
                    self._dropped += dropped
                    self._drop_alarmed = self._drop_alarmed or bool(dropped)
                self._keep(batch)
                self.failures += 1
                logger.warning("Could not write %d product record(s) (try %d): %s", len(batch), self.failures, exc)
                if self.failures >= self.FAILURES_BEFORE_ALARM:
                    self._alarm_write_failed(exc)
                return 0
            self.transactions += 1
            self.written += len(batch)
        if self.failures:
            self.failures = 0
            self._clear(AlarmCode.RECORDS_WRITE_FAILED, "product records written again")
        if not dropped:
            self._clear(AlarmCode.RECORDS_DROPPED, "no product records dropped since the last write")
        return len(batch)

    def _keep(self, batch: List[Dict[str, Any]]) -> None:
        """Put rows that could not be written back in front of the queue, as far as it holds them."""
        with self._lock:
            room = max(0, self.limit - len(self._rows))
            lost = max(0, len(batch) - room)
            if lost:
                self._dropped += lost
                self.dropped_total += lost
            self._rows.extendleft(reversed(batch[lost:]))
        if lost:
            self._report_dropped()

    def _alarm_write_failed(self, exc: Exception) -> None:
        try:
            raise_alarm(
                AlarmCode.RECORDS_WRITE_FAILED, SOURCE,
                f"Product records could not be written {self.failures} times in a row: {exc}",
                AlarmSeverity.CRITICAL, {"failures": self.failures, "waiting": self.waiting()},
            )
        except Exception:
            logger.debug("Could not raise the records.write_failed alarm", exc_info=True)

    @staticmethod
    def _clear(code: str, reason: str) -> None:
        try:
            if alarm_manager.get(code, SOURCE) is not None:
                alarm_manager.clear_alarm(code, SOURCE, reason)
        except Exception:
            logger.debug("Could not clear the %s alarm", code, exc_info=True)

    async def prune(self, days: Optional[int] = None, now: Optional[datetime] = None,
                    before: Optional[datetime] = None) -> int:
        """Delete rows older than ``days`` (RECORDS_RETENTION_DAYS), DELETE_CHUNK at a time. Returns how many.

        With ``before``, only the rows older than that too (rows not archived yet stay).
        """
        days = settings.RECORDS_RETENTION_DAYS if days is None else days
        if not days or days <= 0:
            return 0
        cutoff = utc(now or datetime.now(timezone.utc)) - timedelta(days=days)
        if before is not None:
            cutoff = min(cutoff, utc(before))
        lines = saved_lines()
        removed = 0
        while True:
            # One short transaction per chunk, so the writer never waits long.
            count, _ = await self.delete_chunk(ProductRecord.recorded_at < cutoff, lines)
            removed += count
            if count < self.DELETE_CHUNK:
                break
            await asyncio.sleep(0.05)
        if removed:
            logger.info("Deleted %d product record(s) older than %d day(s)", removed, days)
        return removed

    async def delete_chunk(self, where: Any, lines: Optional[Dict[str, Dict[str, Any]]] = None
                           ) -> Tuple[int, Optional[datetime]]:
        """Delete up to DELETE_CHUNK rows matching ``where``, oldest first, in one transaction.

        The products among them that a counter still holds are added to
        kept_totals in the same transaction. ``lines`` are the saved lines by id
        (saved_lines()): the rows of a line not among them are only deleted.
        Returns the rows deleted and the newest recorded_at among them, which
        storage_state.json keeps as "deleted_through" (when it is the newest yet).
        """
        if lines is None:
            lines = saved_lines()
        rec = ProductRecord
        async with self._sessions()() as db:
            rows = (await db.execute(
                select(rec.id, rec.recorded_at, rec.line_id, rec.camera_id, rec.kind, rec.counted, rec.result, rec.class_name)
                .where(where).order_by(rec.recorded_at, rec.id).limit(self.DELETE_CHUNK)
            )).all()
            if not rows:
                return 0, None
            kept = _kept_counts(rows, lines)
            if kept:
                upsert = sqlite_insert(KeptTotal)
                upsert = upsert.on_conflict_do_update(
                    index_elements=list(_KEPT_KEY), set_={"count": KeptTotal.count + upsert.excluded["count"]})
                await db.execute(upsert, [{**dict(zip(_KEPT_KEY, key)), "count": n} for key, n in kept.items()])
            ids = [row.id for row in rows]
            deleted = 0
            for start in range(0, len(ids), _IDS_PER_STATEMENT):
                result = await db.execute(delete(rec).where(rec.id.in_(ids[start:start + _IDS_PER_STATEMENT])))
                deleted += int(result.rowcount or 0)
            await db.commit()
        newest = utc(rows[-1].recorded_at)
        await _note_deleted_through(newest)
        return deleted, newest

    # ── Life cycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start writing (on the running main loop). Rows are only queued while it runs."""
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._wake_pending = False
        self._next_retention = time.monotonic() + self.FIRST_RETENTION_SECONDS
        self.running = True
        self._task = self._loop.create_task(self._run(), name="production_records")
        logger.info("Product records writer started (keeping %s)",
                    f"{settings.RECORDS_RETENTION_DAYS} days" if settings.RECORDS_RETENTION_DAYS > 0 else "every record")

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.FLUSH_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            with self._lock:
                self._wake_pending = False
            try:
                await self.flush()
            except Exception:
                logger.exception("Product records writer error")
            if time.monotonic() >= self._next_retention:
                self._next_retention = time.monotonic() + self.RETENTION_SECONDS
                try:
                    await self.prune()
                except Exception:
                    logger.exception("Could not delete old product records")

    async def stop(self) -> None:
        """Stop the task and write what is left (at shutdown, before the database closes)."""
        self.running = False
        task, self._task = self._task, None
        if task is not None:
            if task.get_loop() is asyncio.get_running_loop():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            elif not task.get_loop().is_closed():
                # Started by another app instance's loop (tests): stop it there.
                task.get_loop().call_soon_threadsafe(task.cancel)
        for _ in range(3):
            if not self.waiting():
                break
            await self.flush()
        left = self.waiting()
        if left:
            logger.error("%d product record(s) could not be written at shutdown", left)
        self._loop = None
        self._wake = None


production_recorder = ProductionRecorder()


def record_event(payload: Dict[str, Any], kind: str, counted: bool, **extra: Any) -> None:
    """Queue the row of one product or code event. Never raises: recording must not stop counting."""
    if not production_recorder.running:
        return
    try:
        production_recorder.record(build_row(payload, kind, counted, **extra))
    except Exception:
        logger.exception("Could not queue a product record")


# ── Reading ───────────────────────────────────────────────────────────────────

MAX_RANGE_DAYS = 366


@dataclass
class RecordFilter:
    start: datetime
    end: datetime
    line_id: Optional[str] = None
    result: Optional[str] = None
    kind: Optional[str] = None
    batch: Optional[str] = None
    code: Optional[str] = None
    camera_id: Optional[str] = None
    counted: Optional[bool] = None

    def apply(self, stmt: Any) -> Any:
        stmt = stmt.where(ProductRecord.recorded_at >= self.start, ProductRecord.recorded_at < self.end)
        if self.line_id:
            stmt = stmt.where(ProductRecord.line_id == self.line_id)
        if self.result:
            stmt = stmt.where(ProductRecord.result == self.result)
        if self.kind:
            stmt = stmt.where(ProductRecord.kind == self.kind)
        if self.batch:
            stmt = stmt.where(ProductRecord.batch == self.batch)
        if self.code:
            stmt = stmt.where(ProductRecord.code.contains(self.code, autoescape=True))
        if self.camera_id:
            stmt = stmt.where(ProductRecord.camera_id == self.camera_id)
        if self.counted is not None:
            stmt = stmt.where(ProductRecord.counted.is_(self.counted))
        return stmt

    def line_products(self, stmt: Any) -> Any:
        """Only the products in the line totals, unless a camera or ``counted`` was asked for."""
        stmt = self.apply(stmt).where(ProductRecord.kind == "product")
        if self.counted is None and not self.camera_id:
            stmt = stmt.where(ProductRecord.counted.is_(True))
        return stmt


def row_dict(record: ProductRecord) -> Dict[str, Any]:
    return {
        "id": record.id,
        "recorded_at": utc(record.recorded_at).isoformat().replace("+00:00", "Z"),
        **{column: getattr(record, column) for column in COLUMNS if column != "recorded_at"},
    }


async def list_records(db: Any, flt: RecordFilter, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    total = (await db.execute(flt.apply(select(func.count(ProductRecord.id))))).scalar_one()
    stmt = flt.apply(select(ProductRecord)).order_by(ProductRecord.recorded_at.desc(), ProductRecord.id.desc())
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {"total": int(total), "rows": [row_dict(r) for r in rows]}


def _split(rows: Any) -> Dict[Optional[str], Dict[str, int]]:
    """{key: {total, good, reject}} from (key, result, count) rows."""
    out: Dict[Optional[str], Dict[str, int]] = {}
    for key, result, count in rows:
        entry = out.setdefault(key, {"total": 0, "good": 0, "reject": 0})
        entry["total"] += int(count)
        if result in ("good", "reject"):
            entry[result] += int(count)
    return out


def _yield(good: int, total: int) -> Optional[float]:
    return round(good * 100.0 / total, 2) if total else None


async def summary(db: Any, flt: RecordFilter, bucket: str = "hour", tz: tzinfo = timezone.utc) -> Dict[str, Any]:
    """Totals, by class, by reject reason, by camera and per hour or day (in ``tz``) of the filtered rows.

    The totals are the line's products (the counted rows), as the counters
    show them, unless the filter names a camera or ``counted``.
    """
    rec = ProductRecord
    by_class = _split((await db.execute(
        flt.line_products(select(rec.class_name, rec.result, func.count(rec.id))).group_by(rec.class_name, rec.result)
    )).all())
    total = sum(v["total"] for v in by_class.values())
    good = sum(v["good"] for v in by_class.values())
    reject = sum(v["reject"] for v in by_class.values())

    reasons = (await db.execute(
        flt.line_products(select(rec.reject_reason, func.count(rec.id)))
        .where(rec.result == "reject").group_by(rec.reject_reason)
    )).all()

    cameras = (await db.execute(
        flt.apply(select(rec.camera_id, rec.result, func.count(rec.id), func.max(rec.camera_name), func.max(rec.counted)))
        .where(rec.kind == "product").group_by(rec.camera_id, rec.result)
    )).all()
    by_camera: Dict[Optional[str], Dict[str, Any]] = {}
    for camera_id, result, count, name, counted in cameras:
        entry = by_camera.setdefault(camera_id, {"camera_id": camera_id, "camera_name": None, "counted": False,
                                                 "total": 0, "good": 0, "reject": 0})
        entry["camera_name"] = entry["camera_name"] or name
        entry["counted"] = entry["counted"] or bool(counted)
        entry["total"] += int(count)
        if result in ("good", "reject"):
            entry[result] += int(count)

    hours = (await db.execute(
        flt.line_products(select(func.strftime("%Y-%m-%d %H", rec.recorded_at).label("hour"), rec.result, func.count(rec.id)))
        .group_by("hour", rec.result)
    )).all()
    buckets: Dict[datetime, Dict[str, int]] = {}
    for hour, result, count in hours:
        if not hour:
            continue
        start = datetime.strptime(hour, "%Y-%m-%d %H").replace(tzinfo=timezone.utc).astimezone(tz)
        start = start.replace(minute=0, second=0, microsecond=0)
        if bucket == "day":
            # A ZoneInfo gives midnight its own offset (a DST change may fall inside the day).
            start = start.replace(hour=0)
        entry = buckets.setdefault(start, {"total": 0, "good": 0, "reject": 0})
        entry["total"] += int(count)
        if result in ("good", "reject"):
            entry[result] += int(count)

    codes = (await db.execute(
        flt.apply(select(rec.code_status, func.count(rec.id))).where(rec.code_status.is_not(None)).group_by(rec.code_status)
    )).all()

    return {
        "start": flt.start.isoformat().replace("+00:00", "Z"),
        "end": flt.end.isoformat().replace("+00:00", "Z"),
        "line_id": flt.line_id or None,
        "totals": {"total": total, "good": good, "reject": reject, "yield": _yield(good, total)},
        "by_class": sorted(
            ({"class_name": name, **counts} for name, counts in by_class.items()),
            key=lambda e: (-e["total"], str(e["class_name"] or "")),
        ),
        "by_reject_reason": sorted(
            ({"reject_reason": reason, "count": int(count)} for reason, count in reasons),
            key=lambda e: (-e["count"], str(e["reject_reason"] or "")),
        ),
        "by_camera": sorted(by_camera.values(), key=lambda e: (-e["total"], str(e["camera_id"] or ""))),
        "codes": {str(status): int(count) for status, count in codes},
        "bucket": bucket,
        "buckets": [
            {"start": start.isoformat(), **counts, "yield": _yield(counts["good"], counts["total"])}
            for start, counts in sorted(buckets.items())
        ],
    }


# ── Line totals after a restart ───────────────────────────────────────────────

def class_key(class_name: Optional[str]) -> str:
    """A class's key in a counter: lower case, "product" when there is none."""
    return str(class_name or "").strip().lower() or "product"


def reset_tag(line: Dict[str, Any], key: str, own_station: bool = False) -> str:
    """The tag of the reset a counter's class counts from, as kept_totals keeps it.

    The ISO time of the later of the line's counts_reset_at and the class's own
    time in counts_reset_classes (only the line's for an own-station camera);
    "" when there is neither. The times may be ISO text or datetimes.
    """
    when = parse_time(line.get("counts_reset_at"))
    if not own_station:
        for name, value in (line.get("counts_reset_classes") or {}).items():
            at = parse_time(value)
            if class_key(name) == key and at is not None and (when is None or at > when):
                when = at
    return when.isoformat() if when is not None else ""


def saved_lines() -> Dict[str, Dict[str, Any]]:
    """The saved lines by id, for their reset times."""
    from app.services.settings_persistence_service import SettingsPersistenceService

    return {line["id"]: line for line in SettingsPersistenceService.get_lines(with_logic=False)}


async def counted_totals(
    db: Any,
    line_id: str,
    since: Optional[datetime],
    class_resets: Optional[Dict[str, datetime]] = None,
    camera_id: Optional[str] = None,
) -> Dict[str, Any]:
    """A counter's totals from the records: products recorded since ``since``.

    Without ``camera_id`` these are the line totals (counted rows); with it,
    the own-station camera's products. ``class_resets`` leaves out a class's
    products from before the time that class alone was reset. The kept totals
    of deleted records are added while their tag is the class's current one.
    """
    rec = ProductRecord

    def products(stmt: Any) -> Any:
        stmt = stmt.where(rec.line_id == line_id, rec.kind == "product")
        if camera_id is None:
            stmt = stmt.where(rec.counted.is_(True))
        else:
            stmt = stmt.where(rec.counted.is_(False), rec.camera_id == camera_id)
        if since is not None:
            stmt = stmt.where(rec.recorded_at >= utc(since))
        return stmt.group_by(rec.class_name, rec.result)

    counts: Dict[str, int] = {}
    rejects: Dict[str, int] = {}

    def add(rows: Any, sign: int, only: Optional[str] = None) -> None:
        for class_name, result, count in rows:
            key = class_key(class_name)
            if only is not None and key != only:
                continue
            counts[key] = counts.get(key, 0) + sign * int(count)
            if result == "reject":
                rejects[key] = rejects.get(key, 0) + sign * int(count)

    add((await db.execute(products(select(rec.class_name, rec.result, func.count(rec.id))))).all(), 1)
    for name, reset_at in (class_resets or {}).items():
        stmt = products(select(rec.class_name, rec.result, func.count(rec.id)).where(rec.recorded_at < utc(reset_at)))
        add((await db.execute(stmt)).all(), -1, only=class_key(name))

    # Deleted records still counted: the tags are made as delete_chunk made them.
    resets = {"counts_reset_at": since, "counts_reset_classes": class_resets or {}}
    kept = (await db.execute(
        select(KeptTotal.class_name, KeptTotal.result, KeptTotal.reset_tag, KeptTotal.count)
        .where(KeptTotal.line_id == line_id, KeptTotal.camera_id == (camera_id or ""))
    )).all()
    add(((name, result, count) for name, result, tag, count in kept
         if tag == reset_tag(resets, class_key(name), own_station=camera_id is not None)), 1)
    counts = {k: v for k, v in counts.items() if v > 0}
    rejects = {k: min(v, counts.get(k, 0)) for k, v in rejects.items() if v > 0 and k in counts}
    total = sum(counts.values())
    rejected = sum(rejects.values())
    return {
        "total_inspected": total,
        "good_count": total - rejected,
        "rejected_count": rejected,
        "counts_by_class": counts,
        "rejects_by_class": rejects,
    }


def _reset_times(line: Dict[str, Any]) -> Tuple[Optional[datetime], Dict[str, datetime]]:
    since = parse_time(line.get("counts_reset_at"))
    classes = {}
    for name, value in (line.get("counts_reset_classes") or {}).items():
        when = parse_time(value)
        if when is not None and (since is None or when > since):
            classes[str(name)] = when
    return since, classes


async def restore_line_counts(session_factory: Optional[Callable[[], Any]] = None) -> Dict[str, int]:
    """Give every line's counters their totals back from the records (at startup, after the lines are built).

    Returns {line_id: total_inspected}.
    """
    from app.services.line_service import line_manager

    if session_factory is None:
        from app.db.session import AsyncSessionLocal
        session_factory = AsyncSessionLocal
    saved = saved_lines()
    restored: Dict[str, int] = {}
    async with session_factory() as db:
        for runtime in line_manager.all():
            since, classes = _reset_times(saved.get(runtime.id) or {})
            totals = await counted_totals(db, runtime.id, since, classes)
            runtime.counter.restore(totals)
            restored[runtime.id] = totals["total_inspected"]
            for camera_id, aux in list(runtime.aux_counters.items()):
                aux.restore(await counted_totals(db, runtime.id, since, camera_id=camera_id))
    return restored


# ── Kept totals of deleted records ────────────────────────────────────────────

_KEPT_KEY = ("line_id", "camera_id", "class_name", "result", "reset_tag")
# Ids per DELETE: SQLite before 3.32 takes at most 999 values in one statement.
_IDS_PER_STATEMENT = 500


def _kept_counts(rows: Any, lines: Dict[str, Dict[str, Any]]) -> Dict[Tuple[str, ...], int]:
    """The rows (about to be deleted) that a counter still holds, counted by their kept_totals key.

    A counted product is in its line's totals from its class's reset on, an
    own-station product (not counted, with a camera) in its camera's from the
    line's reset on. A line that is not saved any more has no counter.
    """
    tags: Dict[Tuple[str, str, bool], Tuple[str, Optional[datetime]]] = {}
    kept: Dict[Tuple[str, ...], int] = {}
    for row in rows:
        line = lines.get(row.line_id) if row.line_id else None
        if line is None or row.kind != "product" or not (row.counted or row.camera_id):
            continue
        key = class_key(row.class_name)
        own = not row.counted
        if (row.line_id, key, own) not in tags:
            tag = reset_tag(line, key, own_station=own)
            tags[(row.line_id, key, own)] = (tag, parse_time(tag))
        tag, since = tags[(row.line_id, key, own)]
        if since is not None and utc(row.recorded_at) < since:
            continue
        entry = (row.line_id, row.camera_id if own else "", key, "reject" if row.result == "reject" else "good", tag)
        kept[entry] = kept.get(entry, 0) + 1
    return kept


async def _note_deleted_through(newest: datetime) -> None:
    """Keep the newest recorded_at ever deleted in storage_state.json: a rebuilt database re-imports after it."""
    def change(state: Dict[str, Any]) -> None:
        old = parse_time(state.get("deleted_through"))
        if old is None or newest > old:
            state["deleted_through"] = newest.isoformat()

    try:
        await asyncio.to_thread(storage_state.update, change)
    except OSError as exc:
        # The rows are gone either way: a rebuild may then re-import some of them.
        logger.warning("Could not note the newest deleted product record in %s: %s", storage_state.STATE_FILE_NAME, exc)


async def prune_kept_totals(session_factory: Optional[Callable[[], Any]] = None,
                            lines: Optional[Dict[str, Dict[str, Any]]] = None) -> int:
    """Delete the kept totals no counter reads any more: their line was reset since, or is gone. Returns how many."""
    if session_factory is None:
        from app.db.session import AsyncSessionLocal
        session_factory = AsyncSessionLocal
    if lines is None:
        lines = saved_lines()
    k = KeptTotal
    async with session_factory() as db:
        rows = (await db.execute(select(k.id, k.line_id, k.camera_id, k.class_name, k.reset_tag))).all()
        stale = [row.id for row in rows if row.line_id not in lines
                 or row.reset_tag != reset_tag(lines[row.line_id], row.class_name, own_station=bool(row.camera_id))]
        for start in range(0, len(stale), _IDS_PER_STATEMENT):
            await db.execute(delete(k).where(k.id.in_(stale[start:start + _IDS_PER_STATEMENT])))
        await db.commit()
    if stale:
        logger.info("Deleted %d kept total(s) of lines reset or deleted since", len(stale))
    return len(stale)
