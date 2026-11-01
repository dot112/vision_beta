# Camera pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Line setup shows each camera as a small card (picture, summary, Settings, Delete); Settings opens a page per camera with its settings split into pages listed in a menu on the left.

**Architecture:** Each camera keeps its one card element with all its fields (so save, checks, Unsaved changes, the lock and the text reader keep working); only the way it is shown changes. A new pure module, `assets/camera_pages.js`, decides the pages, the summary line and which page to show; `assets/production_lines.js` draws the card and switches Line setup between the cards and one camera's page by CSS classes.

**Tech Stack:** plain browser JavaScript (no build), CSS in production_lines.js's style block, pytest with node for the pure JS.

**Spec:** `docs/superpowers/specs/2026-11-01-camera-pages-design.md`

## Global Constraints

- No server change; no change to what any setting does or to Inputs, Send results and Output actions.
- Every page holds today's fields with today's notes; only Image and Video become one page, `data-fold="image"`, titled "Image and video".
- Page sections keep their `pl-fold` class and `data-fold` names; `open` means "the page shown".
- Page titles, in order: Camera, Job, AI model and classes, Counting ("Reading" for a text reader), Codes, Text fields, Image and video.
- Settings, Back and the menu buttons have no `data-lock`; Delete and the page's Save line setup have `data-lock` and `restricted-l2`.
- Remembered page per camera in local storage under `pl_card_page::<camera id>`, read and written in try/catch.
- Never the real `data/` or the owner's server on port 8000; live checks run on a scratch copy.
- Keep each file's own line endings (the repo has CRLF and LF files).
- Tests run with the main checkout's venv: `F:/projects/claude_fast_api_vision/fastapi_vision_server/.venv/Scripts/python -m pytest`, from the worktree.

## Review Focus

- Changing the Device on an open camera page: the page stays open and follows the new camera id, also after Save (Task 3 test `test_reopen_follows_the_device`).
- Back, or another page: the text reader's Live preview stops (no OCR requests in the background) (Task 3 test `test_leaving_a_page_wakes_the_text_reader`).
- A Save the server refuses keeps you on the camera page with your edits; a Save that works redraws and reopens the same camera and page (Task 3 test `test_the_camera_page_opens_closes_and_comes_back`, live check in Task 5).
- Switching to another line from a camera page shows the new line's cards, after the existing unsaved-changes question (Task 1 test `test_reopen_needs_the_same_line_and_camera`).
- Level 1 and a locked configuration: Settings, Back and the menu work; Save and Delete are greyed out or say "locked" (Task 2 lock tests, live check in Task 5).

---

### Task 1: The pure module `assets/camera_pages.js`

**Files:**
- Create: `assets/camera_pages.js`
- Modify: `dashboard.html` (script tag before `production_lines.js`)
- Test: `tests/test_camera_pages.py`

**Interfaces:**
- Produces (as `window.CameraPages` and `module.exports`, written like `assets/text_reader.js`):
  - `PAGES`: `[{ id: "camera", title: "Camera" }, { id: "job", title: "Job" }, { id: "model", title: "AI model and classes" }, { id: "counting", title: "Counting" }, { id: "codes", title: "Codes" }, { id: "text", title: "Text fields" }, { id: "image", title: "Image and video" }]`
  - `kind` objects everywhere below: `{ vision: boolean, codes: boolean, text: boolean }`
  - `pagesFor(kind) -> string[]`: camera, job always; model if vision; counting if vision or text; codes if codes; text if text; image always; in `PAGES` order.
  - `pageTitle(id, kind) -> string`: "Reading" for counting when `kind.text`, else the `PAGES` title.
  - `fits(id, kind) -> boolean`
  - `pageToShow({ asked, remembered }, kind) -> string`: `asked` if it fits, else `remembered` if it fits, else `"camera"`.
  - `pageAfterJobChange(current, kind) -> string`: `current` if it fits, else `"job"`.
  - `startPage(kind) -> string`: `"model"` for vision, `"text"` for text, `"codes"` for codes, else `"camera"`.
  - `pageForProblem(problem) -> string`: `no_model` → `model`, `no_list` → `codes`, `no_yolo` → `text`, `no_format` → `text`.
  - `summary({ index, type, kind, countMode, joined, codeTrigger }) -> string`: `Camera ${index + 1}`, then ` · ${type}` when type, then ` · ${what}`; what: text → "One picture per trigger" (countMode `trigger`) or "Shows text"; vision → "One picture per trigger" or "Count lines"; else by codeTrigger: continuous "Shows codes", line1 "Reads on wire line 1", line2 "Reads on wire line 2", signal "Reads when an input asks". Then ` · joined` when `joined`.
  - `menuHtml(kind, current) -> string`: `<nav class="pl-page-menu" role="tablist" aria-label="Camera settings">` with one `<button type="button" class="pl-page-link" data-act="page" data-page="<id>" role="tab" aria-selected="true|false">` per page in `PAGES` (title by `pageTitle`), `hidden` on pages that do not fit, class `on` on `current`.
  - `reopen(open, lineId, cameraIds) -> {id, page} | null`: `open` when it is set, `open.lineId === lineId` and `cameraIds` includes `open.id`; else null.

