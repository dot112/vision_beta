"""Backups: small config-only copies of the database, kept per kind, zipped, and mirrored to a second folder."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import main
from app.config import settings
from app.events.alarm_events import AlarmCode, alarm_manager
from app.services import backup_service, storage_state
from tests.test_production_records import _alembic

NOW = datetime(2026, 10, 26, 0, 10, tzinfo=timezone.utc)
# The newest revision in alembic/versions.
HEAD = "0009_kept_totals"
THREE_FILES = ["config.db", "manifest.json", "system_state.json"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _upgrade(tmp_path, name, revision="head"):
    from alembic import command

    engine, config = _alembic(tmp_path, name)
    config.set_main_option("path_separator", "os")      # alembic.ini names it version_path_separator only
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, revision)
    finally:
        engine.dispose()
    return str(tmp_path / name)


@pytest.fixture
def db_at_head(tmp_path):
    """A database upgraded to the newest revision and in WAL mode, like the server's. Gives its path."""
    path = _upgrade(tmp_path, "factory.db")
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
    return path


def _insert_user(path, username):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO users (id, username, hashed_password, role, clearance_level, is_active, created_at, updated_at) "
                   "VALUES (?, ?, 'hash', 'operator', 1, 1, '2026-10-25 12:00:00', '2026-10-25 12:00:00')", (username, username))


def _fill(path):
    """One row in two big tables and in three small ones."""
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO product_records (recorded_at, kind, counted) VALUES ('2026-10-25 12:00:00', 'product', 1)")
        db.execute("INSERT INTO detections (id, model_name, total_detections, detections, inference_time_ms) "
                   "VALUES ('d1', 'model', 0, '[]', 1.5)")
        db.execute("INSERT INTO kept_totals (line_id, camera_id, class_name, result, reset_tag, count) "
                   "VALUES ('line-1', 'cam-1', 'bottle', 'ok', 'tag', 7)")
        db.execute("INSERT INTO product_lists (id, name) VALUES ('list-2', 'Second list')")
    _insert_user(path, "ahmed")


def _tables(db):
    return sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))


