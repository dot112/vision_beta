# Data safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The server's data stays safe on an unattended edge device: the disk never fills, a damaged settings file or database is replaced from good copies, records deleted from the database can be fetched back, totals stay exact, and a wrong clock cannot delete good data.

**Architecture:** One background task (`DataKeeper`) runs every minute, hour and night and calls small single-purpose units: a free-space guard, a daily records archive (gzip CSV per UTC day), small config-only backups with an optional mirror folder, an audit log in monthly JSONL files, and a clock guard. Settings saves keep a history folder; the database gets a start-up check and a rebuild path. One Alembic revision adds `kept_totals` (exact totals after old records are deleted) and an index on `detections.created_at`.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy async + aiosqlite, Alembic, stdlib `sqlite3`/`gzip`/`csv`/`zipfile`, psutil; plain JS assets; pytest and node tests.

**Spec:** `docs/superpowers/specs/2026-10-26-data-safety-design.md`

## Global Constraints

- No new dependencies.
- Repo files are CRLF (most `.py`, `.js`, `.html`, docs); keep each file's line endings (edit with scripts that use `newline=""`, or check `file <path>` after an edit).
- Every path under `data/` is built from `app.services.settings_persistence_service.DATA_DIR` **at call time** (tests replace it); the database path from `settings.DATABASE_URL` through `settings_persistence_service._sqlite_path`.
- All times UTC. Folder and file stamps: `%Y%m%dT%H%M%SZ` (settings history adds microseconds: `%Y%m%dT%H%M%S%fZ`).
- Alarm source for every new alarm: `"storage"`. Alarm codes, severities and the copy of their catalogue entries as in Task 1.
- File and `sqlite3` work runs in `asyncio.to_thread`; database work through the async engine uses short transactions and chunks of 5000 rows.
- Files are written to `<name>.partial` (or `.tmp` for the settings file), flushed, fsynced and renamed; on POSIX the folder is fsynced after the rename.
- Must work on Linux and Windows (the suite runs on Windows locally).
- Help icons only on the floor and the clock pause.
- Run tests from the worktree with the main checkout's venv: `../../../.venv/Scripts/python.exe -m pytest <tests> -q -p no:cacheprovider` (written `PYTEST <tests>` below). The full suite: the same with the 8 deselects of `backup/scratch-tools/pt.sh`.

## Review Focus

1. **First archive run on a full 90-day database** (millions of rows): must stream in 5000-row chunks without stalling the event loop or the records writer. Test: `test_a_big_day_is_read_in_chunks` (Task 6).
2. **Disk full or an error while an archive or backup is written:** no `.partial` file or folder may be left, listed, mirrored or counted as archived. Tests: `test_a_failed_write_leaves_no_partial_file` (Task 6), `test_a_failed_backup_leaves_no_partial_folder` (Task 7).
3. **The second folder is an unplugged USB stick, a file, or holds the owner's other files:** an alarm, no exception, and nothing outside `backups/`, `archive/`, `audit/` there is ever removed. Tests: `test_mirror_never_touches_other_files_in_the_copy_folder`, `test_mirror_to_a_folder_that_cannot_be_written_raises` (Task 7), `test_copy_failed_raises_and_clears` (Task 9).
4. **Windows file locks:** a damaged database cannot be moved while the engine holds it open; the engine must be disposed first. Test: `test_open_database_disposes_before_moving` (Task 10).
5. **A device with no clock battery** gets its clock corrected forward at every boot: that must not pause deleting each time. Test: `test_a_jump_that_lands_near_the_newest_record_does_not_pause` (Task 9).

---

### Task 1: Foundations: settings, alarms, storage state, WAL limit

**Files:**
- Modify: `app/config.py` (settings + validators), `app/events/alarm_events.py` (codes + catalogue), `app/db/session.py` (`_sqlite_pragmas`)
- Create: `app/services/storage_state.py`
- Test: `tests/test_storage_state.py`

**Interfaces:**
- Produces, `app/config.py`: `STORAGE_MIN_FREE_MB: int = 2048` (≥ 0), `STORAGE_WARN_PERCENT: float = 85` (1–100), `DETECTIONS_RETENTION_DAYS: int = 30` (≥ 0), `ARCHIVE_RECORDS: bool = True`, `ARCHIVE_RETENTION_DAYS: int = 365` (≥ 0), `BACKUP_KEEP_NIGHTLY: int = 7` (≥ 1), `BACKUP_COPY_DIR: str = ""`, `AUDIT_RETENTION_DAYS: int = 365` (≥ 1). Placed after `RECORDS_QUEUE_LIMIT` with a comment block in the file's style.
- Produces, `AlarmCode`: `STORAGE_LOW = "storage.low"`, `STORAGE_FULL = "storage.full"`, `STORAGE_RECORDS_LOST = "storage.records_lost"`, `STORAGE_CLOCK_CHECK = "storage.clock_check"`, `STORAGE_SETTINGS_RESTORED = "storage.settings_restored"`, `STORAGE_DATABASE_RESTORED = "storage.database_restored"`, `STORAGE_ARCHIVE_FAILED = "storage.archive_failed"`, `STORAGE_BACKUP_FAILED = "storage.backup_failed"`, `STORAGE_COPY_FAILED = "storage.copy_failed"`.
- Produces, `app/services/storage_state.py`:
  - `STORAGE_SOURCE = "storage"`
  - `data_dir() -> Path`
  - `stamp(now: datetime, micro: bool = False) -> str`
  - `fsync_dir(path: Path) -> None` (no-op on Windows)
  - `write_atomic(path: Path, data: bytes) -> None`
  - `load() -> Dict[str, Any]`: `{}` when missing or damaged; damage is logged
  - `update(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]`: under one `threading.Lock`
  - `add_notice(code: str, message: str, details: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> None`: replaces an open notice with the same code
  - `close_notice(code: str) -> bool`
  - `notices() -> List[Dict[str, Any]]`: each `{"code", "at", "message", "details"}`
- Keys later tasks use inside the state: `"clock"` (Task 10), `"last"` (`{job: iso}`), `"deleted_through"` (iso, Task 5), `"rebuild"` (Task 11), `"notices"`.

- [ ] **Step 1: Write the failing tests**

```python
CATALOG = {"storage.low": "warning", "storage.full": "critical", "storage.records_lost": "critical",
           "storage.clock_check": "warning", "storage.settings_restored": "critical",
           "storage.database_restored": "critical", "storage.archive_failed": "warning",
           "storage.backup_failed": "warning", "storage.copy_failed": "warning"}

def test_settings_defaults_and_limits():
    s = Settings()
    assert (s.STORAGE_MIN_FREE_MB, s.STORAGE_WARN_PERCENT, s.DETECTIONS_RETENTION_DAYS, s.ARCHIVE_RECORDS,
            s.ARCHIVE_RETENTION_DAYS, s.BACKUP_KEEP_NIGHTLY, s.BACKUP_COPY_DIR, s.AUDIT_RETENTION_DAYS) == \
           (2048, 85, 30, True, 365, 7, "", 365)
    for name, bad in (("STORAGE_MIN_FREE_MB", -1), ("STORAGE_WARN_PERCENT", 0), ("STORAGE_WARN_PERCENT", 101),
                      ("DETECTIONS_RETENTION_DAYS", -1), ("ARCHIVE_RETENTION_DAYS", -1),
                      ("BACKUP_KEEP_NIGHTLY", 0), ("AUDIT_RETENTION_DAYS", 0)):
        with pytest.raises(ValidationError):
            Settings(**{name: bad})

def test_the_storage_alarms_are_in_the_catalogue():
    entries = {e["code"]: e for e in ALARM_CATALOG if e["code"].startswith("storage.")}
    assert {c: e["severity"] for c, e in entries.items()} == CATALOG
    assert all(e["group"] == "Storage" and e["scope"] == "server" and e["label"] and e["description"]
               for e in entries.values())

def test_state_round_trip_and_atomic_write(data_dir):
    storage_state.update(lambda s: s.setdefault("last", {}).update(archive="2026-10-26T00:10:00+00:00"))
    assert storage_state.load()["last"]["archive"] == "2026-10-26T00:10:00+00:00"
    assert sorted(p.name for p in data_dir.iterdir()) == ["storage_state.json"]

def test_a_damaged_state_file_starts_empty(data_dir, caplog):
    (data_dir / "storage_state.json").write_text("{cut", encoding="utf-8")
    assert storage_state.load() == {}
    assert "storage_state.json" in caplog.text

def test_notices_replace_by_code_and_close(data_dir):
    storage_state.add_notice("storage.full", "first")
    storage_state.add_notice("storage.full", "second", {"n": 2})
    assert [(n["code"], n["message"], n["details"]) for n in storage_state.notices()] == [("storage.full", "second", {"n": 2})]
    assert storage_state.close_notice("storage.full") is True and storage_state.notices() == []
    assert storage_state.close_notice("storage.full") is False

def test_stamps():
    t = datetime(2026, 10, 26, 0, 10, 0, 123456, tzinfo=timezone.utc)
    assert storage_state.stamp(t) == "20261026T001000Z"
    assert storage_state.stamp(t, micro=True) == "20261026T001000123456Z"

def test_the_wal_file_is_limited_to_64_mb(tmp_path):
    con = sqlite3.connect(tmp_path / "x.db")
    _sqlite_pragmas(con, None)
    assert con.execute("PRAGMA journal_size_limit").fetchone()[0] == 64 * 1024 * 1024
```

`data_dir` is a fixture in the test file: `monkeypatch.setattr(settings_persistence_service, "DATA_DIR", str(tmp_path))`, yields `tmp_path`.

- [ ] **Step 2: Run them to see them fail**

Run: `PYTEST tests/test_storage_state.py`
Expected: FAIL (no `storage_state` module, no settings).

- [ ] **Step 3: Implement**