- [ ] **Step 1: Write the failing tests** in `tests/test_camera_pages.py`, running the module in node like `tests/test_text_reader_card.py` (`_node`, skip without node, `require("./assets/camera_pages.js")`):

```python
V = {"vision": True, "codes": False, "text": False}
VQ = {"vision": True, "codes": True, "text": False}
Q = {"vision": False, "codes": True, "text": False}
T = {"vision": False, "codes": False, "text": True}

def test_the_pages_each_job_shows():
    assert _run("pagesFor", V) == ["camera", "job", "model", "counting", "image"]
    assert _run("pagesFor", VQ) == ["camera", "job", "model", "counting", "codes", "image"]
    assert _run("pagesFor", Q) == ["camera", "job", "codes", "image"]
    assert _run("pagesFor", T) == ["camera", "job", "counting", "text", "image"]

def test_titles():
    assert _run("pageTitle", "counting", T) == "Reading" and _run("pageTitle", "counting", V) == "Counting"
    assert _run("pageTitle", "image", V) == "Image and video"

def test_which_page_shows():
    assert _run("pageToShow", {"asked": "codes", "remembered": "model"}, V) == "model"
    assert _run("pageToShow", {"asked": None, "remembered": None}, V) == "camera"
    assert _run("pageToShow", {"asked": "text", "remembered": "model"}, T) == "text"
    assert _run("pageAfterJobChange", "model", Q) == "job" and _run("pageAfterJobChange", "image", Q) == "image"
    assert [_run("startPage", k) for k in (V, VQ, Q, T)] == ["model", "model", "codes", "text"]
    assert [_run("pageForProblem", p) for p in ("no_model", "no_list", "no_yolo", "no_format")] == ["model", "codes", "text", "text"]

def test_the_summary_line():
    s = lambda **kw: _run("summary", {"index": 1, "type": "IP", "countMode": "track", "joined": False, "codeTrigger": "continuous", **kw})
    assert s(kind=T, countMode="trigger") == "Camera 2 · IP · One picture per trigger"
    assert s(kind=T) == "Camera 2 · IP · Shows text"
    assert s(kind=V) == "Camera 2 · IP · Count lines"
    assert s(kind=VQ, countMode="trigger", joined=True) == "Camera 2 · IP · One picture per trigger · joined"
    assert s(kind=Q, codeTrigger="signal") == "Camera 2 · IP · Reads when an input asks"
    assert s(kind=Q, codeTrigger="line2", type="") == "Camera 2 · Reads on wire line 2"

def test_the_menu():
    html = _run("menuHtml", T, "counting")
    assert html.count('data-act="page"') == 7 and 'data-page="model" role="tab" aria-selected="false" hidden' in html
    assert re.search(r'class="pl-page-link on" data-act="page" data-page="counting"[^>]*>Reading<', html)
    assert "data-lock" not in html

def test_reopen_needs_the_same_line_and_camera():
    o = {"id": "c1", "page": "codes", "lineId": "line-1"}
    assert _run("reopen", o, "line-1", ["c0", "c1"]) == o
    assert _run("reopen", o, "line-2", ["c1"]) is None and _run("reopen", o, "line-1", ["c0"]) is None
    assert _run("reopen", None, "line-1", ["c1"]) is None

def test_the_page_loads_the_module_before_the_line_setup():
    page = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    assert page.index("/assets/camera_pages.js?v=1.0.0") < page.index("/assets/production_lines.js?v=")
```

(`_run(name, *args)` calls the export with the JSON arguments and returns the JSON result. Keep the attribute order of the menu buttons as in the interface, so the assertions above match.)

- [ ] **Step 2: Run them to see them fail**