def _indexes(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _schema(db):
    return sorted(db.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def _count(db, table):
    return db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def _rows(db, table):
    return sorted(db.execute(f'SELECT * FROM "{table}"'), key=repr)


def _backup_folder(data_dir, entry):
    return data_dir / "backups" / entry["name"]


def _seed(data_dir, files):
    for relative, data in files.items():
        path = data_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _files_under(folder):
    return sorted(p.relative_to(folder).as_posix() for p in Path(folder).rglob("*") if p.is_file())


# ── The backup ────────────────────────────────────────────────────────────────

def test_config_db_holds_every_table_with_the_big_two_empty(db_at_head, data_dir):
    _fill(db_at_head)
    entry = backup_service.make_backup("manual", db_at_head, NOW)
    with closing(sqlite3.connect(data_dir / "backups" / entry["name"] / "config.db")) as copy, \
            closing(sqlite3.connect(db_at_head)) as src:
        assert _tables(copy) == _tables(src)
        assert {t: _count(copy, t) for t in ("product_records", "detections")} == {"product_records": 0, "detections": 0}
        assert all(_count(copy, t) == _count(src, t) for t in _tables(src) if t not in backup_service.BIG_TABLES)
        assert all(_rows(copy, t) == _rows(src, t) for t in _tables(src) if t not in backup_service.BIG_TABLES)
        assert _schema(copy) == _schema(src)
        assert "ix_product_records_line_time" in _indexes(copy)
        assert copy.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_folder_holds_settings_and_manifest(db_at_head, data_dir, settings_file):
    _fill(db_at_head)
    Path(settings_file).write_text('{"version": 2, "lines": []}', encoding="utf-8")
    entry = backup_service.make_backup("nightly", db_at_head, NOW)
    folder = data_dir / "backups" / entry["name"]
    assert entry["name"] == "20261026T001000Z-nightly"
    assert sorted(p.name for p in folder.iterdir()) == THREE_FILES
    assert (folder / "system_state.json").read_bytes() == Path(settings_file).read_bytes()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["kind"] == "nightly" and manifest["left_out"] == ["product_records", "detections"]
    assert manifest["tables"]["product_records"] == 1 and manifest["revision"] == HEAD   # rows in the source
    assert manifest["tables"]["detections"] == 1 and manifest["tables"]["users"] == 1
    assert manifest["made_at"] == "2026-10-26T00:10:00+00:00" and manifest["app_version"] == settings.APP_VERSION
    assert entry["kind"] == "nightly" and entry["made_at"] == manifest["made_at"]
    assert entry["size"] == sum(p.stat().st_size for p in folder.iterdir())


def test_a_backup_without_a_settings_file_holds_the_other_two(db_at_head, data_dir, settings_file):
    entry = backup_service.make_backup("manual", db_at_head, NOW)
    assert sorted(p.name for p in _backup_folder(data_dir, entry).iterdir()) == ["config.db", "manifest.json"]


def test_a_backup_reads_one_snapshot_while_the_server_keeps_writing(db_at_head, data_dir, monkeypatch):
    _fill(db_at_head)
    real = backup_service._copy_rows
    wrote = []

    def writing(src, dst, table):
        if not wrote:
            wrote.append(table)
            _insert_user(db_at_head, "late")    # committed by the server while the backup is half done
        return real(src, dst, table)

    monkeypatch.setattr(backup_service, "_copy_rows", writing)
    entry = backup_service.make_backup("manual", db_at_head, NOW)
    with closing(sqlite3.connect(db_at_head)) as src, \
            closing(sqlite3.connect(_backup_folder(data_dir, entry) / "config.db")) as copy:
        assert _count(src, "users") == 2
        assert _count(copy, "users") == 1
    manifest = json.loads((_backup_folder(data_dir, entry) / "manifest.json").read_text())
    assert manifest["tables"]["users"] == 1


def test_a_writer_that_has_not_committed_does_not_hold_the_backup_up(db_at_head, data_dir):
    _fill(db_at_head)
    writer = sqlite3.connect(db_at_head)
    try:
        writer.execute("INSERT INTO kept_totals (line_id, camera_id, class_name, result, reset_tag, count) "
                       "VALUES ('line-1', 'cam-2', 'bottle', 'ok', 'tag', 1)")   # a write transaction left open
        entry = backup_service.make_backup("manual", db_at_head, NOW)
    finally:
        writer.rollback()
        writer.close()
    with closing(sqlite3.connect(_backup_folder(data_dir, entry) / "config.db")) as copy:
        assert _count(copy, "kept_totals") == 1


def test_the_default_database_is_the_one_in_the_settings(db_at_head, data_dir, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{Path(db_at_head).as_posix()}")
    entry = backup_service.make_backup("manual", now=NOW)
    assert (_backup_folder(data_dir, entry) / "config.db").is_file()
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://user@host/db")
    with pytest.raises(ValueError):
        backup_service.make_backup("manual", now=NOW + timedelta(seconds=1))


def test_an_unknown_kind_is_refused(db_at_head, data_dir):
    with pytest.raises(ValueError):
        backup_service.make_backup("weekly", db_at_head, NOW)
    assert not (data_dir / "backups").exists()


def test_a_second_backup_in_the_same_second_replaces_the_first(db_at_head, data_dir):
    backup_service.make_backup("manual", db_at_head, NOW)
    _fill(db_at_head)
    entry = backup_service.make_backup("manual", db_at_head, NOW)
    assert [b["name"] for b in backup_service.list_backups()] == [entry["name"]]
    with closing(sqlite3.connect(_backup_folder(data_dir, entry) / "config.db")) as copy:
        assert _count(copy, "users") == 1


def test_a_failed_backup_leaves_no_partial_folder(db_at_head, data_dir, monkeypatch):
    good = backup_service.make_backup("nightly", db_at_head, NOW)

    def broken(src, dst, table):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup_service, "_copy_rows", broken)
    with pytest.raises(OSError):
        backup_service.make_backup("manual", db_at_head, NOW + timedelta(hours=1))
    assert [p.name for p in (data_dir / "backups").iterdir()] == [good["name"]]


def test_a_backup_never_changes_the_source(db_at_head, data_dir):
    _fill(db_at_head)
    with closing(sqlite3.connect(db_at_head)) as src:
        before = (_schema(src), {t: _rows(src, t) for t in _tables(src)})
    backup_service.make_backup("manual", db_at_head, NOW)
    with closing(sqlite3.connect(db_at_head)) as src:
        assert (_schema(src), {t: _rows(src, t) for t in _tables(src)}) == before


# ── Keeping, listing, zipping ─────────────────────────────────────────────────

def test_keep_counts_per_kind(db_at_head, data_dir, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_KEEP_NIGHTLY", 7)
    assert backup_service.keep_counts() == {"nightly": settings.BACKUP_KEEP_NIGHTLY, "upgrade": 3, "manual": 5}
    for i in range(9):
        backup_service.make_backup("nightly", db_at_head, NOW + timedelta(days=i))
    for i in range(4):
        backup_service.make_backup("upgrade", db_at_head, NOW + timedelta(hours=i))
    for i in range(6):
        backup_service.make_backup("manual", db_at_head, NOW + timedelta(minutes=i))
    kinds = [b["kind"] for b in backup_service.list_backups()]
    assert kinds.count("nightly") == 7 and kinds.count("upgrade") == 3 and kinds.count("manual") == 5
    assert backup_service.list_backups()[0]["made_at"] > backup_service.list_backups()[-1]["made_at"]
    # The oldest ones of a kind went, not the newest.
    nightly = [b["name"] for b in backup_service.list_backups() if b["kind"] == "nightly"]
    assert nightly[0] == "20261103T001000Z-nightly" and nightly[-1] == "20261028T001000Z-nightly"
    assert sorted(p.name for p in (data_dir / "backups").iterdir()) == sorted(b["name"] for b in backup_service.list_backups())


def test_the_nightly_count_follows_the_setting(db_at_head, data_dir, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_KEEP_NIGHTLY", 2)
    for i in range(4):
        backup_service.make_backup("nightly", db_at_head, NOW + timedelta(days=i))
    assert [b["name"] for b in backup_service.list_backups()] == ["20261029T001000Z-nightly", "20261028T001000Z-nightly"]


def test_listing_ignores_other_folders_and_partials(data_dir):
    root = data_dir / "backups"
    for name in ("x", "20261026T001000Z-nightly.partial", "20261026T001000Z-other", "20261340T001000Z-nightly", "2026"):
        (root / name).mkdir(parents=True)
    (root / "20261026T001500Z-manual").write_text("a file, not a backup folder")
    assert backup_service.list_backups() == []
    good = root / "20261026T002000Z-upgrade"
    good.mkdir()
    (good / "config.db").write_bytes(b"12345")
    (good / "manifest.json").write_bytes(b"123")
    assert backup_service.list_backups() == [
        {"name": "20261026T002000Z-upgrade", "kind": "upgrade", "made_at": "2026-10-26T00:20:00+00:00", "size": 8}
    ]


def test_there_are_no_backups_before_the_first(data_dir):
    assert backup_service.list_backups() == []
    assert backup_service.trim() == 0
    assert backup_service.newest_config_db() is None


def test_trim_removes_a_partial_folder_a_crash_left(db_at_head, data_dir):
    leftover = data_dir / "backups" / "20261025T001000Z-nightly.partial"
    leftover.mkdir(parents=True)
    (leftover / "config.db").write_bytes(b"half")
    other = data_dir / "backups" / "notes"
    other.mkdir()
    assert backup_service.trim() == 0
    assert not leftover.exists() and other.exists()


def test_newest_config_db_is_the_newest_backup_that_has_one(db_at_head, data_dir):
    old = backup_service.make_backup("nightly", db_at_head, NOW)
    new = backup_service.make_backup("manual", db_at_head, NOW + timedelta(days=1))
    assert backup_service.newest_config_db() == data_dir / "backups" / new["name"] / "config.db"
    (data_dir / "backups" / new["name"] / "config.db").unlink()
    assert backup_service.newest_config_db() == data_dir / "backups" / old["name"] / "config.db"


def test_the_zip_holds_the_three_files(db_at_head, data_dir, settings_file):
    Path(settings_file).write_text('{"version": 2}', encoding="utf-8")
    entry = backup_service.make_backup("manual", db_at_head, NOW)
    folder = _backup_folder(data_dir, entry)
    with backup_service.zip_file(entry["name"]) as spooled:
        assert spooled.tell() == 0
        with zipfile.ZipFile(spooled) as archive:
            assert sorted(archive.namelist()) == THREE_FILES
            assert archive.testzip() is None
            assert all(archive.read(name) == (folder / name).read_bytes() for name in THREE_FILES)


@pytest.mark.parametrize("name", [
    "nope", "../backups", "", "20261026T001000Z-nightly\n", "20261026T001000Z-weekly",
    "20261026T001000Z-nightly.partial", "20261027T001000Z-nightly",   # well formed, no such backup
])
def test_the_zip_of_an_unknown_name_is_none(db_at_head, data_dir, name):
    backup_service.make_backup("nightly", db_at_head, NOW)
    assert backup_service.zip_file(name) is None


# ── The upgrade backup ────────────────────────────────────────────────────────

def test_upgrade_backup_only_when_the_revision_changes(tmp_path):
    old = _upgrade(tmp_path, "old.db", "0008_code_format")
    new = _upgrade(tmp_path, "new.db")
    assert backup_service.needs_upgrade_backup(old, HEAD) is True
    assert backup_service.needs_upgrade_backup(new, HEAD) is False
    assert backup_service.needs_upgrade_backup(str(tmp_path / "missing.db"), HEAD) is False
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a database file at all" * 20)
    assert backup_service.needs_upgrade_backup(str(garbage), HEAD) is False
    assert backup_service.needs_upgrade_backup(str(tmp_path), HEAD) is False      # a folder, not a file


def test_a_new_or_empty_database_needs_no_upgrade_backup(tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert backup_service.needs_upgrade_backup(str(empty), HEAD) is False
    nothing = tmp_path / "nothing.db"
    with closing(sqlite3.connect(nothing)) as db:
        db.execute("PRAGMA user_version=1")
    assert backup_service.needs_upgrade_backup(str(nothing), HEAD) is False


def test_a_database_with_tables_and_no_revision_gets_an_upgrade_backup(tmp_path):
    path = tmp_path / "unstamped.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)")
    assert backup_service.needs_upgrade_backup(str(path), HEAD) is True


def test_the_upgrade_check_does_not_change_the_file(tmp_path):
    old = _upgrade(tmp_path, "old.db", "0008_code_format")
    before = Path(old).read_bytes()
    backup_service.needs_upgrade_backup(old, HEAD)
    assert Path(old).read_bytes() == before and not Path(old + "-journal").exists()


def test_script_head_is_the_newest_revision():
    assert backup_service.script_head() == HEAD


def test_main_backs_up_before_an_upgrade_and_not_after(tmp_path, data_dir, monkeypatch):
    old = _upgrade(tmp_path, "old.db", "0008_code_format")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{Path(old).as_posix()}")
    asyncio.run(main._backup_before_upgrade())
    [entry] = backup_service.list_backups()
    assert entry["kind"] == "upgrade"
    with closing(sqlite3.connect(data_dir / "backups" / entry["name"] / "config.db")) as copy:
        assert copy.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0008_code_format"
    # At the newest revision (and for other databases) there is nothing to back up.
    head = _upgrade(tmp_path, "head.db")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{Path(head).as_posix()}")
    asyncio.run(main._backup_before_upgrade())
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://user@host/db")
    asyncio.run(main._backup_before_upgrade())
    assert len(backup_service.list_backups()) == 1


def test_a_failed_upgrade_backup_raises_the_alarm_and_the_start_goes_on(tmp_path, data_dir, monkeypatch):
    old = _upgrade(tmp_path, "old.db", "0008_code_format")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{Path(old).as_posix()}")

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup_service, "make_backup", full)
    alarm_manager.reset()
    try:
        asyncio.run(main._backup_before_upgrade())      # does not raise
        alarm = alarm_manager.get(AlarmCode.STORAGE_BACKUP_FAILED, storage_state.STORAGE_SOURCE)
        assert alarm is not None and alarm.severity == "warning" and "upgrade" in alarm.message
    finally:
        alarm_manager.reset()


# ── The second folder ─────────────────────────────────────────────────────────

def test_mirror_copies_new_and_changed_files_and_removes_gone_backups(data_dir, tmp_path):
    _seed(data_dir, {"archive/records-2026-03-02.csv.gz": b"a", "backups/20261026T001000Z-nightly/config.db": b"b", "audit/audit-2026-10.jsonl": b"c"})
    copy = tmp_path / "usb"
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 3, "removed": 0}
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 0, "removed": 0}
    (data_dir / "audit/audit-2026-10.jsonl").write_bytes(b"cc")
    (data_dir / "backups/20261026T001000Z-nightly/config.db").unlink()       # a backup trimmed away
    (data_dir / "backups/20261026T001000Z-nightly").rmdir()
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 1, "removed": 1}
    assert (copy / "audit/audit-2026-10.jsonl").read_bytes() == b"cc"
    assert _files_under(copy) == ["archive/records-2026-03-02.csv.gz", "audit/audit-2026-10.jsonl"]


