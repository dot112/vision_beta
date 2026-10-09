# Section 2: Camera cards on Line setup, up to 8 cameras per line

Read [README.md](README.md) first: it has the goal, the owner's decisions, the project map and the rules.

**Depends on Section 1.** Start from the branch Section 1 pushed; it is in the table in README.md.

## Status

**Done** on branch `claude/magical-ritchie-r7rxvs` (started from `main` after Section 1 was merged).

- **Backend**
  - `MAX_CAMERAS_PER_LINE = 8`; `routes/v1/lines.py` `_check_camera_count` refuses a line with more cameras than `MAX_CONNECTED_CAMERAS`.
  - New camera fields `confidence` (0.05 to 0.99, stored only when set) and `station` (`own` / `join`, stored only when sent, dropped from the counting camera by `normalize_line`), both in `CAMERA_KEPT_KEYS`. `line_config.camera_station(cam)` gives `None` (counting camera, code reader) or `own` / `join`.
  - `confidence` reaches the model: `ContinuousVisionRunner._loop` passes `conf_thresh=runtime.confidence_for(camera_id)` to `submit_frame_if_idle`.
  - `_upgrade_to_v8` calls `copy_count_lines(camera, trigger)` for every vision camera: the line's `line1_position`, `line2_position`, `orientation` (and `direction` and `tracking` when the line has them; the runtime read those from the line too) go into each camera that has none of its own. The line keeps its values. Counting is unchanged: `counting_config_from_dict` reads the camera first and the line second, and the tests compare the configs before and after.
  - A second counting camera is refused with "Only one vision camera can be the counting camera (Camera 1 and Camera 3 are both set to Counting). Set the other one to Inspection station."
  - Every message of `PUT/POST /lines` and every line warning names the camera by position and name: `Camera 2 ('Label side'): ...` (`_name_cameras` in `routes/v1/lines.py`, names from the `cameras` table, else the driver).
  - Renaming a camera (`PATCH /cameras/{id}` with `name`) renames its connected driver too, so `{camera_name}` in messages follows.
  - The line summary's camera rows carry `station` and, for each non-counting vision camera, its own `counts` (for the camera strip).
- **Line setup UI** (`assets/production_lines.js`): the line section (name, min frames/s, yield target, Sync, Save, Start/Stop, **Reset counts…**), then a grid of camera cards (`repeat(auto-fill, minmax(360px, 1fr))`, one column under 520 px) and the **Add camera** card (free cameras, owned ones greyed out with their line; **Scan USB**; **Add network camera** = `POST /system/endpoints` protocol `ipcam`; starting job). Card header: name, job badge, connected dot, live picture (annotated snapshot about once a second, only while Line setup is shown and the tab is visible), move up/down, remove. Folds (remembered per camera in `localStorage`, key `pl_card_folds::<camera_id>`): Camera (device, name, Connect/Disconnect), Job, AI model and classes (+ confidence, + name rule switch, sent `false` for new cards), Counting (flow direction, count mode, count lines A/B as numbers and as draggable lines on a canvas over a still picture of the camera, Advanced tracking with the defaults as placeholders), Codes, Image, Video. One **Save**: `PUT /lines/{id}`, then `PATCH /cameras/{id}` for each camera whose image/video settings or name changed. Unsaved changes: shown next to the line state, kept when leaving and coming back to the page, asked about when switching lines or starting/stopping, and `beforeunload`.
  - A fifth job, **Inspection station + code reader**, exists so a saved non-counting vision camera with `read_codes` keeps it. The four starting jobs are as asked.
  - Settings that the card sends empty go back to their default (`confidence: ""`, `tracking: {}`); count lines, orientation and direction are always sent.
