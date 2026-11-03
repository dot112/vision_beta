# Pattern Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Pattern check" camera job on a line: trained on the camera's page from a picture, boxes, a mask and good samples; each product is found by its edges and compared with the good samples, giving a Match % and a biggest defect per check, and good or reject as an own or joined station.

**Architecture:** `pattern_find` (edge model, coarse-to-fine search) → `pattern_check` (one picture: find, line up, checks, defects, result) and `pattern_training` (a set from a draft) → `match_references` (files: sets, drafts, clean-up) → `match_service` (one checker thread per camera, the overlay). On the line a pattern check is a station like a text reader on One picture per trigger: its pictures come through `trigger_inspection` and the hardware path, and its products carry `match` and `match_status`.

**Tech Stack:** Python 3.14 (Docker 3.12), FastAPI, OpenCV 5 (`opencv-python-headless`), numpy, plain JS dashboard (node tests).

**Spec:** `docs/superpowers/specs/2026-11-03-pattern-check-design.md`

**Refinements of the spec made by this plan (the spec is updated in the same commit):**
- Formulas allow one level after a root (`text.Lot`); `match` alone takes two: `match.score`, `match.<check>.score`, `match.<check>.defect`.
- A picture whose size is not the training picture's size gives `pattern_error` "The camera's picture is W × H; the reference was trained on w × h: train it again" (errors table).
- A grey (one-channel) picture is checked as Grey whatever a check's Colour switch says (industrial mono cameras).

## Global Constraints