def test_archive_and_audit_copies_stay_when_the_local_file_goes(data_dir, tmp_path):
    # The floor deleted the archive file to make room on the main disk: the stick keeps the only copy left.
    files = {"archive/records-2026-10-20.csv.gz": b"a", "audit/audit-2026-10.jsonl": b"c",
             "backups/20261026T001000Z-nightly/config.db": b"b"}
    _seed(data_dir, files)
    copy = tmp_path / "usb"
    backup_service.mirror(str(copy), now=NOW)
    for relative in files:
        (data_dir / relative).unlink()
    (data_dir / "backups/20261026T001000Z-nightly").rmdir()
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 0, "removed": 1}
    assert _files_under(copy) == ["archive/records-2026-10-20.csv.gz", "audit/audit-2026-10.jsonl"]


def test_archive_and_audit_copies_go_at_their_own_retention(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_RETENTION_DAYS", 365)
    monkeypatch.setattr(settings, "RECORDS_RETENTION_DAYS", 90)
    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 365)
    files = ("archive/records-2025-09-01.csv.gz", "archive/records-2026-10-20.csv.gz",
             "audit/audit-2025-08.jsonl", "audit/audit-2026-10.jsonl")
    _seed(data_dir, {relative: b"x" for relative in files})
    copy = tmp_path / "usb"
    backup_service.mirror(str(copy), now=NOW)
    for relative in files:
        (data_dir / relative).unlink()
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 0, "removed": 2}
    assert _files_under(copy) == ["archive/records-2026-10-20.csv.gz", "audit/audit-2026-10.jsonl"]
    # Kept until the disk needs the space: the copy stays however old it is.
    monkeypatch.setattr(settings, "ARCHIVE_RETENTION_DAYS", 0)
    _seed(data_dir, {"archive/records-2024-01-01.csv.gz": b"x"})
    backup_service.mirror(str(copy), now=NOW)
    (data_dir / "archive/records-2024-01-01.csv.gz").unlink()
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 0, "removed": 0}


