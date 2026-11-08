# Configuration Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A lock that stops every change to lines, cameras, connections, models and product lists until an Admin, or a Level 2 user with the lock code, unlocks it.

**Architecture:** One router-level dependency on the protected API refuses (423) every non-GET route that is not on an explicit allowed list while `system_state.json` says locked. A small service owns the lock state, the hashed code, the wrong-code limit and the audit entries. The dashboard shows a lock chip, a dialog, a banner, read-only fields and dimmed save buttons, and reacts to a 423 from any request.

**Tech Stack:** FastAPI, passlib (`hash_password`/`verify_password`), plain JS in `assets/`, pytest + node for script checks.

**Spec:** `docs/superpowers/specs/2026-10-24-configuration-lock-design.md`

## Global Constraints

- Blocked answer: `423`, detail exactly `The configuration is locked. An Admin, or a Level 2 user with the lock code, can unlock it.`
- Lock code: at least 6, at most 128 characters; stored only as `hash_password(code)`; never in any answer.
- Wrong codes: 5 in a row → code unlock refused for 300 s (`429`, `Retry-After`); a right code or an Admin unlock clears the count; kept in memory only.
- Saved key `config_lock`: `{"locked": false, "changed_by": null, "changed_at": null, "code_hash": null, "code_set_by": null, "code_set_at": null}`; missing → unlocked, no code.
- Status shape: `{"locked", "changed_by", "changed_at", "code_set", "retry_after"}`.
- Audit category `Configuration lock`; actions `CONFIG_LOCKED`, `CONFIG_UNLOCKED`, `LOCK_CODE_SET`, `LOCK_CODE_REMOVED`, `UNLOCK_WRONG_CODE`, `UNLOCK_BLOCKED`.
- All repo files are CRLF: edit with the Edit tool or a script that keeps CRLF. Work in `.claude/worktrees/config-lock`; run pytest there with the main folder's `.venv` (not `pt.sh`, which tests `main`).
- The test client is session-wide: every test that locks must unlock in a `finally` (fixture `unlocked` below).

## Review Focus

1. A page that saves on its own (an auto-save, a toggle that posts at once) while locked: no request should fire, or at most one lock message per click, never a stream of toasts. → Task 5 test: `test_no_lock_toast_storm` (the 423 handler shows the message at most once per 3 s).
2. Two screens: one unlocks, the other was showing locked. Within one poll (2 s) the second becomes editable without a reload; a 423 on the second turns it locked at once. → Task 4 test `test_poll_changes_carries_the_lock` and Task 5 test `test_423_turns_the_page_locked`.
3. A code typed with spaces at the ends, or only spaces: spaces at both ends are dropped on set and on unlock; fewer than 6 characters after that is refused. → Task 2 test `test_code_is_trimmed_and_checked`.
4. A restart while locked: still locked after the reload; the wrong-code count starts at 0. → Task 2 test `test_lock_survives_reload`.
5. A Level 2 user when no code is set: a clear "No lock code is set: only an Admin can unlock." (403), not "wrong code", and no wrong-code count. → Task 3 test `test_level2_unlock_without_a_code_set`.

---

### Task 1: Route policy and the route inventory test

**Files:**
- Create: `app/security/config_lock.py`
- Modify: `main.py` (module-level `PROTECTED_ROUTERS`, used by `create_app`)
- Test: `tests/test_config_lock_policy.py`

**Interfaces:**
- Produces: `LOCKED_MESSAGE: str`; `LOCK_FREE_ROUTES: frozenset[tuple[str, str]]` and `LOCKED_ROUTES: frozenset[tuple[str, str]]` of `(method, full route path)` e.g. `("POST", "/api/v1/lines/{line_id}/start")`; `blocks_when_locked(method: str, route_path: str) -> bool` (False for GET/HEAD/OPTIONS, else `(method, route_path) not in LOCK_FREE_ROUTES`); `main.PROTECTED_ROUTERS: tuple` of the route modules now listed inside `create_app` plus the new `config_lock` routes module (Task 3 adds it).

- [ ] **Step 1: Write the failing tests**

