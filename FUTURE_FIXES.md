# Future fixes and adjustments

Written 2026-10-02, last updated 2026-10-06, as a handoff for a new session.

**Status:** all seven requested changes are done: sections 7, 6, 1, 4, 5 and 3
on 2026-10-05, and section 2 on 2026-10-06, when the open checks were also
gone through. What is left is under "Still to check" (a run on Linux CI, a
trial on the real line, the GPU with two models, an old client) and under
"Questions for the owner". Nothing is committed: every change is in the
working tree.

**The goal:** each production line decides good or reject once per product,
from a vision model per camera and a code check against a chosen product list.
It drives the PLC and reports to other systems, and a supervisor sets all of
it up from the dashboard with cards, without code changes.

Items marked "Decided" come from the owner. Items marked "Decided (assistant's
choice)" were left to the assistant; each gives its reason, and the owner can
overrule any of them.

## Rules for working in this repository

- Never start the server from this folder: `data/` and `logs/` hold real data.
  Use the scratch tools in `backup/scratch-tools/` (see its `README.md`; the
  folder is ignored by git and Docker). `launcher.py` copies the working tree
  into `backup/scratch-tools/run/app` and runs it on `127.0.0.1:8765` with test
  credentials it writes to `creds.json`. `fake_cams.py` gives MJPEG cameras
  (optionally showing a QR code), `listeners.py` receives TCP and webhook
  messages. Copy `launch.json.example` to `.claude/launch.json` to start them
  with the preview tool, and delete that file afterwards. The fake cameras
  show nothing a model detects: for detections and crossings use the owner's
  streamer, outside the repository (`video stream\stream_server.py --host
  127.0.0.1 --port 8654 --cameras 2 --image-interval 2`: fruit on a belt on
  8654, a barcode picture on 8656), and upload the two models in
  `model_store/` to the scratch copy (`check_s2_1_setup.py` does that).
- `dashboard.html` and `assets/` are read from disk on every request. For
  dashboard-only edits, copy the file into the scratch copy and reload the
  page instead of restarting.
- Tests: `.venv\Scripts\python.exe -m pytest tests -q`. On 2026-10-06: 553
  pass. Eight fail on Windows and pass in Linux CI: six `TestModbusTCPDriver`
  tests in `test_plc_drivers`,
  `test_plc_protocols::TestDispatcherHardening::test_bad_address_is_not_retried`
  and `test_error_alarms::test_dispatch_to_unreachable_plc_raises_both_alarms`.
  `test_camera_roi_and_qr::test_second_pass_keeps_to_its_time_budget` and
  `test_code_types::test_second_pass_keeps_to_the_chosen_type` are timing
  tests that fail now and then in a full run on this machine, as other timing
  tests do when it is busy (stop the scratch servers first); rerun those on
  their own.
- The working tree has many uncommitted changes (local `main` is behind
  `origin/main`). Do not reset or stash; commit only when asked.
- Most files in the working tree have Windows line endings (CRLF) and some
  have LF (`core.autocrlf=true`). A script that rewrites a file must keep the
  endings that file has (`backup/scratch-tools/crlf.py` does).
- Do not run `backup/scratch-tools/launcher.py` to "check" it: it takes no
  options and starts the scratch server at once.
- Settings are versioned: `schema_version` in `data/system_state.json`, now
  **6**. `app/services/line_config.py` has the list of steps (`_UPGRADES`) and
  `upgrade_state()`; a file is upgraded once, when the server first loads it.
  Any new field needs a default in `line_config.py` so existing files load,
  and a setting that is removed needs an upgrade step that carries its
  meaning over. After an update, an existing line must behave as it did
  before until the user changes something.
- The database is upgraded by Alembic (`alembic/versions/`). Revision 0001
  builds its tables from the models as they are today, so a table added later
  must be named in `_LATER_TABLES` there and created by its own revision.

## What was done (2026-10-05 and 2026-10-06)

Each part has tests and was tried in a scratch copy: sections 7 to 3 with fake
cameras, section 2 with two real models on the owner's test videos.

### 7. Plant overview video (done)
- `GET /api/v1/vision/annotated/camera/{id}` takes `max_width`; the smaller
  picture is made once per frame and width (`shrink_jpeg` and the publisher's
  cache in `app/services/vision_service.py`).