- Work in the worktree `F:\projects\claude_fast_api_vision\fastapi_vision_server\.claude\worktrees\pattern-check`, branch `claude/pattern-check`. Never push. Never touch the repo's real `data/`, the owner's server on port 8000, or the owner's Node-RED.
- Python: `PY=../../../.venv/Scripts/python.exe` from the worktree (the main checkout's venv). One test file: `$PY -m pytest tests/<file>.py -q -p no:cacheprovider`. Full suite: the command in `backup/scratch-tools/pt.sh` (main checkout) run from the worktree with `.venv` replaced by `../../../.venv` (keep its `--deselect` lines).
- Keep each file's line endings (CRLF or LF; new files CRLF like their neighbours); script multi-line edits with `open(..., newline="")`. Write scripts to the scratchpad, not Bash heredocs with triple quotes.
- No new dependency. OpenCV and numpy only; CPU only (never the DirectML lock `_DML_LOCK`).
- Role `match`; job "Pattern check"; settings key `match`; settings version stays 9.
- Limits: `angle` whole degrees 0–45, default 15; 1–4 checks; check `name` `[A-Za-z][A-Za-z0-9_]{0,31}`, unique ignoring case, not `score`; `min_score` whole 0–100, default 80; `max_defect_px` whole 0–1 000 000, default 30; `colour` default `false`; `sensitivity` `low`/`normal`/`high`, default `normal`; `search_area` `null` or `[x1, y1, x2, y2]` fractions, each side ≥ 0.02; `reference_id` `ref-` + 8 lowercase hex.
- Engine numbers: reference edge point gradient ≥ 20, picture ≥ 10 (Sobel 3 × 3, grey 0–255); pyramid top where the reference's shorter side is about 24 px, at most 4 levels; angle step moves the far corner 1.5 px; window ±4 px per finer level; checks 2–4 search ±6 px and ±1°; Sensitivity (k, floor) low (5, 35), normal (4, 25), high (3, 15); 3 × 3 widening of the tolerance; 2 px box border skipped; 3 × 3 opening; 5 spots kept.
- Training numbers: box ≥ 16 px; ≥ 100 edge points at full size and ≥ 20 at the top; a sample found when raw score ≥ 0.5; at most 20 samples; suggestions with ≥ 3 samples: `min_score` = lowest leave-one-out Match % − 5 within 50–95, `max_defect_px` = 1.5 × biggest good spot + 5, at least 10; fewer: 80 and 30 and a note.
- Reasons and PLC codes: `pattern_not_found` 14, `pattern_mismatch` 15, `pattern_defect` 16, `pattern_error` 17. Classes: `pattern ok`, `pattern not found`, `pattern mismatch`, `pattern defect`, `pattern error`. Statuses: `good`, `not_found`, `mismatch`, `defect`, `error`.
- Copy: video bar `PATTERN CHECK`, `NO REFERENCE`, `GOOD · MATCH 97%`, `REJECT · Cap: defect 48 px`; page "Pattern and checks"; Counting page titled "When to check"; panel "Latest pattern checks"; refusals "Reference not found in this picture", "Draw the reference around printing or a shape".
- Files: `data/match-references/<id>/` (a set, written once), `data/match-references/drafts/<camera_id>/` (a draft). Clean-up keeps a set named by a line's camera, a saved line setup or a draft, or made in the last 24 h; drafts unused for 30 days go.
- A trigger waits for the check as for a model: `TRIGGER_INFERENCE_TIMEOUT_S` (10 s). Held setup picture dropped after 600 s unused (shared with the text reader).
- Help icons (`data-help`) only on Sensitivity and the mask brush.
- Each task ends with its tests passing and a commit whose message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A grey industrial camera:** a one-channel (mono) picture must train and check, Colour checks working as Grey, not crash on `cv2.cvtColor` (Task 2, `test_a_grey_picture_is_checked_as_grey`; Task 3, `test_training_from_grey_pictures`).
2. **The camera's picture changed size after training** (resolution or ROI): every product must not quietly reject as "not found"; it is `pattern_error` with "train it again", and the video says so (Task 2, `test_a_picture_of_another_size_is_an_error`).
3. **A product at the edge of the picture:** the found reference near an edge puts a check box partly outside; the outside part counts as masked, under half inside is Match 0 %, and nothing crashes (Task 2, `test_a_check_box_partly_outside_the_picture`).
4. **An older page saves the line:** a camera entry sent without `match` (or its picture settings) keeps the saved ones; without that a stale tab wipes the training link (Task 5, `test_a_client_that_leaves_match_out_keeps_it`).
5. **The line stops mid-check:** a check that finishes after the line stopped or the camera left the line makes no product and no listed check (Task 8, `test_a_result_after_the_line_stopped_makes_nothing`).

---

### Task 1: The edge search

**Files:**
- Create: `app/engines/pattern_find.py`, `tests/pattern_pictures.py` (drawing helpers shared by Tasks 1–3 and the live check's fake camera)
- Test: `tests/test_pattern_find.py`

**Interfaces:**
- Produces (`tests/pattern_pictures.py`): `label(w=360, h=240, text="ORANGE") -> np.ndarray` (grey uint8, the probe's label); `scene(lab, angle, cx, cy, gain=1.0, offset=0, size=(1280, 960), seed=0, defect=False, colour=None) -> np.ndarray` (BGR uint8; `defect` adds the probe's 12 px spot and scratch; `colour` (B, G, R) fills the label's circle, for the colour tests); `crop(img, cx, cy, w, h) -> np.ndarray`.
- Produces (`app/engines/pattern_find.py`, no runtime imports beyond cv2 and numpy):
  - `EDGE_REF_MIN = 20`, `EDGE_PIC_MIN = 10`, `TOP_SIDE = 24`, `MAX_LEVELS = 4`, `STEP_PX = 1.5`, `WINDOW_PX = 4`
  - `class Found(NamedTuple): x: float; y: float; angle: float; score: float` (the reference's centre in the searched picture's pixels, degrees counter-clockwise as `cv2.getRotationMatrix2D`, raw score 0–1)
  - `class EdgeModel`: `levels: int`, `points: int` (edge points at full size, angle 0), `top_points: int`, `size: Tuple[int, int]` (w, h), `angle: int`
  - `build_edge_model(ref: np.ndarray, ignore: Optional[np.ndarray], angle: int) -> EdgeModel` (`ref` grey uint8; `ignore` bool, True = masked)
  - `find(model: EdgeModel, grey: np.ndarray, area: Optional[Tuple[int, int, int, int]] = None) -> Optional[Found]` (`area` x1, y1, x2, y2 in pixels; `None` when the area is smaller than the reference)
  - `refine(model: EdgeModel, grey: np.ndarray, near: Found, shift_px: int = 6, turn_deg: float = 1.0) -> Found` (full size only, around `near`)
  - `to_grey(img: np.ndarray) -> np.ndarray` (BGR or one channel → grey uint8)

- [ ] **Step 1: Write the failing tests**

```python
from app.engines.pattern_find import build_edge_model, find, refine
from tests.pattern_pictures import crop, label, scene

REF = crop(scene(label(), 0, 640, 480), 640, 480, 360, 240)

@pytest.mark.parametrize("angle, cx, cy, gain, offset", [
    (9.46, 757.9, 456.1, 1.03, -29), (-10.61, 749.8, 518.0, 1.19, -2), (12.74, 575.4, 495.2, 1.10, -8),
    (-14.0, 420.0, 330.0, 0.6, 30), (0.0, 640.0, 480.0, 1.0, 0)])
def test_finds_a_turned_shifted_relit_label(angle, cx, cy, gain, offset):
    model = build_edge_model(to_grey(REF), None, 15)
    found = find(model, to_grey(scene(label(), angle, cx, cy, gain, offset, seed=3)))
    assert abs(found.x - cx) <= 2 and abs(found.y - cy) <= 2 and abs(found.angle - angle) <= 0.5
    assert found.score >= 0.6

def test_wrong_blank_and_no_product_score_low():
    model = build_edge_model(to_grey(REF), None, 15)
    assert find(model, to_grey(scene(label(text="APPLE"), 5, 600, 500))).score < 0.55
    assert find(model, to_grey(scene(np.full((240, 360), 225, np.uint8), 0, 600, 500))).score < 0.1

def test_a_search_area_limits_the_search_and_a_small_one_gives_none():
    # a second label outside the area is not found; an area 100 x 100 gives None

def test_masked_edges_are_left_out():
    # masking the word ORANGE: model.points drops; a picture with "APPLE" now scores like a good one (>= 0.6)

def test_refine_finds_a_check_close_by():
    # a small model of the circle; refine from a Found 5 px and 0.8 deg off lands within 1 px and 0.3 deg

def test_angle_zero_searches_without_turning(): ...   # build_edge_model(ref, None, 0): a label turned 10 deg scores < 0.6

def test_speed_is_measured(capsys):
    # 1280 x 960, +-15 deg, whole picture: prints the median of 5 runs in ms; fails only over 300 ms
```

- [ ] **Step 2: Run them to see them fail** — `$PY -m pytest tests/test_pattern_find.py -q -p no:cacheprovider` — Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement** on the probe's method (scratchpad `match_probe/probe_fast.py`, copied to `backup/scratch-tools/match_probe.py` in the main checkout): per level and angle, the reference turned with its valid area (`rotate` keeping the whole box, border 0) and its unit gradients zeroed under the gradient floor, the mask and outside the valid area; the score map is `(matchTemplate(px, mx, TM_CCORR) + matchTemplate(py, my, TM_CCORR)) / n`. Angles at a level: `step = degrees(atan(STEP_PX / radius))`, radius half the turned reference's diagonal at that level; the finer levels take the angles within two steps of the best one and a window of ±`WINDOW_PX` around it, with gradients worked out only inside the window. Models are built once per `(ref, ignore, angle)` call; the caller caches them (Task 2).

- [ ] **Step 4: Run the tests** — Expected: all pass. Write the measured time into the task's commit message; if it is over 60 ms, tune the window and angle steps (keep the tests passing) before committing.

- [ ] **Step 5: Commit** (`Pattern check: the edge search`)

---

### Task 2: Checking one picture

**Files:**
- Create: `app/engines/pattern_check.py`
- Test: `tests/test_pattern_check.py`

**Interfaces:**
- Consumes: `build_edge_model`, `find`, `refine`, `Found`, `to_grey` (Task 1)
- Produces:
  - `SENSITIVITY = {"low": (5, 35), "normal": (4, 25), "high": (3, 15)}`
  - `@dataclass class CheckModel: box: Tuple[float, float, float, float]` (x1, y1, x2, y2 in pixels from the reference box's centre; check 1's box is the reference box); `mean: np.ndarray` (h × w × 3 BGR uint8); `spread: np.ndarray` (h × w × 3 Lab float32); `ignore: np.ndarray` (h × w bool); `typical: float`
  - `@dataclass class ReferenceSet: id: str; picture_size: Tuple[int, int]; checks: List[CheckModel]; samples: int` and `edge_model(self, index: int, angle: int) -> EdgeModel` (built from `checks[index].mean` and `ignore`, cached per `(index, angle)` on the object; thread-safe)
  - `class CheckResult(NamedTuple): name: str; score: int; defect_px: int; spots: List[List[int]]` (picture boxes x1, y1, x2, y2); `box: List[List[float]]` (4 corners in the picture); `fits: bool`
  - `@dataclass class PatternResult: width: int; height: int; ms: float; found: Optional[Found]; checks: List[CheckResult]; result: str; reason: Optional[str]; error: Optional[str] = None`, property `status -> str` (`good`, `not_found`, `mismatch`, `defect`, `error`), `to_dict() -> Dict` (the spec's "Out" shape)
  - `check_picture(ref: ReferenceSet, match: Dict[str, Any], picture: np.ndarray) -> PatternResult` (`match` = a camera's normalized `match` settings)
  - `error_result(error: str, width: int = 0, height: int = 0) -> PatternResult` (reject, `pattern_error`)
  - `match_percent(raw: float, typical: float) -> int`
  - `defects(lined_up: np.ndarray, check: CheckModel, colour: bool, sensitivity: str) -> Tuple[int, List[List[int]]]` (spots in the lined-up box's pixels)
  - `line_up(picture: np.ndarray, found: Found, box: Tuple[float, float, float, float]) -> Tuple[np.ndarray, np.ndarray]` (the box cut out turned back by `found.angle` around `found`'s centre, and its "inside the picture" plane; used by Task 3 too)
  - `match_values(result: PatternResult) -> Dict[str, Any]` → `{"score": <check 1 %>, "status": ..., "checks": {name: {"score", "defect_px"}}}` (checks not done left out; `{"status": "error"}` only for an error)

- [ ] **Step 1: Write the failing tests** (a `ReferenceSet` built in the test from 8 lined-up good crops of `tests/pattern_pictures.scene`, with a helper `_set(samples, checks=...)` that the test file defines from `mean`/`std`; Task 3 replaces it by `train`)

```python
def test_a_good_label_passes_and_a_defect_is_found():
    good = check_picture(SET, MATCH, scene(label(), 7.8, 870, 475, 1.16, -30, seed=11))
    assert good.result == "good" and good.checks[0].score >= 95 and good.checks[0].defect_px == 0
    bad = check_picture(SET, MATCH, scene(label(), -9.0, 654, 342, 1.19, -21, seed=12, defect=True))
    assert bad.reason == "pattern_defect" and bad.checks[0].defect_px >= 100 and bad.checks[0].spots

def test_a_wrong_label_is_not_found_and_stops_there():
    r = check_picture(SET, MATCH, scene(label(text="APPLE"), 5, 600, 500))
    assert r.reason == "pattern_not_found" and len(r.checks) == 1 and r.status == "not_found"

def test_a_missing_cap_is_a_mismatch():   # check 2 = the circle; a label without it: reason "pattern_mismatch"

def test_sensitivity_and_the_floor():     # a spot 20 grey levels off: found with "high", not with "normal" or "low"

def test_colour_rejects_a_wrong_coloured_cap_that_grey_passes():
    # circle filled blue in training, red in the picture: colour=True -> pattern_defect on check 2; colour=False -> good

def test_a_grey_picture_is_checked_as_grey():             # to_grey(picture)[..., None]-shaped and 2-D pictures: same result as Grey

def test_a_picture_of_another_size_is_an_error():
    r = check_picture(SET, MATCH, cv2.resize(scene(label(), 0, 640, 480), (640, 480)))
    assert r.reason == "pattern_error" and "trained on 1280 × 960" in r.error and "train it again" in r.error

def test_a_check_box_partly_outside_the_picture():
    # the label found at the right edge: check 2's box half outside -> scored on the inside part; 3/4 outside -> score 0, mismatch

def test_masked_pixels_are_not_defects():  # a different date printed under the mask: good

def test_the_edge_model_is_cached_per_check_and_angle():   # two calls with angle 15, one with 5: built twice

def test_match_values_and_percent():
    assert match_percent(0.74, 0.74) == 100 and match_percent(0.45, 0.75) == 60 and match_percent(0.9, 0.75) == 100
    # match_values of a good result: {"score": 100, "status": "good", "checks": {"Label": {"score": 100, "defect_px": 0}, ...}}
```

- [ ] **Step 2: Run them to see them fail**

- [ ] **Step 3: Implement** in the spec's order (section 4). The size check first. Line up with `cv2.getRotationMatrix2D((x, y), -angle, 1)` shifted so the check's box lands at its own size (`cv2.warpAffine`, `INTER_LINEAR`, border 0) and the same transform on a "valid" plane (255 inside the picture) to know the outside part. Light: over unmasked valid pixels, Grey `(v − mean_v) / std_v × std_m + mean_m`; Colour: Lab, the L channel so, a and b as they are; Grey uses the L channel only. Tolerance `max(spread × k, floor)` then `cv2.dilate(…, 3 × 3)`; different pixels, minus ignored, outside and a 2 px border, `MORPH_OPEN` 3 × 3, `connectedComponentsWithStats`; spots mapped back to the picture with the inverse transform (their boxes' corners, then min/max). `ms` from `time.perf_counter()`.

- [ ] **Step 4: Run the tests and `tests/test_pattern_find.py`** — Expected: all pass

- [ ] **Step 5: Commit** (`Pattern check: checking one picture`)

---

### Task 3: Training

**Files:**
- Create: `app/engines/pattern_training.py`
- Modify: `tests/test_pattern_check.py` (its `SET` comes from `train` now; the hand-made helper goes)
- Test: `tests/test_pattern_training.py`

**Interfaces:**
- Consumes: Tasks 1–2 (`build_edge_model`, `find`, `line_up`, `match_percent`, `defects`, `ReferenceSet`, `CheckModel`)
- Produces:
  - `MIN_BOX_PX = 16`, `MIN_EDGE_POINTS = 100`, `MIN_TOP_POINTS = 20`, `SAMPLE_MIN_SCORE = 0.5`, `MAX_SAMPLES = 20`
  - `class TrainingRefused(ValueError)`
  - `@dataclass class DraftData: picture: np.ndarray; reference: List[int]` (x1, y1, x2, y2 pixels of the training picture); `checks: List[List[int]]` (boxes 2–4, same pixels); `ignore: np.ndarray` (bool, picture size); `samples: List[np.ndarray]` (sample 1 is `picture`)
  - `find_sample(draft: DraftData, picture: np.ndarray, angle: int) -> Optional[Found]` (`None` under `SAMPLE_MIN_SCORE`)
  - `train(draft: DraftData, angle: int, checks: List[Dict[str, Any]], set_id: str) -> Tuple[ReferenceSet, Dict[str, Any]]` (`checks`: `[{"colour": bool, "sensitivity": str}]`, one per box; the dict is `{"suggested": [{"min_score", "max_defect_px"}], "samples": [{"scores": [...], "defects": [...]}], "note": Optional[str]}`)

- [ ] **Step 1: Write the failing tests**

```python
def test_eight_samples_give_a_set_and_suggestions():
    ref, info = train(DRAFT_8, 15, [{"colour": False, "sensitivity": "normal"}] * 2, "ref-0000000a")
    assert ref.samples == 8 and len(ref.checks) == 2 and ref.picture_size == (1280, 960)
    assert 50 <= info["suggested"][0]["min_score"] <= 95 and info["suggested"][0]["max_defect_px"] >= 10 and info["note"] is None
    # with those suggestions: every good probe picture passes, the defect, wrong label and missing cap are rejected

def test_suggestions_come_from_leave_one_out():   # one odd sample (darker print): its leave-one-out score sets min_score

def test_fewer_than_three_samples_give_the_defaults_and_a_note():
    _, info = train(DRAFT_1, 15, CHECKS, "ref-0000000b")
    assert info["suggested"] == [{"min_score": 80, "max_defect_px": 30}] * 2 and "add" in info["note"].lower()

@pytest.mark.parametrize("draft, message", [
    (_small_box, "at least 16 px"), (_plain_area, "Draw the reference around printing or a shape")])
def test_refusals(draft, message): ...

def test_a_sample_without_the_reference_is_named():   # sample 3 is the APPLE label: TrainingRefused mentions "sample 3"

def test_find_sample(): ...      # a good picture -> Found; the APPLE label -> None

def test_training_from_grey_pictures(): ...   # one-channel samples: a set whose check 1 passes a grey good picture
```

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** per spec section 5: first model from the training picture's reference box (with its part of `ignore`); each sample found and every box cut out with `line_up` (Task 2); mean and Lab spread per box; `CheckModel.ignore` = the mask under the box; the final edge model from the mean; typical scores and suggestions; 1–2 samples turned by ±angle/2 with `cv2.warpAffine` around the found centre.
- [ ] **Step 4: Run the tests and `tests/test_pattern_check.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Pattern check: training`)

---

### Task 4: Reference set and draft files

**Files:**
- Create: `app/services/match_references.py`
- Test: `tests/test_match_references.py` (`data_dir` fixture from `tests/conftest.py`)

**Interfaces:**
- Consumes: `ReferenceSet`, `CheckModel` (Task 2); `DraftData`, `train` (Task 3); `settings_persistence_service.DATA_DIR`; `line_setups.folder` (saved setups: `<DATA_DIR>/line-setups/<line>/<save>.json`, cameras under `line.cameras`); `StorageGuard.floor()`
- Produces:
  - `ID_RE = re.compile(r"^ref-[0-9a-f]{8}$")`; `new_id() -> str`
  - `class ReferenceMissing(LookupError)` (message "the reference set <id> is missing" / "… cannot be read: <why>")
  - `write_set(ref: ReferenceSet, draft: DraftData, made_by: str, camera_id: str) -> None` (into `<id>.partial/`, then renamed; files as spec section 3)
  - `load_set(set_id: str) -> ReferenceSet` (kept in memory per id; raises `ReferenceMissing`)
  - `set_info(set_id: str) -> Optional[Dict[str, Any]]` → `{"checks": int, "reference_size": [w, h], "picture_size": [w, h]}` (reads `set.json` only)
  - `room_ok() -> bool` (free bytes on the data folder's disk ≥ `StorageGuard.floor()`)
  - Drafts (fractions in and out, pixels inside): `draft(camera_id) -> Optional[Dict[str, Any]]` (`{"reference", "checks", "search_area", "samples": [{"n", "score"}], "trained": set_id | None, "picture_size"}`); `draft_data(camera_id) -> Optional[DraftData]`; `start_draft(camera_id, picture)`; `save_boxes(camera_id, reference, checks, search_area)`; `save_mask(camera_id, png: bytes)` (refused when not the training picture's size); `add_sample(camera_id, picture, score) -> int`; `remove_sample(camera_id, n)` (not 1); `set_trained(camera_id, set_id)`; `draft_from_set(camera_id, set_id)`; `delete_draft(camera_id)`; `draft_file(camera_id, name) -> Optional[Path]`
  - `clean_up(lines: List[Dict[str, Any]], now: Optional[float] = None) -> int` (sets and drafts removed)

- [ ] **Step 1: Write the failing tests**

```python
def test_a_set_is_written_once_and_loaded(data_dir):
    write_set(REF, DRAFT, "admin", "c1")
    assert (data_dir / "match-references" / REF.id / "set.json").is_file() and not list(data_dir.glob("**/*.partial"))
    assert load_set(REF.id).checks[0].typical == REF.checks[0].typical
    with pytest.raises(FileExistsError): write_set(REF, DRAFT, "admin", "c1")

def test_a_missing_or_broken_set(data_dir): ...   # ReferenceMissing for an unknown id and for a set.json cut short

def test_set_info(data_dir): ...                  # {"checks": 2, "reference_size": [360, 240], "picture_size": [1280, 960]}

def test_a_draft_survives_and_takes_boxes_mask_and_samples(data_dir): ...
    # start_draft, save_boxes with fractions, save_mask (PNG of the right size; another size -> ValueError),
    # add_sample twice, remove_sample(2); remove_sample(1) -> ValueError; draft_data gives pixels; draft() gives fractions

def test_clean_up_keeps_what_is_named(data_dir, monkeypatch):
    # four sets: one named by a line camera, one by a saved line setup file, one by a draft's "trained",
    # one by none and 25 h old -> only the last is removed; one by none and 1 h old is kept;
    # a draft 31 days unused is removed

def test_room(data_dir, monkeypatch): ...         # shutil.disk_usage patched below StorageGuard.floor(): False
```

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** (`set.json` with `"format": 1`; `check-n-mean.png` via `cv2.imencode`; `np.save` float16 spreads; `cv2.imwrite` JPEG 95 samples; a lock around the drafts; `clean_up` reads saved setups with `json.load`, skipping files that do not parse)
- [ ] **Step 4: Run the tests** — Expected: all pass
- [ ] **Step 5: Commit** (`Pattern check: reference set and draft files`)

---

### Task 5: Pattern check settings on a line

**Files:**
- Modify: `app/services/line_config.py`, `app/services/settings_persistence_service.py` (`save_line`: after `require_camera_models(line)`, `require_match_references(line, match_references.set_info)`; after a save, `match_references.clean_up` over all lines in a thread), `app/services/line_setups.py` (`delete`: clean-up after), `main.py` (`lifespan`: one clean-up after `restore_on_startup`, in a thread; a failure is logged, never stops the start)
- Test: `tests/test_pattern_check_settings.py`

**Interfaces:**
- Consumes: `match_references.set_info`, `clean_up` (Task 4)
- Produces (`line_config`):
  - `CAMERA_ROLES = ("vision", "qr", "ocr", "match")` (message "role must be 'vision', 'qr', 'ocr' or 'match'")
  - `MATCH_ANGLE_LIMITS = (0, 45)`, `DEFAULT_MATCH_ANGLE = 15`, `MAX_MATCH_CHECKS = 4`, `MATCH_SENSITIVITY = ("low", "normal", "high")`, `DEFAULT_MIN_SCORE = 80`, `DEFAULT_MAX_DEFECT_PX = 30`, `MAX_DEFECT_PX = 1_000_000`
  - `REASON_PATTERN_NOT_FOUND`, `REASON_PATTERN_MISMATCH`, `REASON_PATTERN_DEFECT`, `REASON_PATTERN_ERROR` in `REJECT_REASONS`; `REJECT_REASON_CODES` 14–17
  - `normalize_match(raw: Any, label: str) -> Dict[str, Any]`; `is_pattern_check(camera) -> bool`
  - `_reading_and_station(raw, label) -> Dict` (the part of `_text_reader_reading` without `ocr`, used by both roles)
  - `picture_per_trigger` and `camera_station` treat `match` as `ocr`; `PATTERN_CHECK_KEPT_KEYS = ("match",) + TEXT_READER_KEPT_KEYS[1:]` and `_keep_job_settings(cameras, saved, role, keys)` replacing `_keep_text_reader_settings`
  - `require_match_references(line: Dict, info: Callable[[str], Optional[Dict]]) -> None` (ValueError: `f"Camera {n}: train its reference on Pattern and checks"`, `"… its reference set has {k} checks, not {m}: train it again"`, `"… its search area is smaller than its reference"`)

- [ ] **Step 1: Write the failing tests**

```python
GOOD = {"camera_id": "m1", "role": "match", "match": {"reference_id": "ref-0a1b2c3d", "checks": [{"name": "Label"}]}}

def test_defaults():
    cam = normalize_camera(GOOD, 0)
    assert cam["match"] == {"reference_id": "ref-0a1b2c3d", "search_area": None, "angle": 15,
                            "checks": [{"name": "Label", "min_score": 80, "max_defect_px": 30, "colour": False, "sensitivity": "normal"}]}
    assert "count_mode" not in cam and camera_station(cam) is None

@pytest.mark.parametrize("change, message", [
    ({"reference_id": "x"}, "train its reference"), ({"angle": 46}, "0 to 45"), ({"checks": []}, "1 to 4 checks"),
    ({"checks": [{"name": "score"}]}, "cannot be named 'score'"), ({"checks": [{"name": "A"}, {"name": "a"}]}, "two checks are named"),
    ({"checks": [{"name": "A", "min_score": 101}]}, "0 to 100"), ({"checks": [{"name": "A", "sensitivity": "max"}]}, "'low', 'normal' or 'high'"),
    ({"search_area": [0.5, 0, 0.51, 1]}, "at least 0.02")])
def test_refusals(change, message): ...

def test_one_picture_per_trigger_own_and_join(): ...   # count_mode trigger: station own by default; join with join settings; picture_per_trigger true

def test_a_client_that_leaves_match_out_keeps_it(lines): ...   # save a line with the camera, save again with {"camera_id", "role"} only: match kept

def test_the_save_checks_the_reference_set(lines, data_dir): ...
    # no set -> "train its reference on Pattern and checks"; set with 1 check and 2 in the settings -> "has 1 checks, not 2";
    # search area 0.1 x 0.1 of a 1280 x 960 picture with a 360 x 240 reference -> "smaller than its reference"

def test_reason_codes():
    assert [REJECT_REASON_CODES[r] for r in ("pattern_not_found", "pattern_mismatch", "pattern_defect", "pattern_error")] == [14, 15, 16, 17]
```

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the tests and `tests/test_text_reader_settings.py tests/test_line_config.py tests/test_line_setups.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Pattern check: settings on a line`)

---

### Task 6: The checker thread and continuous checking

**Files:**
- Create: `app/services/match_service.py`
- Modify: `app/services/vision_service.py` (`picture_worker(camera_id: str, role: str)` replaces the `text_reader: bool` parameter (callers in `trigger_inspection._take_picture`, `line_service`, `vision_service` pass the camera's role); `ContinuousVisionRunner._feed_camera` feeds a continuous pattern check; `CameraStreamPipeline.remove_camera` and `stop_all` also call `PatternCheckPipeline`'s)
- Test: `tests/test_pattern_check_worker.py`

**Interfaces:**
- Consumes: `check_picture`, `error_result`, `PatternResult` (Task 2); `load_set`, `ReferenceMissing` (Task 4); `is_pattern_check` (Task 5)
- Produces:
  - `NOT_A_CHECK = "the camera is not a pattern check on a line"`
  - `check_for_camera(camera_id: str, mat: np.ndarray) -> PatternResult` (the camera's saved `match` from its line; `error_result(str(missing))` on `ReferenceMissing`; `error_result(NOT_A_CHECK)` when not on a line)
  - `class PatternCheckWorker` with the `OcrReaderWorker` shape: `is_busy`, `last_inferred_frame`, `submit_frame_if_idle(mat, frame_id, conf_thresh=None, copy=False, arrived_at=None, on_result=None, on_failure=None) -> bool` (`on_result(frame_id, result: PatternResult, ms: float, arrived_at)`), `latest() -> Optional[PatternResult]`, `stop()`; thread `PatternCheck-{id[:8]}`, kind `"pattern_check"`
  - `class PatternCheckPipeline`: `get_worker`, `peek`, `remove_camera`, `stop_all`
  - Alarm: a result with `error` for a camera on a running line raises `camera.model_unavailable` from `f"camera:{camera_id}"`, WARNING, `f"Camera '{name}' (pattern check) on line '{line}' cannot check: {error}"`; the next result without one clears it

- [ ] **Step 1: Write the failing tests** (as `tests/test_text_reader_worker.py`: `lines`, `_line`, `fake_camera`; `match_service.check_for_camera` monkeypatched to queued results)

```python
def test_a_continuous_check_is_fed_frames_and_only_kept(lines, fake_camera, monkeypatch):
    runtime = _line(lines, [{"camera_id": "m1", "role": "match", "match": MATCH}])
    ...  # one _feed_camera pass submits the newest frame; worker.latest() is the result; no product, nothing sent

def test_a_triggered_check_is_not_fed_frames(lines, fake_camera): ...
def test_check_for_camera_without_a_set_is_an_error(lines, data_dir): ...   # error mentions "missing"
def test_an_error_raises_the_alarm_and_a_good_check_clears_it(lines, fake_camera, monkeypatch): ...
def test_a_check_that_raises_is_logged_once_a_minute(lines, fake_camera, monkeypatch, caplog): ...   # three failures in 10 s: one log line
def test_picture_worker_by_role(): ...   # "match" -> PatternCheckWorker, "ocr" -> OcrReaderWorker, "vision" -> the inference worker
```

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** on the pattern of `OcrReaderWorker` (no corner picture). A check that raises is logged at most once a minute per camera (`logger.warning` with the camera id) and gives `error_result(str(exc))`; continuous checking then skips that frame.
- [ ] **Step 4: Run the tests and `tests/test_text_reader_worker.py tests/test_session_and_continuous_vision.py tests/test_show_only_reading.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Pattern check: checker thread and continuous checking`)

---

### Task 7: The video overlay and the trigger picture

**Files:**
- Modify: `app/services/match_service.py`, `app/services/vision_service.py` (the stream: `routed[1] == "match"` draws with `PatternCheckPipeline.draw_checks`)
- Test: `tests/test_pattern_check_overlay.py`

**Interfaces:**
- Consumes: `draw_status_bar`, `overlay_scale`, `STATUS_STOPPED`, `STATUS_NO_LINE`, `STATUS_WAITING_TRIGGER` (`inference_engine`)
- Produces: `BANNER = "PATTERN CHECK"`, `NO_REFERENCE = "NO REFERENCE"`; `bar_parts(camera_id: str) -> List[str]`; `PatternCheckPipeline.draw_checks(camera_id, img, scale_x=1.0, scale_y=1.0, fps=0.0) -> np.ndarray`; `draw_result(img: np.ndarray, result: PatternResult, scale: float) -> None`; `render_capture(frame: Any, result: PatternResult) -> Optional[bytes]` (JPEG 85, never raises)

- [ ] **Step 1: Write the failing tests**

```python
def test_the_bar_says_what_the_check_is_doing(lines, monkeypatch):
    # stopped: ["PATTERN CHECK", "LINE STOPPED"]; not on a line: [.., STATUS_NO_LINE]; set missing: [.., "NO REFERENCE"];
    # triggered: [.., STATUS_WAITING_TRIGGER]; continuous good: [.., "GOOD · MATCH 97%", "38ms"];
    # reject: [.., "REJECT · Cap: defect 48 px", "41ms"]; not found: [.., "REJECT · Label: not found"]

def test_checks_are_drawn_green_and_red_with_their_spots(lines, monkeypatch):
    # a 640 x 480 picture, latest: Label fits (box corners at 100..300 x 100..200), Cap not (a spot at 150..160 x 150..160):
    # a pixel on Label's edge is (0, 200, 0), one on Cap's (0, 0, 230), the spot outlined (0, 0, 255)

def test_a_stopped_line_draws_no_old_result(lines, monkeypatch): ...
def test_render_capture_gives_a_jpeg(): ...   # b"\xff\xd8"; a broken frame gives None
```

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** like the text reader's overlay: the found outline (4 corners of check 1) and each check's box as polylines, `f"{name} {score}% · {defect_px} px"` above each; the bar last; nothing of an old result while stopped, not on a line or triggered.
- [ ] **Step 4: Run the tests and `tests/test_video_overlay.py tests/test_text_reader_overlay.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Pattern check: video overlay and trigger picture`)

---

### Task 8: A triggered pattern check on the line

**Files:**
- Modify: `app/services/trigger_inspection.py`, `app/services/vision_service.py` (`_feed_camera`, `_push_triggered`, `_hardware_result`), `app/services/line_service.py` (`configure`, `infers_on_demand`, `_configure_pairing`, `summary` camera rows, recent pattern checks), `app/services/plc_input_service.py` (capture), `app/routes/v1/lines.py` (the camera-trigger check; `GET /lines/{line_id}/match/recent`)
- Test: `tests/test_pattern_check_line.py`

**Interfaces:**
- Consumes: Tasks 2, 5, 6, 7
- Produces (`trigger_inspection`):
  - `PATTERN_CLASSES = {None: "pattern ok", "pattern_not_found": "pattern not found", "pattern_mismatch": "pattern mismatch", "pattern_defect": "pattern defect", "pattern_error": "pattern error"}`; `CHECK_TOO_SLOW = "the pattern check did not finish in time"`
  - `pattern_product(runtime, camera_id: str, result: PatternResult, arrived_at=None, trigger_no=None, sequence=None, request_id=None) -> Dict[str, Any]` (answer as `text_product`'s, with `match` = `match_values(result)` and `match_status` instead of `text`)
  - crossing keys: `crossing["match"]`, `crossing["match_status"]`, `crossing["reject_reason"]`; confidence = check 1's score / 100; bbox = the found outline's bounding box
- Produces (`LineRuntime`): `remember_pattern_check(camera_id, result, answer)`; `recent_pattern_checks(limit: int = 50) -> List[Dict]`, newest first, `{"timestamp", "camera_id", "score", "checks": [{"name", "score", "defect_px", "fits"}], "status", "result", "reason", "track_id"}`; kept 50; cleared by `reset`
- Produces (route): `GET /api/v1/lines/{line_id}/match/recent?limit=1..100` (Operator) → `{"line_id", "checks"}`

- [ ] **Step 1: Write the failing tests** (as `tests/test_text_reader_line.py`: a `_MatchWorker` whose `process(result)` calls `on_result`; `PatternCheckPipeline.get_worker` patched)

```python
@pytest.mark.parametrize("result, totals, reason, cls, status", [
    (GOOD, (1, 1, 0), None, "pattern ok", "good"), (NOT_FOUND, (1, 0, 1), "pattern_not_found", "pattern not found", "not_found"),
    (MISMATCH, (1, 0, 1), "pattern_mismatch", "pattern mismatch", "mismatch"), (DEFECT, (1, 0, 1), "pattern_defect", "pattern defect", "defect"),
    (ERROR, (1, 0, 1), "pattern_error", "pattern error", "error")])
def test_an_own_pattern_check_makes_one_product_per_trigger(lines, match_workers, fake_camera, result, totals, reason, cls, status):
    ...  # aux counter totals; payload class_name / reject_reason / match_status; answer["match"] == match_values(result);
    ...  # trigger_view()[0]["camera_id"] == "m1"; recent_pattern_checks()[0]["result"]

def test_a_check_that_does_not_finish_in_time_is_a_pattern_error(lines, match_workers, fake_camera, monkeypatch): ...
def test_a_disconnected_camera_is_a_no_picture_reject(lines, match_workers): ...
def test_a_joined_pattern_check_decides_the_counting_cameras_product(lines, workers, match_workers, fake_camera): ...
    # vis "bottle" then m1 DEFECT: one product REJECTED, reason "pattern_defect", reject_camera_id "m1";
    # no m1 result within the window: "station_no_result"
def test_a_capture_input_triggers_a_pattern_check(signals, match_workers, fake_camera): ...
def test_a_hardware_triggered_check_checks_every_picture(lines, match_workers, monkeypatch): ...
def test_a_result_after_the_line_stopped_makes_nothing(lines, match_workers, fake_camera): ...
def test_the_status_row_of_a_pattern_check(lines): ...   # role "match", count_mode, station, counts / join settings
def test_the_recent_route(client, admin_headers, lines): ...   # 200 {"line_id", "checks"}; 404 unknown line
```

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** alongside every text reader branch (`is_text_reader` / `routed[1] == "ocr"` / role `"ocr"`): `trigger_camera` skips the model check for a pattern check, turns `_NoPicture` with `taken` into `pattern_product(..., error_result(CHECK_TOO_SLOW))`; `product_from_result` and `render_trigger_capture` hand a pattern check's result to `pattern_product` / `render_capture`.
- [ ] **Step 4: Run the tests and `tests/test_text_reader_line.py tests/test_trigger_inspection.py tests/test_joined_cameras.py tests/test_trigger_notice.py tests/test_trigger_on_demand.py tests/test_trigger_captures.py tests/test_input_trigger.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Pattern check: one picture per trigger, own and joined`)

---

### Task 9: Records, export, values and formulas

**Files:**
- Modify: `app/services/counting_service.py` (copy `match`, `match_status` as `text`), `app/services/line_service.py` (`_joined_match(product)` beside `_joined_text`; `_finish_product` puts them in `fields` / `plc_fields`), `app/services/production_records_service.py` (`build_row` details key `"match"`), `app/services/records_export.py` (column `("pattern", lambda r: _pattern(r.details))` after `"text"`), `app/services/event_values.py`, `app/services/formula.py` (`ROOTS` gains `match`; under `match` alone, `_Checker` and `_Walker` take a second level `score` / `defect` and name it `match.<check>.<score|defect>`; `PLAIN_NAMES` gains `match_status`), `assets/card_values.js` (the line's pattern check values listed)
- Test: `tests/test_pattern_check_records.py`; the export header assertions in `tests/test_production_records.py`; `tests/test_formula.py` cases

**Interfaces:**
- Consumes: crossing keys `match`, `match_status` (Task 8)
- Produces: `values_for(event)` gives `match.score`, `match.<check>.score`, `match.<check>.defect`, `match_status`; record `details.match`; export `pattern` = `"Label 97% 0px; Cap 92% 48px"`; joined: several pattern checks' `checks` merged in card order (a name seen already is kept from the first), `score` the first camera's, status the first that is not `good` in card order (`"error"` for one that saw nothing)

- [ ] **Step 1: Write the failing tests**

```python
def test_values_of_a_pattern_product():
    v = values_for({"match": {"score": 97, "status": "defect", "checks": {"Cap": {"score": 92, "defect_px": 48}}}, "match_status": "defect"})
    assert (v["match.score"], v["match.Cap.score"], v["match.Cap.defect"], v["match_status"]) == (97, 92, 48, "defect")
    assert "match.Label.score" not in v

def test_formulas_take_match_values():
    assert parse("match.Cap.defect > 40").evaluate({"match.Cap.defect": 48}) is True
    for bad in ("match.Cap", "match.Cap.width", "text.Lot.score"): ...   # refused with a message

def test_a_record_and_the_export(): ...         # details.match kept; export column "pattern" == "Label 97% 0px; Cap 92% 48px"
def test_own_and_joined_products_carry_match(lines, match_workers, workers, fake_camera): ...
```

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** per the Files list
- [ ] **Step 4: Run the tests and `tests/test_production_records.py tests/test_event_values.py tests/test_formula.py tests/test_messages_and_plc_values.py tests/test_records_archive.py tests/test_text_reader_records.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Pattern check: records, export and values`)

---

### Task 10: The setup API

**Files:**
- Create: `app/services/held_pictures.py` (moved from `app/routes/v1/ocr.py`: `HELD_TTL_S`, `NOT_CONNECTED`, `_held`, `_now`, `frame`, `hold`, `held`; `ocr.py` imports them; `tests/test_ocr_api.py` patches `held_pictures` instead), `app/routes/v1/match.py`
- Modify: `main.py` (import and `PROTECTED_ROUTERS`), `app/security/config_lock.py` (`LOCK_FREE_ROUTES` gains the two `POST /api/v1/match/picture…` / `preview…` routes), `app/security/api_key_scopes.py` (`/api/v1/match/` → `None`, JWT-only)
- Test: `tests/test_match_api.py`

**Interfaces:**
- Consumes: Tasks 2–5; `held_pictures`
- Produces (all `require_supervisor`; draft routes run their work in `asyncio.to_thread`):
  - the routes of spec section 7's API table, with these answers: preview `PatternResult.to_dict()` (400 with `normalize_camera`'s message for bad settings; 404 "No picture is held for this camera"); `GET draft` the `draft()` dict (404 "No draft for this camera"); `PUT draft` the `draft()` dict; samples `POST` → `{"n", "found": {x, y, angle, score}}` (400 "Reference not found in this picture"; 400 "At most 20 good samples"); `train` → `{"reference_id", "suggested", "samples", "note"}` (400 with `TrainingRefused`'s message); `train` and `samples` answer 507 with the storage message when `room_ok()` is false; `edit` → the `draft()` dict
  - the refused-while-locked routes go through the configuration lock as any change does (they are not in `LOCK_FREE_ROUTES`)

- [ ] **Step 1: Write the failing tests** (`client`, supervisor and operator headers as in `tests/test_ocr_api.py`; `fake_camera` showing `tests/pattern_pictures` scenes; `data_dir`)

```python
def test_train_from_the_page_flow(client, supervisor, fake_camera, data_dir):
    # POST picture; PUT draft {reference: [...], checks: [], search_area: None}; 3 x (show another good scene, POST picture, POST samples);
    # POST train {"angle": 15, "checks": [{"colour": False, "sensitivity": "normal"}]} -> reference_id matches ID_RE, len(suggested) == 1;
    # POST preview {"settings": {"match": {"reference_id": id, "checks": [{"name": "Label"}]}}, "held": True} -> result "good", checks[0].score >= 90

def test_a_sample_without_the_reference_is_refused(client, supervisor, fake_camera, data_dir): ...
def test_a_preview_with_bad_settings_says_what_a_save_would(client, supervisor, fake_camera): ...   # 400 "1 to 4 checks"
def test_edit_and_start_over(client, supervisor, fake_camera, data_dir): ...
def test_train_and_add_as_good_are_refused_without_room(client, supervisor, fake_camera, data_dir, monkeypatch): ...   # both 507
def test_operators_api_keys_and_the_lock(client, operator, supervisor, fake_camera, data_dir): ...
    # operator 403; required_api_key_scopes("POST", "/api/v1/match/preview/camera/c1") is None;
    # locked: picture and preview 200, PUT draft / samples / train refused
def test_the_text_readers_held_picture_still_works(client, supervisor, fake_camera): ...   # tests/test_ocr_api.py's flow
```

- [ ] **Step 2: Run them to see them fail** — Expected: 404 for `/api/v1/match/...`
- [ ] **Step 3: Implement** the move, the router, the registration, the lock and scope entries; `train` runs `match_references.clean_up` after writing the set
- [ ] **Step 4: Run the tests and `tests/test_ocr_api.py tests/test_config_lock_policy.py tests/test_tokens.py tests/test_api.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Pattern check: setup API`)

---

### Task 11: The Pattern and checks page

**Files:**
- Create: `assets/pattern_check.js` (`window.PatternCheck`, `module.exports` in node)
- Modify: `assets/camera_pages.js` (kind `pattern`), `assets/production_lines.js` (`JOBS`, `START_JOBS`, `jobOf`, `jobKind`, `JOB_NOTES`, `cameraCardHtml`, `syncCard`, `bindCard`, `cardEntry`, `saveLineSetup`, the camera page's wake / leave), `dashboard.html` (script tag before `production_lines.js`; bump versions), `assets/dashboard.css`
- Test: `tests/test_pattern_check_page.py` (node), `tests/test_camera_pages.py`, `tests/test_help_tips.py` (`FILES` gains the file; `KEEP` gains the Sensitivity and mask brush sentences)

**Interfaces:**
- Consumes: the API (Task 10); the camera entry shape (Task 5)
- Produces (`window.PatternCheck`):
  - `MAX_CHECKS = 4`, `MAX_SAMPLES = 20`, `MIN_BOX_PX = 16`
  - `foldHtml(match: object|null, opts: {readOnly: bool}) -> string` (the page body as the spec's sketch)
  - `matchFromValues(values: object) -> object` (form values → the `match` dict; numbers parsed, blanks → defaults)
  - `boxFrom(x0, y0, x1, y1, shownW, shownH, pictureW, pictureH, minFraction = 0) -> [x1, y1, x2, y2]` (fractions, ordered, clamped, each side at least 16 px of the real picture and at least `minFraction`, 4 decimals; the search area passes 0.02)
  - `paint(mask: Uint8Array, w: number, h: number, x: number, y: number, radius: number, on: boolean) -> void` (a round dab; outside pixels skipped)
  - `applySuggestions(checks: object[], suggested: object[]) -> object[]`
  - `readCard(card) -> object`; `bind(card, api: {fetchJson, fetchBlob, sendBlob}) -> void`; `wake(card) -> void`
- `camera_pages.js`: page `{ id: "pattern", title: "Pattern and checks" }` before `image`; `fits("counting", kind)` true for `kind.pattern`; `pageTitle("counting", {pattern: true}) === "When to check"`; `startPage({pattern: true}) === "pattern"`; `PROBLEM_PAGES.no_reference = "pattern"`; `summary`: `"Shows pattern"` / `"One picture per trigger"`
- `production_lines.js`: `JOBS.pattern = { label: "Pattern check", short: "Pattern check", role: "match", counting: false, codes: false }`; `jobOf` gives `"pattern"` for `role === "match"`; `JOB_NOTES.pattern = "Finds the trained reference on each product by its edges and compares it with the good samples. One picture per trigger decides good or reject, on its own or joined to the counting camera's product."`; `cardEntry` for `role "match"`: the reading and station keys as a text reader's, and `match: PatternCheck.readCard(card)`; `saveLineSetup` refuses a camera with no `reference_id` with problem `no_reference` and the toast `"Camera N (name): train its reference on Pattern and checks."`
- Draft buttons (Add as good, the mask, Train, Edit training, Start over, sample ×) carry `data-lock`; Live, Take a picture and the preview do not.

- [ ] **Step 1: Write the failing node tests**

```python
def test_script_is_valid_and_loaded(): ...   # node --check; the script tag sits before production_lines.js

def test_box_from_a_drag():
    assert _api("api.boxFrom(500, 300, 100, 100, 640, 480, 1280, 960)") == [0.1563, 0.2083, 0.7813, 0.625]
    assert _api("api.boxFrom(10, 10, 12, 12, 640, 480, 1280, 960)") == [0.0156, 0.0208, 0.0281, 0.0375]   # 16 px of 1280 x 960

def test_paint_and_erase():   # a radius-3 dab at (5, 5) on 20 x 20 sets 29 pixels; erasing the same dab clears them; a dab at (-2, 0) does not throw

def test_values_to_settings():
    assert _api('api.matchFromValues({reference_id: "ref-0a1b2c3d", search_area: null, angle: "15", '
                'checks: [{name: "Label", min_score: "85", max_defect_px: "40", colour: false, sensitivity: "normal"}]})') == {
        "reference_id": "ref-0a1b2c3d", "search_area": None, "angle": 15,
        "checks": [{"name": "Label", "min_score": 85, "max_defect_px": 40, "colour": False, "sensitivity": "normal"}]}

def test_suggestions_fill_min_match_and_max_defect_only(): ...
def test_the_page_has_help_only_where_needed():   # html.count("data-help=") == 2; four check rows at most; data-lock on the draft buttons
def test_camera_pages_for_a_pattern_check(): ...  # pagesFor({pattern: true}) == ["camera", "job", "counting", "pattern", "image"]; titles; start page; summary; no_reference
```

And string checks in `production_lines.js`: `JOBS` has `pattern`, `jobOf` maps `role === "match"`, `cardEntry` sends `match: PatternCheck.readCard(card)`.

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement.** The picture: Live (every 1 s while the page is shown) or held; with a draft, the training picture (`GET …/draft/camera/{id}/picture`). Boxes drawn, moved and resized as the ROI box (`assets/camera_settings.js` `roiDrag`); the mask kept as a `Uint8Array` at the training picture's size, drawn tinted red, sent as PNG (`canvas.toBlob`) 500 ms after the last stroke. After Train: `applySuggestions`, the card marked changed. The preview after each change (300 ms) and each Live picture, drawn as the overlay does (Task 7's colours). "Train again for new suggested thresholds" shows after Colour or Sensitivity changes since the last Train.
- [ ] **Step 4: Run the tests and `tests/test_line_setup_markup.py tests/test_help_tips.py tests/test_camera_cards.py tests/test_text_reader_card.py tests/test_roi_box.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Pattern check: the Pattern and checks page`)

---

### Task 12: The Line dashboard and the Records page

**Files:**
- Modify: `assets/pattern_check.js` (panel renderers, `recordText`), `assets/production_lines.js` (`renderLineExtras`, the refresh beside the text reads, `tileDetail`, `REJECT_REASON`, the Records cell), `assets/dashboard.css`
- Test: `tests/test_pattern_check_dashboard.py` (node)

**Interfaces:**
- Consumes: `GET /api/v1/lines/{id}/match/recent` (Task 8); line status rows with `role "match"`
- Produces (`window.PatternCheck`): `checksPanelHtml() -> string` ("Latest pattern checks", `<tbody id="plPatternBody">`); `checksRowsHtml(checks: object[], ctx: {name, reasons}) -> string` (Time, Camera, Checks as `Label 97% · 0 px`, Product result with its reason; escaped); `recordText(details) -> string` (`Label 97% 0px; Cap 92% 48px`)
- `REJECT_REASON` gains `pattern_not_found: "pattern not found"`, `pattern_mismatch: "pattern mismatch"`, `pattern_defect: "pattern defect"`, `pattern_error: "pattern check error"`; `tileDetail` for `role "match"`: `"Pattern check · one picture per trigger"` / `"Pattern check · shows the pattern"` (+ station as for a vision camera)

- [ ] **Step 1: Write the failing node tests** (rows for a good and a defect check; a name with `<b>` escaped; the panel's tbody id; `recordText`; the reason labels)
- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement**; the panel shows when a camera row of the line has `role === "match"`, refreshed with the text reads
- [ ] **Step 4: Run the tests and `tests/test_line_status_layout.py tests/test_text_reader_dashboard.py tests/test_trigger_view_markup.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Pattern check: Line dashboard panel and Records page`)

---

### Task 13: Docs, the live check, and the vault

**Files:**
- Modify: `README.md` (a "Pattern check" section: what it checks, training steps, Match % and biggest defect, Sensitivity, the reasons and codes, the values, speeds measured, what is not covered), `FILE_TREE.md`, `PROJECT_DESCRIPTION.md`, `API_KEY_QUICKSTART.md` (`/lines/{id}/match/recent` under monitor; the setup routes are JWT-only)
- Create (main checkout, not committed): `backup/scratch-tools/fake_label_cam.py` (an MJPEG camera serving `tests/pattern_pictures` scenes: a label shifted, turned ±14° and relit at random, every 5th with a defect, every 7th the APPLE label), `backup/scratch-tools/check_pattern_live.py`, `backup/scratch-tools/shots_pattern.py`
- Test: the live check; the full suite

- [ ] **Step 1: Write the docs**
- [ ] **Step 2: Full suite** in the worktree (Global Constraints) and `ruff check .` — Expected: all pass, apart from the known load-sensitive timing tests (pass alone)
- [ ] **Step 3: Live check on a scratch copy** (never the real `data/`): `launcher_pages.py` (8765) pointed at this worktree with its own `data/`, `fake_label_cam.py`, headless Chrome:
  1. Line setup: add a Pattern check camera; Take a picture; draw the reference and one check (the circle); mask the LOT line; add 8 good samples; Train; the suggestions fill in; Save. 1280 and 375 px; no side scroll; no page errors.
  2. Continuously: the video shows `PATTERN CHECK`, the outline and the checks.
  3. One picture per trigger, own: `POST /lines/{id}/trigger` at 5 a second for 60 s: no missed triggers, the median and the longest time per picture, the defect and APPLE pictures rejected with `pattern_defect` / `pattern_not_found`, the good ones good.
  4. Joined to a counting camera: the product gets the pattern check's reason.
  5. The panel, the Records page and the CSV export show the scores; a send card with `{match.score}` sends it.
  6. Save the line setup, retrain (a new set), Load the saved setup: the old set comes back and still checks.
- [ ] **Step 4: Record in the vault** (`03 Decisions/Pattern check decisions.md`: the owner's answers, what was built, the measured times, the live check, what was not tested on a real line; link from `Home.md`) and commit (`Pattern check: docs`)