Settings and validators in `app/config.py` (messages in the style of the existing ones, e.g. `"STORAGE_WARN_PERCENT must be between 1 and 100"`). Alarm constants and nine catalogue entries, `"group": "Storage"`, `"scope": "server"`, with these labels and descriptions:

| Code | Label | Description |
|---|---|---|
| storage.low | Disk nearly full | The disk that holds the server's data is nearly full. |
| storage.full | Disk below its free-space floor | Less free space than the floor is left; the oldest data is being deleted to make room. |
| storage.records_lost | Records deleted for space | Records that were in no archive file were deleted early to keep the disk from filling. |
| storage.clock_check | Clock check: deleting paused | The clock looks wrong, so old data is not deleted by age until an Admin confirms the clock on Data and backups. |
| storage.settings_restored | Settings restored from a copy | The settings file could not be read; a saved copy (or the defaults) was loaded and the damaged file kept. |
| storage.database_restored | Database rebuilt | The database was damaged; it was set aside and rebuilt from the newest backup and the archive. |
| storage.archive_failed | Records archive failed | Records could not be written to their archive file; old records are kept until it works. |
| storage.backup_failed | Backup failed | A backup could not be made; it is tried again every hour. |
| storage.copy_failed | Second backup folder not written | Backups and archive files could not be copied to the second folder (BACKUP_COPY_DIR). |

`_sqlite_pragmas` adds `PRAGMA journal_size_limit = 67108864`. `storage_state` as in the Interfaces block; `write_atomic` writes `<path>.partial`, fsyncs, `os.replace`, `fsync_dir`.

- [ ] **Step 4: Run them to see them pass**

Run: `PYTEST tests/test_storage_state.py tests/test_plc_dispatcher.py tests/test_handshake.py`
Expected: PASS (the catalogue tests of the other two files still pass).

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/events/alarm_events.py app/db/session.py app/services/storage_state.py tests/test_storage_state.py
git commit -m "Data safety: settings, storage alarms, storage state file, WAL size limit"
```

---

### Task 2: Settings file history and safe loading

**Files:**
- Modify: `app/services/settings_persistence_service.py` (`save`, `load`, new helpers)
- Test: `tests/test_settings_safety.py`

**Interfaces:**
- Consumes: `storage_state.stamp`, `storage_state.fsync_dir`, `storage_state.add_notice`, `STORAGE_SOURCE`, `AlarmCode.STORAGE_SETTINGS_RESTORED`.
- Produces, on `SettingsPersistenceService`:
  - `HISTORY_KEEP = 20`, `HISTORY_DAYS = 30` (class constants)
  - `_history_dir() -> str` (`<DATA_DIR>/settings-history`)
  - `_copy_to_history(now: Optional[datetime] = None) -> Optional[str]`: copies the current file if it parses as JSON; returns the copy's path
  - `_trim_history(now: datetime) -> None`: keeps the 20 newest plus the newest of each UTC day for the last 30 days
  - `_load_file(path: str) -> Dict[str, Any]`: parse + merge with `DEFAULT_STATE` + `upgrade_state`; raises on any failure
- `load()` keeps its signature and return value.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_save_copies_the_previous_file_into_the_history(settings_file):
    SPS.load(); SPS._state["lines"][0]["name"] = "First"; SPS.save()
    SPS._state["lines"][0]["name"] = "Second"; SPS.save()
    copies = sorted(Path(SPS._history_dir()).iterdir())
    assert copies and json.loads(copies[-1].read_text())["lines"][0]["name"] == "First"

def test_history_keeps_20_newest_and_one_per_day_for_30_days(settings_file):
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    hist = Path(SPS._history_dir()); hist.mkdir(parents=True)
    for day in range(40):                 # 3 copies a day, 40 days back
        for hour in (1, 2, 3):
            t = now - timedelta(days=day, hours=hour)
            (hist / f"system_state-{stamp(t, micro=True)}.json").write_text("{}")
    SPS._trim_history(now)
    names = sorted(p.name for p in hist.iterdir())
    assert len(names) == 20 + (30 - 7)    # the 20 newest cover about 7 days; one each for the other days inside 30
    assert min(names) > f"system_state-{stamp(now - timedelta(days=30), micro=True)}"

def test_a_damaged_file_loads_the_newest_good_copy(settings_file):
    SPS.load(); SPS._state["lines"][0]["name"] = "Packing"; SPS.save(); SPS.save()
    Path(settings_file).write_text('{"lines": [', encoding="utf-8")
    SPS._state = {}
    state = SPS.load()
    assert state["lines"][0]["name"] == "Packing"
    assert json.loads(Path(settings_file).read_text())["lines"][0]["name"] == "Packing"
    broken = list(Path(settings_file).parent.glob("system_state.broken-*.json"))
    assert len(broken) == 1 and broken[0].read_text() == '{"lines": ['
    assert alarm_manager.get("storage.settings_restored", "storage") is not None
    assert [n["code"] for n in storage_state.notices()] == ["storage.settings_restored"]

def test_a_file_that_fails_its_upgrade_counts_as_damaged(settings_file, monkeypatch):
    SPS.load(); SPS.save(); SPS.save()
    data = json.loads(Path(settings_file).read_text()); data["boom"] = True
    Path(settings_file).write_text(json.dumps(data))
    real = persistence_module.upgrade_state
    monkeypatch.setattr(persistence_module, "upgrade_state", lambda s: (_ for _ in ()).throw(ValueError("bad")) if s.get("boom") else real(s))
    SPS._state = {}
    assert "boom" not in SPS.load()

def test_no_good_copy_starts_with_defaults_and_keeps_everything(settings_file):
    Path(settings_file).write_text("not json", encoding="utf-8")
    SPS._state = {}
    state = SPS.load()
    assert state["lines"][0]["name"] == "Line 1"
    assert [p.read_text() for p in Path(settings_file).parent.glob("system_state.broken-*.json")] == ["not json"]
    assert "no good copy" in storage_state.notices()[0]["message"]

def test_a_leftover_tmp_file_is_removed(settings_file):
    Path(settings_file + ".tmp").write_text("half")
    SPS._state = {}; SPS.load()
    assert not Path(settings_file + ".tmp").exists()
```

`settings_file` fixture: points `DATA_DIR`/`STATE_FILE` at `tmp_path`, empties `SPS._state`, resets `alarm_manager`, yields the file path, and restores the state the session's other tests use afterwards (`SPS._state = saved; SPS.save()` into the original paths, as other tests that replace the file do).

- [ ] **Step 2: Run them to see them fail**

Run: `PYTEST tests/test_settings_safety.py`
Expected: FAIL (`_history_dir` missing; the damaged file is replaced by defaults).

- [ ] **Step 3: Implement**

`save()`: `_copy_to_history()` and `_trim_history(now)` before writing the `.tmp`; `fsync_dir` after the rename. `load()`: delete a leftover `.tmp`; try `_load_file(STATE_FILE)`; on failure rename it `system_state.broken-<stamp>.json`, try history copies newest first, save the first that loads; raise the alarm and add the notice (message names the copy and the broken file, or says `"no good copy was found; the defaults were loaded"`). Never call `save()` on the defaults while a broken file was found unless it has been renamed first.

- [ ] **Step 4: Run them to see them pass**

Run: `PYTEST tests/test_settings_safety.py tests/test_lines.py tests/test_lines_api.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/settings_persistence_service.py tests/test_settings_safety.py
git commit -m "Settings: keep a history of saved copies; a damaged file loads the newest good copy"
```

---

### Task 3: Audit log in monthly files

**Files:**
- Create: `app/services/audit_log.py`
- Modify: `app/services/settings_persistence_service.py` (`record_audit`, `get_audit_logs`, `load`, `DEFAULT_STATE`; remove `_prune_expired_audit_logs`), `app/routes/v1/system.py` (`before`), `assets/production_lines.js` (`window.loadAuditLogs`)
- Test: `tests/test_audit_log.py`

**Interfaces:**
- Consumes: `storage_state.data_dir()`, `settings.AUDIT_RETENTION_DAYS`, `production_records_service.parse_time`.
- Produces, `app/services/audit_log.py`:
  - `append(entry: Dict[str, Any]) -> None`: to `audit/audit-YYYY-MM.jsonl` by `entry["timestamp"]`'s UTC month; flush + fsync; module lock
  - `read(limit: int = 100, before: Optional[datetime] = None, line_id: Optional[str] = None) -> List[Dict[str, Any]]`: newest first
  - `prune(days: int, now: Optional[datetime] = None) -> int`: files deleted
  - `move_from_state(entries: List[Dict[str, Any]]) -> int`: appends entries not already in the files (by `id`), oldest first
- `SettingsPersistenceService.get_audit_logs(limit=100, line_id=None, before=None)`.
- `GET /api/v1/system/audit-logs` gains `before: Optional[str]` (ISO with zone; 422 when given and not one).

- [ ] **Step 1: Write the failing tests**