def test_an_old_copy_is_not_deleted_while_the_local_file_is_there(data_dir, tmp_path):
    _seed(data_dir, {"archive/records-2024-01-01.csv.gz": b"x"})
    copy = tmp_path / "usb"
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 1, "removed": 0}
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 0, "removed": 0}


def test_mirror_never_touches_other_files_in_the_copy_folder(data_dir, tmp_path):
    copy = tmp_path / "usb"
    (copy / "photos").mkdir(parents=True)
    (copy / "photos/a.jpg").write_bytes(b"x")
    (copy / "notes.txt").write_text("x")
    backup_service.mirror(str(copy))
    assert (copy / "photos/a.jpg").exists() and (copy / "notes.txt").exists()
    # Not when files are copied either: a file the mirror never copied stays, even inside archive/.
    _seed(data_dir, {"archive/records-2026-03-02.csv.gz": b"a"})
    (copy / "archive").mkdir()
    (copy / "archive/records-2026-03-01.csv.gz").write_bytes(b"the owner's")
    assert backup_service.mirror(str(copy)) == {"copied": 1, "removed": 0}
    assert backup_service.mirror(str(copy)) == {"copied": 0, "removed": 0}
    assert _files_under(copy) == ["archive/records-2026-03-01.csv.gz", "archive/records-2026-03-02.csv.gz",
                                  "notes.txt", "photos/a.jpg"]


