# Line setups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Save a line's setup, its connections and its cameras' image settings to a named file on the server, list a line's saves, load one back onto the stopped line, and delete one; with Save and Load text buttons and two small windows on Line setup.

**Architecture:** A new service `app/services/line_setups.py` keeps one JSON file per save under `data/line-setups/<line id>/` and does the snapshot and the load steps; four routes in `app/routes/v1/lines.py` call it. Loading reuses the existing code: the line update (taken out of `update_line`), `add_or_update_endpoint` for missing connections, and `CameraService.update_camera` for camera settings. The page part is a new `assets/line_setups.js` with node-tested pure parts.

**Tech Stack:** FastAPI, SQLAlchemy async (camera records), JSON files, plain browser JavaScript, pytest + node.

**Spec:** `docs/superpowers/specs/2026-11-01-line-setups-design.md`

## Global Constraints

- Files: `data/line-setups/<line id>/<save id>.json`, the folder from `settings_persistence_service.DATA_DIR` read at call time; written through a temporary file and `os.replace`.
- A save: `{"kind": "line-setup", "format": 1, "schema_version": line_config.SCHEMA_VERSION, "id", "name", "line_id", "line_name", "saved_at" (UTC ISO), "saved_by", "line", "endpoints", "cameras"}`.
- Save id `setup-` + 8 hex characters; the routes accept only `setup-[0-9a-f]{8}`. Name 1–64 characters after trimming.
- `line` = `get_line(line_id, with_logic=True)` without `enabled`, `counts_reset_at`, `batch`, `status`, `warnings`.
- Refusals: running line 409 "Stop the line to load a setup"; newer schema 409 "This setup was saved by a newer version of the server"; missing save or another line's 404; name 422; locked 423 (the lock's own dependency).
- Warnings, exact: "Camera {name} is on {line name} now: left out", "Camera {name} no longer exists: left out".
- Audit actions `SAVE_LINE_SETUP`, `LOAD_LINE_SETUP`, `DELETE_LINE_SETUP`, category `LINES`, the line id; load details "Loaded setup '{name}' onto line '{line name}'".
- API keys: `GET .../setups` is `monitor:read`; POST and DELETE under `/setups` are not allowed (None).
- The lock: `("POST", "/api/v1/lines/{line_id}/setups")` free; load and delete locked.
- Page copy, exact: buttons "Save", "Load"; window titles "Save line setup", "Load line setup"; "Unsaved changes: save the line setup first."; "No saved setups for this line yet. Use Save to keep one."; "Stop the line to load a setup."; confirm "Load '{name}' onto {line}? Its setup and camera settings replace the current ones; connections it uses that no longer exist are added back." + " Unsaved changes are lost." when dirty.
- Never the real `data/` or port 8000; tests use the `data_dir`/client fixtures; live checks a scratch copy.
- Tests run with `F:/projects/claude_fast_api_vision/fastapi_vision_server/.venv/Scripts/python -m pytest` from the worktree; keep each file's line endings.

## Review Focus

- A save made while the page has unsaved edits must hold the server's saved setup, never the edits (Task 3 test `test_save_window_waits_for_unsaved_changes`; the server only reads saved settings, Task 1).
- Loading must not overwrite a connection that exists, even when the save's copy differs (Task 2 test `test_an_existing_connection_is_kept`).
- A camera now on another line must not be taken from that line (Task 2 test `test_a_camera_on_another_line_is_left_out`).
- A Trigger the card controls must not be overwritten by the saved camera settings (Task 2 test `test_a_trigger_the_card_controls_is_kept`).
- A save id from the URL must not reach the file system unchecked (`../`): Task 1 test `test_a_bad_setup_id_is_refused`.

---

### Task 1: Save, list and delete (service, routes, access)

**Files:**
- Create: `app/services/line_setups.py`
- Modify: `app/routes/v1/lines.py` (routes after `clone_line`), `app/security/config_lock.py` (free and locked lists), `app/security/api_key_scopes.py` (the `/api/v1/lines` branch)
- Test: `tests/test_line_setups.py`