```python
def _entry(i, when, line_id=None):
    return {"id": f"e{i}", "timestamp": when.isoformat(), "username": "admin", "role": "ADMIN",
            "clearance_level": 3, "action": "X", "category": "System", "details": str(i), **({"line_id": line_id} if line_id else {})}

def test_entries_are_appended_and_read_newest_first(data_dir):
    for i in range(3): audit_log.append(_entry(i, T0 + timedelta(minutes=i)))
    assert [e["id"] for e in audit_log.read()] == ["e2", "e1", "e0"]

def test_entries_go_into_the_file_of_their_month(data_dir):
    audit_log.append(_entry(1, datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)))
    audit_log.append(_entry(2, datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc)))
    assert sorted(p.name for p in (data_dir / "audit").iterdir()) == ["audit-2026-09.jsonl", "audit-2026-10.jsonl"]

def test_before_and_line_id(data_dir):
    for i in range(6): audit_log.append(_entry(i, T0 + timedelta(minutes=i), "line-a" if i % 2 else None))
    assert [e["id"] for e in audit_log.read(limit=2, before=T0 + timedelta(minutes=4))] == ["e3", "e2"]
    assert [e["id"] for e in audit_log.read(line_id="line-a")] == ["e5", "e3", "e1"]

def test_a_cut_line_is_skipped(data_dir):
    audit_log.append(_entry(1, T0))
    with open(data_dir / "audit" / "audit-2026-03.jsonl", "a", encoding="utf-8") as f: f.write('{"id": "e2", "time')
    audit_log.append(_entry(3, T0 + timedelta(minutes=1)))
    assert [e["id"] for e in audit_log.read()] == ["e3", "e1"]

def test_prune_deletes_months_that_ended_past_the_retention(data_dir):
    audit_log.append(_entry(1, datetime(2025, 9, 15, tzinfo=timezone.utc)))
    audit_log.append(_entry(2, datetime(2025, 11, 15, tzinfo=timezone.utc)))
    assert audit_log.prune(365, now=datetime(2026, 10, 26, tzinfo=timezone.utc)) == 1
    assert [e["id"] for e in audit_log.read()] == ["e2"]

def test_two_threads_appending_lose_nothing(data_dir):
    threads = [threading.Thread(target=lambda k=k: [audit_log.append(_entry(f"{k}-{i}", T0)) for i in range(200)]) for k in (1, 2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(audit_log.read(limit=1000)) == 400

def test_old_entries_move_out_of_the_settings_file_once(settings_file):
    state = json.loads(Path(settings_file).read_text())
    state["audit_logs"] = [_entry(2, T0 + timedelta(minutes=2)), _entry(1, T0)]   # newest first, as stored today
    Path(settings_file).write_text(json.dumps(state))
    SPS._state = {}; SPS.load(); SPS._state = {}; SPS.load()
    assert [e["id"] for e in audit_log.read()] == ["e2", "e1"]
    assert "audit_logs" not in json.loads(Path(settings_file).read_text())

def test_record_audit_writes_a_file_entry_and_the_api_pages(client, admin_headers):
    SPS.record_audit("admin", "admin", 3, "PAGE_TEST", "System", "first")
    newest = client.get("/api/v1/system/audit-logs?limit=1", headers=admin_headers).json()[0]
    assert newest["action"] == "PAGE_TEST"
    older = client.get("/api/v1/system/audit-logs", params={"limit": 5, "before": newest["timestamp"]}, headers=admin_headers)
    assert older.status_code == 200 and all(e["timestamp"] < newest["timestamp"] for e in older.json())
    assert client.get("/api/v1/system/audit-logs?before=yesterday", headers=admin_headers).status_code == 422

def test_the_audit_page_has_load_older():
    js = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    assert 'id="plAuditMore"' in js and "Load older" in js and "&before=" in js
```

`T0 = datetime(2026, 3, 2, 8, tzinfo=timezone.utc)`; `data_dir`/`settings_file` fixtures as in Tasks 1–2.

- [ ] **Step 2: Run them to see them fail**

Run: `PYTEST tests/test_audit_log.py`
Expected: FAIL (no `audit_log` module).

- [ ] **Step 3: Implement**

