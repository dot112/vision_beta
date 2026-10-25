# Code Reader Image Processing Implementation Plan

> **Removed on 2026-10-25** at the owner's request: zxing-cpp already binarizes, inverts and turns the picture while it reads, so this stage only made reading slower. What stays: the corner picture (now always cut out of the camera's picture) and the CODE READER banner. See `tests/test_code_processing_removed.py`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The code reader processes a copy of each picture (perspective, rotate, scale, grayscale, denoise, contrast, sharpen, binarize) before reading, falls back to the original, shows the cropped code in the video's top-left corner, has a live preview in Line setup, and its banner says CODE READER.

**Architecture:** A new pure module `app/engines/code_processing.py` owns the settings check, the steps (one 3 × 3 warp for the geometric ones, then the pixel steps), point mapping and the upright crop. `QREngine.decode` gains a `processing` argument that runs the four reading stages and remembers what read (`last_read`). The reader worker passes each camera's saved `code_processing`, keeps a corner picture, and the overlay draws it. A lock-free `POST /qr/preview/camera/{id}` and a new `assets/code_processing.js` fold in Line setup give the live preview.

**Tech Stack:** OpenCV, numpy, zxing-cpp (`return_errors=True`), FastAPI, plain JS in `assets/`, pytest + node.

**Spec:** `docs/superpowers/specs/2026-10-25-code-reader-image-processing-design.md`

## Global Constraints

- Step order: perspective → rotate → scale (one warp) → grayscale → denoise → contrast → sharpen → binarize (+ invert). Steps 5–8 turn the picture grey first.
- Ranges: angle -180..180 (° clockwise); factor 0.25..4 (long side capped at 4096 px); denoise 1..5 (median kernel 2n+1); contrast 1..10 (CLAHE clip = n, 8 × 8 tiles); sharpen 1..10 (unsharp amount 0.3n, σ 2); binarize method `auto` | `adaptive` | `fixed`, level 0..255, block 3..255 odd (even + 1), adaptive C 5; crop margin 0..200 px, default 16.
- Defaults: exactly the JSON in spec section 3 (corners `[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]`, denoise 2, contrast 3, sharpen 3, level 128, block 31).
- Corners: TL, TR, BR, BL fractions 0..1; convex; area ≥ 1% of the picture.
- Reading stages, each only when the earlier ones read nothing: processed → straightened (≤ 3 error codes, short side ≥ 120 px, ≤ 4×) → original → second pass (existing budgets 40 ms / 150 ms). Processing off = today's path exactly, no `return_errors`.
- `read_by` values: `"processed"`, `"straightened"`, `"original"`, `"none"`.
- Corner picture: long side ≤ 240 px stored; drawn in ≤ 160 × 160 px × `overlay_scale`, ≤ 35% of the video's width and height, 10 px × scale below the bar; frame 2 px × scale, green `(0, 200, 0)` known / red `(0, 0, 230)` unknown; text strip with the first 24 characters and the tag `original`.
- Banner text: `CODE READER` (replaces `QR READER` in all three states).
- Preview: `POST /api/v1/qr/preview/camera/{camera_id}`, body `{"processing", "code_type"="all", "view"="processed"}`; JPEG ≤ 960 px wide, quality 80; headers `X-Codes`, `X-Read-By`, `X-Read-Ms`, `Cache-Control: no-store`; 409 `Camera {id} is not connected`; 422 `Image processing: ...`. Save errors: `Camera N image processing: ...`.
- Repo files are CRLF in the worktree: edit with the Edit tool or a script that keeps CRLF. Work in `.claude/worktrees/code-processing`; run pytest there with the main folder's Python: `../../../.venv/Scripts/python.exe -m pytest ...` (not `pt.sh`, which tests `main`).
- Help icons only on the two controls named in spec section 7.

## Review Focus

