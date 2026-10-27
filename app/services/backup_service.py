"""Backups: small copies of the configuration, and the mirror of the data folder to a second folder.

A backup is a folder, data/backups/<time>-<kind>/ (kind: nightly, upgrade or manual), that holds
  config.db           every table of the database with its indexes, the alembic_version row and every
                      row except those of product_records and detections (the archive protects those);
  system_state.json   a copy of the settings file;
  manifest.json       when, what kind, app version, database revision, rows per table, tables left out.
The folder is built as ``<name>.partial`` and renamed, so a backup is whole or not there. config.db is
read from one read transaction of the live database (WAL lets the server keep writing). Each kind
keeps its newest few folders; the rest are deleted.

mirror() copies backups/, archive/ and audit/ to a second folder (a USB stick or a network share) and
removes there what is gone from data/. It touches nothing else in that folder.

Every function blocks: callers run them in a thread.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.config import settings
from app.services import settings_persistence_service, storage_state
from app.utils.logger import get_logger

logger = get_logger(__name__)

NAME_RE = re.compile(r"^\d{8}T\d{6}Z-(nightly|upgrade|manual)$")
# The two tables that grow without end. A backup keeps them empty; the archive files and the age cleanup deal with them.
BIG_TABLES = ("product_records", "detections")
KINDS = ("nightly", "upgrade", "manual")
# The folders of data/ that mirror() copies.
SUBFOLDERS = ("backups", "archive", "audit")

_ROOT = Path(__file__).resolve().parents[2]
_PARTIAL = ".partial"
_PARTIAL_RE = re.compile(r"\d{8}T\d{6}Z-(nightly|upgrade|manual)\.partial")
# Rows copied per insert.
_CHUNK = 1000
# A zip up to this size is kept in memory, a bigger one in a temp file.
_ZIP_MEMORY = 8 * 1024 * 1024
# A FAT stick keeps file times in 2 second steps: times this close count as the same.
_TIME_TOLERANCE = 2.0

# One backup, trim or zip at a time. A thread that holds it may take it again.
_lock = threading.RLock()
_mirror_lock = threading.Lock()


def keep_counts() -> Dict[str, int]:
    """How many backups of each kind are kept."""
    return {"nightly": settings.BACKUP_KEEP_NIGHTLY, "upgrade": 3, "manual": 5}


def _folder() -> Path:
    return storage_state.data_dir() / "backups"


def _made_at(name: str) -> Optional[datetime]:
    """The time (UTC) in a backup's name; None when the name is not one of ours."""
    if not NAME_RE.fullmatch(name):
        return None
    try:
        return datetime(int(name[0:4]), int(name[4:6]), int(name[6:8]),
                        int(name[9:11]), int(name[11:13]), int(name[13:15]), tzinfo=timezone.utc)
    except ValueError:
        return None  # a month 13: not one of ours


def _kind(name: str) -> str:
    return name.rsplit("-", 1)[1]


def _size(folder: Path) -> int:
    total = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue  # deleted while we looked
    except OSError:
        pass
    return total


def _scan() -> List[Tuple[datetime, str]]:
    """(time, name) of the backup folders, newest first. A file, a .partial folder or any other name is left out."""
    found: List[Tuple[datetime, str]] = []
    try:
        with os.scandir(_folder()) as entries:
            for entry in entries:
                when = _made_at(entry.name)
                if when is None:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        found.append((when, entry.name))
                except OSError:
                    continue  # deleted while we looked
    except FileNotFoundError:
        pass
    found.sort(reverse=True)
    return found


def _entry(when: datetime, name: str) -> Dict[str, Any]:
    return {"name": name, "kind": _kind(name), "made_at": when.isoformat(), "size": _size(_folder() / name)}


def list_backups() -> List[Dict[str, Any]]:
    """The backups, newest first: each {"name", "kind", "made_at" (UTC), "size" (bytes of its files)}."""
    return [_entry(when, name) for when, name in _scan()]


