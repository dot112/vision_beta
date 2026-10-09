# Flexible line control: work split into 5 sections

This folder hands the work over to new sessions: **one session per section**.
Each section file can be read on its own: it has what is done, what is left, the files to change, the tests to write, and the checks to run.
The first message to paste into each section's session is in [session-prompts.md](session-prompts.md).
Every session must keep its own section file up to date.

## The goal (from the owner)

Make the software work on almost any production line:

1. **Cameras as cards on Line setup.** You add cameras one by one, and each camera's card holds *all* of that camera's settings.
2. **Flexibility:**
   - up to 8 cameras per line;
   - count direction and tracking set per camera;
   - several vision cameras that either work on their own or join one product result;
   - sensor- or PLC-triggered inspection.
3. **Product records.** Every product and code scan is stored in the database and can be exported for a chosen start and end time.
4. **A checked and fixed logic** for messages, PLCs and vision.

### Decisions the owner made (do not ask again)

| Question | Answer |
|---|---|
| Several vision cameras on one line | **Both options per camera.** Up to 8 cameras per line. Each extra vision camera is either an *Own station* (counts and rejects on its own, as the second camera does today) or *Joins the product result* (its reject is merged into the counting camera's product, matched by travel time). |
| Read signals from the PLC | **Yes**: start/stop the line, product sensor trigger, reset counters, batch number, real reject confirmation, and user-defined alarms. |
| Export formats | **CSV and Excel (.xlsx)**, the Excel file with a summary sheet, built without a new library. |

## The sections

| # | Section | File | Depends on | Status | Branch with the work |
|---|---|---|---|---|---|
| 1 | Logic fixes (vision, messages, PLC outputs) | [section-1-logic-fixes.md](section-1-logic-fixes.md) | — | **Done** | `claude/tender-cerf-qzfcle` |
| 2 | Camera cards on Line setup, up to 8 cameras | [section-2-camera-cards.md](section-2-camera-cards.md) | 1 | **Done** | `claude/magical-ritchie-r7rxvs` |
| 3 | Several vision cameras: own station or joined result | [section-3-joined-cameras.md](section-3-joined-cameras.md) | 2 | **Done** | `claude/section-3-joined-cameras-mg2wnm` |
| 4 | Product records in the database, Records page, CSV/XLSX export | [section-4-product-records.md](section-4-product-records.md) | 1 (3 only for the `stations` detail) | **Done** | `claude/optimistic-carson-s5ko0o` |
| 5 | PLC inputs, batch number, trigger inspection, reject confirmation | [section-5-plc-inputs.md](section-5-plc-inputs.md) | 2 and 4 | Not started | — |

**Order:** run the sections one after another, 1 → 2 → 3 → 4 → 5. Each new session starts from the branch the previous section pushed, which is in the table above:

```bash
git fetch origin <branch of the previous section>
git checkout -B <your own session branch> origin/<branch of the previous section>
```

If the previous section's branch was already merged into `main` with "Squash and merge", start from `origin/main` instead: the squash puts the same changes into `main` as one new commit, and a branch that still carries the original commits conflicts with it.

Section 4 only needs Section 1. It *can* run beside 2 and 3, but it touches the same files (`line_service.py`, `counting_service.py`, `assets/production_lines.js`, `main.py`). Run it in parallel only if someone is ready to resolve the merge.

**When a section is finished:**
1. Set its row in the table to **Done** and fill in the branch.
2. Fill in the "Status" and "Notes for the next section" parts of its own file.
3. Commit and push.

The next session reads this table to know where to start.

## How the project works (short map)

FastAPI server (`main.py`) plus one HTML dashboard (`dashboard.html`, plus `assets/production_lines.js` and `assets/dashboard.css`, read from disk on every request).

| Area | Files | What it does |
|---|---|---|
| Line settings | `app/services/line_config.py` | Shape, validation and **upgrade steps** of the saved settings (`data/system_state.json`). `normalize_line`, `normalize_camera`, `normalize_send_card`, `product_verdict` / `product_result`, `join_settings`, `joined_cameras`, `_UPGRADES`, `SCHEMA_VERSION` (now **8**), `MAX_CAMERAS_PER_LINE` (**8**), `camera_station`. No runtime imports. |
| Settings storage | `app/services/settings_persistence_service.py` | Loads and saves the JSON state, lines, endpoints (Connections), PLC and send cards, and the audit log. `counting_config_from_dict()` builds a camera's `CountingConfig`. **Line 1 keeps `action_trigger`, `plc_actions` and `send_actions` at the top level of the state** (version 1 compatibility); other lines keep them inside their entry. |
| Lines at runtime | `app/services/line_service.py` | `LineManager` (`line_manager`) and one `LineRuntime` per line. Routes camera frames to lines, holds the counters (Line 1 uses the module-level `counting_service`), Sync pairing (`SyncPairer`), joined cameras (`ProductAssembly`: one result per product from the counting camera, its code and the joined cameras), QR reads, and the models per camera. |
| Counting | `app/services/counting_service.py`, `app/engines/tracker.py` | `CountingService.process_frame` → `WirelineTracker.update` → crossing events. `finish_crossing()` counts one product with its final result and sends **one** event (event bus, PLC cards, send cards). `count_product()` is for products with no vision camera. |
| Vision pipeline | `app/services/vision_service.py`, `app/engines/inference_engine.py` | Per-camera inference worker threads, the stream publisher (draws boxes and count lines), `ContinuousVisionRunner` (feeds every connected camera of a running line), and the detect API. |
| Cameras | `app/services/camera_service.py`, `app/hardware/camera/*`, `app/routes/v1/camera.py` | Camera database rows (`Camera.settings` JSON holds resolution, rotation, ROI and so on), drivers, connect/reconnect. |
| Code reading | `app/services/qr_service.py`, `app/engines/qr_engine.py` | `QRReaderPipeline.get_worker(cam).request_capture(...)` takes one picture (triggered reading). |
| Cards | `app/services/card_triggers.py` | `card_fires(card, event)` is the one check both PLC cards and send cards use. |
| PLC | `app/services/plc_dispatcher_service.py`, `app/hardware/plc/*`, `app/services/plc_failsafe_service.py` | PLC action cards → `PLCDriverFactory.get_driver(endpoint)` → `execute_operation`. Per-endpoint `asyncio.Lock` in `PLCDispatcherService._endpoint_locks`. |
| Messages | `app/services/send_dispatcher_service.py` | Send cards → `deliver(endpoint, message, topic)` on the telemetry dispatcher thread (`counting_service._telemetry_dispatcher`). |
| Lines API | `app/routes/v1/lines.py` | `PUT /lines/{id}` (`_save` → `SettingsPersistenceService.save_line`), `apply_line()`, `set_line_running()`, `/start`, `/stop`, `/clone`. |
| Product records | `app/services/production_records_service.py`, `app/services/records_export.py`, `app/routes/v1/records.py` | `production_recorder.record()` / `record_event(payload, kind, counted)` queue one row per product or code read (never blocks); written in batches to `product_records`. Filters, summary, `restore_line_counts()` (counters after a restart, from each line's `counts_reset_at`), CSV/XLSX export. |
| Alarms | `app/events/alarm_events.py` | `alarm_manager.raise_alarm/clear_alarm`, and `ALARM_CATALOG` (add new codes there). |
| DB | `app/db/models/*`, `alembic/versions/*` | SQLite (async). Revision 0001 builds tables from the models, so **a new table must be added to `_LATER_TABLES` in `0001_initial_schema.py`** and created by its own revision (see 0004/0005/0006). Also import the model in `alembic/env.py`. |
| Dashboard | `dashboard.html`, `assets/production_lines.js`, `assets/camera_settings.js` | `production_lines.js` adds the line selector, the Plant overview, Lines, Products and Production records pages, Line setup (the line section and one card per camera, up to 8, with every camera setting; Section 2) and the Line dashboard's camera strip. `camera_settings.js` is a camera's image/video form (card Image and Video folds, and the Cameras page dialog). Its `window.fetch` wrapper adds `?line_id=` to `/api/v1/counting/`, `/api/v1/plc/actions` and `/api/v1/send/actions` calls while a line other than Line 1 is selected. |

## Rules for working in this repository

- **Never break an existing line.**
  - A change to the saved settings' shape needs an upgrade step in `line_config.py` (append to `_UPGRADES`, raise `SCHEMA_VERSION`) and a default so older files load.
  - An existing line must behave exactly as before until someone changes it.
  - A client that does not send a new field must not wipe it: add the field to `CAMERA_KEPT_KEYS` (cameras), or keep it in `save_line`.
- **Steps before release can be extended.** Nothing has been released since version 7. Sections may add to the version 8 step (`_upgrade_to_v8`) instead of adding version 9, as long as every section's tests still pass.
- **Never start the server from the repository folder**: `data/` and `logs/` may hold real data. Copy the tree to a temporary folder and run it there.
- **Line endings:** this checkout uses LF (no CRLF). Some files start with a UTF-8 BOM (for example `app/db/models/camera.py`); keep it.
- **Tests:** `tests/conftest.py` gives a temporary database and settings file, fake cameras (`fake_camera` fixture), a fake inference engine and fake PLCs. No hardware is needed.
- **Set up and run the checks:**
  ```bash
  python3 -m venv /tmp/venv && /tmp/venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
  /tmp/venv/bin/python -m pytest -q -p no:cacheprovider     # 780 pass after Section 4
  /tmp/venv/bin/ruff check .
  ```
  CI (`.github/workflows/ci.yml`) runs lint, the tests, and a Docker Compose start/stop check on every push.
- **Screenshots:** Playwright's Chromium is pre-installed (`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`). Check any dashboard change at 1440 px and at 375 px wide.
- **Commits:** end every commit message with the attribution lines the session gives you. Never put a model name in the repository.
- **Docs:** each section updates the parts of `README.md`, `FILE_TREE.md`, `PROJECT_DESCRIPTION.md` and `API_KEY_QUICKSTART.md` that its change touches. `FUTURE_FIXES.md` is an older handoff (written on Windows); its rules about `backup/scratch-tools` do not apply here because that folder is not in the repository.

## Problems found when the logic was checked

| # | Where | Problem | Section |
|---|---|---|---|
| V1 | `tracker.py` | A class named like "defect", "scratch" or "broken" was always rejected, even when not ticked. | 1, **done** |
| V2 | `vision_service.py`, `camera_service.camera_orientation` | `int(rotation or 90)` read "No rotation" (0) as 90°, and the exit edge was taken from the rotation, not from the flow. | 1, **done** |
| V3 | `counting_config_from_dict` | Count direction and tracking settings were never read from the settings. Also, "both" mode never counted a product moving from line B back to line A. | 1, **done** (UI: Section 2, **done**) |
| V4 | `VisionService.detect_live_camera`, `/control/trigger/{camera}` | `passed` used the name rule; the trigger endpoint does not count anything. | 1 **done** (passed); trigger: Section 5 |
| V5 | `CountingService` | The speed figure was read without a lock; a partial reset broke good + rejected = total. | 1, **done** |
| M1 | `send_dispatcher_service.deliver` (TCP) | The TCP channel's delimiter, timeout and mode (client/server) are ignored: always JSON + `\n`, a new connection per message, a 2 s timeout. | 1, **done** |
| M2 | Send cards | JSON only, no text format. | 1, **done** |
| P1 | PLC card "Wait for PLC ACK / Reply" | Does nothing but change the status text. | 1 (removed, **done**), 5 (real confirmation) |
| P2 | PLC cards | WRITE only writes a fixed number; no data + strobe in one card. | 1, **done** |
