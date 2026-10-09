# Section 4: Product records in the database, Records page, CSV and Excel export

Read [README.md](README.md) first: it has the goal, the owner's decisions, the project map and the rules.

**Depends on Section 1.** It builds on Section 3 when that is done; otherwise store `stations` as an empty list and leave a note. Start from the latest section branch in the table in README.md.

## Status

**Done** on branch `claude/optimistic-carson-s5ko0o` (started from `main` after Section 3 and its follow-up fix were merged).

- **Table** `product_records` (`app/db/models/production_record.py`), the columns and the four indexes listed below; revision `0006_product_records` (inspects first, so it is safe to run again; `product_records` added to `_LATER_TABLES` of 0001; the model imported in `alembic/env.py`).
- **Recorder** `app/services/production_records_service.py`: `ProductionRecorder` (`production_recorder`) queues rows in a `deque` under a lock (cap `RECORDS_QUEUE_LIMIT`, default 100000; oldest dropped, alarm `records.dropped`, cleared after a write with nothing dropped). A task on the main loop (started in `main.py` right after the migrations) writes every 1 s, or as soon as 500 rows wait, in one `insert(ProductRecord)`. A database error keeps the rows and retries; `records.write_failed` (critical) after 3 failures in a row, cleared on success. The lifespan writes what is left before `engine.dispose()`. Retention: `RECORDS_RETENTION_DAYS` (default 90, 0 = for ever), checked hourly (first one a minute after start), deleted 5000 rows per transaction. Both alarms are in `ALARM_CATALOG` and `AlarmCode`.
- **Hooks**: `CountingService.finish_crossing` (`counted = camera_id is None`; new `record=` argument for columns the message does not carry, used for `product_list_id`), `LineRuntime._emit_code_product` (`class_name` = the name its count is kept under, so restore matches), `_emit_qr` and `_emit_no_read` (`kind="code"`). Rows are built by `build_row(payload, kind, counted, **extra)` from `send_dispatcher_service.template_values`. Nothing is recorded by a counter with `dispatch_telemetry=False`, nor while the recorder is not running. A joined camera's results go into its product's `details.stations` (with `reject_camera_id`); joined cameras write no rows of their own.
- **Counters survive a restart**: `counts_reset_at` (ISO UTC) is kept **inside each line's entry in `state["lines"]`, Line 1 included** (not at the top level). `normalize_line` takes it (and `counts_reset_classes`) only from the saved line, never from a client; `save_line` sets it for a new line; `SettingsPersistenceService.mark_counts_reset(line_id, classes=None)` sets it on a reset (`reset_line_counters`, so the dashboard, the API and Sparkplug) and records single-class resets in `counts_reset_classes`. `_upgrade_to_v8` sets it to the upgrade time (the name rule and the TCP settings of that step are kept). At startup (`main.py` step 4a, after `restore_on_startup`) `restore_line_counts()` rebuilds each line's counter and each own station's from the records (`counted_totals`), through the new `CountingService.restore(totals)`.
- **API** `app/routes/v1/records.py`: `GET /api/v1/records`, `/records/summary`, `/records/export?format=csv|xlsx&tz=…` with the filters of the plan; Operator level; API key scope `records:read` (GET only). The export code is in `app/services/records_export.py`. CSV: UTF-8 with a BOM, streamed; XLSX: `zipfile` + XML into a `SpooledTemporaryFile` (8 MB in memory, then disk), Records sheet (inline strings, numbers, booleans, dates as serial numbers with a `yyyy-mm-dd hh:mm:ss` style) and Summary sheet; 413 above 1,048,575 rows. `tzdata` added to `requirements.txt` for Windows.
- **Dashboard**: *Production records* under *Run* in the sidebar (every signed-in user), built in `assets/production_lines.js` (`buildRecordsPage`, `refreshRecords`, `exportRecords`). The `records:read` checkbox is on the API key form in `dashboard.html`.
- **Docs**: README "Production records and export", `.env.example`, `API_KEY_QUICKSTART.md`, `FILE_TREE.md`, `PROJECT_DESCRIPTION.md`.
- **Checks**: `pytest` **780 passed** (27 new in `tests/test_production_records.py`; `test_version_8_keeps_the_name_rule_on_every_existing_vision_camera` now drops `counts_reset_at`), `ruff check .` clean. Live run from a temporary copy with a fake MJPEG network camera (a square moving down the belt, every third one red) and a stand-in model that finds it: the line counted 40 products (27 good, 13 rejected); records, summary, CSV and XLSX for the last hour agreed with the counter; the .xlsx opened in openpyxl, pandas and LibreOffice (dates read as dates); after a stop and restart the counters were 40 / 27 / 13 again; after **Reset counts** and a restart only the 4 products counted since the reset came back. The page was checked with Playwright at 1440 and 375 px (no page errors, no horizontal scroll; the Result filter and both export buttons work). Screenshots: [`screenshots/section-4/`](screenshots/section-4/).