- `assets/production_lines.js`: the tiles stay in place and only their text is
  updated every 1.5 s; each tile has its own snapshot loop (480 px, at most 8
  pictures a second, about 25 a second for the whole page). The loops stop on
  other pages and when the browser tab is hidden.
- Measured: 7.5 pictures a second per tile with three tiles (before: one per
  1.5 s). Tests: `tests/test_snapshot_width.py`.

### 6. Narrower dropdowns (done)
- `assets/dashboard.css`: a dropdown is at most 320 px wide
  (`--select-max-width`); full width is kept in dialogs, table cells, the
  header's line selector and on a phone. The open list may be wider than its
  box (up to 460 px) and opens to the left near the right edge of the page
  (`menu-right`, set in `initCustomSelects()` in `dashboard.html`).
- Not changed: text and number boxes still fill their column, so on Line
  setup a number box can be wider than the dropdown above it. Say so if the
  owner wants those limited too.

### 1. One auto-connect switch (done)
- `LineManager.connect_on_startup()` in `app/services/line_service.py` is the
  only place cameras are connected at startup: when `camera_auto_connect` is
  on, the cameras of every running line; Line 1 without cameras connects its
  default camera (`pick_default_camera`, never another line's camera).
  `main.py` connects cameras before the MQTT broker and the endpoint checks.
- The per-line switch is gone (settings step 3). **Refined rule:** the one
  switch starts on when the old Cameras page switch was on or a line *that
  has cameras* had its own switch on. A line without cameras connected
  nothing, and new lines had their switch on by default, so counting those
  would have switched it on for everyone.
- Deleting or disabling the selected camera no longer switches it off.
- Tests: `tests/test_auto_connect.py`.

### 4. Up to four product lists (done)
- Tables: `product_lists` and `products.list_id` (Alembic 0005; existing codes
  go into "List 1", id `list-1`, which a new database also gets). No database
  foreign key: SQLite cannot add one in place, so `ProductService` deletes a
  list's codes with the list.
- `app/services/product_service.py`: one lookup per list
  (`product_catalog.lookup(code, list_id)`), swapped in one step.
  `app/routes/v1/products.py`: `GET /products/lists`, `POST /products/import`
  (always a new list; 409 when there are four), `POST
  /products/lists/{id}/replace`, `PUT` and `DELETE /products/lists/{id}` (409
  with the line and camera name while a reader uses it), `list_id` on list,
  add and export. Calls that name no list use the oldest list.
- Each camera that reads codes has `product_list_id` (settings step 4 sets
  existing readers to `list-1`). A reader with no list knows no code.
- **Changed for API clients:** `POST /products/import?replace=true` no longer
  replaces the one list; use `/products/lists/{id}/replace`.
- Tests: `tests/test_product_lists.py`.

### 5. Reader actions (done)
- Camera fields `qr_action` (`report`, `accept_listed`, `reject_listed`),
  `product_list_id`, `qr_no_read` (`reject`, `ignore`), in the camera box on
  Line setup. The rule is `product_verdict()` in `line_config.py`.
- One result per product: `CountingService.process_frame` no longer counts a
  product itself when the line has Sync on; it hands the crossing to the line,
  which pairs it with its code and calls `finish_crossing()` once, with the
  final result and the reason (`reject_reason`: `vision_class`,
  `code_not_in_list`, `code_in_reject_list`, `no_code`). A check beside a
  vision camera switches Sync on.
- A line with only a reader: each code read is one product
  (`LineRuntime._emit_code_product`), with the limit stated on Line setup.
- **Decided (assistant's choice):** a reject card's travel delay runs from the
  moment of the crossing *on a line where a camera checks codes*
  (`delay_from_crossing` on the PLC event). A line that only reports codes
  keeps its timing as it was (the delay starts when the event is sent), so
  nothing already tuned on a real line moves. Line setup warns when a reject
  card's travel delay is shorter than the Sync window (`line_warnings()`).
- A picture with several codes: the code that is in the list decides
  (`_deciding_first`).
- Tests: `tests/test_reader_actions.py`.

### 3. Send cards (done)
- `app/services/card_triggers.py` holds the one "does this card fire" check
  (`card_fires`) for PLC cards and send cards; `plc_dispatcher_service.py`
  uses it. `app/services/send_dispatcher_service.py` sends the cards' messages
  (`MESSAGE_FIELDS` is the list of contents). API: `/api/v1/send/actions`,
  `/send/actions/batch`, `/send/actions/{id}/test`, `/send/fields`.
- Stored per line as `send_actions` (Line 1's at the top level of the
  settings, like its PLC cards). Settings step 5 turns the old settings into
  cards and removes them (`OLD_SEND_KEYS`); `CountingConfig` no longer has
  them, and an older client that still sends them is ignored.
- Dashboard: the "Send results" panel and its dialog in `dashboard.html`
  (`CARD_TRIGGERS`, `CARD_CONDITIONS` and the `card…` helpers are the shared
  definition; the PLC dialog uses them too). The three "Send QR reads to"
  dropdowns are gone from `production_lines.js`.
- Choices made while building it:
  - A code card has the condition "only codes not paired with a product the
    vision camera counted" (`unpaired`). Upgraded code cards use it, because
    the old setting never sent a paired code on its own.
  - "All channels" of a protocol became one card per channel that existed.
  - An address version 1 kept in the line settings becomes a channel named
    "... (from the old settings)".
  - A webhook card sends its channel's saved headers and method. Before,
    "all webhooks" sent no headers and always used POST.
  - A send card can name the line's second vision camera, like a PLC card.
  - The contents `qr` (the code's details) and `line` stand for several
    message keys; a field list from before send cards still names keys
    directly and means the same.
- Tests: `tests/test_send_cards.py`.

### 2. A model per camera; no "Activate" (done 2026-10-06)

**Decided by the owner:** a vision camera needs a model (a line is not saved
with a vision camera that has none); "Products to count" and "Defects to
reject" are set per vision camera and picked from the model's own class
names; Line setup shows per camera whether its model is loaded.

- Each vision camera entry has `model_id`, `expected_classes` and
  `defect_classes` (`normalize_camera` in `line_config.py`). Settings step 6
  (`_upgrade_to_v6`) gives every vision camera its line's model (the old
  active model where the line named none) and its line's two class lists,
  then removes `model_id` from the lines, the two lists from `action_trigger`
  and `active_model_id` from the settings. The owner's own settings file was
  put through it in memory (nothing was written): Line 1's camera keeps its
  model `yolo26s` and its lists.
- Line 1 without cameras was fed by the camera selected on the Cameras page,
  with the active model. Step 6 makes that camera Line 1's counting camera
  (as the version 1 upgrade did), because a camera now needs a model of its
  own. Other cameras that belong to no line still feed Line 1 while it has
  none, but nothing is detected on them.
- `LineManager` (`line_service.py`): `refresh_models()` loads one copy of
  every model a vision camera runs and drops the others;
  `engine_for_camera()` gives a camera its own model, or an engine without a
  model (the video is shown, nothing is detected; there is no fallback to
  another model). Each counter gets the class lists of its own camera
  (`LineRuntime.configure`, for Line 1 too). A counter changed while running
  (`POST /counting/config`) keeps that until its line's saved settings change.
- Alarm `camera.model_unavailable` ("Camera has no model it can run",
  critical, for the camera's line): raised for each vision camera of a running
  line that has no model or whose model did not load, with the reason;
  cleared when it loads, the camera is removed or the line stops. Line setup
  shows the same per camera, and `line_warnings()` (with `model_warnings()`)
  lists a model that is missing, not loaded, or lacks a ticked class.
  `inference.model_not_loaded` is now raised only when a vision camera of a
  running line streams and no model at all is loaded, so a server that only
  reads codes raises nothing.
- AI models page: no Activate button. Status is "Active" while a camera runs
  the model (`is_active` is worked out from the lines; the database column is
  no longer read), a "Used by" column names line and camera, and a model in
  use is not deleted (409 with the names). `GET /models` also gives `loaded`
  and `load_error`.
- Line setup (`assets/production_lines.js`): each camera box has the model
  dropdown with its load state, and two tick lists filled from the model's
  class names (a filter box above 12 classes, ticked ones first, a class in
  one list only). The two text boxes are gone from the counting card.
- **Decided (assistant's choice):** no hidden "active model" is left; the old
  calls keep their version 1 meaning, where the server's one camera is Line
  1's counting camera. `POST /models/{id}/activate` picks the model for that
  camera (for Line 1 without cameras, the camera that feeds it becomes its
  counting camera; 409 with a message when there is no camera),
  `GET /models/active` returns that model, and the detect calls take
  `?model_id=` (without it: the camera's own model, else Line 1's; a message
  when there is none). A model a detect call names that no camera runs is
  loaded for the call and kept until another one is named.
- Other choices made while building it (the owner can overrule each):
  - A client from before this change that sends cameras without a model or
    class lists leaves them as saved. One `model_id` it sends for the line is
    given to the vision cameras it sends without a model (to all of the line's
    when it sends no cameras). The class lists it still sends in
    `action_trigger` are ignored, like the old send settings.
  - Class names a model does not have are not refused (an upgraded line can
    hold typed names such as `defect`): they are shown as "not in this model"
    with a warning, so the line can still be saved.
  - A line from before the change whose camera has no model (no model was
    active) can still be started, stopped and renamed; only saving its
    cameras needs the model. Its camera raises the alarm above while it runs.
  - A model file lying in `model_store/` that was never uploaded or registered
    (`yolov8n.onnx` and two other names) was loaded as a default before. It is
    not any more: upload it on the AI models page.
- Tests: `tests/test_camera_models.py` (16) and two more in
  `tests/test_health.py`.
- Tried in the scratch copy with `yolo26s` and `yolo26n-seg` on the fruit
  video (`check_s2_1` to `check_s2_6` in `backup/scratch-tools/`): a line was
  refused without a model; two lines, and then one line with two vision
  cameras, each counted with its own model and lists (camera A: oranges and
  apples good, bananas rejected; camera B: bananas only, drawn as masks); the
  AI models page showed both Active with line and camera; a file that is not
  a model raised the alarm and showed on Line setup; activate, active and
  detect with `model_id` worked; after a restart the choices were kept and
  both models were loaded before the cameras connected. It ran on the CPU
  (2 to 4 frames a second with two models).

### Bugs found and fixed on the way
- Windows: a settings upgrade was not saved at load, because the file was
  still open while it was replaced (`SettingsPersistenceService.load`). This
  was one of the nine known Windows test failures; it passes now.
- Dashboard: a browser that remembered a line that no longer exists got 404s
  at load and was left half loaded ("Unsaved PLC action changes" when
  switching lines). `loadLines()` in `production_lines.js` now falls back to
  Line 1 and loads again.
- Tests: the shared admin sign-in (`admin_headers` in `tests/conftest.py`) is
  renewed when another test has reset the sessions; test files that sort
  after `test_resilience.py` could not use it before.
- Uploads to `/products/lists/{id}/replace` are size-limited before they are
  stored (`RequestBodyLimitMiddleware` takes a `/*` path).

## Still to check

1. **Linux CI.** Nothing was pushed (the owner has not asked for it). The
   tests ran on Windows only; the Docker job was not run. Push a branch when
   the owner asks, and compare with the eight Windows-only failures above.
2. **The real line.** The reject timing (Sync window against travel delay)
   with a code check, and a real PLC, cannot be shown in the scratch copy.
   What was shown there on 2026-10-06 with a real model: a line with a vision
   camera and a reader set to "Accept only listed codes" gave each product one
   result and one message (good with a listed code, rejected with
   `code_not_in_list` or `no_code`), the card for rejects got only rejects,
   and the reader's own list decided, not the oldest list
   (`check_s2_6_reader_and_send_cards.py`).
3. **The GPU.** The scratch copy ran both models on the CPU. Two or more
   models on the 4 GB GPU of the Docker image were not tried: start the
   container, pick two models, and look at the alarms and the frame rates.
4. **An old client.** `activate`, `active` and the detect calls were tried
   with direct API calls, not with the Flutter app itself.

## Questions for the owner

1. **The test picture** (section 3). A test picture sends nothing by design
   (`on_qr_capture`). Should it fire the code cards?
2. **Number boxes on Line setup** (section 6). Dropdowns are at most 320 px
   wide; text and number boxes still fill their column. Limit those too?
3. **Class lists from an old client** (section 2). They are ignored. Should
   they set the lists of Line 1's counting camera instead, the way `activate`
   sets its model?

## Checked on 2026-10-06, nothing left to do

- **Screenshots.** Line setup (camera boxes, model, class lists), the AI
  models page, and the Send results panel with its dialog were looked at at
  1440 px and at phone width (375 px). One cosmetic thing was seen: at phone
  width the Trigger dropdown in the send card dialog cuts its text ("Product
  crosse"); the opened list shows it in full.
- `FILE_TREE.md` is rewritten from the files in the working tree, and
  `README.md` describes the per-camera models.