Run: `.../python -m pytest tests/test_camera_pages.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Write `assets/camera_pages.js`** with the interface above, and add `<script src="/assets/camera_pages.js?v=1.0.0"></script>` before the `production_lines.js` tag in `dashboard.html`.

- [ ] **Step 4: Run them to see them pass**

Run: `.../python -m pytest tests/test_camera_pages.py -q`
Expected: all pass.

- [ ] **Step 5: Commit** `Camera pages: the pages, the summary line and which page to show`

---

### Task 2: The small card and the page sections

**Files:**
- Modify: `assets/production_lines.js` (`FOLDS`/`foldShown` near line 900, `openFolds`/`rememberFolds`/`foldHtml` near 1047, `cameraCardHtml` 1068-1235, `syncCard` 1359+, `addCard`/`renderImageForms`/`bindCard` 1752-1830, the style block 236-292)
- Modify: `assets/text_reader.js` (the fold-button listener near line 560)
- Modify: `dashboard.html` (`?v=` of production_lines.js to 3.14.0, text_reader.js to 1.0.1)
- Test: `tests/test_line_setup_markup.py`, `tests/test_config_lock_dashboard.py`, `tests/test_text_reader_card.py`

**Interfaces:**
- Consumes: `CameraPages.PAGES`, `pagesFor`, `pageTitle`, `summary`, `menuHtml` (Task 1).
- Produces:
  - `jobKind(job) -> {vision, codes, text}` in production_lines.js: `{ vision: isVisionJob(job), codes: Boolean(JOBS[job].codes), text: job === "text" }`; replaces `foldShown` and `FOLDS`.
  - `pageHtml(name, title, body)`: `<section class="pl-fold" data-fold="<name>"><h3 class="pl-page-title"><title></h3><div class="pl-fold-body"><body></div></section>`; replaces `foldHtml`.
  - Card markup, in order: `.pl-card-head` (dot, `.pl-card-name`, badge, then `.pl-card-tools` with the arrows `data-act="up"` / `data-act="down"` (titles "Move earlier" / "Move later", left/right chevrons); in page mode it also shows `data-act="back"`, `.pl-card-unsaved` and `data-act="save"`), `.pl-card-sub` (summary), `.pl-card-warn` (the upgrade warning, hidden unless flagged), `.pl-card-live`, `.pl-card-actions` with `<button type="button" class="btn-action btn-outline" data-act="settings">Settings</button>` and `<button data-lock type="button" class="btn-action btn-red restricted-l2" data-act="remove">Delete</button>`, then `CameraPages.menuHtml(...)`, then `.pl-folds` with the page sections.
  - The Image and Video sections become one: `pageHtml("image", "Image and video", '<div data-f="image-form"></div><h4 class="pl-page-sub">Video</h4><div data-f="video-form"></div>')`.

- [ ] **Step 1: Write the failing markup tests** in `tests/test_line_setup_markup.py`:

```python
def test_the_card_has_settings_and_delete_and_no_fold_buttons():
    script = LINES.read_text(encoding="utf-8")
    assert 'data-act="settings">Settings</button>' in script
    assert re.search(r'<button data-lock type="button" class="btn-action btn-red restricted-l2" data-act="remove"[^>]*>Delete</button>', script)
    assert "pl-fold-head" not in script and "openFolds(" not in script and "pl_card_folds" not in script
    assert 'pageHtml("image", "Image and video"' in script and 'data-fold="video"' not in script
    assert "CameraPages.menuHtml(" in script and "CameraPages.summary(" in script

def test_the_text_reader_no_longer_listens_for_fold_buttons():
    assert "pl-fold-head" not in (ROOT / "assets" / "text_reader.js").read_text(encoding="utf-8")