def newest_config_db() -> Optional[Path]:
    """The config.db of the newest backup that has one; None when there is none."""
    for _when, name in _scan():
        path = _folder() / name / "config.db"
        if path.is_file():
            return path
    return None


def _remove_folder(path: Path) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except OSError as exc:
        logger.warning("Could not delete the old backup %s: %s", path.name, exc)
        return False


def trim() -> int:
    """Delete the oldest backups of each kind beyond the number kept, and the .partial folders a crash left.

    Returns how many backups were deleted.
    """
    removed = 0
    with _lock:
        keep = keep_counts()
        seen = {kind: 0 for kind in KINDS}
        for _when, name in _scan():
            kind = _kind(name)
            seen[kind] += 1
            if seen[kind] > keep[kind] and _remove_folder(_folder() / name):
                removed += 1
        try:
            with os.scandir(_folder()) as entries:
                leftovers = [Path(e.path) for e in entries if _PARTIAL_RE.fullmatch(e.name) and e.is_dir(follow_symlinks=False)]
        except FileNotFoundError:
            leftovers = []
        for path in leftovers:
            _remove_folder(path)
    if removed:
        logger.info("Deleted %d old backup(s)", removed)
    return removed


# ── Making a backup ───────────────────────────────────────────────────────────

def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _read_only_uri(db_path: str) -> str:
    return Path(db_path).resolve().as_uri() + "?mode=ro"


def _copy_rows(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> None:
    """Copy every row of a table, a chunk at a time."""
    cursor = src.execute(f"SELECT * FROM {_quote(table)}")
    insert = f"INSERT INTO {_quote(table)} VALUES ({','.join('?' * len(cursor.description))})"
    while True:
        rows = cursor.fetchmany(_CHUNK)
        if not rows:
            break
        dst.executemany(insert, rows)


def _write_config_db(source: str, target: Path) -> Tuple[Dict[str, int], Optional[str], List[str]]:
    """Write config.db from one read transaction of the database. Gives (rows per table, revision, tables left out)."""
    with closing(sqlite3.connect(_read_only_uri(source), uri=True, timeout=30, isolation_level=None)) as src, \
            closing(sqlite3.connect(target, isolation_level=None)) as dst:
        # Everything read from here on is the database as it was at the first read, whatever the server writes.
        src.execute("BEGIN")
        tables = src.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                             "ORDER BY rowid").fetchall()
        counts = {name: src.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0] for name, _sql in tables}
        revision = None
        if "alembic_version" in counts:
            row = src.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = row[0] if row else None
        dst.execute("BEGIN")
        for name, sql in tables:
            dst.execute(sql)
            if name not in BIG_TABLES:
                _copy_rows(src, dst, name)
        # The automatic indexes of a UNIQUE or PRIMARY KEY have no SQL: the table makes them.
        for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
                                  "AND name NOT LIKE 'sqlite_%' ORDER BY rowid").fetchall():
            dst.execute(sql)
        dst.execute("COMMIT")
    return counts, revision, [table for table in BIG_TABLES if table in counts]


def _fsync_file(path: Path) -> None:
    with open(path, "r+b") as f:
        os.fsync(f.fileno())