## Goal

1. Every product (good or reject) and every code scan is **stored in the database**.
2. A **Production records** page shows them for a chosen start and end time, with filters and totals.
3. They can be **exported to CSV or Excel (.xlsx)** for that time range.
4. The line counters **survive a server restart**.

## What exists today

- Counters are in memory only (`CountingService`) and start from 0 after a restart.
- `DetectionLog` (`app/db/models/detection.py`) is written only by the detect API calls, not by the production line.
- **Where products and codes are decided** (the hooks for the recorder):
  - `CountingService.finish_crossing()` (`app/services/counting_service.py`): every product a vision camera counted, with its final result, reason and paired code (`fields`). Own-station cameras use it too, through their aux counter (`self.camera_id` is set, so `counting_camera` is False).
  - `LineRuntime._emit_code_product()` (`app/services/line_service.py`): a product on a line with only a code reader that checks codes.
  - `LineRuntime._emit_qr()` and `_emit_no_read()`: code reads or no-reads that are **not** part of a product (report-only readers, unpaired codes).
  - Section 5 adds PLC-sensor products and trigger inspections. They should go through `finish_crossing`/`count_product` and so be recorded too.
- **Database:**
  - SQLite through SQLAlchemy async (`app/db/session.py`: `AsyncSessionLocal`). WAL mode.
  - Migrations run at startup (`main.py` lifespan).
  - **Revision 0001 builds tables from the current models**, so a new table must be listed in `_LATER_TABLES` in `alembic/versions/0001_initial_schema.py` and created by its own revision. See `0005_product_lists.py` for the style: inspect before creating, so it is safe to re-run. Import the new model in `alembic/env.py`.
- CSV safety: `_csv_safe` / `_csv_unescape` in `app/services/product_service.py`.
- API key scopes: `app/security/api_key_scopes.py` (`SCOPES` dict and the path → scope rules).
- Dashboard pages are added by `production_lines.js` with `ensurePage(id, title, html)` and the sidebar; follow how the Products page is added (`buildProductsPage` / `refreshProductsPage` or similar; `grep -n "tabProducts" assets/production_lines.js`).

## What to build

1. **Model** `app/db/models/production_record.py`, table `product_records`:
   - **Columns:**
     - `id` (Integer, autoincrement primary key; not a UUID: the table grows fast);
     - `recorded_at` (DateTime with timezone, UTC, indexed);
     - `line_id` String(48), `line_name` String(64);
     - `camera_id` String(64), `camera_name` String(128);
     - `kind` String(16): `"product"` or `"code"`;
     - `counted` Boolean: True when it is in the line totals; False for own-station products and code rows;
     - `result` String(8): `"good"` / `"reject"` / NULL for code rows;
     - `reject_reason` String(32), `class_name` String(128), `confidence` Float, `track_id` Integer;
     - `code` String(512), `code_format` String(32), `code_status` String(16) (`known` / `unknown` / `no_read`), `product_name` String(128), `product_list_id` String(36);
     - `batch` String(64): the payload's `batch`, which Section 5 fills (the `{batch}` placeholder already reads that key); NULL until then;
     - `details` JSON: `vision_result`, `stations` (Section 3), `bbox`, `reject_camera_id`.
   - **Indexes:** `(line_id, recorded_at)`, `(recorded_at)`, `(batch)`, `(code)`.
   - **Migration** `alembic/versions/0006_product_records.py`.