```

In `tests/test_config_lock_dashboard.py` add `'data-act="save"'` to `SAVE_BUTTONS["assets/production_lines.js"]`, and `'data-act="settings"'`, `'data-act="back"'` to `RUNNING_BUTTONS["assets/production_lines.js"]`. In `tests/test_text_reader_card.py` change `'["text", "Text fields"]' in script` to check `'{ id: "text", title: "Text fields" }'` in `assets/camera_pages.js`.

- [ ] **Step 2: Run them to see them fail**

Run: `.../python -m pytest tests/test_line_setup_markup.py tests/test_config_lock_dashboard.py tests/test_text_reader_card.py -q`
Expected: the new assertions fail.

- [ ] **Step 3: Change the card** as in the Interfaces block:
  - `cameraCardHtml(key, cam, trigger)` (no `openSet`): the head, summary, warning line ("Choose when it reads codes (Settings → Codes)" when `cam.qr_trigger_review`), live picture, actions, menu (`CameraPages.menuHtml(jobKind(job), null)`), and the page sections built with `pageHtml` from today's bodies, unchanged.
  - The page's Back / Unsaved / Save live in the head: `<button type="button" class="pl-icon-btn pl-back" data-act="back" title="Back to the cameras" aria-label="Back to the cameras">` (left chevron) before the dot, and after the tools `<span class="pl-unsaved pl-card-unsaved">Unsaved changes</span><button data-lock type="button" class="btn-action btn-blue restricted-l2" data-act="save" ${readOnly ? "disabled" : ""}>Save line setup</button>`. CSS shows them only in page mode (Task 3).
  - `syncCard`: `f.hidden = !CameraPages.fits(f.dataset.fold, kind)` for the sections, the same for each `.pl-page-link`; the counting title (`.pl-fold[data-fold="counting"] .pl-page-title` and its menu link) from `CameraPages.pageTitle("counting", kind)`; `.pl-card-sub` from `CameraPages.summary({ index, type: row ? typeLabel(row.type) : "", kind, countMode: cardValue(card, "count_mode"), joined: stationShown && joins, codeTrigger: cardValue(card, "qr_trigger") })` (move the line after `joins` is known); `.pl-card-warn` hidden unless `card.dataset.review === "1"` and the trigger is continuous.
  - `addCard(cam, trigger)`: no open set, no `rememberFolds`, no picture load on open folds; `renderImageForms` no longer loads the image picture. `bindCard`: drop the fold-button branch.
  - Delete the unused `FOLDS`, `foldShown`, `openFolds`, `rememberFolds`, `foldHtml`; `renderLineSetup` calls `addCard(cam, trigger)`.
  - `text_reader.js`: drop the `.pl-fold-head` click listener in `bind` (production_lines calls `wake`, Task 3), and update its header comment ("the Text fields page").
  - Styles: `.pl-card:not(.pl-card-page) .pl-folds, .pl-card:not(.pl-card-page) .pl-page-menu, .pl-card:not(.pl-card-page) .pl-back, .pl-card:not(.pl-card-page) .pl-card-unsaved, .pl-card:not(.pl-card-page) [data-act="save"] { display:none; }`; `.pl-card-actions { display:flex; gap:8px; } .pl-card-actions .btn-action { flex:1; }`; `.pl-card-warn` in the warning colour like `.pl-warn`; `.pl-page-title { font-size:15px; font-weight:700; margin:0 0 12px; }`; `.pl-page-sub` for the Video heading; remove the `.pl-fold-head` rules and the `.pl-fold` top border.

- [ ] **Step 4: Run the tests**

Run: `.../python -m pytest tests/test_line_setup_markup.py tests/test_config_lock_dashboard.py tests/test_text_reader_card.py tests/test_camera_setup_markup.py tests/test_trigger_card_markup.py tests/test_help_tips.py -q`
Expected: all pass.

- [ ] **Step 5: Commit** `Camera pages: the small camera card, its pages instead of folds`

---

### Task 3: The camera page

**Files:**
- Modify: `assets/production_lines.js` (`setup`, `markDirty`, `bindCard`, `startCardFeed`/`startCardFeeds`, `renderLineSetup`, `switchLine`, the style block, the Device change handler)
- Test: `tests/test_line_setup_markup.py`

**Interfaces:**
- Consumes: `CameraPages.pageToShow`, `pageAfterJobChange`, `reopen`, `fits` (Task 1); `jobKind`, card markup (Task 2).
- Produces:
  - `setup.openCamera`: `{ id, page, lineId } | null`, kept across `renderLineSetup`.
  - `openCameraPage(card, asked)`: page = `CameraPages.pageToShow({ asked, remembered: rememberedPage(id) }, jobKind(cardJob(card)))`; adds `pl-card-page` to the card and `pl-page-mode` to `#tabActionTrigger`; stops the other cards' feeds; `showCameraPage(card, page)`; scrolls to the top of the tab.
  - `showCameraPage(card, page)`: sets `open` on that section only and `on` / `aria-selected` on its menu link; `setup.openCamera = { id: card.dataset.camera, page, lineId: state.lineId }`; `rememberPage(id, page)`; loads the count-line picture for `counting` (`loadLinesPicture`) and the image picture for `image` (`imageForm.loadPicture`); calls `TextReader.wake(card)`; `refreshSelects()`.
  - `closeCameraPage()`: removes the classes and every section's `open`, `setup.openCamera = null`, calls `TextReader.wake` on the card, `startCardFeeds()`.
  - `rememberedPage(id) -> string | null`, `rememberPage(id, page)`: local storage `pl_card_page::<id>`, in try/catch.