```python
def _all_changing_routes():
    import main
    seen = set()
    for module in main.PROTECTED_ROUTERS:
        for route in module.router.routes:
            for method in route.methods - {"GET", "HEAD"}:
                seen.add((method, "/api/v1" + route.path))
    return seen

def test_every_changing_route_is_classified():
    routes = _all_changing_routes()
    assert routes == LOCKED_ROUTES | LOCK_FREE_ROUTES, sorted(routes ^ (LOCKED_ROUTES | LOCK_FREE_ROUTES))
    assert not LOCKED_ROUTES & LOCK_FREE_ROUTES

def test_running_the_line_is_free_and_setup_is_locked():
    assert not blocks_when_locked("POST", "/api/v1/lines/{line_id}/start")
    assert not blocks_when_locked("PUT", "/api/v1/lines/{line_id}/batch")
    assert not blocks_when_locked("POST", "/api/v1/lines/{line_id}/trigger")
    assert not blocks_when_locked("GET", "/api/v1/lines")
    assert blocks_when_locked("PUT", "/api/v1/lines/{line_id}")
    assert blocks_when_locked("POST", "/api/v1/cameras/{camera_id}/connect")
    assert blocks_when_locked("POST", "/api/v1/products/import")
    assert blocks_when_locked("POST", "/api/v1/brand/new")   # unknown: fail closed
```