2. **Recorder** `app/services/production_records_service.py` (`ProductionRecorder`):
   - `record(row: dict)` is thread-safe and **never blocks**: the inference threads call it. Use a `collections.deque` with a lock; the cap comes from `RECORDS_QUEUE_LIMIT` (default 100000).
   - When full, drop the oldest rows, count them, and raise an alarm `records.dropped` (add it to `ALARM_CATALOG`: scope `server`, `warning`). It clears after a successful flush with nothing dropped.
   - A background task on the **main** loop (started in `main.py`'s lifespan after migrations, like the other services) flushes every 1 s, or as soon as 500 rows wait. It does **one** `insert(ProductRecord)` with a list of dicts in one transaction.
   - On a database error, keep the rows (up to the cap), retry on the next tick, and raise `records.write_failed` (critical) after 3 failed tries in a row; clear it on success.
   - On shutdown, flush what is left, in the lifespan, before the database is closed.
   - **Retention:** a new `RECORDS_RETENTION_DAYS` in `app/config.py` and `.env.example` (default 90; 0 = keep for ever). Once an hour, delete older rows in chunks of 5000, so the database is never locked for long.
3. **Hooks** (one call each; build the row from the payload / read already at hand). The payloads name the code fields differently: a vision product's code comes in `fields` as `qr_code` / `qr_format` / `qr_status`; a code read, a no-read and a code-only product carry `code` / `format` / `qr_status`; none of them carries `camera_name`. `send_dispatcher_service.template_values(payload)` (Section 1) already maps all of them to `code`, `code_format`, `code_status`, `camera_name` (looked up in `app_state.cameras`) and `batch`, the names this table uses. The hooks:
   - `CountingService.finish_crossing`: `kind="product"`, `counted = self.camera_id is None` (own station = False), result, reason, class, confidence, track, code fields from `fields`, and `details`.
   - `LineRuntime._emit_code_product`: `kind="product"`, `counted=True`, from the read.
   - `LineRuntime._emit_qr` / `_emit_no_read`: `kind="code"`, `counted=False`.
   - Respect `dispatch_telemetry=False` counters (tests) by not recording them, or record only when the recorder is started. Then the existing tests do not need a database writer.
4. **Counts survive a restart.**
   - Each line saves `counts_reset_at` (an ISO UTC time) in its settings entry (Line 1: top level, like its other v1 keys, or inside its line entry; pick one and document it).
   - Set it when the counters are reset (`reset_line_counters` in `app/routes/v1/counting.py`, which Sparkplug also uses), and in the v8 upgrade step to the upgrade time, so existing lines do not suddenly show old records. That step is `line_config._upgrade_to_v8`. It already sets `name_based_defects` on vision cameras and sets every TCP channel in `communication_endpoints` to newline, client mode and `keep_open: false`: add to it and keep both. `tests/test_logic_fixes.py::test_version_8_keeps_the_name_rule_on_every_existing_vision_camera` compares `state["lines"]` with a copy taken before the upgrade, so that test must drop `counts_reset_at` too.
   - At startup, after `line_manager.apply_state()`, each line's counter gets its `total_inspected`, `good_count`, `rejected_count`, `counts_by_class` and `rejects_by_class` from `SELECT … WHERE line_id=? AND counted AND kind='product' AND recorded_at >= counts_reset_at GROUP BY class_name, result`. Add a `CountingService.restore(totals)` method.
5. **API** `app/routes/v1/records.py`, registered like the other v1 routers in `main.py`:
   - `GET /api/v1/records`:
     - filters: `line_id` (empty = all lines), `start`, `end` (ISO with timezone; both required, and at most 366 days apart), `result`, `kind`, `batch`, `code` (contains), `camera_id`, `counted`;
     - `limit` (≤ 1000) / `offset`; newest first.
     - Returns `{total, rows}`. Operator level.
   - `GET /api/v1/records/summary`: same filters, `bucket=hour|day`. Returns totals (total, good, reject, yield), by class, by reject reason, by camera, and per bucket.
   - `GET /api/v1/records/export?format=csv|xlsx&…filters…&tz=Area/City`:
     - **CSV:** a `StreamingResponse` that reads in chunks by `id` (keyset pagination, 5000 rows), so memory stays flat. Header row; times as local time in `tz` **and** a UTC column; `_csv_safe` on text cells; `Content-Disposition` with a name like `records_<line>_<from>_<to>.csv`.
     - **XLSX:** built with `zipfile` and XML (no new library), written to a `tempfile.SpooledTemporaryFile` and streamed. A *Records* sheet (inline strings, numbers as numbers, dates as Excel serial numbers with a date-time style) and a *Summary* sheet (the summary endpoint's numbers). Refuse more than 1,048,575 rows with 413 and a message to use CSV or a shorter range.
     - `tz`: `zoneinfo.ZoneInfo`, falling back to UTC with a header note when unknown. Add `tzdata; platform_system == "Windows"` to `requirements.txt` (Windows has no system time zone database).
   - **Scope:** add `records:read` to `SCOPES` and map `/api/v1/records` to it in `required_scopes()` (GET only).
6. **Dashboard page "Production records"** (sidebar under *Run*, built in `production_lines.js`):
   - Line select: the current line, or *All lines*.
   - **From / To** `datetime-local` boxes (browser local time, sent as UTC ISO), with quick buttons: Last hour, This shift (last 8 h), Today, Yesterday, Last 7 days.
   - Filters: result, kind, camera, batch, code.
   - Summary tiles: total, good, rejected, yield for the range, plus a small per-hour bar list in the same style as "Rejects by type".
   - A table with paging (50 rows): time, line, camera, result, reason, class, code, product, batch.
   - **Export CSV** and **Export Excel** buttons. They download with the user's token, using `fetch` + blob + an `<a download>`, because the API needs the Authorization header. Pass `tz=Intl.DateTimeFormat().resolvedOptions().timeZone`.
   - Phone width: the table becomes cards, as other tables do (`table-mobile-cards`).
7. **Docs:** `README.md` gets a new "Production records and export" part, with retention and the API. `.env.example` gets `RECORDS_RETENTION_DAYS` (and `RECORDS_QUEUE_LIMIT`). `API_KEY_QUICKSTART.md` gets the `records:read` scope. Update `FILE_TREE.md`.

## Tests to add (`tests/test_production_records.py`)

- A counted product, an own-station product, a code-only product, an unpaired code and a no-read each give one row with the right `kind`, `counted`, `result` and code fields.
- The writer under load: 5000 `record()` calls from two threads all arrive, in few transactions. Queue overflow drops the oldest and raises then clears `records.dropped`. A database error keeps the rows and retries.
- Retention deletes only rows older than the limit.
- Totals restored after a "restart" (a new `CountingService` + `restore`) equal the totals before, and nothing from before `counts_reset_at` is counted.
- `GET /records` filters (time range, line, result, batch, code) and paging. `summary` numbers.
- CSV export: header, local times in `tz`, formula-safe cells, a large range streamed (memory stays bounded; check that chunking is used).
- XLSX export: open the bytes with `zipfile`, check the parts and the XML of both sheets, a date cell's serial value, and the row-limit refusal.
- Migration: a database at revision 0005 upgrades to 0006, and a new database builds; follow the existing database upgrade tests in `tests/test_product_lists.py`.
- The `records:read` scope is required for API keys.

## Acceptance

- `pytest` and `ruff check .` are green.
- Run the server from a temporary copy of the repository with a fake MJPEG camera. Count some products, export CSV and XLSX for the last hour, and open them (for example with Python's `csv` / `zipfile`). Restart and check the counters are kept.
- Screenshots of the Records page at 1440 px and 375 px.

## Notes for the next section

- **Start from** `claude/optimistic-carson-s5ko0o` (or `main` once it is merged; see README.md).
- **Filling `batch` (Section 5)**: put the line's current batch number into the event payload as `batch` (the key `{batch}` already reads). `build_row` takes `batch` from `template_values(payload)["batch"]`, so a product counted through `finish_crossing` and the code rows of `_emit_qr` / `_emit_no_read` get it with no change here. The simplest place is where the payload is built: `CountingService.finish_crossing` (`payload["batch"] = …` before `record_event`) and `LineRuntime._qr_events` / `_emit_no_read`. The column is `String(64)`, indexed, and the Records page and API already filter on it (exact match).
- **New product sources** (PLC-sensor products, trigger inspections): count them through `finish_crossing` (with a crossing dict) and they are recorded with nothing else to do. A product counted through `count_product` is **not** recorded by itself (it has no payload): build its payload and call `production_records_service.record_event(payload, "product", counted=True, class_name=<the name passed to count_product>)`, as `LineRuntime._emit_code_product` does. Use the same name for `class_name` as for the count, or the restored counters will not match the live ones. Check `counter.dispatch_telemetry` first (see `LineRuntime._record`).
- **Restored counters**: the restore only uses rows with `kind="product"` and `counted=True` (line totals) or `counted=False` with the camera's id (own stations). A new product source that is not in the line totals must set `counted=False`.
- **Export paging** is a keyset on `(recorded_at, id)`, not on `id` alone: with a line picked, SQLite would otherwise sort the whole range again for every chunk (checked with `EXPLAIN QUERY PLAN`).
- **Known limits**:
  - `recorded_at` is when the product was counted (after Sync or a joined camera's wait), not when it crossed; the difference is the Sync window / match window at most.
  - Hour buckets are grouped by the UTC hour and then placed in the browser's zone; in a zone with a half-hour offset (India) a bucket starts at :30 local time.
  - The summary and the restore read the whole range with `GROUP BY`; that is fast on the indexes for a few million rows. A plant that keeps a year of a fast line may want a daily totals table later.
  - Products per minute, the QR statistics of the Line dashboard (`qr_stats`) and a joined camera's matched / unmatched counts still start from 0 after a restart.
- **Found, not fixed**: nothing new outside this section. (The `class_index` bug noted by Section 3 was fixed on `main` before this section.)
- **Test count** after this section: 780.