1. A code at the very edge of the picture (its outline partly outside): the corner picture and the straightened crop are still made, padded with the edge's pixels, no exception. → Task 1 test `test_upright_at_the_picture_edge`.
2. A grey (one-channel) camera picture, or a tiny one (an ROI of a few pixels): every step runs or is skipped, no exception, the reader keeps reading. → Task 1 test `test_tiny_and_grey_pictures`.
3. The camera's size or ROI changes after the corners were set: the corners are fractions, so they follow the new picture. → Task 1 test `test_corners_follow_the_picture_size`.
4. Half-typed values in the preview (an empty field, a `-`): the preview sends the defaults for empty fields and shows a 422 in its status line, never a toast storm. → Task 6 test `test_collect_fills_empty_fields_with_defaults`.
5. A corner picture from before a Stop showing again after Start, as if it were new: it is cleared when the line starts. → Task 4 test `test_corner_picture_is_cleared_when_the_line_starts`.

---

### Task 1: The processing steps

**Files:**
- Create: `app/engines/code_processing.py`
- Test: `tests/test_code_processing.py`

**Interfaces:**
- Produces:
  - `DEFAULT_PROCESSING: Dict[str, Any]` (spec section 3).
  - `normalize_processing(raw: Any) -> Dict[str, Any]`: full dict with defaults; raises `ValueError(message)` without any prefix, e.g. `"the scale factor must be 0.25 to 4"`, `"the four perspective corners must be four [x, y] pairs from 0 to 1"`, `"the four perspective corners must make a convex area of at least 1% of the picture"`, `"the angle must be -180 to 180"`, `"denoise strength must be 1 to 5"`, `"contrast strength must be 1 to 10"`, `"sharpen strength must be 1 to 10"`, `"the binarize method must be 'auto', 'adaptive' or 'fixed'"`, `"the binarize level must be 0 to 255"`, `"the adaptive block size must be 3 to 255"`, `"the crop margin must be 0 to 200 px"`, `"<name> must be true or false"`. Numbers are int/float (not bool, not str).
  - `is_active(settings: Optional[Dict]) -> bool`: `enabled` and at least one step `on`.
  - `prepare(img: np.ndarray, settings: Dict) -> Tuple[np.ndarray, np.ndarray]`: (processed picture, 3 × 3 float64 matrix camera picture → processed picture); identity matrix when no geometric step is on. Accepts BGR or grey.
  - `map_points(points: Sequence[Sequence[float]], matrix: np.ndarray) -> List[List[int]]`.
  - `upright(img: np.ndarray, polygon: Sequence[Sequence[float]], margin_px: int, *, min_short: int = 0, max_scale: float = 4.0, max_long: Optional[int] = None) -> Tuple[np.ndarray, np.ndarray]`: (crop warped upright from the 4-point polygon widened by the margin, 3 × 3 matrix img → crop). A polygon thinner than a third of its length is first made half its length tall, centred. `min_short` enlarges (≤ `max_scale`); `max_long` shrinks. Border: replicate.

- [ ] **Step 1: Write the failing tests** (pictures made with numpy; codes with `zxingcpp.write_barcode` as `tests/test_code_types.py` does):
  - `test_defaults_and_unknown_keys`: `normalize_processing({}) == DEFAULT_PROCESSING`; `normalize_processing({"scale": {"on": True}, "x": 1})["scale"] == {"on": True, "factor": 1.0}` and no `"x"`.
  - `test_ranges_are_checked`: `{"scale": {"factor": 5}}`, `{"rotate": {"angle": 181}}`, `{"denoise": {"strength": 6}}`, `{"binarize": {"method": "otsu"}}`, `{"crop_margin_px": 201}`, `{"enabled": "yes"}`, `{"scale": {"factor": "2"}}` each raise `ValueError`; the scale one's message is `"the scale factor must be 0.25 to 4"`.
  - `test_bad_corners`: three points; a point at 1.2; a bow-tie order `[[0,0],[1,1],[1,0],[0,1]]`; four points inside 5% × 5% → `ValueError`.
  - `test_even_block_is_made_odd`: block 30 → 31.
  - `test_is_active`: off when `enabled` False or no step on; on with `enabled` and `grayscale.on`.
  - `test_pixel_steps`: grayscale → 2-D; binarize auto → `set(np.unique(out)) <= {0, 255}`; with invert the white count and black count swap; contrast alone gives a grey picture.
  - `test_geometric_sizes`: 640 × 480, scale 2 → 1280 × 960; rotate 90 → 480 × 640 (±1); scale 4 on 1920 × 1080 → long side 4096; perspective corners `[[0.25,0.25],[0.75,0.25],[0.75,0.75],[0.25,0.75]]` on 640 × 480 → 320 × 240 (±1).
  - `test_points_map_back`: perspective (a mild trapezoid) + rotate 30 + scale 1.5 on 640 × 480; the four picture corners of the trapezoid's area mapped with `map_points(pts, M)` then `map_points(..., inv(M))` land within 2 px of the start.
  - `test_upright_sizes`: a 200 × 100 rectangle rotated 30° → crop about (200 + 2m) × (100 + 2m) with m = 16 (±3); a thin 1D outline 300 × 4 → crop height ≈ 150 + 2m; `max_long=240` → long side 240; `min_short=120` on a 60 × 40 area → short side ≥ 120 and scale ≤ 4.
  - Review Focus: `test_upright_at_the_picture_edge` (polygon half outside a 100 × 100 picture → a crop, no exception); `test_tiny_and_grey_pictures` (every step on, inputs 4 × 4 BGR and 120 × 80 grey → returns an array, no exception); `test_corners_follow_the_picture_size` (same corners on 640 × 480 and 1280 × 720 → outputs 320 × 240 and 640 × 360, ±1).