- [ ] **Step 2: Run** `pytest tests/test_config_lock_policy.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `app/security/config_lock.py` with the two sets copied from spec section 2 (locked list and allowed list, `/api/v1` prefix, path parameter names as the routers spell them: run the inventory test to read them), without the lock's own routes (Task 3 adds them with the routes); and `PROTECTED_ROUTERS` in `main.py` (the tuple `create_app` iterates).
- [ ] **Step 4: Run** the test → PASS.
- [ ] **Step 5: Commit** `Config lock: which routes the lock blocks`.

### Task 2: Lock state service

**Files:**
- Create: `app/services/config_lock_service.py`
- Modify: `app/services/settings_persistence_service.py` (`DEFAULT_STATE["config_lock"]`; `"code_hash"` in `redact_secrets`' sensitive keys)
- Test: `tests/test_config_lock_service.py`

**Interfaces:**
- Produces: `class LockError(Exception)` with `status_code: int`, `detail: str`, `retry_after: int = 0`; `class ConfigLockService` (classmethods): `status() -> dict`, `is_locked() -> bool`, `lock(user) -> dict`, `unlock(user, code: Optional[str]) -> dict`, `set_code(user, code: str) -> dict`, `remove_code(user) -> dict`, `reset_attempts() -> None`; class attrs `MAX_WRONG_CODES = 5`, `WAIT_SECONDS = 300`, `CODE_MIN = 6`, `CODE_MAX = 128`, `clock = time.monotonic` (tests replace it). `user` is anything with `username`, `role`, `clearance_level`.
- Rules: `lock` needs level ≥ 2 (else `LockError(403, ...)`); `unlock`: level 3 → no code needed; level 2 → no code set: 403 "No lock code is set: only an Admin can unlock."; waiting: 429 "Too many wrong codes. Try again in N s." with `retry_after`; wrong: 403 "Wrong lock code." (count +1, audit `UNLOCK_WRONG_CODE`; the 5th also audits `UNLOCK_BLOCKED`); level 1: 403. Already in the asked state: return status, no save, no audit. State lives in `SettingsPersistenceService._state["config_lock"]` (load first through `get_state()`), saved with `save()`, audited with `record_audit(..., category="Configuration lock")`.

- [ ] **Step 1: Write the failing tests** (with a `SimpleNamespace` user per level and a fixture that resets `config_lock` and attempts):
  - `test_starts_unlocked_without_a_code`: `status() == {"locked": False, "changed_by": None, "changed_at": None, "code_set": False, "retry_after": 0}`.
  - `test_level2_locks_admin_unlocks_without_code`: lock as L2 → `locked` True, `changed_by` the L2 name; `unlock(admin, None)` → False; audit actions `CONFIG_LOCKED`, `CONFIG_UNLOCKED` in order.
  - `test_level1_cannot_lock_or_unlock` → `LockError.status_code == 403`.
  - `test_level2_unlocks_only_with_the_right_code`.
  - `test_five_wrong_codes_wait_five_minutes`: 5 wrong → 6th try with the right code gives 429 with `retry_after == 300`; clock + 301 → right code unlocks; `UNLOCK_BLOCKED` audited once.
  - `test_admin_unlock_clears_the_wrong_count`.
  - `test_code_is_trimmed_and_checked`: `set_code(admin, "  abc123  ")` then `unlock(l2, "abc123")` works; `"  ab   "` and `"x"*129` → 422; L2 `set_code` → 403.
  - `test_code_set_and_removed_are_audited`: `LOCK_CODE_SET` then `LOCK_CODE_REMOVED`; after `remove_code`, `code_set` is False and L2 unlock gives the "No lock code is set" 403.
  - `test_code_hash_never_leaves`: after `set_code`, `"code_hash"` not in `json.dumps(SettingsPersistenceService.redact_secrets(SettingsPersistenceService.get_state()))` and not in `status()`.
  - `test_settings_update_cannot_touch_the_lock`: `update_settings({"config_lock": {"locked": False}})` while locked → still locked.
  - `test_lock_survives_reload`: lock, `SettingsPersistenceService.load()` → still locked, `retry_after == 0`; a state file without `config_lock` loads unlocked.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the service and the two persistence edits.
- [ ] **Step 4: Run** → PASS; also `pytest tests/test_settings_persistence*.py tests/test_api.py -q` still pass.
- [ ] **Step 5: Commit** `Config lock: lock state, code and wrong-code limit`.

### Task 3: Lock routes, enforcement, API keys, polling

**Files:**
- Create: `app/routes/v1/config_lock.py` (router `prefix="/system"`, tags `["Configuration lock"]`)
- Modify: `app/dependencies.py` (`enforce_config_lock`), `main.py` (dependency on `protected_api`, router in `PROTECTED_ROUTERS`), `app/security/api_key_scopes.py`, `app/services/settings_persistence_service.py` (`get_changes_since` adds `"config_lock": ConfigLockService.status()` to all three answers)
- Test: `tests/test_config_lock_api.py`

**Interfaces:**
- Consumes: Task 1 `blocks_when_locked`, `LOCKED_MESSAGE`; Task 2 `ConfigLockService`, `LockError`.
- Produces: `GET/POST /api/v1/system/lock`, `POST /api/v1/system/unlock` body `{"code": str | null}`, `PUT /api/v1/system/lock/code` body `{"code": str}`, `DELETE /api/v1/system/lock/code`; all answer the status. Levels: GET `require_operator`; POST lock and unlock `require_supervisor` (the service decides L2 vs Admin); code routes `require_admin`. `LockError` → `HTTPException(e.status_code, e.detail, headers={"Retry-After": str(e.retry_after)} if 429)`.
- `async def enforce_config_lock(request: Request) -> None`: route path from `request.scope["route"].path`; raises `HTTPException(423, LOCKED_MESSAGE)` when `blocks_when_locked(...)` and `ConfigLockService.is_locked()`. `protected_api` dependencies become `[Depends(require_operator), Depends(enforce_config_lock)]` (sign-in first).
- Scopes: `GET /api/v1/system/lock` → `{"monitor:read"}`; any other method on `/api/v1/system/lock`, `/api/v1/system/unlock`, `/api/v1/system/lock/...` → `None`. Put these before the generic `/api/v1/system/` rules.

- [ ] **Step 1: Write the failing tests** (fixtures: `admin_headers`; a Level 2 and a Level 1 user made through `POST /api/v1/auth/users` and signed in; `unlocked` fixture that unlocks as admin and removes the code in `finally`; an API key with `configuration:write` and `monitor:read` for an admin-owned key):
  - `test_locked_changes_answer_423_for_everyone`: lock; `PUT /api/v1/lines/line-1` (body = current line), `POST /api/v1/cameras`, `POST /api/v1/products/lists`, `POST /api/v1/system/settings` as admin, L2 and the API key → 423 with `LOCKED_MESSAGE`; same requests unlocked → not 423.
  - `test_running_the_line_works_while_locked`: `POST .../start`, `.../stop`, `PUT .../batch`, `POST /api/v1/counting/reset`, `GET /api/v1/lines` → not 423.
  - `test_unsigned_request_is_401_not_423`.
  - `test_lock_routes_by_level`: L1 `POST /system/lock` → 403; L2 lock → 200 locked; L2 unlock without code set → 403 "No lock code is set: only an Admin can unlock."; admin `PUT /system/lock/code` `{"code": "lock-123"}`; L2 unlock wrong → 403, right → 200 unlocked; L2 `PUT /system/lock/code` → 403.
  - `test_level2_unlock_without_a_code_set` (Review Focus 5): no wrong-code count after it (`retry_after == 0` after 5 such tries).
  - `test_too_many_wrong_codes_is_429_with_retry_after`.
  - `test_api_keys_read_but_cannot_lock`: key `GET /system/lock` → 200; `POST /system/lock` → 403.
  - `test_poll_changes_carries_the_lock`: `GET /system/poll-changes?since_version=0` and with the current version → both have `config_lock.locked`; no `code_hash` anywhere in `/system/settings`, `/system/lock`, `/system/poll-changes`.
  - Add the four changing lock routes (`POST /system/lock`, `POST /system/unlock`, `PUT` and `DELETE /system/lock/code`) to `LOCK_FREE_ROUTES` and run Task 1's inventory test again.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the routes, the dependency, the scope rules, the poll field.
- [ ] **Step 4: Run** the new tests, Task 1/2 tests, `tests/test_api.py`, `tests/test_tokens.py`, `tests/test_lines_api.py` → PASS.
- [ ] **Step 5: Commit** `Config lock: lock routes and the server-side check`.

### Task 4: Dashboard lock chip, dialog, banner and code panel

**Files:**
- Create: `assets/config_lock.js` (IIFE, `window.ConfigLock`)
- Modify: `dashboard.html` (chip in `.topbar-right` before `#userMenu`; dialog `#configLockDialog`; banner `#configLockBanner` first in `.content-body`; My account panel `#configLockCodePanel`; `<script src="/assets/config_lock.js?v=1.0.0">` after `production_lines.js`; fetch wrapper calls `ConfigLock.onLockedAnswer()` on a 423; `pollServerChanges` calls `ConfigLock.apply(d.config_lock)`; `ConfigLock.refresh()` where the 2 s poll starts), `assets/dashboard.css` (chip, banner, `body.config-locked [data-lock]`)
- Test: `tests/test_config_lock_dashboard.py`

