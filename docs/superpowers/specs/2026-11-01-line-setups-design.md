# Line setups: save a line's setup to a file and load it later

Date: 2026-11-01. Status: design agreed with the owner, waiting for the spec review.

## Why

The owner wants to keep a line's whole configuration (its Line setup and the connections it uses) under a
name, and put it back later, for example after trying other settings or when a product changes over.
Today a line's setup can only be changed by hand; the settings history keeps copies of the whole server's
settings, which are only put back by hand with the server stopped.

## What the owner asked for (2026-11-01)

- Two small text buttons, **Save** and **Load**, in the Line setup header, white on the dark theme and
  black on the light theme.
- **Save** writes the line's setup and its connections to a save file on the server, under a name the user
  gives.
- **Load** opens a small floating window listing the saves with their names and dates; the line's name is
  shown small and grey beside each name.
- Chosen in the questions:
  - connections on load: **missing ones are added back from the save, existing ones are kept as they are**
    (other lines may use them);
  - a setup is loaded **only while the line is stopped**;
  - the Load window lists **this line's saves only**;
  - a save **includes each camera's image and video settings**.

## What a save holds

One JSON file per save, in `data/line-setups/<line id>/<save id>.json`:

```json
{
  "kind": "line-setup",
  "format": 1,
  "schema_version": 9,
  "id": "setup-1a2b3c4d",
  "name": "Product A",
  "line_id": "line-1",
  "line_name": "Line 1",
  "saved_at": "2026-11-01T10:15:00+00:00",
  "saved_by": "admin",
  "line": { "...": "the line as GET /api/v1/lines/{id} gives it with its logic, without its live state" },
  "endpoints": [ { "...": "each connection the line uses, as stored (with its passwords)" } ],
  "cameras": [ { "id": "ipcam-...", "name": "...", "type": "ip", "settings": { "...": "image and video" } } ]
}
```

- **The line:** its name, Minimum frames/s, Yield target, Sync, the camera cards (jobs, models, classes,
  count lines, tracking, triggers, picture settings, codes, text fields), the inputs, the product checks,
  the send cards, the output actions (PLC cards) and their trigger settings, the handshake and the sequence
  input. Not its live state: not `enabled` (running), its counts, `counts_reset_at` or its `batch`.
- **The connections:** every connection the line uses, found with `endpoints_used_by_line` (send card
  channels, PLC connections of output actions and inputs, the camera entries of IP and industrial cameras),
  as they are stored, passwords included. A save file holds the same as `system_state.json`, in the same
  `data/` folder.
- **The cameras:** for each camera of the line: its id, name, type and image and video settings (the camera
  record's `settings`: resolution, FPS, rotation, flips, ROI, exposure, gain, trigger, stream width and
  quality).
- `schema_version` is the settings version of the server that saved it (`line_config.SCHEMA_VERSION`, 9 today).
- **Not in a save:** AI models and product lists (shared, large: a camera card names them by id), the
  line's production data (counts, batch, records), and other lines.

A save id is `setup-` and 8 random hex characters. A name is 1 to 64 characters after trimming; names need
not be unique.

## The Line setup header

```
┌──────────────────────────────────────────────────────────────────────┐
│ Line setup · Line 1    Save  Load             ● Stopped              │
```

- **Save** and **Load** are small text buttons after the title: no frame or background, 13 px, the page's
  text colour (white on the dark theme, black on the light theme), underlined on hover and focus.
- Both need Level 2 (`restricted-l2`). Level 1 does not see them.

### Save

A small floating window (centred, about 360 px wide, the dashboard's modal look, closed by ✕, Esc or a
click outside):

```
┌─ Save line setup ─────────────────── ✕ ┐
│ Name                                    │
│ [ Product A                          ]  │
│ Saves Line 1's setup as it is saved,    │
│ with its connections and camera images. │
│                          [ Cancel ] [Save] │
└─────────────────────────────────────────┘
```

- It saves the line's setup **as saved on the server**. When the page has unsaved changes the window says
  "Unsaved changes: save the line setup first." and its Save is greyed out.
- Save works while the configuration is locked: a copy changes nothing that runs.
- On success: a "Saved 'Product A'" message, and the window closes.

### Load

A small floating window listing this line's saves, newest first:

```
┌─ Load line setup ─────────────────── ✕ ┐
│ Product A   Line 1         01/11/2026 12:15 ✕ │
│ Product B   Line 1         31/10/2026 17:02 ✕ │
│ Night shift Line 1         30/10/2026 08:40 ✕ │
└─────────────────────────────────────────┘
```

- Each row: the name in bold, the line name small and grey beside it, the date and time it was saved (the
  browser's time), and a small ✕ that deletes the save (after a confirmation).
- No saves yet: "No saved setups for this line yet. Use Save to keep one."
- A click on a row asks "Load 'Product A' onto Line 1? Its setup and camera settings replace the current
  ones; connections it uses that no longer exist are added back." (plus "Unsaved changes are lost." when
  the page has some). Yes loads it.
- On a running line the rows are greyed and the window says "Stop the line to load a setup."
- While the configuration is locked, loading and deleting say that it is locked (the buttons carry
  `data-lock`), as every other change does.
- After a load: "Loaded 'Product A'" (with the warnings, if any), and Line setup is drawn again from the
  server.

## What Load does (server)

