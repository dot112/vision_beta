"""The audit log: who did what, in data/audit/audit-YYYY-MM.jsonl, one file per UTC month.

One JSON object per line. Each entry is appended, flushed and fsynced at once under a lock, so a
power cut costs at most the line being written; a line that does not parse is skipped when reading.
A file is deleted by prune() once its month ended more than AUDIT_RETENTION_DAYS ago, so an entry
is kept at least that long.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from app.services import storage_state
from app.services.production_records_service import parse_time
from app.utils.logger import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
_FILE_NAME = re.compile(r"audit-(\d{4})-(\d{2})\.jsonl")
# A file is read backwards in blocks of this many bytes.
_BLOCK = 64 * 1024


def _folder() -> Path:
    return storage_state.data_dir() / "audit"


def _month_start(name: str) -> Optional[datetime]:
    """The first moment (UTC) of the month an audit file is named for; None when it is not one of ours."""
    match = _FILE_NAME.fullmatch(name)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=timezone.utc)
    except ValueError:
        return None  # a month 13: not one of ours


def _month_end(start: datetime) -> datetime:
    return datetime(start.year + start.month // 12, start.month % 12 + 1, 1, tzinfo=timezone.utc)


def _path_for(when: datetime) -> Path:
    when = when.astimezone(timezone.utc)
    return _folder() / f"audit-{when.year:04d}-{when.month:02d}.jsonl"


def _month_files() -> List[Path]:
    """The audit files, oldest month first."""
    folder = _folder()
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    return [folder / name for name in sorted(names) if _month_start(name) is not None]


def _lines_backwards(path: Path) -> Iterator[bytes]:
    """The lines of a file, the last one first, read from the end in blocks. Empty lines are left out."""
    with open(path, "rb") as f:
        position = f.seek(0, os.SEEK_END)
        rest = b""  # the start of a line whose beginning is in the next block back
        while position > 0:
            step = min(_BLOCK, position)
            position -= step
            f.seek(position)
            pieces = (f.read(step) + rest).split(b"\n")
            rest = pieces[0]
            for piece in reversed(pieces[1:]):
                if piece:
                    yield piece
        if rest:
            yield rest


def _load_line(raw: bytes) -> Optional[Dict[str, Any]]:
    """The entry on a line; None when the line is cut off, damaged or not an object."""
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def _append_lines(path: Path, lines: List[bytes]) -> None:
    """Add whole lines to a file and make them survive a power cut. The caller holds the lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a+b") as f:
        end = f.seek(0, os.SEEK_END)
        if end:
            f.seek(end - 1)
            if f.read(1) != b"\n":
                # Half a line from a power cut: the new entry starts on a line of its own.
                f.write(b"\n")
        f.write(b"".join(lines))
        f.flush()
        os.fsync(f.fileno())
    if is_new:
        storage_state.fsync_dir(path.parent)


def append(entry: Dict[str, Any]) -> None:
    """Add an entry to the file of its month (the UTC month of its timestamp).

    Raises ValueError when the entry has no timestamp with a time zone, OSError when it cannot be written.
    """
    when = parse_time(entry.get("timestamp"))
    if when is None:
        raise ValueError("an audit entry needs a timestamp with a time zone")
    line = json.dumps(entry).encode("utf-8") + b"\n"
    with _lock:
        _append_lines(_path_for(when), [line])


def _newest_first(before: Optional[datetime], line_id: Optional[str]) -> Iterator[Dict[str, Any]]:
    for path in reversed(_month_files()):
        start = _month_start(path.name)
        if before is not None and start is not None and start >= before:
            continue  # every entry of this month is at or after the time asked for
        try:
            for raw in _lines_backwards(path):
                entry = _load_line(raw)
                if entry is None:
                    continue
                when = parse_time(entry.get("timestamp"))
                if when is None or (before is not None and when >= before):
                    continue
                if line_id and entry.get("line_id") != line_id:
                    continue
                yield entry
        except OSError as exc:
            # A month that cannot be read, or was just pruned, does not hide the others.
            logger.warning("Could not read the audit file %s: %s", path.name, exc)


def read(limit: int = 100, before: Optional[datetime] = None, line_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Up to ``limit`` entries, newest first. ``before`` (a time with a zone) leaves out entries at or after it;
    ``line_id`` keeps only the entries of that line."""
    entries = _newest_first(before, line_id)
    try:
        return list(itertools.islice(entries, max(limit, 0)))
    finally:
        entries.close()


def prune(days: int, now: Optional[datetime] = None) -> int:
    """Delete the files whose month ended more than ``days`` days before ``now``. Returns how many were deleted."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    deleted = 0
    with _lock:
        for path in _month_files():
            start = _month_start(path.name)
            if start is None or _month_end(start) >= cutoff:
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                logger.warning("Could not delete the old audit file %s: %s", path.name, exc)
    return deleted


def _ids_in(path: Path) -> Set[Any]:
    ids: Set[Any] = set()
    try:
        for raw in _lines_backwards(path):
            entry = _load_line(raw)
            if entry is not None and entry.get("id") is not None:
                ids.add(entry["id"])
    except FileNotFoundError:
        pass
    return ids


def move_from_state(entries: List[Dict[str, Any]]) -> int:
    """Add the entries an earlier version kept in the settings file to the audit files, oldest first.

    An entry whose id a file already has is not added again, so moving twice is safe. An entry
    without a readable time is dropped. Returns how many were added; raises OSError when they
    cannot be written.
    """
    dated: List[Tuple[datetime, Dict[str, Any]]] = []
    for entry in entries:
        when = parse_time(entry.get("timestamp")) if isinstance(entry, dict) else None
        if when is not None:
            dated.append((when, entry))
    if len(dated) < len(entries):
        logger.warning("%d audit entries in the settings have no readable time and were dropped", len(entries) - len(dated))
    dated.sort(key=lambda pair: pair[0])
    new_lines: Dict[Path, List[bytes]] = {}
    with _lock:
        known: Dict[Path, Set[Any]] = {}
        for when, entry in dated:
            path = _path_for(when)
            if path not in known:
                known[path] = _ids_in(path)
            if entry.get("id") is not None:
                if entry["id"] in known[path]:
                    continue
                known[path].add(entry["id"])
            new_lines.setdefault(path, []).append(json.dumps(entry).encode("utf-8") + b"\n")
        for path, lines in new_lines.items():
            _append_lines(path, lines)
    return sum(len(lines) for lines in new_lines.values())