- [ ] **Step 1: Write the failing tests** in `tests/test_line_setup_markup.py`:

```python
def test_the_camera_page_opens_closes_and_comes_back():
    script = LINES.read_text(encoding="utf-8")
    for name in ("function openCameraPage(card, asked)", "function showCameraPage(card, page)", "function closeCameraPage()"):
        assert name in script
    assert "CameraPages.reopen(setup.openCamera, state.lineId" in script        # renderLineSetup draws it again
    assert "pl_card_page::" in script and "pl-page-mode" in script

def test_leaving_a_page_wakes_the_text_reader():
    body = _function(LINES.read_text(encoding="utf-8"), "closeCameraPage")
    assert "TextReader.wake(" in body
    assert "TextReader.wake(" in _function(LINES.read_text(encoding="utf-8"), "showCameraPage")

def test_reopen_follows_the_device():
    script = LINES.read_text(encoding="utf-8")
    assert re.search(r"setup\.openCamera && setup\.openCamera\.id === previous\)\s*setup\.openCamera\.id = ", script)
```

(`_function` cuts a function's text out of the script, as `tests/test_config_lock_dashboard.py` does; copy its helper.)

- [ ] **Step 2: Run them to see them fail**

Run: `.../python -m pytest tests/test_line_setup_markup.py -q`
Expected: the three new tests fail.

- [ ] **Step 3: Write the camera page**:
  - The functions in the Interfaces block. `bindCard`'s click: `settings` → `openCameraPage(card, null)`, `back` → `closeCameraPage()`, `page` → `showCameraPage(card, button.dataset.page)`, `save` → `saveLineSetup()`.
  - The job change (`onJobChange` or where `syncCard` runs after it): when the card is the open page, `showCameraPage(card, CameraPages.pageAfterJobChange(setup.openCamera.page, kind))`.
  - The Device change (where `card.dataset.camera` is set to the new id): keep `previous`, and `if (setup.openCamera && setup.openCamera.id === previous) setup.openCamera.id = <new id>`.
  - `startCardFeeds`: in page mode only the open card's feed starts.
  - `markDirty` also adds `pl-dirty` to `#plLineSetup`; `renderLineSetup` removes it where it sets `setup.dirty = false`.
  - `renderLineSetup`, after the cards are added: `const open = CameraPages.reopen(setup.openCamera, state.lineId, cardsOnPage().map((c) => c.dataset.camera))`; when set, `openCameraPage(<that card>, open.page)`, else clear `pl-page-mode` and `setup.openCamera = null`. The early return that keeps unsaved edits stays as it is (the page is still open).
  - Styles, in the style block:
    - `#tabActionTrigger.pl-page-mode > :not(#plLineSetup), #tabActionTrigger.pl-page-mode #plLineSetup > :not(#plCamGrid), #tabActionTrigger.pl-page-mode #plCamGrid > :not(.pl-card-page) { display:none !important; }`
    - `#tabActionTrigger.pl-page-mode #plCamGrid { display:block; }`
    - `.pl-card-page { display:grid; grid-template-columns:260px minmax(0, 1fr); grid-template-areas:"head head" "live pages" "menu pages"; grid-template-rows:auto auto 1fr; gap:12px 20px; padding:16px 20px; }` with `.pl-card-head` in `head` (bottom border, padding-bottom 12px), `.pl-card-live` in `live`, `.pl-page-menu` in `menu`, `.pl-folds` in `pages`; `.pl-card-page .pl-card-sub, .pl-card-page .pl-card-warn, .pl-card-page .pl-card-actions, .pl-card-page .pl-card-tools [data-act="up"], .pl-card-page .pl-card-tools [data-act="down"] { display:none; }`; `.pl-card-page .pl-fold:not(.open) { display:none; }`; `.pl-card-unsaved` shown only under `#plLineSetup.pl-dirty`.
    - The menu: `.pl-page-menu { display:flex; flex-direction:column; gap:2px; }`, `.pl-page-link` full width, left aligned, 8px 10px padding, 6px radius, transparent; `.pl-page-link.on` in the primary colour with a light primary background.
    - `@media (max-width:720px)`: `.pl-card-page { grid-template-columns:minmax(0, 1fr); grid-template-areas:"head" "live" "menu" "pages"; }`, `.pl-page-menu { flex-direction:row; overflow-x:auto; }`, `.pl-page-link { flex:none; white-space:nowrap; }`; the head wraps so Save goes on its own line.

- [ ] **Step 4: Run the tests**

Run: `.../python -m pytest tests/test_line_setup_markup.py tests/test_camera_pages.py tests/test_text_reader_card.py -q`
Expected: all pass.

- [ ] **Step 5: Commit** `Camera pages: the camera page with its menu`

---

### Task 4: The ways in: Add camera, a Save problem, the Cameras page

**Files:**
- Modify: `assets/production_lines.js` (`bindAddCard`'s add, `saveLineSetup`'s checks, `focusCard`, the `openCamSettings` wrapper)
- Test: `tests/test_line_setup_markup.py`

**Interfaces:**
- Consumes: `openCameraPage(card, asked)` (Task 3), `CameraPages.startPage`, `pageForProblem` (Task 1).
- Produces: `setup.focusCamera` stays the Cameras page's way in; `focusCard()` now opens `openCameraPage(card, "image")`.

- [ ] **Step 1: Write the failing tests**:

```python
def test_the_ways_into_a_camera_page():
    script = LINES.read_text(encoding="utf-8")
    assert "openCameraPage(added, CameraPages.startPage(jobKind(job)))" in script
    for problem in ("no_model", "no_list", "no_yolo", "no_format"):
        assert f'CameraPages.pageForProblem("{problem}")' in script
    assert 'openCameraPage(card, "image")' in _function(script, "focusCard")
```

- [ ] **Step 2: Run it to see it fail**

Run: `.../python -m pytest tests/test_line_setup_markup.py -q`
Expected: FAIL.

- [ ] **Step 3: Write the ways in**:
  - Add camera: `addCard(cam, trigger)` then, after the job is set, `openCameraPage(added, CameraPages.startPage(jobKind(job)))` (instead of scrolling to the card).
  - `saveLineSetup`: each camera check keeps its toast and also opens that card's page: `openCameraPage(cards[i], CameraPages.pageForProblem("no_model"))` (and `no_list`, `no_yolo`, `no_format`).
  - `focusCard()`: open the camera's page at `"image"` instead of its Camera and Image folds; no scroll or flash.

- [ ] **Step 4: Run the whole suite**

Run: `.../python -m pytest -q -x -p no:cacheprovider` (as the project runs it; the known Windows-only failures and the three timing-flaky tests are allowed, rerun those alone)
Expected: all pass apart from those.

- [ ] **Step 5: Commit** `Camera pages: Add camera, Save problems and the Cameras page open the camera's page`

---

### Task 5: Live check, docs and the vault

**Files:**
- Modify: `README.md` (the Line setup lines that mention the card's folds, if any)
- Scratch only: `backup/scratch-tools/shots_camera_pages.py` (git-ignored)

- [ ] **Step 1: Run a scratch server** on a copy of `data/` with `backup/scratch-tools/launcher_ocr.py` (spare port, never 8000), with the fake cameras it starts.

- [ ] **Step 2: Check in the browser**, on Line 1 of the copy:
  - the cards show picture, summary, Settings and Delete; the arrows move them;
  - Settings opens the page with the menu; each page shows today's fields; Image and video shows both forms;
  - change the job: the menu changes, a page that no longer fits goes to Job;
  - change a field, Back, Settings again: the edit is still there; Save on the page: stays on it;
  - Save with a vision camera's model cleared: the toast, and the AI model page opens;
  - the text reader page's Live stops after Back (no `/api/v1/ocr/` requests);
  - Add camera opens the new camera's page; the Cameras page Settings opens Image and video;
  - Level 1 user: Settings works, Save and Delete greyed; the lock on: Settings, Back and the menu work, Save says locked.

- [ ] **Step 3: Screenshots** at desktop (1400 px) and phone (390 px) width with headless Chrome (`shots_camera_pages.py`, like `shots_ocr.py`): the cards, a camera page, the phone page with the tab row; check there is no sideways scroll.

- [ ] **Step 4: Fix what the check finds**, test first where a test can show it; commit each fix.

- [ ] **Step 5: README and vault**: update any README text about the card's folds; add a section to the vault ("03 Decisions/Camera pages decisions.md" with the spec's choices and the branch), and a line on Home.md. Commit `Camera pages: docs`.