**Interfaces:**
- Produces (`app/services/line_setups.py`):
  - `SETUP_ID = re.compile(r"setup-[0-9a-f]{8}")`, `MAX_NAME = 64`
  - `folder(line_id: str) -> Path`
  - `async snapshot(db: AsyncSession, line_id: str, name: str, saved_by: str) -> Dict` — builds and writes a save; raises `ValueError` for a bad name, `KeyError` for a missing line; returns the summary.
  - `summary(save: Dict) -> Dict` → `{id, name, line_name, saved_at, saved_by}`
  - `list_setups(line_id: str) -> List[Dict]` (summaries, newest first; unreadable files skipped)
  - `read(line_id: str, setup_id: str) -> Dict` (raises `KeyError` when missing, bad id or another line's)
  - `delete(line_id: str, setup_id: str) -> Dict` (the deleted summary; `KeyError` as `read`)
- Routes: `GET /lines/{line_id}/setups` (require_operator), `POST /lines/{line_id}/setups` (require_supervisor, `{"name"}`, 201), `DELETE /lines/{line_id}/setups/{setup_id}` (require_supervisor). Camera records come from `Depends(get_db)` and `CameraService.get_camera_by_id`.

- [ ] **Step 1: Write the failing tests** in `tests/test_line_setups.py` (fixtures `client`, `admin_headers`, `data_dir`, and a `new_line` fixture like `tests/test_lines_api.py`'s):

```python
def test_a_save_holds_the_line_its_connections_and_camera_settings(client, admin_headers, data_dir, new_line):
    hook = _endpoint(client, admin_headers, {"name": "MES hook", "protocol": "webhook", "url": "https://mes.example/hook", "headers": {"Authorization": "Bearer s3cret"}})
    cam = _endpoint(client, admin_headers, {"name": "Belt cam", "protocol": "ipcam", "camera_kind": "ip", "source": "http://10.0.0.5/video"})
    client.patch(f"/api/v1/cameras/{cam}", json={"settings": {"roi_enabled": True, "roi_x": 0.1, "roi_y": 0.1, "roi_w": 0.5, "roi_h": 0.5}}, headers=admin_headers)
    line = new_line("Packing", cameras=[{"camera_id": cam, "role": "qr", "qr_trigger": "signal"}],
                    send_actions=[{"id": "send_mes", "name": "To MES", "endpoint_id": hook}])
    res = client.post(f"/api/v1/lines/{line['id']}/setups", json={"name": "  Product A  "}, headers=admin_headers)
    assert res.status_code == 201 and res.json()["name"] == "Product A"
    save = json.loads(next((data_dir / "line-setups" / line["id"]).glob("setup-*.json")).read_text(encoding="utf-8"))
    assert save["kind"] == "line-setup" and save["format"] == 1 and save["schema_version"] == SCHEMA_VERSION
    assert save["line"]["send_actions"][0]["endpoint_id"] == hook and "enabled" not in save["line"] and "batch" not in save["line"]
    assert {e["id"] for e in save["endpoints"]} == {hook, cam}
    assert next(e for e in save["endpoints"] if e["id"] == hook)["headers"]["Authorization"] == "Bearer s3cret"   # as stored
    assert save["cameras"][0]["id"] == cam and save["cameras"][0]["settings"]["roi_w"] == 0.5

def test_the_list_is_this_lines_newest_first_without_contents(...):
    # two saves on line A, one on line B: A's list has 2, newest first, keys exactly {id, name, line_name, saved_at, saved_by}

def test_a_name_must_be_1_to_64_characters(...):   # "" and " " and "x"*65 → 422; "x"*64 → 201

def test_a_bad_setup_id_is_refused(...):           # DELETE .../setups/..%2F..%2Fsystem_state and .../setups/setup-XYZ → 404; nothing deleted

def test_delete_removes_the_save(...):              # 200, file gone, list empty, audit DELETE_LINE_SETUP written

def test_api_keys_and_the_lock(client, admin_headers):
    assert required_api_key_scopes("GET", "/api/v1/lines/line-2/setups") == {"monitor:read"}
    assert required_api_key_scopes("POST", "/api/v1/lines/line-2/setups") is None
    assert required_api_key_scopes("POST", "/api/v1/lines/line-2/setups/setup-1a2b3c4d/load") is None
    assert required_api_key_scopes("DELETE", "/api/v1/lines/line-2/setups/setup-1a2b3c4d") is None
    assert not blocks_when_locked("POST", "/api/v1/lines/{line_id}/setups")
    assert blocks_when_locked("POST", "/api/v1/lines/{line_id}/setups/{setup_id}/load")
    assert blocks_when_locked("DELETE", "/api/v1/lines/{line_id}/setups/{setup_id}")
```

(`_endpoint` posts to `/api/v1/system/endpoints` and returns the id. The load route exists only from Task 2: its lock entry is added now, and `tests/test_config_lock_policy.py` passes once Task 2 adds the route.)

- [ ] **Step 2: Run them to see them fail**

Run: `.../python -m pytest tests/test_line_setups.py -q -p no:cacheprovider`
Expected: FAIL (404 on the new routes; import error for nothing else).

- [ ] **Step 3: Write `app/services/line_setups.py` and the three routes**; endpoints from `SettingsPersistenceService.get_endpoints()` filtered by `endpoints_used_by_line(line)`; the audit with `_audit(user, "SAVE_LINE_SETUP", f"Saved setup '{name}' of line '{line_name}'", line_id)` and the same for delete. A line that does not exist: 404. `api_key_scopes`: in the `/api/v1/lines` branch, `if "/setups" in path and method not in {"GET", "HEAD"}: return None` before the other write rules.

- [ ] **Step 4: Run the tests**

Run: `.../python -m pytest tests/test_line_setups.py tests/test_lines_api.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit** `Line setups: save, list and delete a line's setup`

---

### Task 2: Load

**Files:**
- Modify: `app/services/line_setups.py`, `app/routes/v1/lines.py` (`update_line` split; the load route)
- Test: `tests/test_line_setups.py`

**Interfaces:**
- Consumes: `read`, `folder` (Task 1).
- Produces:
  - `lines.py`: `async def _update_line(line_id: str, body: Dict, user) -> Dict` — the body of today's `update_line` (checks, `_save`, audit `UPDATE_LINE`, `_apply_trigger_modes`, `apply_line`, `_fire_line_state`, the view with its `warning`); `update_line` calls it.
  - `line_setups.py`: `async load(db, line_id: str, setup_id: str, user, update_line) -> Dict` → `{"loaded": True, "name", "warnings": List[str], "line": view}`; `update_line` is `_update_line`, passed in so the service does not import the routes. Raises `KeyError` (404), `LoadRefused(str)` (409), `ValueError` / `HTTPException` from the update (422).
  - Steps as small functions: `_check(line, save)`, `async _add_missing_endpoints(save, user) -> List[str]`, `_cameras_to_keep(save, line_id) -> (cards, warnings)`, `async _put_camera_settings(db, save, kept_ids)`.
  - Route `POST /lines/{line_id}/setups/{setup_id}/load` (require_supervisor); audit `LOAD_LINE_SETUP`.

- [ ] **Step 1: Write the failing tests**:

```python
def test_load_puts_the_setup_back(...):
    # save; then PUT the line with another name, no send cards, another camera card setting, and PATCH the camera's ROI
    # load → 200, loaded True, warnings []; GET line: name, send_actions, camera card as saved; camera ROI as saved; counts/batch untouched

def test_load_is_refused_on_a_running_line(...):        # start the line (as tests/test_lines_api.py does) → 409 "Stop the line to load a setup"; nothing changed
def test_load_refuses_a_newer_save(...):                 # write schema_version SCHEMA_VERSION + 1 into the file → 409 "...newer version..."
def test_load_of_another_lines_save_is_404(...):
def test_a_deleted_connection_is_added_back(...):        # delete the send card's webhook (after removing it from the line), load → the endpoint exists again with its id and headers
def test_an_existing_connection_is_kept(...):            # change the webhook's URL after saving, load → URL is the new one
def test_a_camera_on_another_line_is_left_out(...):      # move the camera to line B after saving, load A → warning "Camera Belt cam is on B now: left out"; B still has it; A has no card for it; a send card that named it has no camera_id
def test_a_trigger_the_card_controls_is_kept(...):       # an industrial (gige) stand-in camera whose card is One picture per trigger / system: saved settings trigger_mode "hardware" → after load the camera's trigger_mode is what the card sets ("software")
def test_load_is_in_the_audit_log(...):                  # LOAD_LINE_SETUP "Loaded setup 'Product A' onto line 'Packing'"
```

(For the gige case use the industrial camera stand-in other tests use, e.g. `tests/test_trigger_mode_from_card.py`'s helpers.)

- [ ] **Step 2: Run them to see them fail**

Run: `.../python -m pytest tests/test_line_setups.py -q -p no:cacheprovider`
Expected: the new tests fail (404 on the load route).

- [ ] **Step 3: Write the load**: order as the spec says — check (running = the line's `enabled`; schema), add missing connections (`add_or_update_endpoint(endpoint_data=dict(saved), username=..., role=..., clearance_level=...)` for each id not in `get_endpoints()`), keep the camera cards (`line_for_camera(id)` names another line → left out; no camera record after the connections step → left out; send and PLC cards' `camera_id` of a left-out camera cleared), `_update_line(line_id, {**saved_line, "cameras": kept_cards}, user)` (saved line without the live keys), then camera settings: `CameraService.update_camera(db, id, CameraUpdate(settings=s))` with `trigger_mode` dropped when `line_trigger_mode(camera)` is not None.

- [ ] **Step 4: Run the tests**

Run: `.../python -m pytest tests/test_line_setups.py tests/test_lines_api.py tests/test_config_lock_policy.py tests/test_trigger_mode_from_card.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit** `Line setups: load a save back onto its stopped line`

---

### Task 3: The Save and Load buttons and windows

**Files:**
- Create: `assets/line_setups.js`
- Modify: `assets/production_lines.js` (`renderLineSetup` header; after a load), `assets/dashboard.css`, `dashboard.html` (script before `production_lines.js`, `?v=` numbers), `tests/test_config_lock_dashboard.py` (button lists)
- Test: `tests/test_line_setups_page.py`

**Interfaces:**
- Consumes: the four routes (Tasks 1–2).
- Produces (`window.LineSetups` and `module.exports`, like `assets/text_reader.js`):
  - `buttonsHtml(readOnly: boolean) -> string` — `""` when readOnly, else `<button type="button" class="ls-link" data-ls="save">Save</button><button type="button" class="ls-link" data-ls="open-load">Load</button>`
  - `nameProblem(name: string) -> string` — `""`, "Give the setup a name.", or "A name has at most 64 characters."
  - `saveWindowHtml({ lineName, dirty }) -> string` — the name input `data-ls="name"`, Cancel, `<button type="button" class="btn-action btn-blue" data-ls="do-save" ${dirty ? "disabled" : ""}>Save</button>`, the dirty note.
  - `loadWindowHtml({ lineName, saves, running }) -> string` — one row per save `<div class="ls-row">` with `<button data-lock type="button" class="ls-pick" data-ls="load" data-id>` holding `<b>name</b> <span class="ls-line">lineName</span> <span class="ls-date">date</span>`, and `<button data-lock type="button" class="ls-del" data-ls="delete" data-id aria-label="Delete {name}">✕</button>`; picks `disabled` and the running note when running; the empty note when no saves.
  - `bind(container, opts)` — opts `{ lineId(), lineName(), dirty(), running(), fetchJson, toast, confirm(title, message, onYes), afterLoad() }`; opens and closes the windows (a `.ls-backdrop` appended to `document.body`, closed by ✕, Esc, a click outside).
- `production_lines.js`: the header title becomes `Line setup · ${esc(d.name)}${LineSetups.buttonsHtml(readOnly)}`; `LineSetups.bind(card, {...})` in `renderLineSetup`; `afterLoad` sets `setup.dirty = false`, `await loadLines()`, `renderLineSetup()`.

- [ ] **Step 1: Write the failing tests** in `tests/test_line_setups_page.py` (node, as `tests/test_camera_pages.py`):

```python
def test_the_buttons_are_level_2_only():
    assert _run("buttonsHtml", True) == "" and 'data-ls="open-load">Load</button>' in _run("buttonsHtml", False)
def test_a_name_must_be_1_to_64_characters():
    assert [_run("nameProblem", n) for n in ("", "  ", "x" * 65, "Product A")] == ["Give the setup a name.", "Give the setup a name.", "A name has at most 64 characters.", ""]
def test_save_window_waits_for_unsaved_changes():
    html = _run("saveWindowHtml", {"lineName": "Line 1", "dirty": True})
    assert "Unsaved changes: save the line setup first." in html and re.search(r'data-ls="do-save"[^>]*disabled', html)
def test_load_window_rows():
    html = _run("loadWindowHtml", {"lineName": "Line 1", "running": False, "saves": [{"id": "setup-1a2b3c4d", "name": "<b>A</b>", "saved_at": "2026-11-01T10:15:00+00:00"}]})
    assert "&lt;b&gt;A&lt;/b&gt;" in html and '<span class="ls-line">Line 1</span>' in html and 'data-id="setup-1a2b3c4d"' in html and "2026" in html
def test_load_window_empty_and_running():
    # no saves → the empty note; running → "Stop the line to load a setup." and every ls-pick disabled
def test_the_page_uses_it():
    # production_lines.js: "LineSetups.buttonsHtml(readOnly)" and "LineSetups.bind("; dashboard.html loads line_setups.js?v=1.0.0 before production_lines.js
    # dashboard.css: .ls-link uses color: var(--text-color) and no background/border
```

In `tests/test_config_lock_dashboard.py`: `'data-ls="load"'`, `'data-ls="delete"'` to `SAVE_BUTTONS["assets/line_setups.js"]`; `'data-ls="save"'`, `'data-ls="open-load"'`, `'data-ls="do-save"'` to `RUNNING_BUTTONS["assets/line_setups.js"]`.

- [ ] **Step 2: Run them to see them fail**

Run: `.../python -m pytest tests/test_line_setups_page.py tests/test_config_lock_dashboard.py -q -p no:cacheprovider`
Expected: FAIL (module not found).

- [ ] **Step 3: Write `assets/line_setups.js`, the CSS and the hook**:
  - `.ls-link { background:none; border:0; padding:0 2px; font-size:13px; font-weight:600; color: var(--text-color); cursor:pointer; }`, `.ls-link:hover, .ls-link:focus-visible { text-decoration:underline; }` (the theme's text colour: white on dark, black on light).
  - The windows: the dashboard's modal look (`--card-bg`, border, radius), about 360 px wide (`width:min(360px, calc(100vw - 32px))`), centred; rows as flex with the name bold, `.ls-line` and `.ls-date` small and `var(--text-muted)`; the list scrolls past 8 rows.
  - Calls: save `POST .../setups {name}` → toast "Saved '{name}'"; open-load `GET .../setups`; load → confirm → `POST .../load` → toast "Loaded '{name}'" (+ warnings joined by "; ", toast "warning") → `afterLoad()`; delete → confirm "Delete '{name}'?" → `DELETE` → redraw the list.

- [ ] **Step 4: Run the tests**

Run: `.../python -m pytest tests/test_line_setups_page.py tests/test_config_lock_dashboard.py tests/test_line_setup_markup.py tests/test_help_tips.py tests/test_lines_api.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit** `Line setups: Save and Load on Line setup`

---

### Task 4: Live check, docs and the vault

**Files:**
- Modify: `README.md` (a short "Saved line setups" part after the Line setup steps)
- Scratch only: `backup/scratch-tools/shots_line_setups.py`

- [ ] **Step 1:** Run `launcher_pages.py line-setups` (8765) and `fake_text_cam.py 8661 8662`.
- [ ] **Step 2:** In headless Chrome on Line 1 of the scratch copy: Save "Product A"; change a camera card, an input, a send card and a camera's ROI and save the line; delete a connection the save holds (after taking it off the line); Load "Product A" → all back, the connection back; start the line → Load window rows greyed with the note; delete a save. Dark and light theme; 1400 px and 390 px; screenshots of the header and both windows.
- [ ] **Step 3:** Fix what it finds, test first; commit each fix.
- [ ] **Step 4:** README part; vault note "03 Decisions/Line setups decisions.md" and a line on Home.md. Commit `Line setups: docs`.
- [ ] **Step 5:** The full suite (`--deselect` the known Windows-only failures, see the local test environment memory).