def test_mirror_does_not_copy_what_the_server_is_still_writing(data_dir, tmp_path):
    _seed(data_dir, {
        "archive/records-2026-03-02.csv.gz.partial": b"half",
        "backups/20261026T001000Z-nightly.partial/config.db": b"half",
        "audit/audit-2026-10.jsonl": b"c",
    })
    copy = tmp_path / "usb"
    assert backup_service.mirror(str(copy)) == {"copied": 1, "removed": 0}
    assert _files_under(copy) == ["audit/audit-2026-10.jsonl"]


def test_mirror_removes_the_folders_it_empties_and_a_copy_a_crash_left(data_dir, tmp_path):
    copy = tmp_path / "usb"
    old = ("backups/20260101T000000Z-nightly/config.db", "backups/20260101T000000Z-nightly/manifest.json")
    _seed(data_dir, {relative: b"old" for relative in old})
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 2, "removed": 0}
    (copy / "backups/20260101T000000Z-nightly/config.db.partial").write_bytes(b"half")     # a power cut during a copy
    for relative in old:
        (data_dir / relative).unlink()
    (data_dir / "backups/20260101T000000Z-nightly").rmdir()
    _seed(data_dir, {"backups/20261026T001000Z-nightly/config.db": b"b"})
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 1, "removed": 2}
    assert _files_under(copy) == ["backups/20261026T001000Z-nightly/config.db"]
    assert not (copy / "backups/20260101T000000Z-nightly").exists()
    assert (copy / "backups").is_dir()