**Interfaces:**
- Produces: `ConfigLock.apply(status)`, `ConfigLock.refresh()`, `ConfigLock.locked() -> bool`, `ConfigLock.openDialog()`, `ConfigLock.onLockedAnswer()`, `ConfigLock.saveCode()`, `ConfigLock.removeCode()`; `body.config-locked` while locked; constant `LOCKED_ROOTS` = `#tabLines, #tabActionTrigger, #tabCameras, #tabModels, #tabComms, #tabProducts, #sendActionSettingsModal, #plcActionSettingsModal, #plcSignalSettingsModal, #commModalOverlay, #modelUploadOverlay` plus the Products dialogs (`.modal-backdrop[id^="pf"]` or the ids `productsDialog` is called with).
- Behaviour:
  - fields (`input, select, textarea`) inside `LOCKED_ROOTS` and not inside `[data-lock-free]` get `disabled` and `data-lock-off`; a `MutationObserver` (childList, subtree) per root re-applies after renders; unlocking re-enables only `[data-lock-off]`;
  - a capture-phase `click` listener on `document` stops clicks on `[data-lock]` while locked and shows `LOCKED_MESSAGE` with `showToast(..., "warning")`;
  - `onLockedAnswer()` sets locked, then `refresh()`; it shows the message at most once per 3 s;
  - the banner shows on the six locked tabs only (wrap `window.switchTab`): `Locked by {changed_by} at {local time}: settings can be viewed but not changed.` plus **Unlock** for level ≥ 2;
  - chip: `Locked` / `Unlocked` with a padlock SVG; the dialog: Lock (level ≥ 2), Unlock (Admin), code field + Unlock (Level 2), state only for Level 1; error line for 403/429 answers;
  - My account panel shown only for level 3: `Code set` / `No code`, new code + confirm (must match, ≥ 6), **Save code**, **Remove code**.