1. **Refuses** (nothing changed):
   - the line is running: 409 "Stop the line to load a setup";
   - the save is not this line's, or is not found: 404;
   - the save was made by a newer server (`schema_version` higher than this server's): 409 "This setup was
     saved by a newer version of the server";
   - the configuration is locked: 423, as for every locked change.
2. **Connections:** each connection in the save whose id no longer exists is added back, through the same
   code as adding a connection on the Connections page (`add_or_update_endpoint`, with its saved id; an IP
   camera's record follows its connection as it does there). A connection that exists is left as it is.
3. **Cameras:** a camera card is left out of the loaded line, with a warning, when its camera:
   - is now on another line ("Camera X is on Line 2 now: left out"), or
   - no longer exists and could not be added back (a USB camera, or a camera whose connection is not in the
     save) ("Camera X no longer exists: left out").
   Output actions and send cards that named a left-out camera keep working without it (their camera choice
   is cleared, as the line's own save requires).
4. **The line:** the saved line is put on through the same code as Save line setup (`PUT /lines/{id}`: the
   same checks, saving, trigger modes and applying), so an invalid setup is refused the same way (422 with
   the reason). Its id, running state, counts and batch stay as they are now; everything else is the save's,
   its name included.
5. **Camera settings:** each loaded camera gets its saved image and video settings, through the same code as
   a camera's Settings (`CameraService.update_camera`), except a Trigger mode that the line card now controls
   (`line_trigger_mode`): that one stays as the card sets it.
6. **Answer:** `{"loaded": true, "name": ..., "warnings": [...], "line": <the line view>}`. The load is
   written to the audit log ("Loaded setup 'Product A' onto line 'Line 1'"), with any warnings.

If adding a connection back or putting the line on fails, the load stops with the reason; connections
already added back stay (they are harmless and usable).

## API

| Method and path | What | Access |
|---|---|---|
| `GET /api/v1/lines/{line_id}/setups` | This line's saves, newest first: `[{id, name, line_name, saved_at, saved_by}]` (no contents) | Operator; API keys `monitor:read` |
| `POST /api/v1/lines/{line_id}/setups` `{"name": "..."}` | Save the line's setup as it is saved now; answers the new save's summary | Level 2; free while locked; not API keys |
| `POST /api/v1/lines/{line_id}/setups/{setup_id}/load` | Load a save onto its line (stopped) | Level 2; locked when locked; not API keys |
| `DELETE /api/v1/lines/{line_id}/setups/{setup_id}` | Delete a save | Level 2; locked when locked; not API keys |

- Save, load and delete are written to the audit log (`SAVE_LINE_SETUP`, `LOAD_LINE_SETUP`,
  `DELETE_LINE_SETUP`, category `LINES`, with the line id).
- API keys: the list is `monitor:read`; save, load and delete are not allowed (a save holds connection
  passwords, and loading changes connections that other lines use).
- The config lock: `POST .../setups` is on the lock's free list; load and delete are on its locked list.
- A line that is deleted keeps its save files on disk (they are not listed anywhere); they cost little and
  can be removed by hand.

## How it is built

- `app/services/line_setups.py` (new): the files (folder, save id, write through a temporary file and
  rename, read, list, delete), what a save holds (`snapshot(line_id, by)`), and the load steps above
  (`load(line_id, setup_id, user)`), each step a small function. The folder follows the settings service's
  `DATA_DIR`, so tests and scratch copies use their own.
- `app/routes/v1/lines.py`: the four routes; the load uses the line update's own code, taken out of
  `update_line` into a function both call.
- `app/security/config_lock.py` and `app/security/api_key_scopes.py`: the routes classified as above.
- `assets/line_setups.js` (new, node-tested pure parts like `text_reader.js`): the two text buttons, the
  Save window, the Load window and its rows, the calls; `window.LineSetups`.
- `assets/production_lines.js`: puts the buttons in the Line setup header, tells LineSetups whether the
  page has unsaved changes and whether the line runs, and draws Line setup again after a load.
- `assets/dashboard.css`: the text buttons and the two windows (theme colours, phone width).
- `dashboard.html`: the script tag and new `?v=` numbers.
- `README.md`: a short "Saved line setups" part under Line setup.

## Testing

- **Server (pytest):**
  - a save holds the line with its logic, the connections it uses (with passwords), and its cameras' image
    settings; not its counts, batch or running state;
  - the list is newest first, has no contents, and lists only that line's saves;
  - a name of 0 or 65 characters is refused;
  - load puts back a changed setup (camera cards, inputs, send cards, output actions, name) and camera
    settings;
  - load on a running line, a save of another line, a missing save, a newer `schema_version`: refused,
    nothing changed;
  - a connection deleted since the save is added back with its id; one changed since is kept as it is now;
  - a camera now on another line is left out with a warning;
  - a Trigger mode the card controls is not overwritten;
  - delete removes the file;
  - API keys: list with `monitor:read`; save, load, delete refused; the lock: save free, load and delete
    blocked; every new route classified (the lock and scope tests).
- **Page (node and markup):** the Save window's name check and its "save the line setup first" state; the
  Load window's rows (name, grey line name, date, ✕), empty and running states; the buttons' theme colours
  and `restricted-l2` / `data-lock`.
- **Live check on a scratch copy** (never the real `data/` or port 8000): save, change the line (a camera
  card, an input, a send card, a camera's ROI), delete a connection, load, and see it all back; refused on a
  running line; dark and light theme; desktop and phone width; screenshots.

## Not in this change

- Downloading a save file or uploading one (to move a setup to another server).
- Saves in the nightly backups and the second backup folder.
- Loading a save onto another line.