def test_mirror_keeps_a_folder_that_still_exists_locally(data_dir, tmp_path):
    copy = tmp_path / "usb"
    (data_dir / "backups/20261026T001000Z-nightly").mkdir(parents=True)
    (copy / "backups/20261026T001000Z-nightly").mkdir(parents=True)
    backup_service.mirror(str(copy))
    assert (copy / "backups/20261026T001000Z-nightly").is_dir()


def test_mirror_copies_a_file_whose_time_changed_but_not_a_rounded_time(data_dir, tmp_path):
    _seed(data_dir, {"audit/audit-2026-10.jsonl": b"c"})
    local = data_dir / "audit/audit-2026-10.jsonl"
    copy = tmp_path / "usb"
    backup_service.mirror(str(copy))
    stat = local.stat()
    # A stick formatted FAT keeps even seconds: the same file must not be copied again and again.
    os.utime(copy / "audit/audit-2026-10.jsonl", (stat.st_atime, stat.st_mtime + 1.5))
    assert backup_service.mirror(str(copy)) == {"copied": 0, "removed": 0}
    os.utime(local, (stat.st_atime, stat.st_mtime + 60))
    assert backup_service.mirror(str(copy)) == {"copied": 1, "removed": 0}
    assert backup_service.mirror(str(copy)) == {"copied": 0, "removed": 0}


def test_mirror_leaves_the_copy_alone_when_a_local_folder_is_missing(data_dir, tmp_path):
    # A data folder with no backups/ is a fault to look at, not the owner deleting every backup.
    copy = tmp_path / "usb"
    _seed(data_dir, {"backups/20261026T001000Z-nightly/config.db": b"b", "audit/audit-2026-10.jsonl": b"c"})
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 2, "removed": 0}
    (data_dir / "backups/20261026T001000Z-nightly/config.db").unlink()
    (data_dir / "backups/20261026T001000Z-nightly").rmdir()
    (data_dir / "backups").rmdir()
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 0, "removed": 0}
    assert (copy / "backups/20261026T001000Z-nightly/config.db").exists()
    # Once the local folder is back and the backup is not, the copy the mirror made goes.
    (data_dir / "backups").mkdir()
    assert backup_service.mirror(str(copy), now=NOW) == {"copied": 0, "removed": 1}