- **Image and Video settings** moved into `assets/camera_settings.js` (`CameraSettingsForm.render(container, cam, parts)` and `.read(root, type)`, the same fields and PATCH body as the old `saveCamSettings()`). The Cameras page dialog uses it too. The dashboard's custom dropdowns now honour `disabled` options.
- **Removed**: the **Counting and rejects** card (`#countingSetupCard`), `onWirelineModeChange`, `updateWirelineModeUI`, `saveActionTriggerConfig`, the `cfgLine1/2` and `_unsavedLineSetupFields` handling in `loadServerSettings()` and at load, and the `action_trigger_cfg` browser cache.
- **Cameras page**: **Settings** of a camera on a line opens Line setup for that line with its card scrolled to and its Camera and Image folds open (`window.openCamSettings` is wrapped); a camera on no line keeps the dialog.
- **PLC and send card dialogs**: `cardCameraOptionsHtml` already lists every camera of the line (`ProductionLines.lineCameras()`); nothing assumed two.
- **Line dashboard**: the large feed (the dashboard's own MJPEG reader) shows the counting camera or the tile clicked; the other cameras are tiles in a strip beside it (two columns from 3 tiles; below it, two per row, on a phone) with their job, station counts, and for code readers the Last picture / Live switch and Test picture. Tiles use the snapshot loop (at most 2 per second each, 6 in all). `ProductionLines.feedCameraId()` tells `dashboard.html` which camera is large.
- **Docs**: `README.md` (Production lines: step 2 rewritten for the cards; the "Counting per vision camera" paragraph is now the card's Counting section; the Line dashboard paragraph; the API camera fields), `PROJECT_DESCRIPTION.md`, `FILE_TREE.md`.
- **Tests**: `tests/test_camera_cards.py` (13 tests: 3 and 8 cameras saved and running with one counter per vision camera, 9 refused, more than `MAX_CONNECTED_CAMERAS` refused, each camera counting with its own lines and direction, empty values going back to the default, confidence/station validation, the second counting camera message, camera names in messages, fields kept when a client leaves them out, confidence reaching `predict_mat` through the runner, the v8 copy for Line 1 and another line plus a version 7 file loaded and counting as before, driver rename, summary counts). Also extended `test_a_client_that_does_not_send_the_new_settings_leaves_them_as_saved`. Existing tests changed only where the behaviour was meant to change: `test_line_validation` (9 cameras, not 3), the v1 upgrade's expected Line 1 camera (now has its count lines), the fake inference worker's signature in `test_lines.py` (it takes `conf_thresh` like the real one), and `FakeInferenceEngine` records `conf_thresholds`.
- **Checks**: `pytest` 716 passed, `ruff check .` clean. UI checked with Playwright against a server run from a temporary copy with four fake MJPEG network cameras: add three cards, change a flow direction, drag count line A, set a confidence, save, reload, values kept; no horizontal scroll at 375 px; strip tile click; Cameras page Settings; one-counting-camera rule. Screenshots: [`screenshots/section-2/`](screenshots/section-2/) (Line setup and Line dashboard at 1440 and 375 px).

## Goal

On **Line setup** the user adds cameras as **cards**. Each card holds every setting of that camera: which device, its job, the AI model and classes, counting, code reading, the image (resolution, rotation, ROI and so on), and the video stream. A line can have **up to 8 cameras**. Nothing has to be set on another page.

## What exists today

- **Backend:**
  - `line_config.MAX_CAMERAS_PER_LINE = 2`.
  - `normalize_camera()` takes `camera_id`, `role` (`vision`/`qr`), `counting`, `qr_hold_ms`, `read_codes`, code settings (`qr_trigger`, `qr_trigger_delay_ms`, `code_type`, `product_list_id`, `qr_action`, `qr_no_read`), vision settings (`model_id`, `expected_classes`, `defect_classes`, `line1_position`, `line2_position`, `orientation`), and since Section 1 `direction`, `tracking{}` and `name_based_defects`.
  - Exactly one vision camera is the **counting** camera (`counting: true`); `normalize_line` makes the first vision camera the counting camera when none is marked.
  - Other vision cameras get their own counter (`LineRuntime.aux_counters`, a dict, so it already works for any number) and fire only cards that name them.
- **Line setup UI** (`assets/production_lines.js`): `renderLineSetup()` draws a "Line setup" card with two fixed boxes (`cameraBoxHtml(0)` and `cameraBoxHtml(1)`, ids `plCam0`, `plRole0`, `plCamModel0` and so on). `setupBoxes()` and `syncLineSetup()` loop over `[0, 1]`; `saveLineSetup()` builds `cameras[]` and does `PUT /api/v1/lines/{id}`. The model and class pickers are `renderClassLists`, `renderModelState` and `onCameraModelChange`.
- **Count lines** are a separate card, **Counting and rejects** (`#countingSetupCard` in `dashboard.html`: `selectWirelineMode`, `cfgLine1`, `cfgLine2`, saved by `saveActionTriggerConfig()` into the line's `action_trigger` through the fetch wrapper in `production_lines.js`). It holds the **Reset counts** danger zone too.
- **Device settings** (resolution, FPS, JPEG quality, max stream width, rotation, flips, ROI with a drawn preview, USB image controls, IP transport and buffer) are in a dialog on the Cameras page:
  - `#camSettingsOverlay`, `openCamSettings()`, `saveCamSettings()` (it does `PATCH /api/v1/cameras/{id}` with `{settings: {...}}`);
  - the ROI helpers `loadROIPreviewPicture`, `readROIFields`, `updateROIPreview`, `setROIFields`, `resetROI`, and the drag-to-draw code nearby.
- **Cameras page:** a table with Connect / Settings / Delete buttons (`addCameraRowActions()` at the end of `dashboard.html`), **Scan for cameras** (`scanHardwareCameras` → `GET /api/v1/cameras/discover/usb`, which saves new USB devices), and the auto-connect switch. Network cameras are added under **Connections** as protocol `ipcam` (`POST /api/v1/system/endpoints` with `name`, `source` (rtsp/http URL), `transport`, `buffer_size`). They are copied into the `cameras` table by `CameraService.sync_ip_cameras`.
- **Line dashboard:** the main feed (counting camera) and **one** second feed: `secondCamera()`, `renderFeedRow()`, `ensureFeedRow()`, `openFeed()` in `production_lines.js`.

## What to build

### Backend

1. **Raise the camera limit:** `MAX_CAMERAS_PER_LINE = 8`. Also refuse a line with more cameras than `settings.MAX_CONNECTED_CAMERAS` (`app/config.py`; check in `routes/v1/lines.py` `_save`, with a clear message).
2. **New camera fields** (in `normalize_camera`, kept by `CAMERA_KEPT_KEYS` when a client leaves them out):
   - `confidence`: 0.05 to 0.99, or empty for the model's own threshold. Pass it to the inference worker: `ContinuousVisionRunner._loop` calls `worker.submit_frame_if_idle(mat, fid, copy=False)`. Give it the camera's `confidence` (look it up through `line_manager.route(camera_id)[0].camera_entry(camera_id)`, or keep it on the runtime for speed).
   - `station`: `"own"` (default for non-counting vision cameras) or `"join"`. **Store and validate it now; the join behavior is Section 3.** For now a `join` camera behaves like `own`, and the UI shows "Joins the product result: comes with the next update", or keep the option hidden until Section 3.
   - Section 3 fills in `join_offset_ms`, `join_window_ms` and `join_missing`.
   - `name` is **not** stored in the line: the camera's name is the database row's `Camera.name` (rename through `PATCH /cameras/{id}`). That `PATCH` does not change a connected driver's `name`, and the send cards' `{camera_name}` placeholder (Section 1, `send_dispatcher_service._camera_name`) reads `app_state.cameras[id].name`: on a rename, update the driver's `name` too.
3. **Upgrade (extend `_upgrade_to_v8`, do not add v9):** copy the line's `action_trigger` count lines (`line1_position`, `line2_position`, `orientation`) into each vision camera that has none. For Line 1 the trigger is at the top level of the state (`state["action_trigger"]`); for other lines it is `line["action_trigger"]`. A line behaves exactly as before. Keep the line-level values: Line 1's top-level `action_trigger` is what version 1 and old API clients (`POST /counting/config`, `GET /system/settings`) read. The step already sets `name_based_defects` on every vision camera in `state["lines"]` and pins the TCP channels in `communication_endpoints` (Section 1). Add the copy to its camera loop. Its tests must still pass: `test_version_8_keeps_the_name_rule_on_every_existing_vision_camera` in `tests/test_logic_fixes.py` (it compares the lines before and after the step) and `test_old_tcp_channels_keep_sending_what_they_sent` in `tests/test_messages_and_plc_values.py`.
4. **`POST /api/v1/counting/config`** changes a running counter until the line's settings change again (see `LineRuntime._set_config`). Leave it as is.
5. **Validation messages** name the camera by its position and its name where known.

### Line setup UI (the main work: `assets/production_lines.js`, styles in `assets/dashboard.css` or the `style` block at the top of `production_lines.js`)

Replace the fixed two boxes with:

- **Line section** (top): name, minimum frames/s, yield target, the Sync switch and window. The counting camera is no longer a separate select: it is the card whose job is "Counting". Add **Reset counts…** here (move the danger zone from `#countingSetupCard`), and Start/Stop.
- **Camera grid:** one card per camera, plus an **Add camera** card. At 1440 px the grid is `repeat(auto-fill, minmax(360px, 1fr))`; on a phone, one column.
- **Add camera** card:
  - pick a camera that belongs to no line (`GET /api/v1/cameras`; owned cameras are greyed out with their line's name);
  - **Scan USB** (`GET /api/v1/cameras/discover/usb`);
  - **Add network camera**: name + URL, which creates an `ipcam` endpoint with `POST /api/v1/system/endpoints` exactly as the Connections dialog does, then reloads the camera list;
  - then choose a **starting job**:
    - Counting (vision, `counting: true`);
    - Inspection station (vision, `station: own`);
    - Code reader (`role: qr`);
    - Counting + code reader (vision, `read_codes`).
  - The first vision camera added is the counting camera.
- **Camera card header:**
  - the camera name, a job badge and a connected dot (`state.detail.status.cameras[].connected`);
  - a small **live picture**: `GET /api/v1/vision/annotated/camera/{id}?max_width=320`, refreshed about once a second while Line setup is visible and the tab is not hidden. Stop the loop on other pages; look at the tile loop on the Plant overview, `runTileFeed`, for the pattern;
  - **move up / move down** (camera order = order in `cameras[]`), and **Remove** (with a confirm).
- **Folding sections in the card** (only the ones that apply to the job are shown; remember which were open per camera in `localStorage`):
  1. **Camera:** device select (change device), Connect / Disconnect (`POST /api/v1/cameras/{id}/connect|disconnect`), rename.
  2. **Job:**
     - Counting / Inspection station / Code reader / Counting + code reader;
     - for an inspection station, **Own station** or **Joins the product result** (Section 3);
     - exactly one Counting card per line with a vision camera: choosing Counting on one card turns the previous counting card into an Own station.
  3. **AI model and classes** (vision jobs): the model select with its load state, Products to count, Defects to reject (reuse `renderClassLists`, `renderModelState` and `onCameraModelChange`, made per card instead of per index), plus:
     - **Confidence threshold** (empty = the model's);
     - **Also reject classes whose name contains "defect", "scratch" or "broken"** (`name_based_defects`; on for upgraded cameras, **off for cards added now**: send `false` explicitly).
  4. **Counting** (vision jobs):
     - **Flow direction** as one choice: top → bottom, bottom → top, left → right, right → left. Save it as `orientation` + `direction`, with lines A < B: top→bottom = `horizontal`/`forward`, bottom→top = `horizontal`/`backward`, left→right = `vertical`/`forward`, right→left = `vertical`/`backward`.
     - **Count mode:** "A then B" (the direction above) or "Both ways" (`direction: both`).
     - **Count lines A and B**, as two number boxes **and** draggable lines on the card's picture (draw the two lines on a canvas over the picture; dragging updates the boxes).
     - **Advanced tracking:** min hits, missed frames, max speed px, match threshold, position tolerance, high/low confidence. Empty = default; show the defaults as placeholders (from `TRACKING_LIMITS` / `CountingConfig`). A key the client leaves out keeps its saved value (`CAMERA_KEPT_KEYS`, `_fill_camera_models`). So to go back to a default, send the key empty (`""` or `null`, and `tracking: {}`) instead of leaving it out. The same holds for the count lines, `orientation` and `direction`.
  5. **Codes** (code reader jobs): code type, when to read (continuous / one picture on wire line 1 or 2), picture delay, hold time, action, product list, when no code is read. Move this unchanged from `cameraBoxHtml`, with the same notes (`ACTION_NOTES`, `READER_ONLY_NOTE`).
  6. **Image:** width, height, FPS, rotation, flip H/V, **ROI** (enable switch, X/Y/W/H and drag-to-draw on the full picture `GET /api/v1/cameras/{id}/frame?full=true`), USB controls (brightness, contrast, saturation, exposure, auto exposure) for USB, transport and buffer for IP.
     - Move the code out of the Cameras-page dialog into functions that render into a given container; keep the dialog working for cameras that belong to no line.
     - The fields and the `PATCH` body are exactly those of `saveCamSettings()`.
  7. **Video:** max stream width, JPEG quality.
- **One Save button** for the whole page:
  1. `PUT /api/v1/lines/{id}` with the line fields and `cameras[]`;
  2. then `PATCH /api/v1/cameras/{id}` for every camera whose image or video settings changed.

  Show the server's `warning` / errors in a toast (as `saveLineSetup()` does). Warn before leaving with unsaved changes; the line selector already does this for cards, so extend the same check.
- **Remove the Counting and rejects card** (`#countingSetupCard` in `dashboard.html`) and the code only it uses (`onWirelineModeChange`, `updateWirelineModeUI`, the `cfgLine1/2` handling in `saveActionTriggerConfig()` and in `loadServerSettings()`). First check with `grep` what else calls them.
- **Cameras page:** for a camera that belongs to a line, **Settings** opens Line setup for that line with that camera's card open (`switchLine(lineId)`, `switchTab("tabActionTrigger")`, scroll to the card). A camera on no line keeps the old dialog.
- **PLC and send card dialogs:** the camera select (`cardCameraOptionsHtml` in `dashboard.html`) must list all of the line's cameras. Check that it does not assume two.

### Line dashboard

Replace the single second feed with a **camera strip**:
- the counting camera stays large;
- the other cameras are small tiles beside it (below it on a phone). Click one to show it large.
- QR readers keep their live / last-picture switch (`secondView`).
- Only the large camera uses the MJPEG stream; tiles use the snapshot loop at a low rate. Look at the bandwidth budget comment above `TILE_MAX_FPS`.

### Docs

`README.md`, "Production lines (version 2)" → "Setting up a line": rewrite step 2 for the cards, the up-to-8 limit, the per-camera counting settings, and the defect-name switch. Section 1 described the last two in the **Counting per vision camera** paragraph under step 3, which says "the dashboard does not show them yet". Move it into step 2 and drop that part. Update `PROJECT_DESCRIPTION.md` and `FILE_TREE.md` if files are added.

## Tests to add (`tests/test_camera_cards.py`)

- A line with 3, then 8 cameras saves and runs (frames routed, one counter per vision camera); 9 is refused; more than `MAX_CONNECTED_CAMERAS` is refused.
- Each vision camera counts with its **own** count lines and direction (two cameras, different flows).
- The confidence threshold reaches `predict_mat` (the fake engine in `conftest.py` records calls).
- **The v8 upgrade copies the line's count lines into its cameras.** A version 7 file loads, upgrades, and counts exactly as before, for Line 1 (top-level `action_trigger`) and for another line.
- A client that sends cameras without the new fields keeps them (extends `test_a_client_that_does_not_send_the_new_settings_leaves_them_as_saved` in `tests/test_logic_fixes.py`).
- Choosing a second counting camera is refused with a clear message (`normalize_line` already refuses two; check the message).
- **UI check with Playwright** (a scratch script, not a pytest test, unless you add a light one): add three cards, change a flow direction, drag a count line, save, reload, and see the values kept. Screenshots at 1440 px and 375 px.

## Acceptance

- `pytest` and `ruff check .` are green.
- An upgraded line counts exactly as before.
- Line setup has no camera setting left on another page.
- Screenshots of Line setup with 3 cards, and of the Line dashboard camera strip, at both widths.

## Notes for the next section

- **Start from** `claude/magical-ritchie-r7rxvs` (or `main` once it is merged; see README.md).
- **Where `station` is read.**
  - Settings: `line_config.STATIONS`, `camera_station(cam)` (`None` for the counting camera and code readers, else `cam.get("station") or "own"`). `normalize_camera` stores `station` only when sent; `normalize_line` drops it from the counting camera. Add `join_offset_ms`, `join_window_ms` and `join_missing` in `normalize_camera` and to `CAMERA_KEPT_KEYS` (and to the v8 step only if old files need a default; nothing has `join` yet).
  - Runtime: `LineRuntime.configure` makes an aux counter for **every** non-counting vision camera, whatever its station. That is the place to treat `join` cameras differently (their result merged into the counting camera's product). `LineManager.summary` already gives each camera row `station` and its own `counts`.
  - UI: the Job fold's `[data-f="station"]` select, the note in `syncCard` (`.pl-station-note`, now "comes with the next update"), `cardEntry` (sends `station` for non-counting vision cards), and the strip's `tileDetail` ("Joins the product result"). Add the join timing fields in `cameraCardHtml` (job fold) and `cardEntry`.
- **Confidence**: `LineRuntime.confidence_for(camera_id)`; the runner passes it as `conf_thresh`. The detect API calls do not use it (they take their own `conf_threshold`).
- **Count lines**: every vision camera now holds its own after the v8 step; new cards always send them. `counting_config_from_dict` still falls back to the line's `action_trigger` (Line 1: top level), which older API clients (`POST /system/settings`, version 1) still write: such a write no longer moves a camera that has its own lines. `POST /counting/config` is unchanged.
- **Playwright tips**: the page scrolls inside `.main-content`, so `fullPage` screenshots only show the window; grow the window to `.main-content.scrollHeight` instead. The custom dropdowns hide the native `<select>`: set `select.value` and dispatch `change`. New vision cards open their Camera, Job and AI model folds. Fake network cameras: an MJPEG server on 127.0.0.1 added as `ipcam` endpoints works with the real IP driver. The scratch script is not in the repository.
- **Test count** after this section: 716.