- [ ] **Step 2: Run** `../../../.venv/Scripts/python.exe -m pytest tests/test_code_processing.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** the module. Geometric matrix: P = `getPerspectiveTransform(corners in px → (0,0),(W,0),(W,H),(0,H))` with W, H the averaged edge lengths; R = rotation about the current picture's centre, `cv2.getRotationMatrix2D(centre, -angle, 1)` widened to 3 × 3 with a translation so the turned picture's bounding box starts at 0; S = diag(f, f, 1) with f reduced so the long side ≤ 4096; M = S · R · P; output size from the transformed corners; one `warpPerspective` (`INTER_CUBIC` when f ≥ 1, else `INTER_AREA`; `BORDER_REPLICATE`). Pixel steps with `medianBlur`, `createCLAHE(clipLimit=n, tileGridSize=(8, 8))`, `addWeighted(img, 1 + a, GaussianBlur(img, (0, 0), 2), -a, 0)`, `threshold(... THRESH_OTSU)`, `adaptiveThreshold(... ADAPTIVE_THRESH_GAUSSIAN_C, THRESH_BINARY, block, 5)`, `threshold(level)`, `bitwise_not`. Skip any step a picture is too small for (median kernel or block larger than the picture).
- [ ] **Step 4: Run** the tests → PASS.
- [ ] **Step 5: Commit** `Code processing: the steps, the settings check and the upright crop`.

### Task 2: Reading stages in the engine

**Files:**
- Modify: `app/engines/qr_engine.py` (`_decode_zxing`, `decode`, new `LastRead`, `QREngine.last_read`)
- Test: `tests/test_code_processing_read.py`

**Interfaces:**
- Consumes: Task 1 `is_active`, `prepare`, `map_points`, `upright`.
- Produces:
  - `class LastRead(NamedTuple)`: `processed: Optional[np.ndarray]` (None when processing was off), `matrix: Optional[np.ndarray]` (camera picture → processed), `read_by: str`.
  - `QREngine.decode(image_input, hard_budget_ms: float = 0.0, code_type: str = CODE_TYPE_ALL, processing: Optional[Dict[str, Any]] = None) -> BarcodeDecodeResponse`; polygons always in camera-picture coordinates; sets `self.last_read: Optional[LastRead]` on every call.
  - `_decode_zxing(img, w, h, only, errors: Optional[List[List[List[int]]]] = None)`: with `errors` given, reads with `return_errors=True` and appends the corner points of each result with `valid` False.

- [ ] **Step 1: Write the failing tests** (a picture: an EAN-13 `4006381333931` from `zxingcpp.write_barcode(BarcodeFormat.EAN13, ..., 400, 120)`, 60 px white border, turned 45° on a white canvas; this does not read today: checked on 2026-10-25):
  - `test_turned_1d_code_reads_only_with_rotate`: `decode(pic)` finds nothing; `decode(pic, processing={"enabled": True, "rotate": {"on": True, "angle": -45}})` (normalized) finds `4006381333931`, `last_read.read_by == "processed"`, and the polygon's centre is within 10 px of the code's centre in `pic`.
  - `test_perspective_reads_a_tilted_code`: the turned code warped once more by a trapezoid (assert it does not read as is) reads with the perspective corners at the code's own four corners in the picture (TL, TR, BR, BL of the code as printed, as fractions).
  - `test_turned_ean13_reads_straightened`: the turned EAN-13 with only `grayscale` on: zxing reports it with a checksum error and its outline (probed 2026-10-25), so it reads in stage 2, `read_by == "straightened"`. Probe the outline first; if zxing's error outline for this picture cannot be straightened into a readable crop, drop this test, keep the stubbed one below, and say so in the task report.
  - `test_ruined_processing_falls_back_to_the_original`: an upright QR (`cv2.QRCodeEncoder` as `tests/test_qr_trigger.py`), processing binarize fixed level 255 → the code reads, `read_by == "original"`.
  - `test_straightened_stage`: monkeypatch `QREngine._read` so the processed picture gives nothing, `_decode_zxing` to append one error polygon (the code's corners in the processed picture), and the read of a crop returns a code at crop coordinates (10, 10)–(50, 50) → `read_by == "straightened"`, polygon mapped back through `inv(crop matrix)` then `inv(matrix)`.
  - `test_processing_off_is_todays_path`: spy on `zxingcpp.read_barcodes`; `decode(qr)` and `decode(qr, processing={"enabled": False, ...})` never pass `return_errors=True`; `last_read.read_by == "original"` and `last_read.processed is None`.
  - `test_code_type_applies_to_processed_reads`: with processing on and `code_type="2d"`, the turned EAN-13 is not read.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the stages in `decode` (spec section 2). Straightened: `upright(processed, pts, margin_px=settings["crop_margin_px"], min_short=120)`; read the crop with `_read`; map `inv(crop_m)` then `inv(matrix)`. Read time covers all stages. OpenCV fallback (no zxing-cpp): stage 2 skipped.
- [ ] **Step 4: Run** the new tests and `tests/test_camera_roi_and_qr.py tests/test_code_types.py tests/test_qr_trigger.py` → PASS.
- [ ] **Step 5: Commit** `Code processing: read the processed picture, then straightened codes, then the original`.

### Task 3: Settings in Line setup

**Files:**
- Modify: `app/services/line_config.py` (`normalize_camera`, `READER_KEPT_KEYS`)
- Test: `tests/test_code_processing_settings.py`

**Interfaces:**
- Consumes: Task 1 `normalize_processing`.
- Produces: a code-reading camera entry may carry `code_processing` (the full normalized dict); absent → not stored; `"code_processing"` in `READER_KEPT_KEYS`.

- [ ] **Step 1: Write the failing tests** (through `PUT /api/v1/lines/{id}` with the admin client, as `tests/test_lines_api.py` does, and directly on `normalize_camera`):
  - `test_saved_with_defaults_filled`: a QR camera with `{"enabled": True, "grayscale": {"on": True}}` → saved entry's `code_processing` equals `DEFAULT_PROCESSING` with those two changed.
  - `test_bad_value_is_422_with_the_camera_number`: factor 9 on the second camera → 422, detail starts with `Camera 2 image processing: the scale factor must be 0.25 to 4`.
  - `test_absent_is_not_stored`: a camera sent without it and never saved with it → no `code_processing` key.
  - `test_kept_when_a_client_leaves_it_out`: save with it, save again without it → still there.
  - `test_dropped_when_the_camera_stops_reading_codes`: a vision camera with `read_codes` and `code_processing`, saved again without `read_codes` → no key.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**: in `normalize_camera`, inside `if reads_codes(camera):`, when `raw` has `code_processing`, store `normalize_processing(raw["code_processing"])`, re-raising `ValueError` as `f"{label} image processing: {exc}"`; add the key to `READER_KEPT_KEYS`.
- [ ] **Step 4: Run** the new tests, `tests/test_lines_api.py`, `tests/test_line_config*.py` → PASS.
- [ ] **Step 5: Commit** `Code processing: saved per camera in Line setup`.

### Task 4: Reader worker, corner picture, banner

**Files:**
- Modify: `app/services/qr_service.py` (`_QRReaderWorker.process`, `capture`, new `_processing`, `corner`, `clear_corner`; `draw_reads`; `draw_capture`; new `CornerPicture`, `draw_corner`; `QRReaderPipeline.clear_corners`), `app/services/vision_service.py` (pass the vision bar's height), `app/services/line_service.py` (`LineRuntime.configure`: clear on start), `tests/test_video_overlay.py` (`"QR READER"` → `"CODE READER"`)
- Test: `tests/test_code_reader_corner_picture.py`

**Interfaces:**
- Consumes: Task 2 `decode(..., processing=)`, `last_read`; Task 1 `upright`, `map_points`, `is_active`.
- Produces:
  - `class CornerPicture(NamedTuple)`: `image: np.ndarray` (BGR, long side ≤ 240), `code: str`, `known: bool`, `read_by: str`, `processing: bool`.
  - `_QRReaderWorker.corner() -> Optional[CornerPicture]`; `_QRReaderWorker.clear_corner() -> None`; `QRReaderPipeline.clear_corners(camera_ids: Iterable[str]) -> None`.
  - `draw_corner(img: np.ndarray, corner: CornerPicture, top: int) -> None` (draws in place).
  - `QRReaderPipeline.draw_reads(camera_id, img, scale_x=1.0, scale_y=1.0, fps=0.0, banner=True, top=0)`: `top` is the height already covered (a vision camera's bar) when `banner` is False.
  - `draw_capture(mat, record, max_width=1280, corner: Optional[CornerPicture] = None) -> bytes`.

- [ ] **Step 1: Write the failing tests** (route stubbed as `tests/test_video_overlay.py` does with `SimpleNamespace(enabled=True, capture_triggers={})`; `_camera_entry` monkeypatched to return a `code_processing`):
  - `test_a_read_keeps_a_corner_picture`: `worker.process(pic)` on the turned EAN-13 with rotate -45 → `corner()` has `code == "4006381333931"`, `read_by == "processed"`, `processing` True, long side ≤ 240.
  - `test_corner_drawn_top_left_below_the_banner`: after a read, `draw_reads` on a black 640 × 480 → non-black pixels in the box x ∈ [10, 170], y ∈ [bar + 10, bar + 200]; the frame's colour is green for a known code (product lookup stubbed), red for unknown.
  - `test_original_tag`: a read with `read_by == "original"` while processing is on → the strip text (spy on `cv2.putText`) contains `original`; processing off → no tag.
  - `test_no_corner_while_stopped`: `enabled=False` → the corner box area stays black.
  - `test_below_a_vision_bar`: `draw_reads(..., banner=False, top=60)` → corner starts at y ≥ 70.
  - `test_triggered_capture_draws_the_corner`: `worker.capture({...})` with a stubbed `_fresh_frame` returning the picture → the JPEG decoded has non-black pixels in the corner box below the CAPTURE banner (on a black background picture with the code placed bottom-right).
  - `test_banner_says_code_reader`: spy on `draw_status_bar` parts in all three states → first part is `CODE READER`.
  - Review Focus: `test_corner_picture_is_cleared_when_the_line_starts`: a corner kept; `LineRuntime.configure` with `enabled` False then True → `corner()` is None.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**:
  - `_processing()` returns `self._camera_entry().get("code_processing")`; `process` and `capture` pass it to `decode`.
  - After a read: pick the first known code, else the first; its polygon in the read picture is `map_points(polygon, last_read.matrix)` when `read_by` is processed or straightened (crop from `last_read.processed`), else the camera polygon (crop from the camera picture); `upright(..., margin_px=crop margin or 16, max_long=240)`; grey crops to BGR; store under `_state_lock`.
  - `draw_corner`: size and colours from Global Constraints; the strip under the picture with the text (first 24 characters) plus `  original` when `processing` and `read_by == "original"`.
  - `draw_reads`: `CODE READER`; draws the corner after the bar in the running and the waiting-for-crossing states, not when stopped; `top` used when `banner` is False.
  - `vision_service`: where it calls `draw_reads(..., banner=False)`, pass `top=status_bar_height(hud_parts(line_manager.counter_for_camera(self.camera_id), fps, latency_ms, status), w, h)` for the annotated picture's `w, h`.
  - `LineRuntime.configure`: when `enabled` goes from False to True, `QRReaderPipeline.clear_corners` of its cameras.
  - `capture` passes `corner=self.corner()` to `draw_capture`, which draws it below its own banner.
- [ ] **Step 4: Run** the new tests, `tests/test_video_overlay.py`, `tests/test_qr_trigger.py`, `tests/test_camera_roi_and_qr.py`, `tests/test_lines_api.py` → PASS.
- [ ] **Step 5: Commit** `Code processing: the reader uses it; corner picture; CODE READER banner`.

### Task 5: Preview route

**Files:**
- Modify: `app/services/qr_service.py` (`QRService.preview`, a preview engine and lock), `app/routes/v1/qr.py` (route), `app/security/config_lock.py` (`LOCK_FREE_ROUTES`)
- Test: `tests/test_code_preview_api.py`

**Interfaces:**
- Consumes: Task 1 `normalize_processing`, `map_points`; Task 2 `decode(..., processing=)`, `last_read`.
- Produces: `QRService.preview(camera_id: str, processing: Any, code_type: str = "all", view: str = "processed") -> Tuple[bytes, Dict[str, str]]` (sync; raises `LookupError(f"Camera {camera_id} is not connected")` or `ValueError(message)`); route `POST /api/v1/qr/preview/camera/{camera_id}` → `Response(jpeg, media_type="image/jpeg", headers=...)`, `LookupError` → 409, `ValueError` → 422 `f"Image processing: {exc}"` (a bad `view` → 422 `"view must be 'processed' or 'original'"`).

- [ ] **Step 1: Write the failing tests** (a fake driver in `app_state.cameras` with `is_connected=True` and `get_latest_raw_mat` returning a 320 × 240 picture with a QR; removed in `finally`):
  - `test_preview_answers_a_jpeg_with_headers`: 200, `image/jpeg`, `X-Codes == "1"`, `X-Read-By` in the four values, `X-Read-Ms` a number, `Cache-Control: no-store`.
  - `test_processed_view_follows_scale`: scale 2 → decoded JPEG 640 × 480; `view=original` → 320 × 240.
  - `test_wide_pictures_are_capped_at_960`: a 1600 × 1200 picture → 960 wide.
  - `test_not_connected_is_409` and `test_bad_settings_are_422` (`"Image processing: the scale factor must be 0.25 to 4"`).
  - `test_preview_works_while_locked`: lock as admin (fixture from `tests/test_config_lock_api.py`, unlocked in `finally`) → 200.
  - `tests/test_config_lock_policy.py::test_every_changing_route_is_classified` passes with the new route on `LOCK_FREE_ROUTES`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**: picture from `get_latest_raw_mat(copy=True)` (else `grab_raw_frame()`); `decode` on a module-level preview `QREngine` under its own lock with `hard_budget_ms=SINGLE_PICTURE_HARD_BUDGET_MS`; processed view = `last_read.processed` (or the picture when None), outlines mapped with `last_read.matrix`; outlines green, 2 px; grey → BGR; scale to ≤ 960 wide; `imencode` quality 80. The route runs it with `asyncio.to_thread`.
- [ ] **Step 4: Run** the new tests and `tests/test_config_lock_policy.py tests/test_config_lock_api.py` → PASS.
- [ ] **Step 5: Commit** `Code processing: live preview route`.

### Task 6: Line setup fold and live preview

**Files:**
- Create: `assets/code_processing.js` (IIFE, `window.CodeProcessing`, its own `<style>` like `production_lines.js` line 155)
- Modify: `assets/production_lines.js` (`foldShown` at line 897, the card's folds after `codes`, `cardEntry` at about line 2366, the fold-open handler in `bindCard` at about line 1772, and the render path that reopens open folds at about line 1739), `dashboard.html` (`<script src="/assets/code_processing.js?v=1.0.0">` before `production_lines.js`, whose `?v=` goes to `3.11.0`), `tests/test_help_tips.py` (`FILES` + the two `KEEP` sentence starts)
- Test: `tests/test_code_processing_dashboard.py`

**Interfaces:**
- Consumes: Task 5 route; Task 3 entry key `code_processing`; `ConfigLock.locked()`.
- Produces: `CodeProcessing.foldBody(cam) -> string`, `CodeProcessing.collect(card) -> object` (the `code_processing` dict), `CodeProcessing.open(card) -> void` (starts the preview loop for that card if not running). Field names (`data-f`): `proc_enabled`, `proc_persp`, `proc_corners` (hidden, JSON), `proc_rotate`, `proc_angle`, `proc_scale`, `proc_factor`, `proc_gray`, `proc_denoise`, `proc_denoise_n`, `proc_contrast`, `proc_contrast_n`, `proc_sharpen`, `proc_sharpen_n`, `proc_bin`, `proc_bin_method`, `proc_bin_level`, `proc_bin_block`, `proc_invert`, `proc_margin`; preview box `data-f="proc-pic"` (img + canvas, class `pl-lines-pic`), `data-f="proc-status"`, buttons `data-act="proc-view"` with `data-view="processed|original"`, `data-act="proc-reset-corners"`. `collect` reads fields only through `card.querySelector('[data-f="..."]')`.

- [ ] **Step 1: Write the failing tests**:
  - `test_script_is_valid`: `node --check assets/code_processing.js`; the script tag precedes `production_lines.js` in `dashboard.html`.
  - `test_fold_only_for_reader_jobs`: `foldShown` source contains `fold === "processing"` tied to `JOBS[job].codes`; `cameraCardHtml` builds `foldHtml("processing", "Image processing", ...)`.
  - `test_collect_reads_the_fields`: node runs `code_processing.js` with a stub `window`/`document` and a stub card whose `querySelector` returns `{value, checked}` objects → `collect(card)` equals the expected dict (enabled, rotate on 12.5, binarize adaptive block 41 invert, margin 20, corners from the hidden JSON).
  - Review Focus: `test_collect_fills_empty_fields_with_defaults`: empty `value` strings and a `"-"` → the defaults of spec section 3 for those keys.
  - `test_entry_carries_code_processing`: `cardEntry` source has `entry.code_processing = CodeProcessing.collect(card)` inside `if (spec.codes)`.
  - `tests/test_help_tips.py` passes with the two help sentences.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the fold (spec section 7: the switch with its note, one row per step, Binarize's Level/Block shown by method, Reset corners, Crop margin, preview box and status line). Preview loop per card: runs while the fold is open, `card.isConnected`, `document.body.dataset.tab === "tabActionTrigger"` and `!document.hidden`; one request at a time, next ≥ 250 ms after the answer; body `{processing: collect(card), code_type: cardValue code_type, view}`; status line from the headers (`2 codes · read in the processed picture · 23 ms`, `No code read · 31 ms`); 409 → `Camera not connected`, retry in 2 s; 422 → its detail in the status line (no toast). Corner drag on the canvas in the Original view while Perspective is on: handles 12 px, pointer events, writes `proc_corners`; ignored while `ConfigLock.locked()`. Revoke old blob URLs.
- [ ] **Step 4: Run** the new tests, `tests/test_help_tips.py`, `tests/test_config_lock_dashboard.py`, `tests/test_lines_api.py` → PASS.
- [ ] **Step 5: Commit** `Code processing: Line setup fold with a live preview`.

### Task 7: Docs, full suite, live check

**Files:**
- Modify: `README.md` (after the second-pass paragraph at line 160: an **Image processing** paragraph: the steps and order, the switch, the fallback, the corner picture, the preview route; and `CODE READER`)
- Scratch only (not committed): a check script in the session scratchpad based on `backup/scratch-tools/shots_s5.py` and a launcher copy with `REPO` = this worktree; `backup/scratch-tools/fake_cams.py` extended in the scratch copy to show an EAN-13 turned 45° on one port.

- [ ] **Step 1:** write the README paragraph.
- [ ] **Step 2:** full suite in the worktree (pt.sh's command line run from the worktree). Expected: only the known timing flakes, each passing alone.
- [ ] **Step 3:** live check on the scratch server (port 8765): a line with a code reader on the fake camera; Line setup → Image processing: preview in both views, rotate -45 makes it read, a corner drag changes the processed view, Save; Line dashboard: `CODE READER` banner and the corner picture; read time on the banner with processing on and off (report both); screenshots at 1440 and 375 px; no page errors, no sideways scroll.
- [ ] **Step 4: Commit** `Code processing: docs`; stop the scratch server by its port's PID.
