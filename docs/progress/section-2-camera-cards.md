# Section 2: Camera cards on Line setup, up to 8 cameras per line

Read [README.md](README.md) first: it has the goal, the owner's decisions, the project map and the rules.

**Depends on Section 1.** Start from the branch Section 1 pushed; it is in the table in README.md.

## Status

Not started.

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
   - `name` is **not** stored in the line: the camera's name is the database row's `Camera.name` (rename through `PATCH /cameras/{id}`).
3. **Upgrade (extend `_upgrade_to_v8`, do not add v9):** copy the line's `action_trigger` count lines (`line1_position`, `line2_position`, `orientation`) into each vision camera that has none. For Line 1 the trigger is at the top level of the state (`state["action_trigger"]`); for other lines it is `line["action_trigger"]`. A line behaves exactly as before. Keep the line-level values: Line 1's top-level `action_trigger` is what version 1 and old API clients (`POST /counting/config`, `GET /system/settings`) read.
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
     - **Advanced tracking:** min hits, missed frames, max speed px, match threshold, position tolerance, high/low confidence. Empty = default; show the defaults as placeholders (from `TRACKING_LIMITS` / `CountingConfig`).
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

`README.md`, "Production lines (version 2)" → "Setting up a line": rewrite step 2 for the cards, the up-to-8 limit, the per-camera counting settings, and the defect-name switch. Update `PROJECT_DESCRIPTION.md` and `FILE_TREE.md` if files are added.

## Tests to add (`tests/test_camera_cards.py`)

- A line with 3, then 8 cameras saves and runs (frames routed, one counter per vision camera); 9 is refused; more than `MAX_CONNECTED_CAMERAS` is refused.
- Each vision camera counts with its **own** count lines and direction (two cameras, different flows).
- The confidence threshold reaches `predict_mat` (the fake engine in `conftest.py` records calls).
- **The v8 upgrade copies the line's count lines into its cameras.** A version 7 file loads, upgrades, and counts exactly as before, for Line 1 (top-level `action_trigger`) and for another line.
- A client that sends cameras without the new fields keeps them (extends the Section 1 test).
- Choosing a second counting camera is refused with a clear message (`normalize_line` already refuses two; check the message).
- **UI check with Playwright** (a scratch script, not a pytest test, unless you add a light one): add three cards, change a flow direction, drag a count line, save, reload, and see the values kept. Screenshots at 1440 px and 375 px.

## Acceptance

- `pytest` and `ruff check .` are green.
- An upgraded line counts exactly as before.
- Line setup has no camera setting left on another page.
- Screenshots of Line setup with 3 cards, and of the Line dashboard camera strip, at both widths.

## Notes for the next section

(Fill in when done: branch, commits, anything Section 3 must know, for example where `station` is read.)