def make_backup(kind: str, db_path: Optional[str] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Make a backup of ``kind`` (nightly, upgrade or manual) and delete the ones beyond the number kept.

    ``db_path`` is the SQLite file; it defaults to the one in DATABASE_URL. Gives the backup's entry (as
    list_backups does). On any failure the .partial folder is deleted and the error raised. Two backups of
    one kind in the same second: the second replaces the first.
    """
    if kind not in KINDS:
        raise ValueError(f"A backup is one of {', '.join(KINDS)}, not {kind!r}")
    source = db_path or settings_persistence_service._sqlite_path(settings.DATABASE_URL)
    if not source:
        raise ValueError("Backups need a SQLite database")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    name = f"{storage_state.stamp(now)}-{kind}"
    with _lock:
        folder = _folder()
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / (name + _PARTIAL)
        shutil.rmtree(partial, ignore_errors=True)  # one an earlier crash left
        partial.mkdir()
        try:
            counts, revision, left_out = _write_config_db(source, partial / "config.db")
            _fsync_file(partial / "config.db")
            try:
                settings_copy = Path(settings_persistence_service.STATE_FILE).read_bytes()
            except FileNotFoundError:
                settings_copy = None  # a server that has not saved its settings yet
            if settings_copy is not None:
                storage_state.write_atomic(partial / "system_state.json", settings_copy)
            manifest = {
                "made_at": now.isoformat(),
                "kind": kind,
                "app_version": settings.APP_VERSION,
                "revision": revision,
                "tables": counts,
                "left_out": left_out,
            }
            storage_state.write_atomic(partial / "manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
            storage_state.fsync_dir(partial)
            shutil.rmtree(folder / name, ignore_errors=True)
            os.replace(partial, folder / name)
        except BaseException:
            # No half-made folder left taking space on a disk that may be full.
            shutil.rmtree(partial, ignore_errors=True)
            raise
        storage_state.fsync_dir(folder)
        entry = _entry(now, name)
        logger.info("Backup %s made (%d bytes)", name, entry["size"])
        trim()
    return entry


def zip_file(name: str) -> Optional[Any]:
    """A backup folder as a zip, in a temporary file (in memory while small) at position 0. None for an unknown name.

    The caller closes it. The zip holds the files of the folder, without the folder itself.
    """
    if _made_at(name) is None:
        return None
    spooled = tempfile.SpooledTemporaryFile(max_size=_ZIP_MEMORY)
    try:
        with _lock:
            folder = _folder() / name
            if folder.is_symlink() or not folder.is_dir():
                spooled.close()
                return None
            with zipfile.ZipFile(spooled, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(folder.iterdir()):
                    if path.is_file() and not path.is_symlink():
                        archive.write(path, path.name)
    except FileNotFoundError:
        spooled.close()
        return None  # trimmed while we looked
    except BaseException:
        spooled.close()
        raise
    spooled.seek(0)
    return spooled


# ── The upgrade backup ────────────────────────────────────────────────────────

def script_head() -> str:
    """The newest database revision in alembic/versions."""
    config = Config()
    config.set_main_option("script_location", str(_ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


def needs_upgrade_backup(db_path: str, head: str) -> bool:
    """True when the database holds tables and its revision is not ``head``: an upgrade is about to change it.

    False for a missing, empty or new file, and for one that cannot be read: a damaged database
    is for the start-up check to deal with. Never raises.
    """
    try:
        if not os.path.isfile(db_path):
            return False
        with closing(sqlite3.connect(_read_only_uri(db_path), uri=True, timeout=5)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
            if not tables - {"alembic_version"}:
                return False
            revision = None
            if "alembic_version" in tables:
                row = db.execute("SELECT version_num FROM alembic_version").fetchone()
                revision = row[0] if row else None
            return revision != head
    except Exception as exc:
        logger.warning("Could not read %s to decide on an upgrade backup: %s", db_path, exc)
        return False


# ── The second folder ─────────────────────────────────────────────────────────

def _is_link(path: Any) -> bool:
    """A symbolic link or a Windows junction: it leads out of the folder it stands in."""
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda _path: False)(path)


def _is_inside(path: Path, folder: Path) -> bool:
    inner, outer = (os.path.normcase(str(p.resolve())) for p in (path, folder))
    return inner == outer or inner.startswith(outer.rstrip(os.sep) + os.sep)


def _copy_file(source: Path, target: Path) -> bool:
    """Copy a file unless the target is the same size and time. True when it was copied.

    The copy is written as ``<name>.partial`` and renamed, so the target is the old file or the new one.
    A source that is gone by now (the keeper deleted it) is not an error.
    """
    try:
        wanted = source.stat()
    except FileNotFoundError:
        return False
    try:
        have = target.stat()
        if have.st_size == wanted.st_size and abs(have.st_mtime - wanted.st_mtime) <= _TIME_TOLERANCE:
            return False
    except FileNotFoundError:
        pass
    partial = target.with_name(target.name + _PARTIAL)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        partial.unlink(missing_ok=True)
        shutil.copy2(source, partial)
        _fsync_file(partial)
        os.replace(partial, target)
    except BaseException as exc:
        try:
            partial.unlink()
        except OSError:
            pass
        if isinstance(exc, FileNotFoundError) and not source.exists():
            return False
        raise
    return True


def _copy_tree(source: Path, target: Path) -> int:
    """Copy the new and changed files under ``source`` to ``target``. Gives how many were copied."""
    try:
        with os.scandir(source) as entries:
            found = list(entries)
    except FileNotFoundError:
        return 0  # deleted while we looked
    copied = 0
    for entry in found:
        # A .partial file or folder is still being written; a link is not ours to copy.
        if entry.name.endswith(_PARTIAL) or _is_link(entry.path):
            continue
        if entry.is_dir(follow_symlinks=False):
            copied += _copy_tree(Path(entry.path), target / entry.name)
        elif entry.is_file(follow_symlinks=False):
            copied += _copy_file(Path(entry.path), target / entry.name)
    return copied


def _remove_gone(source: Path, target: Path) -> int:
    """Delete the files under ``target`` that no longer exist under ``source``, and the folders that empties.

    Never follows a link, and deletes nothing but files and empty folders. Gives how many files went.
    """
    try:
        with os.scandir(target) as entries:
            found = list(entries)
    except FileNotFoundError:
        return 0
    removed = 0
    for entry in found:
        if _is_link(entry.path):
            continue
        twin = source / entry.name
        if entry.is_dir(follow_symlinks=False):
            removed += _remove_gone(twin, Path(entry.path))
            if not os.path.lexists(twin):
                try:
                    os.rmdir(entry.path)  # only when empty
                except OSError:
                    pass
        elif entry.is_file(follow_symlinks=False) and not os.path.lexists(twin):
            os.unlink(entry.path)
            removed += 1
    return removed


def mirror(copy_dir: str) -> Dict[str, int]:
    """Make backups/, archive/ and audit/ in ``copy_dir`` the same as in the data folder. Gives {"copied", "removed"}.

    New and changed files are copied (``.partial``, then renamed); files gone from the data folder are
    deleted there. Everything is copied first, so a copy that fails leaves the second folder with
    more files, never fewer. Only files inside those three folders of ``copy_dir`` are ever deleted,
    and only when the same file is gone from the same folder of the data folder: other files in
    ``copy_dir`` are not touched, and no link is followed. A folder missing from the data folder is
    not mirrored and nothing is deleted for it (that is a fault to look at, not a clean-up).

    Raises OSError when the folder cannot be written, is not there (a stick that is not plugged in:
    its parent folder is missing) or is the data folder or inside backups/, archive/ or audit/.
    """
    if not copy_dir or not copy_dir.strip():
        raise ValueError("The second folder is not set")
    source = storage_state.data_dir()
    target = Path(copy_dir)
    for name in SUBFOLDERS:
        # Copying a folder into itself would never end (or copy a file onto itself).
        if _is_inside(target / name, source / name) or _is_inside(source / name, target / name):
            raise OSError(errno.EINVAL, f"The second folder would hold a copy of {name}/ inside {name}/ itself", str(target))
    with _mirror_lock:
        # Only the folder itself is made: a missing parent means the stick or the share is not there.
        target.mkdir(exist_ok=True)
        folders = [name for name in SUBFOLDERS if (source / name).is_dir()]
        copied = sum(_copy_tree(source / name, target / name) for name in folders)
        removed = sum(_remove_gone(source / name, target / name)
                      for name in folders if (target / name).is_dir() and not _is_link(target / name))
    if copied or removed:
        logger.info("Second folder %s: %d file(s) copied, %d removed", target, copied, removed)
    return {"copied": copied, "removed": removed}