`audit_log` as in Interfaces (`read` walks the month files newest first, reading each file's lines in reverse and skipping lines that do not parse). In the settings service: `DEFAULT_STATE` loses `"audit_logs"`; the first-start `SYSTEM_INIT` entry goes through `audit_log.append`; `record_audit` appends instead of inserting into the state (the in-memory `_recent_changes` ring stays); `load()` moves `state.pop("audit_logs")` with `move_from_state` and saves; `_prune_expired_audit_logs` and its calls go. The route passes `before`. In `production_lines.js`, `window.loadAuditLogs(more)` keeps the oldest timestamp shown; a `Load older` button (`id="plAuditMore"`) under the table asks for `limit=100&before=<oldest>` and appends rows; it hides when fewer than 100 came back; a line filter change starts again from the newest.

- [ ] **Step 4: Run them to see them pass**

Run: `PYTEST tests/test_audit_log.py tests/test_config_lock_service.py tests/test_batch.py tests/test_carton_batch.py tests/test_plc_inputs.py`
Expected: PASS (those files read audit entries through the service).

- [ ] **Step 5: Commit**

```bash
git add app/services/audit_log.py app/services/settings_persistence_service.py app/routes/v1/system.py assets/production_lines.js tests/test_audit_log.py
git commit -m "Audit log: monthly files kept a year, moved out of the settings file; Load older"
```

---

### Task 4: Revision 0009 and exact totals after deleting

**Files:**
- Create: `app/db/models/kept_total.py`, `alembic/versions/0009_kept_totals.py`
- Modify: `alembic/versions/0001_initial_schema.py` (`_LATER_TABLES`), `alembic/env.py` (import), `app/db/models/detection.py` (index), `app/services/production_records_service.py`
- Test: `tests/test_production_records.py` (its `sessions` fixture also creates `kept_totals`), `tests/test_kept_totals.py`

**Interfaces:**
- Produces, `KeptTotal` (`kept_totals`): columns as in spec §8; `UniqueConstraint("line_id", "camera_id", "class_name", "result", "reset_tag", name="uq_kept_totals_key")`.
- Produces, `DetectionLog.__table_args__ = (Index("ix_detections_created_at", "created_at"),)`.
- Produces, revision `"0009_kept_totals"` (down `"0008_code_format"`): creates the table and the index only where missing; downgrade drops both.
- Produces, `production_records_service`:
  - `class_key(class_name: Optional[str]) -> str`: lower-cased, stripped, `"product"` when empty (the rule `counted_totals` uses today; `counted_totals` uses it too)
  - `reset_tag(line: Dict[str, Any], key: str, own_station: bool = False) -> str`: ISO of the later of `counts_reset_at` and `counts_reset_classes[key]` (only the line's for `own_station`), `""` when neither
  - `saved_lines() -> Dict[str, Dict[str, Any]]`: the saved lines by id (`get_lines(with_logic=False)`)
  - `ProductionRecorder.delete_chunk(where: Any, lines: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[int, Optional[datetime]]`: one transaction: read up to `DELETE_CHUNK` rows matching `where` oldest first (`recorded_at`, `id`), add the ones still in a counter to `kept_totals` (upsert `count = count + n`), delete them; returns (rows deleted, newest `recorded_at` deleted) and raises `storage_state` `"deleted_through"` to that time
  - `ProductionRecorder.prune(days: Optional[int] = None, now: Optional[datetime] = None, before: Optional[datetime] = None) -> int`: rows older than the cutoff and, when `before` is given, older than `before`; through `delete_chunk`
  - `async prune_kept_totals(session_factory: Optional[Callable[[], Any]] = None, lines: Optional[Dict[str, Dict[str, Any]]] = None) -> int`: deletes kept rows whose tag is no longer current or whose line is gone
  - `counted_totals(...)` (same signature) adds the kept rows of the line (`camera_id == ""`, or the camera's) whose tag equals the current one for their class

- [ ] **Step 1: Write the failing tests**

In `tests/test_kept_totals.py`, using `lines`, `sessions`, `_line`, `_cross`, `_Recorder`, `_totals` imported from `tests.test_production_records`:

```python
def _age_all(sessions, line_id, days):   # every record `days` older, and the line's reset time a day before them
    async def run():
        async with sessions() as db:
            await db.execute(sa.update(ProductRecord).values(recorded_at=ProductRecord.recorded_at - timedelta(days=days)))
            await db.commit()
    asyncio.run(run())
    line = SettingsPersistenceService._find_line(line_id)
    line["counts_reset_at"] = (datetime.now(timezone.utc) - timedelta(days=days + 1)).isoformat()
    SettingsPersistenceService.save()

def _restart_totals(sessions, runtime_id, monkeypatch):
    fresh = LineManager(); monkeypatch.setattr(line_module, "line_manager", fresh); fresh.apply_state()
    asyncio.run(restore_line_counts(sessions))
    rt = fresh.get(runtime_id)
    return _totals(rt.counter), dict(rt.counter.counts_by_class), _totals(rt.aux_counters["own"])

def test_totals_are_the_same_after_old_records_are_deleted(lines, sessions, monkeypatch):
    rec = _Recorder(sessions); monkeypatch.setattr(records_module, "production_recorder", rec)
    runtime = _line(lines, [{"camera_id": "top"}, {"camera_id": "own", "station": "own"}])
    for name, defect in (("bottle", False), ("bottle", True), ("can", False)):
        _cross(runtime.counter, "top", is_defect=defect, class_name=name)
    _cross(runtime.aux_counters["own"], "own", is_defect=True)
    asyncio.run(rec.flush())
    _age_all(sessions, runtime.id, 100)
    before = _restart_totals(sessions, runtime.id, monkeypatch)
    assert asyncio.run(rec.prune(days=90)) == 4
    assert _count(sessions) == 0
    assert _restart_totals(sessions, runtime.id, monkeypatch) == before == ((3, 2, 1), {"bottle": 2, "can": 1}, (1, 0, 1))

def test_a_line_reset_stops_old_kept_totals_counting(...):   # prune, then reset_line_counters → restart totals (0, 0, 0)
def test_a_class_reset_stops_that_class_only(...):           # prune, then mark_counts_reset(id, ["bottle"]) → {"can": 1}
def test_records_from_before_a_reset_are_not_kept(...):      # reset, then prune rows from before it → kept_totals empty
def test_stale_kept_rows_are_cleaned(...):                   # after a reset: prune_kept_totals returns the old rows' count; table empty; a deleted line's rows go too
def test_prune_stops_before_the_given_time(sessions):        # rows at 100 and 95 days; prune(days=90, before=now-97d) deletes only the 100-day row
def test_a_delete_notes_the_newest_deleted_time(sessions, data_dir):   # storage_state.load()["deleted_through"] == newest deleted recorded_at
def test_reset_tag():
    line = {"counts_reset_at": "2026-10-01T00:00:00+00:00", "counts_reset_classes": {"can": "2026-10-05T00:00:00+00:00"}}
    assert reset_tag(line, "can") == "2026-10-05T00:00:00+00:00"
    assert reset_tag(line, "bottle") == reset_tag(line, "can", own_station=True) == "2026-10-01T00:00:00+00:00"
    assert reset_tag({}, "can") == ""

def test_a_database_at_0008_upgrades_to_0009_and_back(tmp_path):
    engine, config = _alembic(tmp_path, "old.db")       # from tests.test_production_records
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0008_code_format"); command.upgrade(config, "head"); command.upgrade(config, "head")
        insp = sa.inspect(conn)
        assert "kept_totals" in insp.get_table_names()
        assert "ix_detections_created_at" in {i["name"] for i in insp.get_indexes("detections")}
        command.downgrade(config, "0008_code_format")
        assert "kept_totals" not in sa.inspect(conn).get_table_names()
    engine.dispose()
```

Each one-line test above is written out in the same style as the first: the setup named in its comment and that exact assertion.

- [ ] **Step 2: Run them to see them fail**

Run: `PYTEST tests/test_kept_totals.py`
Expected: FAIL (no `KeptTotal`, `reset_tag`, `delete_chunk`).

- [ ] **Step 3: Implement**

Model, revision (modelled on `0006_product_records.py`: inspect first), `_LATER_TABLES` + `"kept_totals"`, the `env.py` import, the detections index. In the service: `class_key`, `reset_tag`, `saved_lines`, `delete_chunk`, `prune` through `delete_chunk` (keeps its `DELETE_CHUNK` loop and 0.05 s pause), `prune_kept_totals`, and the kept rows in `counted_totals`. A row is "still in a counter" when `kind == "product"` and either `counted` and `recorded_at >= reset_tag(line, class_key(class_name))` (or the tag is `""`), or not `counted`, `camera_id` set and `recorded_at >= reset_tag(line, key, own_station=True)`; rows of lines not in `lines` are deleted without being kept. The `sessions` fixture in `tests/test_production_records.py` creates `KeptTotal.__table__` too.

- [ ] **Step 4: Run them to see them pass**

Run: `PYTEST tests/test_kept_totals.py tests/test_production_records.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db/models/kept_total.py alembic/versions/0009_kept_totals.py alembic/versions/0001_initial_schema.py alembic/env.py app/db/models/detection.py app/services/production_records_service.py tests/test_kept_totals.py tests/test_production_records.py
git commit -m "Records: kept_totals keep the line totals exact after old records are deleted"
```

---

### Task 5: Detections cleanup

**Files:**
- Create: `app/services/detections_cleanup.py`
- Test: `tests/test_detections_cleanup.py`

**Interfaces:**
- Produces: `CHUNK = 5000`; `async prune(session_factory, days: int, now: datetime) -> int` (rows with `created_at` older than `now - days`; `0` days deletes nothing); `async delete_oldest(session_factory, limit: int = CHUNK) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
def test_prune_deletes_rows_older_than_the_limit(det_sessions):
    _insert(det_sessions, ages_days=(40, 31, 29, 1))
    assert asyncio.run(detections_cleanup.prune(det_sessions, 30, NOW)) == 2
    assert asyncio.run(detections_cleanup.prune(det_sessions, 0, NOW)) == 0
    assert _ages(det_sessions) == [29, 1]

def test_delete_oldest_takes_the_oldest_first(det_sessions):
    _insert(det_sessions, ages_days=(5, 50, 20))
    assert asyncio.run(detections_cleanup.delete_oldest(det_sessions, 2)) == 2
    assert _ages(det_sessions) == [5]

def test_prune_uses_the_created_at_index(det_sessions):
    plan = _explain(det_sessions, "SELECT id FROM detections WHERE created_at < '2026-01-01' LIMIT 5000")
    assert "ix_detections_created_at" in plan
```

`det_sessions`: a temp database with `DetectionLog.__table__`; `_insert` writes rows with `created_at = NOW - age`; `_ages` returns the ages left, oldest first; `_explain` runs `EXPLAIN QUERY PLAN`.

- [ ] **Step 2: Run them to see them fail** — Run: `PYTEST tests/test_detections_cleanup.py`. Expected: FAIL (no module).

- [ ] **Step 3: Implement** `detections_cleanup` (delete by an id subquery, `LIMIT CHUNK`, one short transaction per chunk, 0.05 s between chunks, as `ProductionRecorder.prune` does).

- [ ] **Step 4: Run them to see them pass** — Run: `PYTEST tests/test_detections_cleanup.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/detections_cleanup.py tests/test_detections_cleanup.py
git commit -m "Detections: delete rows older than DETECTIONS_RETENTION_DAYS"
```

---

### Task 6: Records archive

**Files:**
- Create: `app/services/records_archive.py`
- Modify: `app/routes/v1/records.py` (two routes)
- Test: `tests/test_records_archive.py`

**Interfaces:**
- Consumes: `records_export.iter_chunks`, `records_export.CHUNK_ROWS`, `RecordFilter`, `COLUMNS`, `normalize_row`, `storage_state.data_dir/write_atomic`.
- Produces, `app/services/records_archive.py`:
  - `HEADER = ["id", *COLUMNS]`, `CHUNK = records_export.CHUNK_ROWS` (rows read per chunk; tests lower it)
  - `cell_out(value: Any) -> str` / `cell_in(text: str) -> str`: the `'` rule of spec §5
  - `file_for(day: date) -> Path`, `has_file(day: date) -> bool`
  - `list_days() -> List[Dict[str, Any]]`: `[{"day": "YYYY-MM-DD", "size": int}]`, oldest first; only `records-YYYY-MM-DD.csv.gz`
  - `read_day(day: date) -> Iterator[Dict[str, Any]]`: rows as `normalize_row` gives them, without `id`
  - `async write_day(session_factory, day: date) -> int`: rows written; `0` writes no file
  - `async days_to_archive(session_factory, now: datetime) -> List[date]`: days that have records, ended at least 10 minutes before `now`, and have no file; oldest first
  - `async archive_pending(session_factory, now: datetime) -> List[date]`: writes them; raises on the first failure
  - `async archived_until(session_factory) -> Optional[datetime]`: start of the oldest UTC day that has records and no file; `None` without records
  - `prune_files(days: int, now: datetime) -> int`, `delete_oldest_file() -> Optional[str]`
  - `async reimport(session_factory, days: List[date], after: Optional[datetime]) -> int`: inserts the days' rows newer than `after`, in chunks of 5000, new ids
- Routes: `GET /api/v1/records/archive` → `{"days": list_days(), "oldest": first day or None, "newest": last day or None}`; `GET /api/v1/records/archive/{day}` → `FileResponse` (`application/gzip`, attachment `records-YYYY-MM-DD.csv.gz`), 404 for anything that is not an existing day file.

- [ ] **Step 1: Write the failing tests**

```python
ODD = ["=HYPERLINK(\"x\")", "'quoted", "-5", "@a", "plain", None]

def test_a_day_is_written_and_reads_back_the_same(sessions, data_dir):
    rows = [_product(n=i, recorded_at=DAY0 + timedelta(hours=i), code=ODD[i % len(ODD)],
                     details={"stations": [{"camera_id": "c", "result": "good"}]} if i % 2 else None) for i in range(12)]
    _insert(sessions, rows + [_product(n=99, recorded_at=DAY0 + timedelta(days=1))])
    assert asyncio.run(records_archive.write_day(sessions, DAY0.date())) == 12
    assert list(records_archive.read_day(DAY0.date())) == [normalize_row(r) for r in rows]

def test_the_file_is_utf8_csv_with_a_bom_and_crlf(sessions, data_dir):
    _insert(sessions, [_product(recorded_at=DAY0)]); asyncio.run(records_archive.write_day(sessions, DAY0.date()))
    raw = gzip.decompress(records_archive.file_for(DAY0.date()).read_bytes())
    assert raw.startswith("﻿".encode()) and b"\r\n" in raw
    assert raw.decode("utf-8-sig").split("\r\n")[0].split(",") == records_archive.HEADER

def test_a_day_without_records_writes_no_file(sessions, data_dir):
    assert asyncio.run(records_archive.write_day(sessions, DAY0.date())) == 0
    assert not records_archive.has_file(DAY0.date())

def test_days_to_archive(sessions, data_dir):
    _insert(sessions, [_product(recorded_at=DAY0 + timedelta(days=d, hours=1)) for d in (0, 1, 2)])
    _write_dummy(DAY0.date())                                     # day 0 is archived already
    day = lambda d: (DAY0 + timedelta(days=d)).date()
    assert asyncio.run(records_archive.days_to_archive(sessions, DAY0 + timedelta(days=3, minutes=11))) == [day(1), day(2)]
    assert asyncio.run(records_archive.days_to_archive(sessions, DAY0 + timedelta(days=3, minutes=5))) == [day(1)]   # day 2 ended 5 minutes ago

def test_a_failed_write_leaves_no_partial_file(sessions, data_dir, monkeypatch):
    _insert(sessions, [_product(n=i, recorded_at=DAY0 + timedelta(minutes=i)) for i in range(5)])
    monkeypatch.setattr(records_archive, "CHUNK", 2)
    _break_iter_chunks_after(monkeypatch, 1)                      # the second chunk raises OSError
    with pytest.raises(OSError):
        asyncio.run(records_archive.write_day(sessions, DAY0.date()))
    assert list((data_dir / "archive").iterdir()) == []

def test_a_big_day_is_read_in_chunks(sessions, data_dir, monkeypatch):
    _insert(sessions, [_product(n=i, recorded_at=DAY0 + timedelta(seconds=i)) for i in range(7)])
    chunks = _count_chunks(monkeypatch); monkeypatch.setattr(records_archive, "CHUNK", 3)
    assert asyncio.run(records_archive.write_day(sessions, DAY0.date())) == 7
    assert chunks == [3, 3, 1]

def test_listing_ignores_partial_and_other_files(data_dir): ...      # records-2026-03-02.csv.gz.partial and notes.txt not listed
def test_archived_until(sessions, data_dir): ...                      # files for days 0 and 1, records on 0, 1, 3 → start of day 3; no records → None
def test_prune_files_and_delete_oldest(data_dir): ...                 # files 400, 300, 10 days old: prune_files(365) == 1; delete_oldest_file() == the 300-day one; prune_files(0) == 0
def test_reimport_puts_days_back_after_the_given_time(sessions, data_dir): ...   # write 2 days, delete all rows, reimport(after = noon of day 0) == rows after noon

def test_archive_routes(client, admin_headers, data_dir_for_client):
    _write_dummy(date(2026, 3, 2), b"gzipped")
    listing = client.get("/api/v1/records/archive", headers=admin_headers).json()
    assert listing == {"days": [{"day": "2026-03-02", "size": 7}], "oldest": "2026-03-02", "newest": "2026-03-02"}
    got = client.get("/api/v1/records/archive/2026-03-02", headers=admin_headers)
    assert got.status_code == 200 and got.content == b"gzipped" and got.headers["content-type"] == "application/gzip"
    for bad in ("2026-03-03", "2026-13-01", "..%2Fsystem_state.json", "x"):
        assert client.get(f"/api/v1/records/archive/{bad}", headers=admin_headers).status_code == 404
    assert required_api_key_scopes("GET", "/api/v1/records/archive/2026-03-02") == {"records:read"}
```

(`DAY0 = datetime(2026, 3, 2, tzinfo=timezone.utc)`; `_product`, `_insert`-style helpers as in `tests/test_production_records.py`; `_write_dummy(day, data=b"x")` writes a file; `data_dir_for_client` points `DATA_DIR` at a temp folder for the client test and restores it. Each `...` test is written with the setup and assertion named in its comment.)

- [ ] **Step 2: Run them to see them fail** — Run: `PYTEST tests/test_records_archive.py`. Expected: FAIL (no module).

- [ ] **Step 3: Implement**

`write_day` streams `iter_chunks(session_factory, RecordFilter(start=day, end=day+1), CHUNK)` and writes each chunk's CSV (`csv.writer(lineterminator="\r\n")`, BOM first) through a `gzip.GzipFile(compresslevel=6)` opened on `<file>.partial`, with the write in `asyncio.to_thread`; on any exception the `.partial` is deleted and the exception re-raised; on success fsync + rename + `fsync_dir`. `read_day` reverses each cell: `cell_in`, `""` → `None`, `recorded_at` parsed, `counted` from `true`/`false`, `confidence` float, `track_id` int, `details` from JSON, then `normalize_row`. `days_to_archive` walks the days from the oldest record to the last day that ended 10 minutes ago, asking `EXISTS` per day. Routes validate `day` with `date.fromisoformat` and a `^\d{4}-\d{2}-\d{2}$` match.

- [ ] **Step 4: Run them to see them pass** — Run: `PYTEST tests/test_records_archive.py tests/test_production_records.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/records_archive.py app/routes/v1/records.py tests/test_records_archive.py
git commit -m "Records: daily archive files, listed and downloaded from the records API"
```

---

### Task 7: Backups and the second folder

**Files:**
- Create: `app/services/backup_service.py`
- Modify: `main.py` (upgrade backup before `_upgrade_schema`)
- Test: `tests/test_backups.py`

**Interfaces:**
- Consumes: `storage_state.data_dir/stamp/fsync_dir`, `settings.BACKUP_KEEP_NIGHTLY`, `settings_persistence_service.STATE_FILE`, `_sqlite_path`, `settings.APP_VERSION`.
- Produces, `app/services/backup_service.py`:
  - `NAME_RE = re.compile(r"^\d{8}T\d{6}Z-(nightly|upgrade|manual)$")`, `BIG_TABLES = ("product_records", "detections")`
  - `keep_counts() -> Dict[str, int]`: `{"nightly": settings.BACKUP_KEEP_NIGHTLY, "upgrade": 3, "manual": 5}`
  - `make_backup(kind: str, db_path: Optional[str] = None, now: Optional[datetime] = None) -> Dict[str, Any]`: entry `{"name", "kind", "made_at", "size"}`; then `trim()`
  - `list_backups() -> List[Dict[str, Any]]`: newest first
  - `trim() -> int`, `newest_config_db() -> Optional[Path]`
  - `zip_file(name: str) -> Optional[Any]`: a spooled temporary file at position 0, `None` for an unknown name
  - `script_head() -> str`, `needs_upgrade_backup(db_path: str, head: str) -> bool`: `False` when the file is missing or cannot be read (a damaged file is Task 10's)
  - `mirror(copy_dir: str) -> Dict[str, int]`: `{"copied": n, "removed": m}`; raises `OSError` when the folder cannot be written
- `main.py`: before the upgrade, `if needs_upgrade_backup(db_path, script_head()): make_backup("upgrade", db_path)`, in `try/except`: log and raise `storage.backup_failed`, then carry on.

- [ ] **Step 1: Write the failing tests**

```python
def test_config_db_holds_every_table_with_the_big_two_empty(db_at_head, data_dir):
    _fill(db_at_head)            # a row in product_records, detections, kept_totals, product_lists, users
    entry = backup_service.make_backup("manual", db_at_head, NOW)
    copy = sqlite3.connect(data_dir / "backups" / entry["name"] / "config.db")
    src = sqlite3.connect(db_at_head)
    assert _tables(copy) == _tables(src)
    assert {t: _count(copy, t) for t in ("product_records", "detections")} == {"product_records": 0, "detections": 0}
    assert all(_count(copy, t) == _count(src, t) for t in _tables(src) if t not in backup_service.BIG_TABLES)
    assert "ix_product_records_line_time" in _indexes(copy)
    assert copy.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0009_kept_totals"

def test_the_folder_holds_settings_and_manifest(db_at_head, data_dir, settings_file):
    _fill(db_at_head)
    entry = backup_service.make_backup("nightly", db_at_head, NOW)
    folder = data_dir / "backups" / entry["name"]
    assert entry["name"] == "20261026T001000Z-nightly"
    assert sorted(p.name for p in folder.iterdir()) == ["config.db", "manifest.json", "system_state.json"]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["kind"] == "nightly" and manifest["left_out"] == ["product_records", "detections"]
    assert manifest["tables"]["product_records"] == 1 and manifest["revision"] == "0009_kept_totals"   # rows in the source

def test_keep_counts_per_kind(db_at_head, data_dir):
    for i in range(9): backup_service.make_backup("nightly", db_at_head, NOW + timedelta(days=i))
    for i in range(4): backup_service.make_backup("upgrade", db_at_head, NOW + timedelta(hours=i))
    kinds = [b["kind"] for b in backup_service.list_backups()]
    assert kinds.count("nightly") == 7 and kinds.count("upgrade") == 3
    assert backup_service.list_backups()[0]["made_at"] > backup_service.list_backups()[-1]["made_at"]

def test_listing_ignores_other_folders_and_partials(data_dir): ...     # "x", "20261026T001000Z-nightly.partial" not listed
def test_the_zip_holds_the_three_files(db_at_head, data_dir): ...      # zipfile namelist == the 3 names; zip_file("nope") is None
def test_upgrade_backup_only_when_the_revision_changes(tmp_path): ...  # db at 0008 → True; at head → False; no file → False; a garbage file → False
def test_a_failed_backup_leaves_no_partial_folder(db_at_head, data_dir, monkeypatch): ...   # copying a table raises → no folder under backups/

def test_mirror_copies_new_and_changed_files_and_removes_gone_ones(data_dir, tmp_path):
    _seed(data_dir, {"archive/records-2026-03-02.csv.gz": b"a", "backups/20261026T001000Z-nightly/config.db": b"b", "audit/audit-2026-10.jsonl": b"c"})
    copy = tmp_path / "usb"
    assert backup_service.mirror(str(copy)) == {"copied": 3, "removed": 0}
    (data_dir / "audit/audit-2026-10.jsonl").write_bytes(b"cc"); (data_dir / "archive/records-2026-03-02.csv.gz").unlink()
    assert backup_service.mirror(str(copy)) == {"copied": 1, "removed": 1}
    assert (copy / "audit/audit-2026-10.jsonl").read_bytes() == b"cc" and not (copy / "archive/records-2026-03-02.csv.gz").exists()

def test_mirror_never_touches_other_files_in_the_copy_folder(data_dir, tmp_path):
    copy = tmp_path / "usb"; (copy / "photos").mkdir(parents=True); (copy / "photos/a.jpg").write_bytes(b"x"); (copy / "notes.txt").write_text("x")
    backup_service.mirror(str(copy))
    assert (copy / "photos/a.jpg").exists() and (copy / "notes.txt").exists()

def test_mirror_to_a_folder_that_cannot_be_written_raises(data_dir, tmp_path):
    _seed(data_dir, {"archive/records-2026-03-02.csv.gz": b"a"})
    blocker = tmp_path / "file"; blocker.write_text("x")
    with pytest.raises(OSError):
        backup_service.mirror(str(blocker))
```

`db_at_head`: a temp database upgraded to head with Alembic (`_alembic` helper from `tests.test_production_records`); `NOW = datetime(2026, 10, 26, 0, 10, tzinfo=timezone.utc)`.

- [ ] **Step 2: Run them to see them fail** — Run: `PYTEST tests/test_backups.py`. Expected: FAIL (no module).

- [ ] **Step 3: Implement**

`make_backup` builds the folder as `<name>.partial`: `config.db` from one read transaction of the source (`sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`, `BEGIN`; for each table in `sqlite_master` (not `sqlite_%`) run its `CREATE` SQL on the new file and copy rows unless it is in `BIG_TABLES`; then each index's `CREATE INDEX` SQL); the settings copy; the manifest (`made_at`, `kind`, `app_version`, `revision`, `tables` with row counts of the source, `left_out`). A failure removes the `.partial` folder and re-raises. Rename, `fsync_dir`, `trim()`. `script_head` uses Alembic's `ScriptDirectory` on the repo's `alembic` folder. `mirror` walks `backups/`, `archive/`, `audit/` under `data/`, copies files whose size or mtime differ (`.partial`, then rename, `shutil.copy2`), and removes files under those three subfolders of the copy folder that are gone locally (and folders left empty there).

- [ ] **Step 4: Run them to see them pass** — Run: `PYTEST tests/test_backups.py tests/test_production_records.py::test_a_database_at_0005_upgrades_and_a_new_one_builds`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/backup_service.py main.py tests/test_backups.py
git commit -m "Backups: small config backups (no records), kept per kind, zipped, mirrored to a second folder"
```

---

### Task 8: Free-space guard

**Files:**
- Create: `app/services/storage_guard.py`
- Test: `tests/test_storage_guard.py`

**Interfaces:**
- Consumes: `detections_cleanup.delete_oldest`, `ProductionRecorder.delete_chunk`, `records_archive.archived_until`, `records_archive.delete_oldest_file`, `storage_state.add_notice`, alarm codes.
- Produces:
  - `@dataclass DiskReading: total: int; free: int; used_percent: float`
  - `read_disk(path: Optional[str] = None) -> DiskReading` (`psutil.disk_usage` of `data/`)
  - `async room_in_database(session_factory) -> int` (`freelist_count * page_size`)
  - `class StorageGuard(session_factory, recorder, disk: Callable[[], DiskReading] = read_disk, room: Optional[Callable[[], Awaitable[int]]] = None)`
    - `async check(now: datetime) -> Dict[str, Any]`: `{"total", "free", "room", "used_percent", "floor", "warn_percent", "state"}` with `state` `"ok" | "low" | "full"`; raises/clears `storage.low` and `storage.full`; runs `free_space` when full
    - `async free_space(target: int, now: datetime) -> List[Dict[str, Any]]`: the steps taken, `[{"step": "detections" | "archived_records" | "archive_files" | "records", "deleted": n}]`

- [ ] **Step 1: Write the failing tests**

```python
MB = 1024 * 1024

def test_low_is_raised_at_the_warn_level_and_clears_two_points_below(guard, disk):
    disk.used_percent = 85; asyncio.run(guard.check(NOW)); assert _active("storage.low")
    disk.used_percent = 84; asyncio.run(guard.check(NOW)); assert _active("storage.low")
    disk.used_percent = 82.9; asyncio.run(guard.check(NOW)); assert not _active("storage.low")

def test_room_inside_the_database_counts_as_free(guard, disk, room):
    disk.free = 1500 * MB; room.value = 600 * MB
    assert asyncio.run(guard.check(NOW))["state"] == "ok"

def test_space_is_freed_in_order_until_1_25_times_the_floor(guard, disk, room, full_db):
    # full_db: 3 detections; 2 records on days 0-1 (archive files exist); 2 records 5 days ago (no file); 2 in the last hour
    disk.free = 1000 * MB; room.per_row = 200 * MB; disk.per_file = 100 * MB      # 1000+600+400+200+400 = 2600 >= 2560
    steps = asyncio.run(guard.free_space(int(2048 * MB * 1.25), NOW))
    assert [s["step"] for s in steps] == ["detections", "archived_records", "archive_files", "records"]
    assert _record_count(full_db) == 2 and _active("storage.records_lost")         # the two from the last hour
    assert storage_state.notices()[0]["code"] == "storage.records_lost"

def test_freeing_stops_as_soon_as_there_is_enough(guard, disk, room, full_db):
    disk.free = 2400 * MB; room.per_row = 200 * MB
    assert [s["step"] for s in asyncio.run(guard.free_space(int(2048 * MB * 1.25), NOW))] == ["detections"]

def test_the_newest_24_hours_are_never_deleted(...): ...      # only last-day records; free stays low; state full; message says nothing more to delete
def test_full_raises_and_clears_at_the_floor(guard, disk, room, full_db): ...   # free < floor → storage.full active; free back ≥ floor → cleared
def test_floor_zero_turns_it_off(guard, disk, monkeypatch): ...  # STORAGE_MIN_FREE_MB=0, free 0 → state "ok", nothing deleted
def test_totals_stay_exact_when_space_deletes(...): ...         # records deleted by free_space go to kept_totals (delete_chunk is used)
```

Fixtures: `disk` is a mutable fake (`free`, `total`, `used_percent`, and `per_file` added to `free` for each archive file deleted); `room` a fake whose value grows by `per_row` for each record or detection deleted; `full_db` a temp database (records, detections, kept_totals) plus archive files under a temp `DATA_DIR`; `guard = StorageGuard(sessions, ProductionRecorder(sessions), disk=disk, room=room)`.

- [ ] **Step 2: Run them to see them fail** — Run: `PYTEST tests/test_storage_guard.py`. Expected: FAIL (no module).

- [ ] **Step 3: Implement**

`free = disk.free + await room()`; floor `STORAGE_MIN_FREE_MB * 1024 * 1024`; `0` = off. `free_space` re-measures after each chunk or file and stops at `target`. Step "archived_records" deletes `recorded_at < await archived_until()`; step "records" deletes `recorded_at < now - 24 h`, and when it deleted anything raises `storage.records_lost` and adds a notice with the oldest and newest time deleted. When every step has nothing left, `storage.full` stays and its message says `"nothing more the server can delete"`.

- [ ] **Step 4: Run them to see them pass** — Run: `PYTEST tests/test_storage_guard.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/storage_guard.py tests/test_storage_guard.py
git commit -m "Storage: a free-space floor that deletes the oldest data in a set order, with alarms"
```

---

### Task 9: Clock guard and the keeper

**Files:**
- Create: `app/services/data_keeper.py`
- Modify: `app/services/production_records_service.py` (`_run` no longer prunes; remove `RETENTION_SECONDS`, `FIRST_RETENTION_SECONDS`, `_next_retention`), `main.py` (start after `production_recorder.start()`, stop before `production_recorder.stop()`), `tests/conftest.py` (the session client patches `DataKeeper.start` to a no-op, like the other background services)
- Test: `tests/test_data_keeper.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8 by the names in their Interfaces blocks; `audit_log.prune`; `restore_line_counts`.
- Produces, `app/services/data_keeper.py`:
  - `class ClockGuard`: `check_start(newest: Optional[datetime], now: datetime, days: int) -> Optional[str]`; `check_jump(wall: datetime, mono: float, newest: Optional[datetime]) -> Optional[str]`; `paused() -> bool`; `confirm(newest: Optional[datetime], now: datetime) -> None`; reasons `"would_delete_everything"` and `"clock_jumped"`; state in `storage_state["clock"] = {"paused", "reason", "since", "confirmed_newest"}`
  - `class DataKeeper(session_factory=None, recorder=None, guard=None, clock: Callable[[], datetime] = utc now, monotonic: Callable[[], float] = time.monotonic)`; `TICK_SECONDS = 60`, `NIGHT_AFTER = time(0, 10)`
    - `start() -> None`, `async stop() -> None`
    - `async tick() -> None`, `async first_run() -> None`, `async hourly(now: datetime) -> None`, `async nightly(now: datetime, force_backup: bool = False) -> None`
    - `async backup_now() -> Dict[str, Any]`, `async confirm_clock() -> None`, `async newest_record() -> Optional[datetime]`
    - `async status() -> Dict[str, Any]` (the shape in Task 11)
  - `data_keeper = DataKeeper()`
- Records `storage_state["last"]` for `cleanup`, `archive`, `backup`, `copy`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_cleanup_that_would_delete_every_record_pauses(data_dir):
    g = ClockGuard()
    assert g.check_start(newest=NOW - timedelta(days=120), now=NOW, days=90) == "would_delete_everything"
    assert g.paused() and _active("storage.clock_check")
    assert g.check_start(newest=NOW - timedelta(days=10), now=NOW, days=90) is None and g.paused()   # stays until confirmed

def test_confirming_with_the_same_newest_record_does_not_pause_again(data_dir):
    g = ClockGuard(); newest = NOW - timedelta(days=120)
    g.check_start(newest, NOW, 90); g.confirm(newest, NOW)
    assert not g.paused() and not _active("storage.clock_check")
    assert g.check_start(newest, NOW + timedelta(hours=1), 90) is None

def test_a_forward_jump_while_running_pauses(data_dir):
    g = ClockGuard(); g.check_jump(NOW, 1000.0, newest=NOW)
    assert g.check_jump(NOW + timedelta(days=3), 1060.0, newest=NOW) == "clock_jumped"

def test_a_jump_that_lands_near_the_newest_record_does_not_pause(data_dir):
    g = ClockGuard(); boot = datetime(2000, 1, 1, tzinfo=timezone.utc)
    g.check_jump(boot, 10.0, newest=NOW)
    assert g.check_jump(NOW + timedelta(hours=2), 70.0, newest=NOW) is None and not g.paused()

def test_the_pause_survives_a_restart(data_dir):
    ClockGuard().check_start(NOW - timedelta(days=120), NOW, 90)
    assert ClockGuard().paused()

def test_hourly_skips_age_cleanup_while_paused_but_the_floor_still_runs(keeper, spies): ...   # paused: recorder.prune, detections prune, prune_files, audit prune not called; guard.check called on tick
def test_hourly_prunes_records_only_up_to_the_archived_days(keeper, spies): ...              # recorder.prune called with before == archived_until(); ARCHIVE_RECORDS=False → before None
def test_nightly_runs_once_per_utc_day_after_00_10(keeper, spies): ...                       # ticks at 00:05, 00:11, 05:00, next day 00:11 → archive_pending/make_backup("nightly") called twice
def test_first_run_archives_and_backs_up_only_without_a_nightly_backup_today(keeper, spies): ...
def test_a_failing_job_raises_its_alarm_and_the_next_still_runs(keeper, spies): ...          # archive raises → storage.archive_failed; backup still made; next good archive clears it
def test_copy_failed_raises_and_clears(keeper, spies, tmp_path, monkeypatch): ...            # BACKUP_COPY_DIR a file → storage.copy_failed; a folder → cleared
def test_first_run_raises_the_alarms_of_open_notices(keeper, data_dir): ...                  # notice storage.records_lost in state → alarm active after first_run
def test_the_records_writer_no_longer_prunes(): assert not hasattr(ProductionRecorder, "RETENTION_SECONDS")
```

`keeper` is a `DataKeeper` with a fake clock and monotonic, temp `sessions`, and `spies` replacing the jobs (`recorder.prune`, `detections_cleanup.prune`, `records_archive.archive_pending/prune_files`, `backup_service.make_backup/mirror`, `audit_log.prune`, `guard.check`) with recorders of their calls. `NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)`.

- [ ] **Step 2: Run them to see them fail** — Run: `PYTEST tests/test_data_keeper.py`. Expected: FAIL (no module).

- [ ] **Step 3: Implement**

The schedule of spec §12. `tick()`: `check_jump`, `guard.check(now)`, then `hourly` when an hour has passed (by `monotonic`) and `nightly` once per UTC day when `now.time() >= NIGHT_AFTER`; the first tick calls `first_run()` (re-raise alarms of open notices and of a clock pause; Task 10's rebuild work hooks in here; then `nightly(now)` with the nightly backup only when `list_backups()` has none of kind `nightly` made that UTC day). `hourly`: unless paused, `check_start` then age cleanup (`recorder.prune(before=archived_until()` when `ARCHIVE_RECORDS`), `detections_cleanup.prune`, `prune_kept_totals`, `records_archive.prune_files`, `audit_log.prune`); then `mirror`. Each job in its own `try/except`: log, raise its alarm (archive, backup, copy), clear it on the next success. `start()` makes the task on the running loop with the first tick after `TICK_SECONDS`.

- [ ] **Step 4: Run them to see them pass** — Run: `PYTEST tests/test_data_keeper.py tests/test_production_records.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/data_keeper.py app/services/production_records_service.py main.py tests/conftest.py tests/test_data_keeper.py
git commit -m "Data keeper: one schedule for cleanup, archive, backups and copies; the clock guard"
```

---

### Task 10: Damaged database at start-up

**Files:**
- Create: `app/db/recovery.py`
- Modify: `main.py` (the database step uses `open_database`; `mark_stopped()` after `engine.dispose()`), `app/services/data_keeper.py` (`first_run` calls `finish_rebuild`)
- Test: `tests/test_database_recovery.py`

**Interfaces:**
- Consumes: `backup_service.newest_config_db`, `records_archive.list_days/reimport`, `storage_state`, `production_recorder.flush`, `restore_line_counts`, `AlarmCode.STORAGE_DATABASE_RESTORED`.
- Produces, `app/db/recovery.py`:
  - `MARKER = ".server_running"`; `unclean_end() -> bool`; `mark_running() -> None`; `mark_stopped() -> None`
  - `check(db_path: str, full: bool) -> bool`: `True` = healthy or missing
  - `is_damage(exc: BaseException) -> bool`: an `sqlite3.DatabaseError` (directly, as `.orig`, or in the cause chain) whose message has `"malformed"` or `"not a database"`
  - `rebuild(db_path: str, now: Optional[datetime] = None) -> Dict[str, Any]`: `{"broken_dir", "backup"}`; stores `storage_state["rebuild"] = {"broken_dir", "backup", "pending": True}`
  - `async open_database(db_path: str, upgrade: Callable[[], Awaitable[None]], dispose: Callable[[], Awaitable[None]]) -> Optional[Dict[str, Any]]`: check (full after an unclean end) → `dispose` + `rebuild` when bad → `upgrade`; an upgrade that fails with `is_damage` → `dispose`, `rebuild`, `upgrade` once more; `mark_running()` on success; returns the rebuild info or `None`
  - `salvage_rows(broken_db: str, after: Optional[datetime]) -> List[Dict[str, Any]]`: rows newer than `after`, read until an error, never raising
  - `async finish_rebuild(session_factory) -> Dict[str, int]`: `{"salvaged", "reimported"}`; inserts salvage, re-imports the archived days inside `RECORDS_RETENTION_DAYS` after `deleted_through`, flushes the records writer, restores the counters, sets `pending` false, adds the notice and raises `storage.database_restored`

- [ ] **Step 1: Write the failing tests**

```python
def test_the_marker_tells_an_unclean_end(data_dir):
    assert not recovery.unclean_end()
    recovery.mark_running(); assert recovery.unclean_end()
    recovery.mark_stopped(); assert not recovery.unclean_end()

def test_checks(tmp_path, good_db):
    assert recovery.check(str(tmp_path / "missing.db"), full=True)
    assert recovery.check(good_db, full=False) and recovery.check(good_db, full=True)
    garbage = tmp_path / "g.db"; garbage.write_bytes(b"\x00garbage" * 4096)
    assert not recovery.check(str(garbage), full=False) and not recovery.check(str(garbage), full=True)

def test_damage_errors_are_told_from_others():
    assert recovery.is_damage(sqlite3.DatabaseError("database disk image is malformed"))
    assert recovery.is_damage(DBAPIError("x", {}, sqlite3.DatabaseError("file is not a database")))
    assert not recovery.is_damage(sqlite3.OperationalError("database is locked"))

def test_rebuild_moves_the_files_aside_and_copies_the_newest_backup(data_dir, good_db): ...
    # backup made from good_db; db, -wal, -shm files present → all three under broken/<time>/, the database == the backup's config.db, state rebuild pending

def test_open_database_rebuilds_a_garbage_file_and_upgrades(data_dir, good_db_with_user):
    backup_service.make_backup("nightly", good_db_with_user)
    Path(good_db_with_user).write_bytes(b"\x00garbage" * 4096)
    recovery.mark_running()                                  # an unclean end: full check
    info = asyncio.run(recovery.open_database(good_db_with_user, upgrade=_alembic_upgrade(good_db_with_user), dispose=_noop))
    assert info["backup"].endswith("-nightly")
    assert _users(good_db_with_user) == ["admin"]

def test_open_database_retries_once_when_the_upgrade_finds_damage(data_dir, good_db): ...   # fake upgrade raises a malformed error the first time → rebuild, called twice, result not None
def test_without_a_backup_the_upgrade_builds_an_empty_database(data_dir, tmp_path): ...      # garbage file, no backups → after open_database the file has every table, no users
def test_open_database_disposes_before_moving(data_dir, good_db): ...                        # order of calls: dispose before the move (spy on shutil.move / os.replace)
def test_salvage_reads_what_it_can(good_db_with_records, tmp_path): ...                      # rows after a time come back; a garbage file gives []
def test_finish_rebuild_puts_records_back_and_restores_the_counters(...): ...                # archive files for 2 days inside 90, salvage file with 3 newer rows → counts, restore_line_counts called after flush, notice + alarm
```

- [ ] **Step 2: Run them to see them fail** — Run: `PYTEST tests/test_database_recovery.py`. Expected: FAIL (no module).

- [ ] **Step 3: Implement**

`check`: `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`; `PRAGMA quick_check` when `full` (healthy when the first row is `"ok"`), else `SELECT count(*) FROM sqlite_master`; any `sqlite3.DatabaseError` = damaged. `rebuild`: move the three files with `os.replace` into `broken/<stamp>/`, copy `newest_config_db()` to `db_path` (when there is one). `salvage_rows` reads `product_records` ordered by `recorded_at` in pages of 5000 with `LIMIT/OFFSET` until a page raises. In `main.py`, the database step becomes `await recovery.open_database(db_path, upgrade=<run _upgrade_schema in engine.begin()>, dispose=engine.dispose)`; the version 1 backup and the upgrade backup (Task 7) stay before it. A `None` database path (not SQLite) skips the checks.

- [ ] **Step 4: Run them to see them pass** — Run: `PYTEST tests/test_database_recovery.py tests/test_data_keeper.py tests/test_api.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db/recovery.py main.py app/services/data_keeper.py tests/test_database_recovery.py
git commit -m "Start-up: check the database after an unclean end; rebuild a damaged one from the backup and the archive"
```

---

### Task 11: Admin routes for Data and backups

**Files:**
- Create: `app/routes/v1/data_backups.py`
- Modify: `main.py` (`PROTECTED_ROUTERS`), `app/security/api_key_scopes.py`, `app/security/config_lock.py` (`LOCK_FREE_ROUTES`)
- Test: `tests/test_data_backups_api.py`

**Interfaces:**
- Consumes: `data_keeper` (Task 9), `backup_service.zip_file`, `storage_state.close_notice`, `SettingsPersistenceService.record_audit`, `require_admin`.
- Produces: router `prefix="/system"` with the five routes of spec §14. `GET /system/data` answers `await data_keeper.status()`:

```json
{"disk": {"total": 0, "free": 0, "room": 0, "used_percent": 0, "floor": 0, "warn_percent": 85, "state": "ok"},
 "database": {"size": 0, "oldest": null, "newest": null},
 "archive": {"enabled": true, "days": 0, "size": 0, "oldest": null, "newest": null, "retention_days": 365},
 "backups": [{"name": "", "kind": "", "made_at": "", "size": 0}],
 "copy": {"dir": "", "ok": null, "last": null},
 "clock": {"paused": false, "reason": null, "since": null},
 "notices": [{"code": "", "at": "", "message": "", "details": {}}],
 "last": {"cleanup": null, "archive": null, "backup": null, "copy": null}}
```

- Audit actions (category `"System"`): `BACKUP_MADE`, `BACKUP_DOWNLOADED`, `CLOCK_CONFIRMED`, `NOTICE_CLOSED`.

- [ ] **Step 1: Write the failing tests**

```python
def test_signed_out_and_lower_levels_are_refused(client, admin_headers):
    assert client.get("/api/v1/system/data").status_code == 401
    for level in (1, 2):
        h = _user_headers(client, admin_headers, level)
        assert client.get("/api/v1/system/data", headers=h).status_code == 403
        assert client.post("/api/v1/system/backups", headers=h).status_code == 403

def test_the_status_has_every_part(client, admin_headers):
    assert set(client.get("/api/v1/system/data", headers=admin_headers).json()) == \
           {"disk", "database", "archive", "backups", "copy", "clock", "notices", "last"}

def test_back_up_now_and_download(client, admin_headers):
    made = client.post("/api/v1/system/backups", headers=admin_headers)
    assert made.status_code == 200 and made.json()["kind"] == "manual"
    got = client.get(f"/api/v1/system/backups/{made.json()['name']}", headers=admin_headers)
    assert got.status_code == 200 and got.headers["content-type"] == "application/zip"
    assert sorted(zipfile.ZipFile(io.BytesIO(got.content)).namelist()) == ["config.db", "manifest.json", "system_state.json"]
    actions = [e["action"] for e in client.get("/api/v1/system/audit-logs?limit=5", headers=admin_headers).json()]
    assert "BACKUP_MADE" in actions and "BACKUP_DOWNLOADED" in actions
    for bad in ("x", "20261026T001000Z-weekly", "..%2F..%2Fdata"):
        assert client.get(f"/api/v1/system/backups/{bad}", headers=admin_headers).status_code == 404

def test_clock_ok_and_notice_ok_clear_their_alarms(client, admin_headers): ...   # pause via ClockGuard + notice storage.records_lost; POSTs → 200, alarms cleared, notice gone; unknown notice → 404
def test_api_keys_cannot_reach_data_or_backups():
    for path in ("/api/v1/system/data", "/api/v1/system/backups", "/api/v1/system/backups/x", "/api/v1/system/data/clock-ok"):
        assert required_api_key_scopes("GET", path) is None and required_api_key_scopes("POST", path) is None
def test_the_post_routes_work_while_the_configuration_is_locked(client, admin_headers): ...   # lock, POST /system/backups → 200, unlock
```

(`_user_headers` from `tests.test_config_lock_api`; `test_every_changing_route_is_classified` in `tests/test_config_lock_policy.py` covers the lock inventory.)

- [ ] **Step 2: Run them to see them fail** — Run: `PYTEST tests/test_data_backups_api.py tests/test_config_lock_policy.py`. Expected: FAIL (404s; the inventory test names three unclassified routes).

- [ ] **Step 3: Implement** the router (zip streamed with `records_export.iter_file`), the scope rule (`None` for paths under `/api/v1/system/data` and `/api/v1/system/backups`, before the generic system rule), the three `LOCK_FREE_ROUTES` entries under a `# Data and backups: they change no configuration.` comment, and the router in `PROTECTED_ROUTERS`.

- [ ] **Step 4: Run them to see them pass** — Run: `PYTEST tests/test_data_backups_api.py tests/test_config_lock_policy.py tests/test_config_lock_api.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/v1/data_backups.py main.py app/security/api_key_scopes.py app/security/config_lock.py tests/test_data_backups_api.py
git commit -m "Data and backups: Admin routes for the status, backups, the clock and notices"
```

---

### Task 12: Dashboard pages

**Files:**
- Create: `assets/data_backups.js`
- Modify: `dashboard.html` (sidebar item after Audit log, `TAB_TITLES.tabData = "Data and backups"`, the item shown for level 3 only next to `adminMenuTab`'s rule, `switchTab` loads it, script tag `assets/data_backups.js?v=1.0.0`, page styles in `assets/dashboard.css`), `assets/production_lines.js` (Records archive control)
- Test: `tests/test_data_backups_dashboard.py`

**Interfaces:**
- Consumes: `GET /api/v1/system/data`, `POST /api/v1/system/backups`, `GET /api/v1/system/backups/{name}`, `POST /api/v1/system/data/clock-ok`, `POST /api/v1/system/data/notices/{code}/ok`, `GET /api/v1/records/archive[/{day}]`.
- Produces, `window.DataBackups` (and `module.exports` for node): `load()`, `pageHtml(status) -> string`, `formatBytes(n) -> string`, `CLOCK_REASONS = {would_delete_everything: "…", clock_jumped: "…"}`.

- [ ] **Step 1: Write the failing tests**

```python
STATUS = {"disk": {"total": 32 * GB, "free": 3 * GB, "room": 0, "used_percent": 90.6, "floor": 2 * GB, "warn_percent": 85, "state": "low"},
          "database": {"size": 4 * GB, "oldest": "2026-07-28T00:00:00Z", "newest": "2026-10-26T11:59:00Z"},
          "archive": {"enabled": True, "days": 120, "size": 300 * MB, "oldest": "2026-06-28", "newest": "2026-10-25", "retention_days": 365},
          "backups": [{"name": "20261026T001000Z-nightly", "kind": "nightly", "made_at": "2026-10-26T00:10:00Z", "size": 2 * MB}],
          "copy": {"dir": "", "ok": None, "last": None},
          "clock": {"paused": True, "reason": "clock_jumped", "since": "2026-10-26T06:00:00Z"},
          "notices": [{"code": "storage.records_lost", "at": "2026-10-26T05:00:00Z", "message": "<b>3 days</b> deleted", "details": {}}],
          "last": {"cleanup": None, "archive": None, "backup": None, "copy": None}}

def test_the_page_shows_every_part(): 
    html = _node_page(STATUS)
    for text in ("Back up now", "The clock is right", "20261026T001000Z-nightly", "Download", "No second folder", "OK"):
        assert text in html
    assert "&lt;b&gt;3 days&lt;/b&gt;" in html and "<b>3 days</b>" not in html

def test_the_disk_bar_marks_the_floor(): assert 'data-floor="6.3"' in _node_page(STATUS)      # 2 GB of 32 GB, one decimal
def test_a_clock_that_is_fine_has_no_button(): assert "The clock is right" not in _node_page({**STATUS, "clock": {"paused": False, "reason": None, "since": None}})
def test_format_bytes(): assert _node("formatBytes(0)|formatBytes(1536)|formatBytes(3*1024**3)") == "0 B|1.5 KB|3.0 GB"
def test_the_sidebar_item_is_admin_only_and_the_page_is_wired():
    html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    assert "switchTab('tabData')" in html and 'tabData: "Data and backups"' in html and "assets/data_backups.js?v=1.0.0" in html
    assert 'getElementById("dataMenuTab").style.display = lvl === 3' in html
def test_the_records_page_downloads_an_archived_day():
    js = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    assert 'id="recArchiveDay"' in js and "Download archived day" in js and "/api/v1/records/archive" in js
def test_help_only_on_the_floor_and_the_clock():
    assert _node_page(STATUS).count("data-help=") == 2
```

`_node_page(status)` runs `pageHtml` in node (as `tests/test_system_usage_dashboard.py` runs `system_usage.js`).

- [ ] **Step 2: Run them to see them fail** — Run: `PYTEST tests/test_data_backups_dashboard.py`. Expected: FAIL (no file).

- [ ] **Step 3: Implement** the page with the existing `card-panel`, `kpi`, `pl-table` and `btn-action` classes (no new design language); text escaped with the file's own `esc`. Copy: the clock reasons `"The newest record is older than the records are kept, so deleting would remove them all."` and `"The clock jumped forward while the server was running."`; the help on the floor `"Below this much free space the oldest data is deleted to make room."`, on the pause `"Old data is not deleted by age until an Admin confirms the clock. The free-space floor still works."`. Downloads go through `fetch` with the sign-in header and a blob link, as `exportRecords` does. The Records control is a date box limited to `oldest`–`newest` and a **Download archived day** button, hidden while there are no days. Phone width: the page stacks in one column below 640 px, with no side scroll.

- [ ] **Step 4: Run them to see them pass** — Run: `PYTEST tests/test_data_backups_dashboard.py tests/test_help_tips.py tests/test_topbar_phone.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add assets/data_backups.js assets/dashboard.css dashboard.html assets/production_lines.js tests/test_data_backups_dashboard.py
git commit -m "Dashboard: Data and backups page; archived days on the Records page"
```

---

### Task 13: Docs, live check and the full suite

**Files:**
- Modify: `README.md` ("Data safety" section: what is kept where, the settings table of spec §15, restoring by hand, the second folder in Docker), `.env.example`, `docker-compose.yml` (commented bind mount `/media/usb/vision-backups:/backup-copy` and `BACKUP_COPY_DIR=/backup-copy`)

- [ ] **Step 1: Write the docs** as listed above; the README restore steps: stop the server; copy `config.db` over the database file and `system_state.json` over the settings file; start it; download archived days for older records.
- [ ] **Step 2: Live check on a scratch server** (launcher copy with REPO = this worktree, port 8765, its own scratch `data/`; never the owner's server on 8000 or the real `data/`):
  1. Start, sign in, open Data and backups at 1280 px and 375 px (screenshots, no side scroll); **Back up now**, download it.
  2. Stop; set `STORAGE_MIN_FREE_MB` above the free space; start with seeded records and archive files: `storage.full`, the steps in order, the notice and **OK**.
  3. Stop; cut `system_state.json` in half; start: the lines come back, the notice shows.
  4. Stop the server hard (kill); write garbage into the database; start: rebuilt from the backup, records back from the archive, counters match.
  5. Records page: download an archived day; Audit log: **Load older**.
- [ ] **Step 3: Run the full suite**

Run: the `pt.sh` command from the worktree (Global Constraints).
Expected: no failures besides the 8 deselected ones.

- [ ] **Step 4: Commit**

```bash
git add README.md .env.example docker-compose.yml
git commit -m "Data safety: docs"
```