def test_a_fresh_data_folder_never_deletes_the_copies_an_earlier_one_made(data_dir, tmp_path):
    # A new disk or a reinstall with the same BACKUP_COPY_DIR: the stick may hold the only copies left.
    copy = tmp_path / "usb"
    _seed(data_dir, {"archive/records-2026-03-02.csv.gz": b"a", "backups/20261026T001000Z-nightly/config.db": b"b",
                     "audit/audit-2026-10.jsonl": b"c"})
    backup_service.mirror(str(copy))
    # The data folder is lost, the list of copied files with it (the copy folder sits next to it here).
    (data_dir / "storage_state.json").unlink()
    for name in backup_service.SUBFOLDERS:
        for path in sorted((data_dir / name).rglob("*"), key=lambda p: len(p.parts), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
    _seed(data_dir, {"audit/audit-2026-11.jsonl": b"new"})
    assert backup_service.mirror(str(copy)) == {"copied": 1, "removed": 0}
    assert _files_under(copy) == ["archive/records-2026-03-02.csv.gz", "audit/audit-2026-10.jsonl",
                                  "audit/audit-2026-11.jsonl", "backups/20261026T001000Z-nightly/config.db"]


def test_the_list_of_copied_files_is_kept_per_second_folder(data_dir, tmp_path):
    _seed(data_dir, {"backups/20261026T001000Z-nightly/config.db": b"b"})
    first, second = tmp_path / "usb", tmp_path / "share"
    backup_service.mirror(str(first), now=NOW)
    # The owner moves to another folder that already holds a backup (copied by hand).
    (second / "backups/20260101T000000Z-manual").mkdir(parents=True)
    (second / "backups/20260101T000000Z-manual/config.db").write_bytes(b"by hand")
    (data_dir / "backups/20261026T001000Z-nightly/config.db").unlink()
    (data_dir / "backups/20261026T001000Z-nightly").rmdir()
    assert backup_service.mirror(str(second), now=NOW) == {"copied": 0, "removed": 0}
    assert (second / "backups/20260101T000000Z-manual/config.db").exists()
    assert backup_service.mirror(str(first), now=NOW) == {"copied": 0, "removed": 1}


def test_a_blank_second_folder_is_an_os_error(data_dir):
    for blank in ("", "   "):
        with pytest.raises(OSError):
            backup_service.mirror(blank)


def test_mirror_to_a_folder_that_cannot_be_written_raises(data_dir, tmp_path):
    _seed(data_dir, {"archive/records-2026-03-02.csv.gz": b"a"})
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(OSError):
        backup_service.mirror(str(blocker))
    with pytest.raises(OSError):
        backup_service.mirror(str(tmp_path / "no stick" / "folder"))     # its parent is not there: not mounted


def test_mirror_refuses_a_folder_that_would_hold_a_copy_inside_itself(data_dir, tmp_path):
    _seed(data_dir, {"backups/20261026T001000Z-nightly/config.db": b"b", "archive/records-2026-03-02.csv.gz": b"a"})
    for bad in (data_dir, data_dir / "backups", data_dir / "backups" / "again", data_dir / "archive" / "usb"):
        with pytest.raises(OSError):
            backup_service.mirror(str(bad))
    assert _files_under(data_dir) == ["archive/records-2026-03-02.csv.gz", "backups/20261026T001000Z-nightly/config.db"]
    # A folder next to them, or one that holds the data folder, is fine.
    assert backup_service.mirror(str(data_dir / "usb")) == {"copied": 2, "removed": 0}
    assert backup_service.mirror(str(data_dir.parent / (data_dir.name + "-copy"))) == {"copied": 2, "removed": 0}


def test_a_copy_that_fails_leaves_the_old_copy_and_no_partial_file(data_dir, tmp_path, monkeypatch):
    _seed(data_dir, {"audit/audit-2026-10.jsonl": b"old", "backups/20261026T001000Z-nightly/config.db": b"b"})
    copy = tmp_path / "usb"
    backup_service.mirror(str(copy))
    (data_dir / "audit/audit-2026-10.jsonl").write_bytes(b"new and longer")
    (data_dir / "backups/20261026T001000Z-nightly/config.db").unlink()
    (data_dir / "backups/20261026T001000Z-nightly").rmdir()
    real = backup_service.shutil.copy2

    def half(src, dst, **kwargs):
        Path(dst).write_bytes(b"half")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup_service.shutil, "copy2", half)
    with pytest.raises(OSError):
        backup_service.mirror(str(copy))
    monkeypatch.setattr(backup_service.shutil, "copy2", real)
    assert (copy / "audit/audit-2026-10.jsonl").read_bytes() == b"old"
    # Nothing was removed after the failure: the copy is not made smaller until it is complete.
    assert _files_under(copy) == ["audit/audit-2026-10.jsonl", "backups/20261026T001000Z-nightly/config.db"]
    assert backup_service.mirror(str(copy)) == {"copied": 1, "removed": 1}


def test_a_file_that_vanishes_while_it_is_copied_is_not_an_error(data_dir, tmp_path, monkeypatch):
    _seed(data_dir, {"archive/records-2026-03-02.csv.gz": b"a", "archive/records-2026-03-03.csv.gz": b"b"})
    copy = tmp_path / "usb"
    real = backup_service.shutil.copy2

    def vanishing(src, dst, **kwargs):
        if str(src).endswith("03-02.csv.gz"):
            Path(src).unlink()          # the keeper deleted it between the listing and the copy
        return real(src, dst, **kwargs)

    monkeypatch.setattr(backup_service.shutil, "copy2", vanishing)
    assert backup_service.mirror(str(copy)) == {"copied": 1, "removed": 0}
    assert _files_under(copy) == ["archive/records-2026-03-03.csv.gz"]


