# Data safety: storage, archive, backups and recovery

Date: 2026-10-26. Branch `claude/data-safety` (from `main` at `18be425`).

A check of the data handling on 2026-10-26 found that the server can lose its whole setup to one
damaged file, can fill the disk before its 90 days are up, keeps one table for ever, has no
backups, and cannot start on a damaged database. This change makes the data safe for an edge
device that runs unattended: the disk never fills, damaged files are replaced from good copies,
old records can be fetched back, and the line keeps running whatever happens to the data.

## Owner's decisions (2026-10-26)

- **Full disk:** keep a free-space floor. Delete the oldest data early when needed, raise an
  alarm, and never stop recording.
- **Getting deleted records back:** daily archive files (compressed CSV), downloadable from the
  Records page.
- **Where:** in `data/` on the same disk, plus an optional second folder (a USB stick or a
  network share) set in `.env` that gets a copy of everything.
- **Audit log:** kept 1 year, in its own files.
- **Approach:** one database file as now; the backups hold everything except the two big
  tables (records and detections), which the archive protects instead.

## Measurements (throwaway files, 2026-10-26)

| What | Size |
|---|---|
| One `product_records` row, with its four indexes | 465 bytes |
| One `detections` row with 5 detections | 735 bytes |
| One record in a gzip CSV archive | about 25 bytes |
| `system_state.json` (the owner's server) | 37 KB |

A line at 1 product a second makes about 40 MB of records a day, 3.6 GB in 90 days, and 0.8 GB
of archive a year. Deleting rows does not shrink the database file: SQLite keeps the pages and
reuses them for new rows (measured: after deleting half the rows, re-adding them grew the file by
0.1 MB). So the file levels off at about 90 days of data, and freeing space means stopping it
from growing, not shrinking it.

## 1. Files in `data/`

```
data/
  factory_data.db (+ -wal, -shm)          the database (DATABASE_URL)
  system_state.json                       the settings
  system_state.broken-<time>.json         a settings file that failed to load, kept as it was
  settings-history/system_state-<time>.json
  archive/records-YYYY-MM-DD.csv.gz       one file per UTC day of records
  backups/<time>-<kind>/                  config.db, system_state.json, manifest.json
  broken/<time>/                          a damaged database, moved aside
  audit/audit-YYYY-MM.jsonl               the audit log, one file per UTC month
  storage_state.json                      clock guard, notices, last runs, rebuild work
  .server_running                         present while the server runs
```

`<time>` is UTC, `20261026T001000Z` (with microseconds for settings history). `data/` is
`settings_persistence_service.DATA_DIR`; the database path comes from `DATABASE_URL`, as now.

## 2. New units

| File | Does |
|---|---|
| `app/services/storage_state.py` | Reads and writes `storage_state.json` (temp file, fsync, rename; one lock). A file that does not parse is logged and started again empty: it holds nothing the server cannot do without. |
| `app/services/storage_guard.py` | Disk reading, the floor, the `storage.low` / `storage.full` alarms, what to delete when space runs short. |
| `app/services/records_archive.py` | Writes, lists, reads and deletes the day files; puts archived days back into the database. |
| `app/services/backup_service.py` | Makes, lists, zips and trims backups; mirrors to the second folder. |
| `app/services/audit_log.py` | Appends, reads and trims the audit files. |
| `app/services/data_keeper.py` | The background task that runs everything on a schedule; the clock guard. |
| `app/db/recovery.py` | The start-up check of the database and its rebuild. |
| `app/db/models/kept_total.py` | The `KeptTotal` model. |
| `alembic/versions/0009_kept_totals.py` | The `kept_totals` table and an index on `detections.created_at`. |
| `app/routes/v1/data_backups.py` | The Admin routes of the Data and backups page. |
| `assets/data_backups.js` | The Data and backups page (`window.DataBackups`). |

Changed: `app/config.py`, `app/events/alarm_events.py`, `app/db/session.py`, `main.py`,
`app/services/settings_persistence_service.py`, `app/services/production_records_service.py`,
`app/routes/v1/records.py`, `app/routes/v1/system.py`, `app/security/api_key_scopes.py`,
`app/security/config_lock.py`, `dashboard.html`, `assets/production_lines.js`,
`docker-compose.yml`, `.env.example`, `README.md`.

## 3. Settings file safety

**History.** Before each save replaces `system_state.json`, the file on disk is copied to
`settings-history/system_state-<time>.json`, if it parses as JSON. The folder keeps the 20 newest
copies plus the newest copy of each UTC day for the last 30 days (about 2 MB). It is trimmed after
each copy.

**Loading.** A file *loads* when it parses to a JSON object and the merge with the defaults and
`upgrade_state` run without an exception.
1. `system_state.json` loads: as today.
2. It exists but does not load: it is renamed `system_state.broken-<time>.json`. The history
   copies are tried newest first; the first that loads becomes the settings and is saved as
   `system_state.json`. A notice and the alarm `storage.settings_restored` (critical) name the
   copy used and the broken file.
3. Nothing loads: the defaults, as today, but the broken file and the history are kept, and the
   notice and alarm say no good copy was found.

Defaults are never saved over a file that exists. A leftover `system_state.json.tmp` is deleted.
On Linux the folder is fsynced after the rename, so the rename survives a power cut.

## 4. Free-space floor

Every minute the keeper reads the disk that holds `data/` (`psutil.disk_usage`) and the room
inside the database (`PRAGMA freelist_count` × `page_size`).

- **Free** = the disk's free space + the room inside the database (new rows use that room first).
- `storage.low` (warning) while the disk is `STORAGE_WARN_PERCENT` (85) % used or more; it clears
  below 83 %.
- `storage.full` (critical) while free is under `STORAGE_MIN_FREE_MB` (2048). The keeper then
  deletes, one step after the other, oldest first, until free is back to 1.25 × the floor:
  1. detections;
  2. records of days that have an archive file;
  3. archive files;
  4. records that are in no archive file, but never those of the newest 24 hours. This raises
     `storage.records_lost` (critical) with the time span deleted, and a notice.

  Steps 1, 2 and 4 make room inside the database, step 3 on the disk. Records are deleted in
  chunks of 5000, through the same function as the age cleanup (so the totals stay exact, §8),
  with the room measured again after each chunk. `storage.full` clears when free is back to the
  floor. If nothing is left to delete, it stays with "nothing more the server can delete".
- `STORAGE_MIN_FREE_MB=0` turns the floor off.
- The WAL file is limited to 64 MB after a burst of writes (`PRAGMA journal_size_limit`).

## 5. Daily archive

- **What:** every record of one UTC day, in
  `archive/records-YYYY-MM-DD.csv.gz`. Every column of the table, in table order, with `id` first:
  `recorded_at` as `YYYY-MM-DDTHH:MM:SS.ffffffZ`, `counted` as `true`/`false`, empty for none,
  `details` as compact JSON. UTF-8 with a byte order mark, `\r\n` line ends, gzip level 6.
  A text cell starting with `=`, `+`, `-`, `@`, tab, return or `'` gets a `'` in front, so a
  spreadsheet does not run it, and the file reads back exactly (one leading `'` is removed).
- **When:** each night after 00:10 UTC, and at the first keeper run after a start, every day that
  has ended at least 10 minutes ago, has records and has no file yet is written, oldest first. A
  day is read in chunks of 5000 rows, written to `.partial`, fsynced and renamed.
- **How long:** files older than `ARCHIVE_RETENTION_DAYS` (365) are deleted every hour; `0` keeps
  them until space is needed.
- **Age cleanup waits for the archive:** while `ARCHIVE_RECORDS` is on, the 90-day cleanup
  deletes only records of days that have a file. If archiving keeps failing, old records stay,
  `storage.archive_failed` (warning) says so, and the floor still protects the disk.
- **Getting them:** the Records page lists the archived days and downloads one. Same access as
  the export (Operator; API key scope `records:read`).
- `ARCHIVE_RECORDS=false` writes no files; records are then deleted at 90 days as today.

## 6. Backups

- **What:** `backups/<time>-<kind>/` holds
  - `config.db`: a SQLite file with every table of the database, its indexes and the
    `alembic_version` row, and every row except those of `product_records` and `detections` (left
    empty). Made from one read transaction of the live database (WAL lets the server keep
    writing), written as `.partial` and renamed;
  - `system_state.json`: a copy of the settings file;
  - `manifest.json`: time, kind, app version, database revision, rows per table, tables left out.
- **When:** `nightly` after 00:10 UTC (one a day), `upgrade` before the database is upgraded
  (its revision is not the newest one in `alembic/versions`), `manual` from the "Back up now"
  button. Kept: `BACKUP_KEEP_NIGHTLY` (7) nightly, 3 upgrade, 5 manual.
- **Second folder:** when `BACKUP_COPY_DIR` is set, `backups/`, `archive/` and `audit/` are
  mirrored there after each backup and archive run and every hour: new or changed files are copied
  (`.partial`, then renamed). A backup gone from `data/` is removed there; an archive or audit copy
  stays until it is past its own retention, even when the main disk dropped it earlier for space
  (changed after the whole-branch review, 2026-10-27). Only files the server copied are ever removed. If the folder cannot be
  written, `storage.copy_failed` (warning) is raised; it clears on the next good copy. The local
  files are always made first.
- **Who:** backups hold password hashes, encrypted API key secrets and connection settings, so
  listing, making and downloading them is Admin only, signed in (no API keys), and each download
  and manual backup goes in the audit log. A backup restores on another machine only with the
  same `SECRET_KEY` in `.env`.
- **Restoring by hand** (README): stop the server, copy `config.db` over the database file and
  `system_state.json` over the settings file, start it.
- A backup that fails raises `storage.backup_failed` (warning) and is tried again the next hour;
  it clears on the next good backup. A failed `upgrade` backup does not stop the upgrade (as with
  today's version 1 backup).

## 7. Damaged database at start-up

- **Marker:** `.server_running` is written once the database check has passed and is removed at
  the end of a clean shutdown. Found at start = the last run did not end cleanly (power cut, crash,
  kill).
- **Check:** after an unclean end, `PRAGMA quick_check` runs on the database before anything else
  opens it; otherwise only a cheap open (`SELECT count(*) FROM sqlite_master`). A missing file is
  fine (a new install). The upgrade step runs as now; if it fails with a SQLite "malformed" or
  "not a database" error, the database counts as damaged too.
- **Rebuild when damaged:**
  1. The engine is closed; the database and its `-wal` and `-shm` files are moved to
     `broken/<time>/` and kept.
  2. The newest backup's `config.db` (any kind) is copied in as the database. With no backup, the
     upgrade builds an empty one; an admin account then comes only from `ADMIN_INITIAL_PASSWORD`,
     as today.
  3. The upgrade runs (once more), and the server starts as usual. The lines run.
  4. In the background, at the keeper's first run:
     - **salvage:** the records newer than the newest archived day are read from the damaged
       file, as far as they read; what reads is inserted into the new database;
     - **re-import:** the archived days inside the 90 days and newer than the newest record ever
       deleted (`storage_state.json`) are inserted back;
     - the records writer is flushed and the counters are restored from the records again.
     Inserted rows get new ids.
  5. A notice and `storage.database_restored` (critical) say what was used, how many records were
     salvaged and re-imported, and where the damaged file is.
- **The server never refuses to start** because of the database. If the rebuild itself fails,
  the start fails as today, with the reason in the log.

## 8. Totals past 90 days

Today a counter is rebuilt at start from the records since the line's last reset; records deleted
at 90 days drop out of it. New table `kept_totals`:

| Column | |
|---|---|
| `id` | integer key |
| `line_id` | String(48) |
| `camera_id` | String(64); `""` = the line's totals, else an own-station camera's |
| `class_name` | String(128), the counter's key: lower case, `"product"` when empty |
| `result` | String(8): `good` or `reject` |
| `reset_tag` | String(40): the ISO time of the reset the count belongs to; `""` = never reset |
| `count` | integer |

Unique on (`line_id`, `camera_id`, `class_name`, `result`, `reset_tag`).

- **The tag** of a class of a line's totals is the later of the line's `counts_reset_at` and that
  class's time in `counts_reset_classes`; for an own-station camera it is `counts_reset_at`.
- **Deleting a chunk of records** (age cleanup or floor) reads the chunk first. The products that
  are still in a counter (a counted product at or after its class's tag time, or an own-station
  product at or after the line's reset) are added to `kept_totals` under the current tag, in the
  same transaction as the delete. The newest `recorded_at` ever deleted is noted in
  `storage_state.json` (the re-import of §7 starts after it).
- **`counted_totals`** adds the `kept_totals` rows of the line (and camera) whose tag is the
  class's current tag.
- **A reset changes the tag,** so the old kept rows stop counting without being touched. The
  hourly cleanup deletes kept rows whose tag is no longer current, and rows of lines that no
  longer exist.

## 9. Clock guard

Deleting by age trusts the clock. A device that boots with its clock far ahead would delete good
records.

- **Pause** age deleting (records, detections, archive files, audit files, stale kept totals)
  when:
  1. the newest record is older than `RECORDS_RETENTION_DAYS`, so the cleanup would delete every record, unless
     an Admin confirmed the clock with that same newest record; or
  2. while the server runs, the clock jumps forward by more than a day compared with the server's
     own running time, unless the clock then lands within a day of the newest record (a clock
     that was behind being corrected).
- During a pause, `storage.clock_check` (warning) is active and the page shows why. The floor
  (§4) keeps working: it deletes only as far as space needs, oldest first, whatever the clock says.
- **The clock is right** (Admin, audit-logged) ends the pause and records the newest record time
  it was confirmed with. The pause and the confirmation are kept in `storage_state.json`, so a
  restart keeps them.
- Not handled: records written while the clock is wrong keep their wrong times.

## 10. Detections

- `DETECTIONS_RETENTION_DAYS` (30) are kept; older rows are deleted every hour in chunks of 5000
  (new index `ix_detections_created_at`). `0` keeps them for ever. Not archived.
- They are the first thing the floor deletes.

## 11. Audit log

- **Files:** `audit/audit-YYYY-MM.jsonl` by the entry's UTC month, one JSON object per line, the
  same fields as today. Each entry is appended, flushed and fsynced at once, under a lock. A line
  that does not parse (cut by a power cut) is skipped when reading.
- **Kept:** files whose month ended more than `AUDIT_RETENTION_DAYS` (365) ago are deleted every
  hour, so entries are kept at least that long.
- **Moved over:** at the first start, the entries in `system_state.json` (`audit_logs`) are
  appended, oldest first, and the key is removed from the settings. The 24-hour and 500-entry
  limits go away.
- **Reading:** `GET /api/v1/system/audit-logs` keeps its answer and gains `before` (an ISO time):
  entries older than it, newest first. The page gets a **Load older** button under the table.
- The in-memory ring of recent changes, which dashboards poll, stays as it is.

## 12. The keeper

`DataKeeper` (`data_keeper.py`) starts after the records writer and stops before it at shutdown.
It ticks every 60 s; the first tick comes 60 s after the start.

| When | What |
|---|---|
| Every tick | Clock jump check (§9); disk reading, alarms and floor (§4). |
| First tick after a start | Re-raises the alarms of open notices and of a clock pause; rebuild work (§7); then the night jobs below (the nightly backup only if none was made that UTC day). |
| Every hour | Age cleanup of records (90 days, archived days only), detections, kept totals, archive files, audit files; mirror to the second folder. |
| Each night after 00:10 UTC | Archive the finished days; the nightly backup; mirror. |

Each job runs on its own: an error is logged, raises its alarm where it has one, and the job is
tried again at the next hour. Database work uses short transactions and chunks, as the records
writer does now; file work runs in a thread. `ProductionRecorder` no longer schedules the cleanup
itself; its `prune` is what the keeper calls.

## 13. Alarms

Group "Storage", scope "server".

| Code | Severity | Raised | Cleared |
|---|---|---|---|
| `storage.low` | warning | disk ≥ 85 % used | below 83 % |
| `storage.full` | critical | free under the floor | back to the floor |
| `storage.records_lost` | critical | records in no archive deleted for space | notice OK |
| `storage.clock_check` | warning | age deleting paused | The clock is right |
| `storage.settings_restored` | critical | settings loaded from a copy or the defaults | notice OK |
| `storage.database_restored` | critical | database rebuilt | notice OK |
| `storage.archive_failed` | warning | archive run failed | next good run |
| `storage.backup_failed` | warning | backup failed | next good backup |
| `storage.copy_failed` | warning | second folder not written | next good copy |

A **notice** is kept in `storage_state.json` until an Admin presses OK on the Data and backups
page, which also clears its alarm; at each start the alarms of open notices are raised again.

## 14. API

| Route | Who | Does |
|---|---|---|
| `GET /api/v1/records/archive` | Operator, `records:read` | `{"days": [{"day", "size"}], "oldest", "newest"}` |
| `GET /api/v1/records/archive/{day}` | Operator, `records:read` | The day's `.csv.gz` (`YYYY-MM-DD`; 404 if none) |
| `GET /api/v1/system/data` | Admin, signed in | Disk, floor, database size and room, oldest/newest record, archive, backups, second folder, clock, notices, last runs |
| `POST /api/v1/system/backups` | Admin, signed in | Make a manual backup now; answers its entry |
| `GET /api/v1/system/backups/{name}` | Admin, signed in | The backup folder as a zip |
| `POST /api/v1/system/data/clock-ok` | Admin, signed in | The clock is right (§9) |
| `POST /api/v1/system/data/notices/{code}/ok` | Admin, signed in | Close a notice and clear its alarm |
| `GET /api/v1/system/audit-logs?before=` | as today | Older entries (§11) |

- API keys: the archive falls under the existing records rule; `/api/v1/system/data` and
  `/api/v1/system/backups` are JWT only (a new rule before the generic system rule).
- Configuration lock: the three POST routes change no configuration and go in `LOCK_FREE_ROUTES`.
- Backup names must match `^\d{8}T\d{6}Z-(nightly|upgrade|manual)$`; anything else is 404.

## 15. Settings (`.env`)

| Name | Default | |
|---|---|---|
| `STORAGE_MIN_FREE_MB` | 2048 | the floor; 0 = off |
| `STORAGE_WARN_PERCENT` | 85 | 1–100 |
| `DETECTIONS_RETENTION_DAYS` | 30 | 0 = for ever |
| `ARCHIVE_RECORDS` | true | |
| `ARCHIVE_RETENTION_DAYS` | 365 | 0 = until space is needed |
| `BACKUP_KEEP_NIGHTLY` | 7 | at least 1 |
| `BACKUP_COPY_DIR` | empty | the second folder; empty = none |
| `AUDIT_RETENTION_DAYS` | 365 | at least 1 |

`RECORDS_RETENTION_DAYS` (90) stays. In Docker, the second folder is a bind mount; `docker-compose.yml`
gets a commented example (`/media/usb/vision-backups:/backup-copy` with
`BACKUP_COPY_DIR=/backup-copy`).

## 16. Dashboard

- **Data and backups** (Admin section of the sidebar, Admin only like Users & API keys; built by
  `assets/data_backups.js`):
  - open notices at the top, each with **OK**;
  - Storage: the disk bar with the floor marked, free space, database size and room inside, the
    days of records kept, the archive (days, size, oldest and newest), the last cleanup, archive,
    backup and copy, and the second folder's state;
  - Clock: "OK", or why deleting is paused, with **The clock is right**;
  - Backups: time, kind and size, a **Download** button each, and **Back up now**.
- **Records page:** next to the export buttons, a date box limited to the archived days and
  **Download archived day**, shown when there is at least one file.
- **Audit log page:** **Load older** under the table.
- Help icons only where a word needs it: the floor, and the clock pause.

## 17. Tests

Written first, each unit with temporary folders, a fake disk reading and a fake clock:
- settings: a damaged file loads the newest good copy, is kept as broken, and raises the alarm;
  no good copy gives defaults without overwriting; history keeps 20 + daily;
- floor: the deletion order, the 24-hour limit, the alarms and their clearing, room inside the
  database counted as free;
- archive: a day written and read back equals its rows (formula-like and `'` cells too); catch-up
  of missed days; age cleanup waits for the file; retention; the routes and their access;
- backups: `config.db` holds every table, the two big ones empty; keep counts; the zip; the mirror
  copies and removes; a missing second folder raises and clears its alarm; the upgrade backup is
  made only when the revision changes;
- start-up: a garbage database file is moved aside and rebuilt from the newest backup, the lines'
  settings and users come back, salvage and re-import put the records back, the counters match;
  an unclean end runs `quick_check`;
- totals: the counters after a cleanup and a restart equal those before; a line reset and a class
  reset stop old kept rows counting;
- clock: both pause rules, the floor still working while paused, the confirmation;
- detections: retention and the index; audit: moving the old entries, `before`, retention, a cut
  line skipped;
- the dashboard pieces (markup and node tests), the scope and lock inventories, the alarm
  catalogue;
- then the full suite, and a live check on a scratch server: a forced high floor, a damaged
  settings file, a damaged database, the pages at phone and desktop widths.

## Not in this change

- Shrinking the database file (`VACUUM`: it needs as much free space as the file itself).
- A restore button (restore is automatic for a damaged database, by hand otherwise).
- Encrypting backups; sending them to a cloud.
- Viewing archived days inside the Records page (they are downloaded).
- Records that arrive with a time inside a day that is already archived (only with a wrong clock)
  are not in that day's file.
- After a rebuild, the counts of records deleted between the last backup and the failure are
  missing from the totals of lines not reset for 90 days.