- [ ] **Step 1: Write the failing tests**: markup ids exist in `dashboard.html`; the script tag is present; `node --check assets/config_lock.js`; a node run of `config_lock.js` on a tiny DOM stand-in is not needed: instead `test_423_turns_the_page_locked` and `test_no_lock_toast_storm` cut `onLockedAnswer` out of the script (as `tests/test_trigger_card_markup.py` does with `_function`) and run it with node on stubs (`showToast` counter, fake clock): two calls 1 s apart → one toast; 4 s apart → two; `locked()` true after the first.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the script, markup and CSS. Add the chip's `data-help` text to the list in `tests/test_help_tips.py` (that test fails for help texts it does not know).
- [ ] **Step 4: Run** the new test file and `tests/test_help_tips.py`, `tests/test_lines_api.py` (node syntax checks) → PASS.
- [ ] **Step 5: Commit** `Config lock: chip, dialog, banner and lock code panel`.

### Task 5: Mark the save buttons and view-only fields

**Files:**
- Modify: `dashboard.html`, `assets/production_lines.js`, `assets/plc_signals.js`, `assets/camera_settings.js`, `assets/product_fields.js` (where they render the buttons below)
- Test: `tests/test_config_lock_dashboard.py` (add)

**Interfaces:**
- Consumes: Task 4 `[data-lock]` / `[data-lock-free]` meanings.
- Rule: `data-lock` on every button whose click sends a request on `LOCKED_ROUTES`; nothing on buttons that run, test, read or only open/close (Start/Stop, Reset counts, Read now, Test, Software trigger, Take a picture, view switches, Settings/Edit that only open a dialog: the dialog's own Save is marked). `data-lock-free` on fields that only choose what to see: the line picker, search and filter fields, the records filters are not in a locked root anyway.
- Known buttons (grep each handler to confirm, and add any other button that calls a locked route):
  - `dashboard.html`: `#commBtnDelete`, `#commSaveBtn`, Add card ×2, Apply (send cards), Add action ×2, Apply (output actions), Add input, Apply (inputs), Upload model, the upload dialog's Upload, Delete model ×2, model activate/mode controls, Add connection ×2, connection row Delete, camera row Delete, the camera add/save buttons on Cameras, the dialogs' Save/Done of send card, output action and input cards.
  - `production_lines.js`: `#plSaveSetup`, camera remove (`data-act="remove"`), `#plAddCard`, `.pl-check-add`, `.pl-check-del`, Lines page New line / Rename / Clone / Delete, Products page New list / Rename / Replace / Import / Add product / Edit dialog Save / Delete product / code format Save, Line setup's Connect switch (an input: covered by the field rule).
  - `plc_signals.js`: Add input, delete signal. `camera_settings.js`: its Save if it has one. `product_fields.js`: list fields Save.
- [ ] **Step 1: Write the failing test** `test_save_buttons_are_marked`: for each known button (by id, or by its handler text such as `onclick="applySendActionCards()"`, `id="plSaveSetup"`) assert its tag contains `data-lock`; and `test_running_buttons_are_not_marked`: `id="plStartLine"`, `id="plStopLine"`, `id="plResetCounts"`, `PlcSignals.readNow`, `cs-trigger-btn` tags do not.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Mark** the buttons and the view-only fields.
- [ ] **Step 4: Run** the dashboard tests and `node --check` of each changed script → PASS.
- [ ] **Step 5: Commit** `Config lock: mark the save buttons of the locked pages`.

### Task 6: Docs, full suite, live check

**Files:**
- Modify: `README.md` (a "Configuration lock" paragraph under the security / accounts part, and the API line), `API_KEY_QUICKSTART.md` (`GET /api/v1/system/lock` under Monitoring; a 423 line under Common errors)
- Scratch only (not committed): a headless Chrome check script in the session scratchpad, based on `backup/scratch-tools/shots_s5.py` and `launcher.py` with `REPO` = this worktree.

- [ ] **Step 1:** write the docs.
- [ ] **Step 2:** full suite in the worktree (pt.sh's command line with `cd` into the worktree). Expected: only the known timing flakes, each passing alone.
- [ ] **Step 3:** live check on the scratch server (port 8765): admin sets code `lock-123`, locks; Line setup shows the banner, fields disabled, Save dimmed; a forced `PUT /lines/{id}` from the page answers 423 and shows the message once; Start/Stop and batch work; a Level 2 user (second headless profile) unlocks with the code; the admin's page turns editable within 2 s; screenshots at 1440 and 375 px; no page errors, no sideways scroll.
- [ ] **Step 4: Commit** `Config lock: docs`; stop the scratch server by its port's PID.