@pytest.fixture(params=["symlink", "junction"])
def link_dir(request):
    """A function that makes a link to a folder: a symbolic link, or on Windows a junction. Skips where it cannot."""
    def make(link, target):
        if request.param == "symlink":
            try:
                os.symlink(target, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                pytest.skip("this machine cannot make a symbolic link")
        else:
            try:
                import _winapi
                _winapi.CreateJunction(str(target), str(link))
            except (ImportError, AttributeError):
                pytest.skip("junctions are a Windows thing")
    return make


def test_mirror_never_follows_a_link_out_of_the_copy_folder(data_dir, tmp_path, link_dir):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("the owner's file")
    (outside / "sub").mkdir()
    (outside / "sub" / "deep.txt").write_text("the owner's file")
    copy = tmp_path / "usb"
    (copy / "archive").mkdir(parents=True)
    link_dir(copy / "archive" / "link", outside)
    (copy / "archive" / "records-2026-03-01.csv.gz").write_bytes(b"not ours")
    _seed(data_dir, {"archive/records-2026-03-02.csv.gz": b"a"})
    assert backup_service.mirror(str(copy)) == {"copied": 1, "removed": 0}
    assert (copy / "archive" / "records-2026-03-01.csv.gz").read_bytes() == b"not ours"
    assert (outside / "keep.txt").read_text() == "the owner's file"
    assert (outside / "sub" / "deep.txt").exists()
    assert (copy / "archive" / "link").exists()


def test_mirror_does_not_delete_through_a_linked_subfolder(data_dir, tmp_path, link_dir):
    outside = tmp_path / "nas"
    outside.mkdir()
    (outside / "records-2026-03-01.csv.gz").write_bytes(b"on the share")
    copy = tmp_path / "usb"
    copy.mkdir()
    link_dir(copy / "archive", outside)
    _seed(data_dir, {"archive/records-2026-03-02.csv.gz": b"a"})
    backup_service.mirror(str(copy))
    # The new file goes where the owner pointed the folder; what is there already is left alone.
    assert (outside / "records-2026-03-02.csv.gz").read_bytes() == b"a"
    assert (outside / "records-2026-03-01.csv.gz").read_bytes() == b"on the share"


def test_a_listed_file_is_never_deleted_through_a_link(data_dir, tmp_path, link_dir):
    copy = tmp_path / "usb"
    name = "backups/20261026T001000Z-nightly/config.db"
    _seed(data_dir, {name: b"b"})
    backup_service.mirror(str(copy))
    # The owner swaps the copy's backups/ for a link to a share holding a file of the same name.
    share = tmp_path / "nas"
    (share / "20261026T001000Z-nightly").mkdir(parents=True)
    (share / "20261026T001000Z-nightly" / "config.db").write_bytes(b"on the share")
    (copy / name).unlink()
    (copy / "backups" / "20261026T001000Z-nightly").rmdir()
    (copy / "backups").rmdir()
    link_dir(copy / "backups", share)
    (data_dir / name).unlink()
    (data_dir / "backups" / "20261026T001000Z-nightly").rmdir()
    assert backup_service.mirror(str(copy)) == {"copied": 0, "removed": 0}
    assert (share / "20261026T001000Z-nightly" / "config.db").read_bytes() == b"on the share"


def test_a_trigger_and_a_view_come_back_too(db_at_head, data_dir):
    with closing(sqlite3.connect(db_at_head)) as con:
        con.execute("CREATE VIEW rejects AS SELECT id FROM product_records WHERE result = 'reject'")
        con.execute("CREATE TRIGGER no_empty_line BEFORE INSERT ON kept_totals WHEN NEW.line_id = '' "
                    "BEGIN SELECT RAISE(ABORT, 'no line'); END")
        con.commit()
    entry = backup_service.make_backup("manual", db_at_head, NOW)
    with closing(sqlite3.connect(data_dir / "backups" / entry["name"] / "config.db")) as copy:
        kinds = dict(copy.execute("SELECT name, type FROM sqlite_master WHERE type IN ('view', 'trigger')").fetchall())
    assert kinds == {"rejects": "view", "no_empty_line": "trigger"}
