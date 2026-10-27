# OCR Text Reader (Part 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Text reader" camera job on a line: RapidOCR reads printed dates and lot codes in up to 4 fields (region, YOLO class or whole picture), checks each against a format, and gives good or reject as an own or joined station, with a live view in Line setup and on the Line dashboard.

**Architecture:** `text_format` (pure notation) → `ocr_engine` (RapidOCR, one shared engine per model and device) → `text_reading` (one picture: areas, read, match fields) → `ocr_service` (one reader thread per camera, the video overlay). The line treats a text reader on One picture per trigger like a triggered inspection station: it is a station (`own`/`join`), its pictures come through `trigger_inspection` and the hardware path, and its products carry `text` and `text_status`.

**Tech Stack:** Python 3.14 (Docker 3.12), FastAPI, `rapidocr==3.10.*` on ONNX Runtime (CPU, DirectML, CUDA), OpenCV, plain JS dashboard (node tests).

**Spec:** `docs/superpowers/specs/2026-10-27-ocr-text-reader-design.md`

**Two refinements of the spec, made by this plan (the spec is updated in the same commit):**
- The reading mode is the camera's `count_mode`, as for a vision camera: `"trigger"` = One picture per trigger; left out = continuously. The spec's `ocr_read` key is not used. This lets every trigger and station path that already tests `count_mode == "trigger"` serve a text reader with a role check widened, instead of a second key.
- Reading one picture (areas, read, match) lives in `app/engines/text_reading.py`, not in `ocr_service.py`: it has no thread and no line, and is tested on its own. `ocr_service.py` keeps the thread, the corner picture and the overlay.

## Global Constraints

- Work in the worktree `F:\projects\claude_fast_api_vision\fastapi_vision_server\.claude\worktrees\ocr-reader`, branch `claude/ocr-reader`. Never push. Never touch the repo's real `data/`, the owner's server on port 8000, or the owner's Node-RED.
- Python: `PY=../../../.venv/Scripts/python.exe` from the worktree (the main checkout's venv). One test file: `$PY -m pytest tests/<file>.py -q -p no:cacheprovider`. Full suite: the command in `backup/scratch-tools/pt.sh` (main checkout) run from the worktree with `.venv` replaced by `../../../.venv` (keep its 8 `--deselect` lines).
- Keep each file's line endings (CRLF or LF); script multi-line edits with `open(..., newline="")`. Write scripts to the scratchpad, not Bash heredocs with triple quotes.
- The only new dependency: `rapidocr==3.10.*`. No PaddlePaddle, PyTorch or EasyOCR at run time.
- Detection: `Det.limit_type: "max"`, `Det.limit_side_len: 640`; engine keeps lines down to 0.30 (`Global.text_score`).
- Settings limits: 1–4 fields; field name `[A-Za-z][A-Za-z0-9_]{0,31}`, unique ignoring case; format 1–64 characters; region `[x1, y1, x2, y2]` fractions 0–1, `x1 < x2`, `y1 < y2`, each side ≥ 0.02; `join_dots` 0 or odd 3–15; `ink` `dark`/`light`; `min_confidence` 0.30–0.99, default 0.50; `model` `tiny`/`small`/`medium`, left out = `small`.
- Reasons and PLC codes: `text_wrong` 11, `no_text` 12, `reader_error` 13. Classes: `text ok`, `text wrong`, `no text`, `reader error`.
- Copy: job "Text reader"; video bar `TEXT READER`, `READER NOT READY`, `FIELDS {ok}/{n} OK`; panel "Latest text reads".
- A trigger waits for the reader as for a model: `TRIGGER_INFERENCE_TIMEOUT_S` (10 s). Held setup picture dropped after 600 s unused.
- Help icons (`data-help`) only on Format and Join dots.
- Each task ends with its tests passing and a commit whose message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Two fields, one line:** two `all` fields and a picture with two lines must give each field its own line, never the same one to both (Task 4, `test_a_line_goes_to_one_field_only`).
2. **An older page saves the line:** a camera entry sent without `ocr` (or without its picture settings) keeps the saved ones; without that, a save from a stale tab wipes the fields (Task 3, `test_a_client_that_leaves_the_reader_settings_out_keeps_them`).
3. **Two readers and YOLO at once:** two text readers share one engine; on DirectML their calls and YOLO's must take turns on the same lock or the process dies (Task 2, `test_calls_take_turns_and_dml_uses_the_yolo_lock`).
4. **The line stops mid-read:** a read that finishes after the line stopped or the camera left the line must not make a product or a shown read (Task 7, `test_a_result_after_the_line_stopped_makes_nothing`; Task 5, `test_a_read_for_a_camera_no_line_has_is_not_shown`).
5. **Boxes at the edge:** a YOLO box partly outside the picture, or of zero size, is clipped or skipped, not a crash (Task 4, `test_yolo_boxes_are_clipped_and_empty_ones_skipped`).

---

### Task 1: The format notation

**Files:**
- Create: `app/engines/text_format.py`
- Test: `tests/test_text_format.py`

**Interfaces:**
- Produces:
  - `MAX_FORMAT_CHARS = 64`
  - `DIGIT_FIXES = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}`; `LETTER_FIXES = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"}`
  - `class Fit(NamedTuple): text: str; fits: bool; differences: int`
  - `check_format(value: Any, label: str) -> str` (stripped; `ValueError(f"{label}: the format must be 1 to {MAX_FORMAT_CHARS} characters")`)
  - `fit(fmt: str, reading: str) -> Fit`
  - `allowed_characters(formats: Iterable[str]) -> Optional[str]`
  - `suggest_format(reading: str) -> str`
  - No imports beyond the standard library.

- [ ] **Step 1: Write the failing tests**

```python
from app.engines.text_format import allowed_characters, check_format, fit, suggest_format

@pytest.mark.parametrize("fmt, reading, expected", [
    ("LOT A99999", "LOT A12345", ("LOT A12345", True, 0)),
    ("LOT A99999", "lot a12345", ("LOT A12345", True, 0)),            # capitals
    ("EXP 99/99/9999", "EXP12/O3/2027", ("EXP12/03/2027", True, 0)),  # spaces ignored, O -> 0 at a 9
    ("LOT A99999", "LOT 812345", ("LOT B12345", True, 0)),            # 8 -> B at an A: the shape fits
    ("LOT A99999", "L0T A12345", ("L0T A12345", False, 1)),           # literals are not fixed
    ("99/99", "12/345", ("12/345", False, 1)),                        # one too many
    ("99/99", "12/3", ("12/3", False, 1)),                            # one too few
    ("L*9", "L-5", ("L-5", True, 0)),
    ("X9", "B7", ("B7", True, 0)),                                    # nothing fixed at an X
])
def test_fit(fmt, reading, expected):
    assert tuple(fit(fmt, reading)) == expected

def test_allowed_characters():
    assert allowed_characters(["99/99"]) == " /0123456789"
    assert allowed_characters(["LOT A99999"]) == " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    assert allowed_characters(["99", "L*9"]) is None

@pytest.mark.parametrize("reading, fmt", [("LOT 12345", "LOT 99999"), ("EXP 03/2027", "EXP 99/9999"), (" a1 ", "A9")])
def test_suggest_format(reading, fmt):
    assert suggest_format(reading) == fmt

def test_check_format():
    assert check_format("  LOT 9 ", "Field") == "LOT 9"
    for bad in ("", "   ", "9" * 65, None, 5):
        with pytest.raises(ValueError, match="1 to 64 characters"):
            check_format(bad, "Field")
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PY -m pytest tests/test_text_format.py -q -p no:cacheprovider`
Expected: FAIL, `ModuleNotFoundError: app.engines.text_format`

- [ ] **Step 3: Implement `app/engines/text_format.py`**

`fit` walks the reading in capitals; spaces are copied and skipped; each other character takes the next non-space format token:

```python
def fit(fmt: str, reading: str) -> Fit:
    tokens = [c for c in fmt.upper() if c != " "]
    out, k, differences = [], 0, 0
    for ch in reading.upper():
        if ch == " ":
            out.append(ch)
            continue
        if k < len(tokens):
            token = tokens[k]
            ch = DIGIT_FIXES.get(ch, ch) if token == "9" else LETTER_FIXES.get(ch, ch) if token == "A" else ch
            differences += not _matches(token, ch)
            k += 1
        else:
            differences += 1
        out.append(ch)
    differences += len(tokens) - k
    return Fit("".join(out).strip(), differences == 0, differences)
```

`_matches`: `9` an ASCII digit, `A` `A`–`Z`, `X` either, `*` anything, else equal. `allowed_characters`: union over formats of digits (`9`, `X`), `A`–`Z` (`A`, `X`) and the other non-`*` characters, plus space, as one sorted string; `None` when a format has `*`. `suggest_format`: strip, capitals, each digit → `9`.

- [ ] **Step 4: Run the tests**

Run: `$PY -m pytest tests/test_text_format.py -q -p no:cacheprovider`
Expected: all pass

- [ ] **Step 5: Commit** (`Text reader: the format notation`)

---

### Task 2: Reader models and the OCR engine

**Files:**
- Modify: `requirements.txt` (after the `zxing-cpp` line: `rapidocr==3.10.*            # OCR: PaddleOCR's PP-OCR models on ONNX Runtime (text reader cameras)`)
- Modify: `app/config.py` (after `MODEL_STORE_PATH`: `OCR_MODEL_DIR: str = "./model_store/ocr"   # the text reader's tiny and medium models`), `.env.example` (same setting, commented)
- Create: `app/engines/ocr_models.py`, `app/engines/ocr_engine.py`
- Test: `tests/test_ocr_engine.py`

**Interfaces:**
- Produces (`ocr_models`):
  - `MODEL_NAMES = ("tiny", "small", "medium")`, `FETCHED = ("tiny", "medium")`
  - `model_files(name: str) -> Optional[Tuple[Path, Path]]`: `(det, rec)` when both files exist. `small`: `<rapidocr package>/models/PP-OCRv6_det_small.onnx` and `PP-OCRv6_rec_small.onnx`; `tiny`/`medium`: `Path(settings.OCR_MODEL_DIR) / f"PP-OCRv6_{det|rec}_{name}.onnx"` (the names RapidOCR's own download writes, measured).
  - `available() -> List[str]` (in `MODEL_NAMES` order), `new_reader_model() -> str` (`"tiny"` when available, else `"small"`)
  - `fetch(names: Iterable[str] = FETCHED) -> List[str]`; `main(argv: Optional[List[str]] = None) -> int` for `python -m app.engines.ocr_models --fetch`
  - `FETCH_HINT = "run: python -m app.engines.ocr_models --fetch"`
- Produces (`ocr_engine`):
  - `DET_LIMIT_SIDE = 640`, `ENGINE_MIN_SCORE = 0.30`
  - `class OcrLine(NamedTuple): text: str; confidence: float; box: List[List[int]]` (4 corners, pixels of the picture given)
  - `class OcrUnavailable(RuntimeError)`
  - `class OcrEngine: __init__(self, model: str, device: Optional[str] = None)` (sets `kind` = `_device_kind(device or settings.INFERENCE_DEVICE)` and `_lock`; loading waits for first use); `kind: str` (`"cpu"`, `"dml"`, `"cuda"`; `"cpu"` after a GPU fallback); `ready: bool` (property, loads on first use); `error: Optional[str]`; `read(self, img: np.ndarray, *, upside_down: bool = False, allowed: Optional[str] = None) -> List[OcrLine]` (raises `OcrUnavailable` when not ready)
  - `engine_for(model: str) -> OcrEngine` (one per `(model, settings.INFERENCE_DEVICE)`, thread-safe)
  - `join_dots(img: np.ndarray, size: int, ink: str = "dark") -> np.ndarray` (BGR out; `size == 0` returns `img`)
  - `_build(model: str, kind: str) -> Any` (the `RapidOCR` object for a model on a device kind; tests replace it)

- [ ] **Step 1: Install RapidOCR into the venv**

Run: `$PY -m pip install --dry-run "rapidocr==3.10.*"` then, when it would only add packages (rapidocr, shapely, pyclipper, omegaconf, colorlog, tqdm) and change none already installed, `$PY -m pip install "rapidocr==3.10.*"`.
Expected: `Successfully installed ... rapidocr-3.10.x ...`. If the dry run would change an installed package, stop and report it.

- [ ] **Step 2: Write the failing tests** (`tests/test_ocr_engine.py`; pictures drawn with `cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, 2.0, ink, 9, cv2.LINE_AA)` on a 1280×720 light picture, as the probe did; every engine built with `device="cpu"`)

```python
def test_small_is_always_there_and_the_others_come_from_the_model_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OCR_MODEL_DIR", str(tmp_path))
    assert ocr_models.available() == ["small"] and ocr_models.new_reader_model() == "small"
    (tmp_path / "PP-OCRv6_det_tiny.onnx").write_bytes(b"x")
    assert ocr_models.available() == ["small"]                     # its recognizer is missing
    (tmp_path / "PP-OCRv6_rec_tiny.onnx").write_bytes(b"x")
    assert ocr_models.available() == ["tiny", "small"] and ocr_models.new_reader_model() == "tiny"

def test_reads_printed_text_with_boxes_in_picture_pixels():
    lines = OcrEngine("small", "cpu").read(_picture(["EXP 12/03/2027", "LOT A12345"]))
    assert sorted(line.text for line in lines) == ["EXP 12/03/2027", "LOT A12345"]
    lot = next(line for line in lines if line.text.startswith("LOT"))
    xs, ys = [p[0] for p in lot.box], [p[1] for p in lot.box]
    assert 290 < min(xs) < 380 and 350 < min(ys) < 440 and len(lot.box) == 4

def test_the_character_limit_keeps_only_allowed_characters():
    lines = OcrEngine("small", "cpu").read(_picture(["LOT 12345"]), allowed=" 0123456789")
    assert lines and all(set(line.text) <= set(" 0123456789") for line in lines)

def test_every_call_passes_its_switches(monkeypatch):
    engine = OcrEngine("small", "cpu")
    assert engine.ready
    calls = []
    real = engine._rapid
    engine._rapid = lambda img, **kw: calls.append(kw) or real(img, **kw)
    engine.read(_picture(["LOT 1"]), upside_down=True)
    engine.read(_picture(["LOT 1"]))
    assert [(c["use_det"], c["use_cls"], c["use_rec"]) for c in calls] == [(True, True, True), (True, False, True)]

def test_a_missing_model_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OCR_MODEL_DIR", str(tmp_path))
    engine = OcrEngine("tiny", "cpu")
    assert engine.ready is False and "python -m app.engines.ocr_models --fetch" in engine.error
    with pytest.raises(OcrUnavailable):
        engine.read(_picture(["X"]))

def test_join_dots_joins_the_dots_of_a_stroke():
    dots = _dot_picture("EXP 12")                # dots 6 px wide, 9 px apart (the probe's)
    before = cv2.connectedComponents((cv2.cvtColor(dots, cv2.COLOR_BGR2GRAY) < 100).astype("uint8"))[0]
    joined = join_dots(dots, 7)
    after = cv2.connectedComponents((cv2.cvtColor(joined, cv2.COLOR_BGR2GRAY) < 100).astype("uint8"))[0]
    assert after * 5 < before and joined.shape == dots.shape
    assert join_dots(dots, 0) is dots
    light = 255 - dots                           # light print on a dark surface: closing
    assert cv2.connectedComponents((cv2.cvtColor(join_dots(light, 7, "light"), cv2.COLOR_BGR2GRAY) > 155).astype("uint8"))[0] * 5 < before

def test_calls_take_turns_and_dml_uses_the_yolo_lock(monkeypatch):
    from app.engines import inference_engine
    engine = OcrEngine("small", "cpu")
    assert engine.ready and engine._lock is not inference_engine._DML_LOCK
    monkeypatch.setattr(ocr_engine, "_device_kind", lambda device: "dml")
    assert OcrEngine("small", "auto")._lock is inference_engine._DML_LOCK
    results = []
    threads = [threading.Thread(target=lambda: results.append(engine.read(_picture(["LOT A12345"])))) for _ in range(2)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert [[line.text for line in r] for r in results] == [["LOT A12345"], ["LOT A12345"]]

def test_a_gpu_that_cannot_start_falls_back_to_the_cpu(monkeypatch):
    real = ocr_engine._build
    monkeypatch.setattr(ocr_engine, "_device_kind", lambda device: "cuda")
    monkeypatch.setattr(ocr_engine, "_build", lambda model, kind: real(model, kind) if kind == "cpu" else 1 / 0)
    engine = OcrEngine("small", "auto")
    assert engine.ready and engine.kind == "cpu"

def test_engine_for_shares_one_engine_per_model():
    assert engine_for("small") is engine_for("small")
```

- [ ] **Step 3: Run them to see them fail** — Expected: `ModuleNotFoundError: app.engines.ocr_models`

- [ ] **Step 4: Implement `app/engines/ocr_models.py`**

`fetch` builds `RapidOCR(params={"Global.log_level": "error", "Global.model_root_dir": settings.OCR_MODEL_DIR, "Det.model_type": ModelType(name), "Rec.model_type": ModelType(name), "Det.ocr_version": OCRVersion.PPOCRV6, "Rec.ocr_version": OCRVersion.PPOCRV6})` and calls it once on a 64×320 white picture (RapidOCR downloads on first use; `model_type` must be the `ModelType` enum, a string raises `TypeError`). `main` prints each model and whether it is on disk; exit code 1 when a fetch failed.

- [ ] **Step 5: Implement `app/engines/ocr_engine.py`**

- `_device_kind(device: str) -> str`: `"cpu"` when the device is `cpu`; `"dml"` for `auto`/`gpu`/`dml`/`directml` (or `dml:N`) with `DmlExecutionProvider` available; `"cuda"` for `auto`/`gpu`/`cuda` with `CUDAExecutionProvider` available; else `"cpu"` (the same order as `InferenceEngine` for these two).
- Loading (first `ready` or `read`) goes through `_build(model, kind)`: `model_files(model)` or `error = f"the text reader model '{model}' is not on this server; {FETCH_HINT}"`. Params: `Global.log_level "error"`, `Global.text_score ENGINE_MIN_SCORE`, `Det.limit_type "max"`, `Det.limit_side_len DET_LIMIT_SIDE`, `Det.model_path`, `Rec.model_path`, `Det/Rec.ocr_version PPOCRV6`, `Det/Rec.model_type ModelType(model)`, and `EngineConfig.onnxruntime.use_dml` / `use_cuda` by `_device_kind`. Then one call on a 64×320 white picture (RapidOCR builds its recognizer lazily), and wrap its decoder: keep `self._rapid.text_rec.postprocess_op` as `self._decode` and set it to a function that, when `self._allowed` is set, zeroes `preds[:, :, i]` for every character index `i > 0` whose character (`self._decode.character[i]`) is not in `self._allowed`, then calls `self._decode`. A GPU kind whose load or first call raises is loaded again on the CPU, with one warning in the log.
- `self._lock = inference_engine._DML_LOCK if kind == "dml" else threading.Lock()`; `read` holds it for the whole call, sets `self._allowed`, calls `self._rapid(img, use_det=True, use_cls=upside_down, use_rec=True)` (every switch on every call: RapidOCR keeps them otherwise), resets `self._allowed`, and maps `out.boxes` / `out.txts` / `out.scores` (`boxes` is `None` when nothing was found) to `OcrLine`s with `int(round())` corners.
- `join_dots`: grey, `cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))`, `MORPH_OPEN` for `dark`, `MORPH_CLOSE` for `light`, back to BGR.

- [ ] **Step 6: Run the tests** — Expected: all pass (the first test run loads the model: a few seconds)

- [ ] **Step 7: Measure the engine's memory** (not a test): in a scratch script, `psutil.Process().memory_info().rss` before and after `OcrEngine("small", "cpu").ready` and one read; and the same for `tiny` after `python -m app.engines.ocr_models --fetch` into a scratch `OCR_MODEL_DIR`. Write both numbers into the vault note "03 Decisions/OCR text reader decisions.md".

- [ ] **Step 8: Commit** (`Text reader: RapidOCR engine and reader models`)

---

### Task 3: Text reader settings on a line

**Files:**
- Modify: `app/services/line_config.py`
- Test: `tests/test_text_reader_settings.py`

**Interfaces:**
- Consumes: `text_format.check_format`, `text_format.MAX_FORMAT_CHARS`
- Produces:
  - `CAMERA_ROLES = ("vision", "qr", "ocr")`
  - `OCR_MODELS = ("tiny", "small", "medium")`, `OCR_FIND = ("region", "yolo", "all")`, `OCR_INK = ("dark", "light")`, `MAX_TEXT_FIELDS = 4`, `JOIN_DOTS_LIMITS = (3, 15)`, `TEXT_CONFIDENCE_LIMITS = (0.30, 0.99)`, `DEFAULT_TEXT_CONFIDENCE = 0.5`, `MIN_REGION = 0.02`
  - `REASON_TEXT_WRONG = "text_wrong"`, `REASON_NO_TEXT = "no_text"`, `REASON_READER_ERROR = "reader_error"`; added to `REJECT_REASONS`; `REJECT_REASON_CODES` gains `11`, `12`, `13`
  - `TEXT_READER_KEPT_KEYS = ("ocr", "count_mode", "station", "picture_source", "picture_delay_ms", "notice_trigger", "picture_wait_ms") + JOIN_KEYS`
  - `normalize_text_reader(raw: Any, label: str) -> Dict[str, Any]` (the `ocr` dict)
  - `is_text_reader(camera: Dict[str, Any]) -> bool` (`role == "ocr"`)
  - `picture_per_trigger(camera: Dict[str, Any]) -> bool` (role `vision` or `ocr` and `count_mode == "trigger"`)
  - `camera_station(camera)` also gives `"own"`/`"join"` for a text reader on One picture per trigger, `None` for a continuous one
  - `cameras_using_model(...)` and `vision_models(...)` also count a text reader's `ocr.locator_model_id`

- [ ] **Step 1: Write the failing tests**

```python
READER = {"camera_id": "c1", "role": "ocr", "count_mode": "trigger", "triggers": [1],
          "ocr": {"fields": [{"name": "Lot", "find": "region", "region": [0.1, 0.2, 0.6, 0.4], "format": "LOT A99999"}]}}

def test_a_text_reader_saves_its_settings():
    cam = normalize_camera(READER, 0)
    assert cam["role"] == "ocr" and cam["count_mode"] == "trigger" and cam["station"] == "own"
    assert cam["picture_delay_ms"] == 0 and cam["picture_wait_ms"] == 1000 and cam["triggers"] == [1]
    assert cam["ocr"] == {"model": "small", "locator_model_id": None, "upside_down": False, "join_dots": 0,
                          "ink": "dark", "min_confidence": 0.5,
                          "fields": [{"name": "Lot", "find": "region", "region": [0.1, 0.2, 0.6, 0.4], "format": "LOT A99999"}]}
    assert "no_product" not in cam and "model_id" not in cam

def test_a_continuous_text_reader_has_no_station_and_no_picture_settings():
    cam = normalize_camera({**READER, "count_mode": "track", "station": "join"}, 0)
    assert "count_mode" not in cam and "station" not in cam and "picture_source" not in cam and cam.get("triggers", []) == []
    assert camera_station(cam) is None and picture_per_trigger(cam) is False

@pytest.mark.parametrize("ocr, message", [
    ({"fields": []}, "needs 1 to 4 text fields"),
    ({"fields": [_field("Lot")] * 5}, "needs 1 to 4 text fields"),
    ({"fields": [_field("1lot")]}, "the name must start with a letter"),
    ({"fields": [_field("Lot"), _field("LOT")]}, "two text fields are named 'LOT'"),
    ({"fields": [_field("Lot", region=[0.5, 0.2, 0.4, 0.4])]}, "the region must be [x1, y1, x2, y2]"),
    ({"fields": [_field("Lot", region=[0.1, 0.2, 0.11, 0.4])]}, "at least 0.02 wide and high"),
    ({"fields": [_field("Lot", find="yolo")]}, "choose the YOLO class it reads in"),
    ({"fields": [_field("Lot", find="yolo", yolo_class="dateBox")]}, "choose the YOLO model whose boxes its fields read in"),
    ({"fields": [_field("Lot", format="")]}, "the format must be 1 to 64 characters"),
    ({"model": "huge", "fields": [_field("Lot")]}, "reader model must be 'tiny', 'small' or 'medium'"),
    ({"join_dots": 4, "fields": [_field("Lot")]}, "join dots must be 0 (off) or an odd size from 3 to 15 px"),
    ({"min_confidence": 0.2, "fields": [_field("Lot")]}, "lowest confidence"),
    ("nope", "text reader settings must be an object"),
])
def test_bad_reader_settings_say_what_is_wrong(ocr, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        normalize_camera({**READER, "ocr": ocr}, 0)

def test_a_yolo_field_keeps_its_model_and_a_region_field_drops_it():
    cam = normalize_camera({**READER, "ocr": {"locator_model_id": "m1", "fields": [_field("Lot", find="yolo", yolo_class="dateBox")]}}, 0)
    assert cam["ocr"]["locator_model_id"] == "m1" and cam["ocr"]["fields"][0] == {"name": "Lot", "find": "yolo", "yolo_class": "dateBox", "format": "LOT A99999"}
    assert normalize_camera({**READER, "ocr": {"locator_model_id": "m1", "fields": [_field("Lot")]}}, 0)["ocr"]["locator_model_id"] is None

def test_the_text_reasons_have_plc_numbers():
    assert (REJECT_REASON_CODES["text_wrong"], REJECT_REASON_CODES["no_text"], REJECT_REASON_CODES["reader_error"]) == (11, 12, 13)

def test_a_joined_text_reader_is_a_joined_station():
    cam = normalize_camera({**READER, "station": "join", "join_offset_ms": 300, "join_missing": "reject"}, 0)
    assert joined_cameras([cam]) == [cam] and locks_trigger_mode(cam) and trigger_mode_for_card(cam) == "software"

def test_the_locator_model_is_in_use_and_loaded(lines):
    line = SettingsPersistenceService.save_line({"name": "OCR", "cameras": [{**READER, "ocr": {"locator_model_id": "m1",
        "fields": [_field("Lot", find="yolo", yolo_class="dateBox")]}}]})
    assert vision_models(line) == ["m1"]
    assert [cam["camera_id"] for _, cam in cameras_using_model([line], "m1")] == ["c1"]

def test_a_client_that_leaves_the_reader_settings_out_keeps_them(lines):
    saved = SettingsPersistenceService.save_line({"name": "OCR", "cameras": [{**READER, "station": "join", "join_offset_ms": 250}]})
    again = SettingsPersistenceService.save_line({"id": saved["id"], "name": "OCR", "cameras": [{"camera_id": "c1", "role": "ocr"}]})
    cam = again["cameras"][0]
    assert cam["ocr"] == saved["cameras"][0]["ocr"] and cam["count_mode"] == "trigger" and cam["join_offset_ms"] == 250

def test_a_capture_input_may_name_a_triggered_text_reader(lines):
    ...  # a "capture" input with camera_ids ["c1"] saves, and c1 gets the input's trigger number (as test_input_trigger does for a vision camera)
```

(`_field(name, find="region", region=(0.1, 0.2, 0.6, 0.4), format="LOT A99999", **extra)` builds one field dict; `lines` is the fixture from `tests/test_joined_cameras.py`; `save_line` updates with `id` as `tests/test_lines.py` does.)

- [ ] **Step 2: Run them to see them fail** — Expected: FAIL, `role must be 'vision' or 'qr'`

- [ ] **Step 3: Implement in `app/services/line_config.py`**

- `normalize_camera` for `role == "ocr"`: `count_mode` must be in `COUNT_MODES` (message as for a vision camera); only `"trigger"` is kept. With it: `camera.update(_picture_settings(raw, label))`, `station` (default `"own"`, values and message as for vision) and `_join_settings` for `join`. Always `camera["ocr"] = normalize_text_reader(raw.get("ocr"), label)`. The role message becomes `f"{label} role must be 'vision', 'qr' or 'ocr'"`. The existing `triggers` and `picture_source == "camera"` blocks then serve it unchanged.
- `normalize_text_reader`: the rules and messages of the tests above, each message starting with `label` (fields: `f"{label} text field '{name}': ..."`, the first rule `f"{label} text field {i + 1}: the name must start with a letter and have only letters, digits and _ (at most 32)"`). Regions rounded to 4 decimals. `min_confidence` through `_number(..., f"{label} lowest confidence", DEFAULT_TEXT_CONFIDENCE, *TEXT_CONFIDENCE_LIMITS)` rounded to 2. `locator_model_id` kept (stripped, ≤ 64) only when a field finds by `yolo`; required then.
- `_keep_text_reader_settings(cameras, saved) -> List[Any]` (beside `_fill_camera_models`, called after it in `normalize_line`): an `ocr` camera matched by id to a saved `ocr` camera gets each `TEXT_READER_KEPT_KEYS` key it left out.
- `picture_per_trigger`; use it in `trigger_mode_for_card`, `locks_trigger_mode`; `trigger_mode_to_set` returns `None` for roles other than `vision` and `ocr`, and for an `ocr` card that does not lock. `camera_station` per the Interfaces. `_check_signal_cameras` message: `"... must be a vision camera or a text reader with 'One picture per trigger', or read codes ..."`.
- `line_warnings`: the block at "takes its own pictures without a trigger notice" uses `picture_per_trigger(cam)`, with the "ignores a picture with nothing in it" warning only for `role == "vision"`; the "no trigger is ticked" warning uses `picture_per_trigger(cam)` and says "under Counting" for vision, "under Reading" for a text reader.

- [ ] **Step 4: Run the tests and the settings tests around them**

Run: `$PY -m pytest tests/test_text_reader_settings.py tests/test_lines.py tests/test_trigger_card_settings.py tests/test_trigger_mode_from_card.py tests/test_input_trigger.py -q -p no:cacheprovider`
Expected: all pass

- [ ] **Step 5: Commit** (`Text reader: settings on a line`)

---

### Task 4: Reading one picture

**Files:**
- Create: `app/engines/text_reading.py`
- Test: `tests/test_text_reading.py`

**Interfaces:**
- Consumes: `OcrEngine.read`, `OcrLine`, `OcrUnavailable`, `join_dots` (Task 2); `fit`, `allowed_characters` (Task 1); `REASON_*` (Task 3); `DetectionItem` (`app/schemas/vision.py`)
- Produces:
  - `class TextField(NamedTuple): name: str; text: Optional[str]; raw: Optional[str]; fits: bool; confidence: Optional[float]; box: Optional[List[List[int]]]; area: Optional[List[int]]` (`area` = `[x1, y1, x2, y2]` px where it looked)
  - `class TextResult(NamedTuple): fields: List[TextField]; lines: List[OcrLine]; ms: float; result: str; reason: Optional[str]; error: Optional[str]` with property `status -> str` (`"good"`; `"none"` when no field has a line; else `"wrong"`)
  - `read_picture(mat: np.ndarray, ocr: Dict[str, Any], engine: Any, locate: Optional[Callable[[np.ndarray], List[DetectionItem]]] = None) -> TextResult` (never raises)
  - `error_result(ocr: Dict[str, Any], why: str) -> TextResult` (every field empty, `result "reject"`, `reason "reader_error"`, `error why`)
  - `text_values(result: TextResult) -> Dict[str, str]` (`{field name: text}` for fields with a line)

- [ ] **Step 1: Write the failing tests** (a `_FakeEngine` whose `read(img, upside_down, allowed)` records `(img.shape, upside_down, allowed)` and returns the `OcrLine`s queued for that call; boxes in crop pixels)

```python
def test_a_region_field_reads_its_cut_out_and_maps_boxes_back():
    engine = _FakeEngine([[OcrLine("LOT A12345", 0.95, [[2, 3], [80, 3], [80, 20], [2, 20]])]])
    r = read_picture(_mat(1000, 500), _ocr(_field("Lot", region=[0.1, 0.2, 0.5, 0.6])), engine)
    assert engine.calls[0][0][:2] == (200, 400)                    # rows 100..300, columns 100..500
    lot = r.fields[0]
    assert (lot.text, lot.fits, lot.area, lot.box[0]) == ("LOT A12345", True, [100, 100, 500, 300], [102, 103])
    assert (r.result, r.reason, r.status) == ("good", None, "good")
    assert engine.calls[0][2] == " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"   # the formats' characters

def test_a_field_takes_the_line_that_fits_else_the_closest():
    lines = [OcrLine("EXP 12/03/2027", 0.9, B), OcrLine("LOT A1234", 0.99, B), OcrLine("LOT A12345", 0.6, B)]
    r = read_picture(_mat(), _ocr(_field("Lot", find="all")), _FakeEngine([lines]))
    assert (r.fields[0].text, r.fields[0].fits) == ("LOT A12345", True)
    r = read_picture(_mat(), _ocr(_field("Lot", find="all")), _FakeEngine([lines[:2]]))
    assert (r.fields[0].text, r.fields[0].fits, r.reason, r.status) == ("LOT A1234", False, "text_wrong", "wrong")

def test_a_line_goes_to_one_field_only():
    lines = [OcrLine("LOT A12345", 0.9, B), OcrLine("LOT B67890", 0.8, B)]
    r = read_picture(_mat(), _ocr(_field("Lot", find="all"), _field("Lot2", find="all")), _FakeEngine([lines]))
    assert [f.text for f in r.fields] == ["LOT A12345", "LOT B67890"]
    assert len(r.lines) == 2                                       # the whole picture is read once

def test_a_fitting_line_goes_to_the_field_it_fits_before_any_closest_match():
    r = read_picture(_mat(), _ocr(_field("A", find="all", format="99/99"), _field("B", find="all", format="LOT A99999")),
                     _FakeEngine([[OcrLine("LOT A12345", 0.9, B)]]))
    assert ([f.text for f in r.fields], r.reason, r.status) == ([None, "LOT A12345"], "no_text", "wrong")

def test_no_line_at_all_is_no_text():
    r = read_picture(_mat(), _ocr(_field("A", find="all")), _FakeEngine([[]]))
    assert (r.reason, r.status) == ("no_text", "none")

def test_lines_under_the_lowest_confidence_are_dropped():
    r = read_picture(_mat(), {**_ocr(_field("Lot", find="all")), "min_confidence": 0.7},
                     _FakeEngine([[OcrLine("LOT A12345", 0.65, B)]]))
    assert r.reason == "no_text" and r.lines == []

def test_yolo_fields_read_in_their_class_boxes():
    found = [_det("dateBox", 10, 20, 210, 70), _det("datepage", 0, 0, 50, 50), _det("dateBox", 300, 20, 500, 70)]
    engine = _FakeEngine([[], [OcrLine("LOT A12345", 0.9, B)]])
    r = read_picture(_mat(), _ocr(_field("Lot", find="yolo", yolo_class="dateBox")), engine, locate=lambda img: found)
    assert [c[0][:2] for c in engine.calls] == [(50, 200), (50, 200)] and r.fields[0].area == [300, 20, 500, 70]

def test_yolo_boxes_are_clipped_and_empty_ones_skipped():
    found = [_det("dateBox", -40, 450, 100, 600), _det("dateBox", 30, 30, 30, 90)]     # picture 640 x 480
    engine = _FakeEngine([[]])
    read_picture(_mat(640, 480), _ocr(_field("Lot", find="yolo", yolo_class="dateBox")), engine, locate=lambda img: found)
    assert [c[0][:2] for c in engine.calls] == [(30, 100)]

def test_a_yolo_field_without_a_model_and_an_engine_that_fails_are_reader_errors():
    r = read_picture(_mat(), _ocr(_field("Lot", find="yolo", yolo_class="dateBox")), _FakeEngine([]))
    assert (r.result, r.reason) == ("reject", "reader_error") and "YOLO model" in r.error
    r = read_picture(_mat(), _ocr(_field("Lot")), _FakeEngine(raise_=OcrUnavailable("no model")))
    assert (r.reason, r.error) == ("reader_error", "no model")

def test_join_dots_and_upside_down_reach_the_engine(monkeypatch):
    joined = []
    monkeypatch.setattr(text_reading, "join_dots", lambda img, size, ink: joined.append((size, ink)) or img)
    engine = _FakeEngine([[]])
    read_picture(_mat(), {**_ocr(_field("Lot")), "join_dots": 7, "ink": "light", "upside_down": True}, engine)
    assert joined == [(7, "light")] and engine.calls[0][1] is True

def test_text_values():
    r = read_picture(_mat(), _ocr(_field("Lot", find="all"), _field("Exp", find="all", format="99/99")),
                     _FakeEngine([[OcrLine("LOT A12345", 0.9, B)]]))
    assert text_values(r) == {"Lot": "LOT A12345"}
```

- [ ] **Step 2: Run them to see them fail** — Expected: `ModuleNotFoundError: app.engines.text_reading`

- [ ] **Step 3: Implement `read_picture`**

1. Areas: `region` → `[round(x1*W), round(y1*H), round(x2*W), round(y2*H)]`; `yolo` → each detection whose `class_name` equals the field's `yolo_class`, its bbox clipped to the picture, skipped when under 2 px wide or high (`locate` is called once per picture, only when a field needs it; `locate is None` with a `yolo` field → `error_result(ocr, "the YOLO model of the text reader is not loaded")`); `all` → the whole picture.
2. Each distinct area is read once (`join_dots` when `ocr["join_dots"]`; `engine.read(crop, upside_down=ocr["upside_down"], allowed=allowed_characters(formats))`), boxes offset by the area's corner, lines under `min_confidence` dropped.
3. Two passes over the fields in order, each line used once: first each field takes, among the unused lines of its areas, the one that fits with the highest confidence; then each field still without a line takes the unused line with the fewest `fit(...).differences` (then the highest confidence); else none. The field's `text` is `fit(...).text`, `raw` the line's text.
4. `result "good"` when every field fits; else `"reject"` with `no_text` / `text_wrong` from the first field in order that does not fit. `ms` from `time.perf_counter()`.
5. Any exception from the engine (or `OcrUnavailable`) → `error_result(ocr, str(exc))`.

- [ ] **Step 4: Run the tests** — Expected: all pass

- [ ] **Step 5: Commit** (`Text reader: reading one picture`)

---

### Task 5: The reader thread and continuous reading

**Files:**
- Create: `app/services/ocr_service.py`
- Modify: `app/services/vision_service.py` (`ContinuousVisionRunner._feed_camera`; `CameraStreamPipeline.remove_camera` and `stop_all` also call `OcrReaderPipeline`'s), `app/services/line_service.py` (`LineRuntime`: text reads; `configure` clears the readers' corners when the line starts, as for `QRReaderPipeline`), `app/routes/v1/lines.py`
- Test: `tests/test_text_reader_worker.py`

**Interfaces:**
- Consumes: `read_picture`, `error_result`, `TextResult`, `text_values` (Task 4); `engine_for` (Task 2); `CornerPicture`, `DEFAULT_CROP_MARGIN_PX`, `CORNER_MAX_SIDE` and `upright` (as `qr_service` uses them)
- Produces (`ocr_service`):
  - `class OcrReaderWorker: __init__(self, camera_id: str)`; `is_busy: bool`; `last_inferred_frame`; `submit_frame_if_idle(self, mat, frame_id, conf_thresh=None, copy=False, arrived_at=None, on_result=None, on_failure=None) -> bool` (the vision worker's shape, so `trigger_inspection` and the hardware path use it unchanged; `on_result(frame_id, result: TextResult, ms: float, arrived_at)`); `latest() -> Optional[TextResult]`; `corner() -> Optional[CornerPicture]`; `clear_corner()`; `stop()`
  - `class OcrReaderPipeline` with classmethods `get_worker(camera_id)`, `peek(camera_id)`, `remove_camera(camera_id)`, `clear_corners(camera_ids)`, `stop_all()`
  - `read_for_camera(camera_id: str, mat: np.ndarray) -> TextResult`: the camera's saved `ocr` settings from its line (`error_result` when it has none), `engine_for(ocr["model"])`, and `locate` = the locator engine's `predict_mat(img).detections` when `line_manager.engine_for_camera(camera_id)[0].is_loaded`
- Produces (`LineRuntime`):
  - `show_text_read(camera_id: str, result: TextResult, now: Optional[float] = None) -> bool` (a continuous read: listed when a field's text is new or was last listed more than `qr_hold_ms` ago; `True` when listed)
  - `remember_text_read(camera_id: str, result: TextResult, answer: Optional[Dict[str, Any]] = None) -> None` (a triggered read, with its product's `result`, `reject_reason`, `track_id`)
  - `recent_text_reads(limit: int = 50) -> List[Dict[str, Any]]`, newest first; each `{"timestamp", "camera_id", "fields": [{"name", "text", "fits"}], "status", "result", "reason", "track_id", "shown_only"}`; kept: 50
  - `model_id_for(camera_id)` gives a text reader's `ocr.locator_model_id` (so `engine_for_camera` gives its YOLO engine)
- Produces (route): `GET /api/v1/lines/{line_id}/ocr/recent?limit=1..100` (Operator) → `{"line_id", "reads"}`
- Produces (alarm): a read whose result has an `error`, for a camera on a running line, raises `camera.model_unavailable` from `f"camera:{camera_id}"`, WARNING, `f"Camera '{name}' (text reader) on line '{line}' cannot read: {error}"`, details `{line_id, camera_id}`; the next read without an error clears it.

- [ ] **Step 1: Write the failing tests** (`lines`, `_line`, `fake_camera` fixtures; `ocr_service.read_for_camera` monkeypatched to return queued `TextResult`s)

```python
def test_a_continuous_reader_is_fed_frames_and_its_reads_are_only_shown(lines, fake_camera, monkeypatch):
    runtime = _line(lines, [{"camera_id": "c1", "role": "ocr", "ocr": OCR}])
    ...  # one ContinuousVisionRunner._feed_camera pass submits the newest frame to OcrReaderPipeline.get_worker("c1")
    assert runtime.recent_text_reads()[0]["fields"] == [{"name": "Lot", "text": "LOT A12345", "fits": True}]
    assert runtime.recent_text_reads()[0]["shown_only"] is True and runtime.sent == [] and runtime.counter.total_inspected == 0

def test_the_same_text_within_the_hold_time_is_listed_once(lines):
    runtime = _line(lines, [{"camera_id": "c1", "role": "ocr", "qr_hold_ms": 1000, "ocr": OCR}])
    assert runtime.show_text_read("c1", GOOD, now=10.0) is True
    assert runtime.show_text_read("c1", GOOD, now=10.5) is False
    assert runtime.show_text_read("c1", GOOD, now=11.6) is True

def test_a_triggered_reader_is_not_fed_frames(lines, fake_camera):
    ...  # count_mode "trigger": a _feed_camera pass submits nothing to the OCR worker

def test_the_worker_keeps_the_corner_picture_of_its_first_field(lines, fake_camera, monkeypatch):
    ...  # after a read with a box, worker.corner().code == "LOT A12345" and .known is True; clear_corner() empties it

def test_a_read_for_a_camera_no_line_has_is_not_shown(lines, fake_camera, monkeypatch):
    ...  # the camera is removed from the line while its read runs: nothing is listed, no error

def test_a_reader_that_cannot_read_raises_an_alarm_and_clears_it(lines, fake_camera, monkeypatch):
    ...  # a read with error "no model" -> alarm_manager.active("camera:c1") has camera.model_unavailable naming "(text reader)";
    ...  # the next good read clears it

def test_the_recent_route(client, admin_headers, lines):
    ...  # GET /api/v1/lines/{id}/ocr/recent -> 200 {"line_id", "reads": [...]}, 404 for an unknown line
```

- [ ] **Step 2: Run them to see them fail** — Expected: `ModuleNotFoundError: app.services.ocr_service`

- [ ] **Step 3: Implement** `OcrReaderWorker` on the pattern of `_QRReaderWorker` (own supervised thread `OCRReader-{id[:8]}`, kind `"ocr_reader"`, one frame in flight). After each read: keep `latest`; keep the corner (the first field with a box: `upright(mat, box, DEFAULT_CROP_MARGIN_PX, max_long=CORNER_MAX_SIDE)` → `CornerPicture(crop, text, fits)`); with `on_result` call it; without, `runtime.show_text_read(...)` when `line_manager.route(camera_id)` still gives a line with the camera on it. `_feed_camera`: for role `"ocr"`, a continuous reader gets the newest frame like a code reader; a reader with `count_mode "trigger"` none (its pictures come from triggers).

- [ ] **Step 4: Run the tests and the frame runner tests** — `tests/test_text_reader_worker.py tests/test_session_and_continuous_vision.py tests/test_show_only_reading.py` — Expected: all pass

- [ ] **Step 5: Commit** (`Text reader: reader thread, continuous reading, recent reads`)

---

### Task 6: The video overlay and the trigger picture

**Files:**
- Modify: `app/services/ocr_service.py`, `app/services/vision_service.py` (the stream: `routed[1] == "ocr"` draws with `OcrReaderPipeline.draw_reads`)
- Test: `tests/test_text_reader_overlay.py`

**Interfaces:**
- Consumes: `draw_status_bar`, `overlay_scale`, `STATUS_STOPPED`, `STATUS_NO_LINE`, `STATUS_WAITING_TRIGGER` (`inference_engine`); `draw_corner` (`qr_service`)
- Produces:
  - `READER_NOT_READY = "READER NOT READY"`
  - `OcrReaderPipeline.draw_reads(camera_id: str, img: np.ndarray, scale_x: float = 1.0, scale_y: float = 1.0, fps: float = 0.0) -> np.ndarray`
  - `bar_parts(camera_id: str) -> List[str]` (what the bar says; tested without pixels)
  - `render_capture(frame: np.ndarray, result: TextResult) -> Optional[bytes]` (JPEG 85, never raises)

- [ ] **Step 1: Write the failing tests**

```python
def test_the_bar_says_what_the_reader_is_doing(lines, monkeypatch):
    # stopped line: ["TEXT READER", "LINE STOPPED"]; not on a line: ["TEXT READER", STATUS_NO_LINE];
    # engine not ready, or a YOLO field whose model is not loaded: ["TEXT READER", "READER NOT READY"];
    # triggered: ["TEXT READER", STATUS_WAITING_TRIGGER];
    # reading: ["TEXT READER", "FIELDS 1/2 OK", "35ms"]

def test_fields_are_drawn_green_when_they_fit_and_red_when_not(lines, monkeypatch):
    # a 640x480 grey picture, latest result: Lot fits (box at 100..300 x 100..140), Exp does not (box at 100..300 x 200..240):
    # a pixel on Lot's box edge is (0, 200, 0), one on Exp's is (0, 0, 230); the region of a region field is outlined grey

def test_a_stopped_line_draws_no_old_result_and_no_corner(lines, monkeypatch): ...

def test_render_capture_gives_a_jpeg(): ...   # starts with b"\xff\xd8"; a broken frame gives None
```

- [ ] **Step 2: Run them to see them fail**

- [ ] **Step 3: Implement** like `QRReaderPipeline.draw_reads`: scale by `overlay_scale`, regions thin grey `(128, 128, 128)`, each field's box `(0, 200, 0)` / `(0, 0, 230)` with `f"{name}: {text}"` above it (`f"{name}: no text"` when none), the bar last but the corner below it (`draw_corner(img, corner, top)`); nothing of an old result while the line is stopped, not on a line, or triggered (the result is on the trigger picture). `READER NOT READY` when `engine_for(model).ready` is false, or a field finds by YOLO class and `line_manager.engine_for_camera(camera_id)[0].is_loaded` is false.

- [ ] **Step 4: Run the tests and `tests/test_video_overlay.py tests/test_code_reader_corner_picture.py`** — Expected: all pass

- [ ] **Step 5: Commit** (`Text reader: video overlay and trigger picture`)

---

### Task 7: A triggered text reader on the line

**Files:**
- Modify: `app/services/trigger_inspection.py`, `app/services/vision_service.py` (`_feed_camera`, `_push_triggered`, `_hardware_result`), `app/services/line_service.py` (`configure`, `infers_on_demand`, `_configure_pairing`, the line status camera rows in `summary`), `app/services/plc_input_service.py` (the capture action), `app/routes/v1/lines.py` (the `picture_source == "camera"` check)
- Test: `tests/test_text_reader_line.py`

**Interfaces:**
- Consumes: `picture_per_trigger`, `camera_station`, `is_text_reader`, the reasons (Task 3); `OcrReaderPipeline`, `render_capture`, `LineRuntime.remember_text_read` (Tasks 5, 6); `text_values`, `TextResult` (Task 4)
- Produces (`trigger_inspection`):
  - `TEXT_CLASSES = {None: "text ok", "text_wrong": "text wrong", "no_text": "no text", "reader_error": "reader error"}`
  - `text_product(runtime, camera_id: str, result: TextResult, arrived_at: Optional[float] = None, trigger_no: Optional[int] = None, sequence: Any = None, request_id: Optional[int] = None) -> Dict[str, Any]` (answer as `product_from_result`'s, plus `text` and `text_status`)
  - `product_from_result(...)` hands a text reader's result to `text_product`; `render_trigger_capture(...)` uses `render_capture` for a text reader
  - crossing keys for later tasks: `crossing["text"] = text_values(result)`, `crossing["text_status"] = result.status`, `crossing["reject_reason"]` the text reason

- [ ] **Step 1: Write the failing tests** (a `_TextWorker` like `tests/test_trigger_inspection.py::_Worker` whose `process(result)` calls the `on_result` with a `TextResult`; `OcrReaderPipeline.get_worker` monkeypatched to it; `lines`, `_line`, `_run`, `_totals`, `fake_camera`, `signals` fixtures)

```python
def test_trigger_cameras_include_a_triggered_text_reader_only(lines):
    runtime = _line(lines, [_reader("t1"), {**_reader("c1"), "count_mode": "track"}])
    assert trigger_cameras(runtime) == ["t1"] and runtime.infers_on_demand("t1") and not runtime.infers_on_demand("c1")

@pytest.mark.parametrize("result, totals, reason, cls, status", [
    (GOOD, (1, 1, 0), None, "text ok", "good"),
    (WRONG, (1, 0, 1), "text_wrong", "text wrong", "wrong"),
    (NONE, (1, 0, 1), "no_text", "no text", "none"),
    (ERROR, (1, 0, 1), "reader_error", "reader error", "none"),
])
def test_an_own_text_reader_makes_one_product_per_trigger(lines, text_workers, fake_camera, result, totals, reason, cls, status):
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("t1")])
    fake_camera("t1")
    answer = _run_trigger(runtime, "t1", result)
    assert _totals(runtime.aux_counters["t1"]) == totals and _totals(runtime.counter) == (0, 0, 0)
    payload, event = runtime.sent[0]
    assert (payload["class_name"], payload["reject_reason"], payload["text_status"]) == (cls, reason, status)
    assert answer["text"] == text_values(result) and event["counting_camera"] is False
    assert runtime.trigger_view()[0]["camera_id"] == "t1" and runtime.recent_text_reads()[0]["result"] == ("good" if reason is None else "reject")

def test_a_reader_that_does_not_finish_in_time_is_a_reader_error(lines, text_workers, fake_camera, monkeypatch):
    monkeypatch.setattr(trigger_inspection, "TRIGGER_INFERENCE_TIMEOUT_S", 0.05)
    ...  # the worker never answers: reject "reader_error", class "reader error", status "inspected"

def test_a_disconnected_reader_is_a_no_picture_reject(lines, text_workers): ...

def test_a_joined_text_reader_decides_the_counting_cameras_product(lines, workers, text_workers, fake_camera):
    runtime = _line(lines, [_trigger_cam(), {**_reader("t1"), "station": "join", "join_offset_ms": 0, "join_window_ms": 80, "join_missing": "reject"}])
    ...  # vis "bottle" then t1 WRONG: one product, REJECTED, reason "text_wrong", reject_camera_id "t1", payload["text"] == {"Lot": "LOT A1234"}
    ...  # vis "bottle" and no t1 result within the window: REJECTED "station_no_result"

def test_a_capture_input_triggers_a_text_reader(signals, text_workers, fake_camera): ...

def test_a_hardware_triggered_reader_reads_every_picture(lines, text_workers, fake_camera, monkeypatch):
    ...  # trigger_mode "hardware", picture_source "camera": _push_triggered hands the camera's next triggered frame
    ...  # to the OCR worker; its result makes one product

def test_a_result_after_the_line_stopped_makes_nothing(lines, text_workers, fake_camera):
    ...  # the line is stopped while the worker holds the picture: no product, no recent read

def test_the_status_row_of_a_text_reader(lines, text_workers, fake_camera):
    ...  # lines.summary(runtime)["cameras"]: the reader's row has role "ocr", count_mode "trigger", station "own" and
    ...  # counts {total_inspected, good_count, rejected_count}; a joined one has station "join", join settings and station_stats

def test_the_card_sets_the_cameras_trigger(lines): ...   # locks_trigger_mode / trigger_mode_for_card for a triggered reader: "software" (system), "hardware" (camera)
```

- [ ] **Step 2: Run them to see them fail**

- [ ] **Step 3: Implement**

- `trigger_cameras`: `picture_per_trigger(cam)`. `_take_picture`: the worker is `OcrReaderPipeline.get_worker(camera_id)` for a text reader. `trigger_camera`: no `engine.is_loaded` check for a text reader (its errors are `reader_error` products); the error text "is not a vision camera ... in trigger mode" names a text reader too; a `_NoPicture` with `taken=True` from a text reader becomes `text_product(runtime, camera_id, error_result(ocr, MODEL_TOO_SLOW.replace("model", "reader")), ...)`; the answer's `inference_time_ms` is the reader's `ms`.
- `text_product`: `counter = runtime.counter_for(camera_id)`, `make_crossing(TEXT_CLASSES[result.reason], result.result != "good", track_id=_next_track_id(runtime), confidence=min(conf of fields with a line) or 0.0, bbox=<first field box as {x1, y1, x2, y2, width, height}> or None, camera_id=..., arrived_at=...)`; sets `reject_reason`, `text`, `text_status`; `_add_context`; `counter.emit_crossing`; `runtime.remember_text_read(camera_id, result, answer)`.
- `vision_service`: `every_model = role in ("vision", "ocr") and entry.get("count_mode") == "trigger"`; `_push_triggered` takes `OcrReaderPipeline.get_worker` for an `ocr` camera; `_hardware_result` reads `last_inferred_frame` from `OcrReaderPipeline.peek` for one.
- `line_service.configure`: `aux_ids = {c["camera_id"] for c in self.cameras if camera_station(c)}`; `infers_on_demand` and `_configure_pairing` use `picture_per_trigger`. In `summary`, a text reader's row gets `count_mode` and, as a vision station's row does, `station` with `counts` or the join settings and `station_stats` (the station part moves out of the `role == "vision"` block into one both roles use). `plc_input_service` capture and the `lines.py` camera-trigger check accept a text reader.

- [ ] **Step 4: Run the tests and the trigger and station tests**

Run: `$PY -m pytest tests/test_text_reader_line.py tests/test_trigger_inspection.py tests/test_joined_cameras.py tests/test_trigger_notice.py tests/test_trigger_on_demand.py tests/test_trigger_captures.py tests/test_input_trigger.py -q -p no:cacheprovider`
Expected: all pass

- [ ] **Step 5: Commit** (`Text reader: one picture per trigger, own and joined`)

---

### Task 8: The text in records, exports and values

**Files:**
- Modify: `app/services/counting_service.py` (the product payload and PLC event copy `text` and `text_status` from the crossing, as they copy `trigger_no`), `app/services/line_service.py` (`_finish_product`: a joined text reader's `text` / `text_status` go into `fields` and `plc_fields`), `app/services/production_records_service.py` (`build_row` details keys gain `"text"`, `"text_status"`), `app/services/records_export.py` (column `("text", lambda r: _text(r.details))` after `"sequence"`), `app/services/event_values.py` (docstring and values)
- Test: `tests/test_text_reader_records.py`; update the export header assertions in `tests/test_production_records.py`

**Interfaces:**
- Consumes: crossing keys `text`, `text_status` (Task 7)
- Produces: payload / PLC event keys `text` (`{name: text}`) and `text_status`; `values_for(event)` gives `text.<name>` and `text_status`; record `details.text`, `details.text_status`; export column `text` as `"Expiry=12/03/2027; Lot=A12345"` (fields in the order of the dict)
- Joined: several joined text readers: their `text` dicts merged in card order; `text_status` the first that is not `"good"` in card order, else `"good"`

- [ ] **Step 1: Write the failing tests**

```python
def test_values_of_a_text_product():
    values = values_for({"result": "reject", "reject_reason": "text_wrong", "text": {"Lot": "LOT A1234"}, "text_status": "wrong"})
    assert values["text.Lot"] == "LOT A1234" and values["text_status"] == "wrong" and values["reject_reason"] == "text_wrong"
    assert "text.Exp" not in values_for({"text": {"Lot": "X"}})

def test_a_record_keeps_the_text_and_the_export_shows_it():
    row = build_row({"is_defect": False, "class_name": "text ok", "text": {"Expiry": "12/03/2027", "Lot": "A12345"},
                     "text_status": "good", "camera_id": "t1", "line_id": "line-1"}, "product", False)
    assert row["details"]["text"] == {"Expiry": "12/03/2027", "Lot": "A12345"} and row["details"]["text_status"] == "good"
    ...  # the export's text column for that row: "Expiry=12/03/2027; Lot=A12345"

def test_an_own_and_a_joined_product_carry_the_text(lines, text_workers, workers, fake_camera): ...   # payload and PLC event have text and text_status
```

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** per the Files list
- [ ] **Step 4: Run the tests and `tests/test_production_records.py tests/test_event_values.py tests/test_messages_and_plc_values.py tests/test_records_archive.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Text reader: text in records, exports and values`)

---

### Task 9: The setup API

**Files:**
- Create: `app/routes/v1/ocr.py`
- Modify: `main.py` (import and `PROTECTED_ROUTERS`), `app/security/config_lock.py` (`LOCK_FREE_ROUTES` gains `("POST", "/api/v1/ocr/picture/camera/{camera_id}")` and `("POST", "/api/v1/ocr/preview/camera/{camera_id}")`), `app/security/api_key_scopes.py` (`/api/v1/ocr/` → `None`, JWT-only)
- Test: `tests/test_ocr_api.py`

**Interfaces:**
- Consumes: `normalize_camera` (Task 3), `read_picture` (Task 4), `engine_for` (Task 2), `ocr_models.available` / `new_reader_model`, `line_manager.api_engine(model_id=...)` for an unsaved locator model
- Produces (all `require_supervisor`):
  - `POST /api/v1/ocr/picture/camera/{camera_id}` → JPEG of the camera's newest picture, kept as its held picture (`HELD_TTL_S = 600`; `404` "The camera is not connected" / "The camera has no picture yet")
  - `GET /api/v1/ocr/picture/camera/{camera_id}?held=0|1` → JPEG (`404` "No picture is held for this camera" when `held=1` and none)
  - `POST /api/v1/ocr/preview/camera/{camera_id}` body `{"settings": {...}, "held": bool}` → `{"width", "height", "ms", "lines": [{"text", "confidence", "box"}], "fields": [{"name", "text", "raw", "fits", "box", "area"}], "result", "reason", "error"}`; the settings are checked by `normalize_camera({**settings, "camera_id": camera_id, "role": "ocr"}, 0)` and a `ValueError` is `400` with its message; the read runs in `asyncio.to_thread`
  - `GET /api/v1/ocr/models` → `{"models": [...], "new": "...", "ready": bool, "error": str | None}` (`ready`/`error` of `engine_for(new)`)

- [ ] **Step 1: Write the failing tests** (`client`, `admin_headers`; a supervisor and an operator user made as `tests/test_plc_inputs.py::operator_headers` does; `fake_camera`; `ocr_engine.engine_for` monkeypatched to a fake engine)

```python
def test_take_a_picture_then_preview_it(client, supervisor_headers, fake_camera, fake_engine): ...
    # POST picture -> 200 image/jpeg; POST preview {"settings": {...Lot region...}, "held": true}
    # -> fields[0] == {"name": "Lot", "text": "LOT A12345", "fits": True, ...}, result "good"

def test_a_preview_with_bad_settings_says_what_a_save_would(client, supervisor_headers, fake_camera, fake_engine):
    # settings fields [] -> 400, detail contains "needs 1 to 4 text fields"

def test_no_held_picture_is_404_and_it_expires(client, supervisor_headers, fake_camera, monkeypatch): ...

def test_models(client, supervisor_headers, fake_engine): ...      # {"models": ["small"], "new": "small", "ready": True, "error": None}

def test_operators_api_keys_and_the_lock(client, operator_headers, supervisor_headers, fake_camera, fake_engine):
    # operator -> 403; required_api_key_scopes("POST", "/api/v1/ocr/preview/camera/c1") is None;
    # with the configuration locked (as tests/test_config_lock_api.py locks it) both POSTs still answer 200
```

- [ ] **Step 2: Run them to see them fail** — Expected: 404 for `/api/v1/ocr/...`
- [ ] **Step 3: Implement** the router, the registration, the lock and scope entries
- [ ] **Step 4: Run the tests and `tests/test_config_lock_policy.py tests/test_tokens.py tests/test_api.py`** (the route inventories) — Expected: all pass
- [ ] **Step 5: Commit** (`Text reader: setup API`)

---

### Task 10: The Text reader card in Line setup

**Files:**
- Create: `assets/text_reader.js` (`window.TextReader`, `module.exports` in node, as `assets/data_backups.js`)
- Modify: `assets/production_lines.js` (`JOBS`, `START_JOBS`, `jobOf`, `JOB_NOTES`, `FOLDS`, `foldShown`, `cameraCardHtml`, `syncCard`, `bindCard`, `cardEntry`, `saveLineSetup`), `dashboard.html` (`<script src="/assets/text_reader.js?v=1.0.0"></script>` before `production_lines.js`; bump `production_lines.js?v=3.13.0`), `assets/dashboard.css`
- Test: `tests/test_text_reader_card.py` (node), `tests/test_help_tips.py` (`FILES` gains `assets/text_reader.js`; `KEEP` gains the Format and Join dots help sentences)

**Interfaces:**
- Consumes: `/api/v1/ocr/models`, `/api/v1/ocr/picture/camera/{id}`, `/api/v1/ocr/preview/camera/{id}` (Task 9); the camera entry shape (Task 3)
- Produces (`window.TextReader`):
  - `foldHtml(ocr: object|null, opts: {models: string[], newModel: string, yoloModels: [{id, name, classes}], readOnly: bool}) -> string` (the "Text fields" fold body: Reader settings, up to 4 field rows, the picture with its canvas, Live / Take a picture / Read buttons)
  - `ocrFromValues(values: object) -> object` (pure: the form's values → the `ocr` dict the server takes)
  - `readCard(card: Element) -> object` (collects the values, then `ocrFromValues`)
  - `suggestFormat(text: string) -> string` (same rule as `text_format.suggest_format`)
  - `regionFrom(x0, y0, x1, y1, width, height) -> [x1, y1, x2, y2]` (fractions, ordered, clamped to 0–1, each side at least 0.02, 4 decimals)
  - `bind(card: Element, api: {fetchJson, fetchBlob}) -> void` (field rows, region drag, Take a picture, Live, Read, preview after each change and each Live picture, drawing the preview on the canvas)
  - `MAX_FIELDS = 4`
- The job: `JOBS.text = { label: "Text reader", short: "Text reader", role: "ocr", counting: false, codes: false }`; `jobOf` gives `"text"` for `role === "ocr"`; `START_JOBS` gains `"text"`; `JOB_NOTES.text = "Reads printed text (dates, lot codes) and checks it against a format. One picture per trigger decides good or reject, on its own or joined to the counting camera's product."`
- The card for a text reader shows the folds Camera, Job, Counting (titled "Reading"), Text fields, Image, Video; in Reading, Count mode's options read "Continuously (shows the text only)" / "One picture per trigger", and When nothing is found and the tracking fields are hidden; the station select shows with One picture per trigger; the camera's Trigger is set by the card as for a vision camera on One picture per trigger.
- Add field and remove buttons carry `data-lock` (the configuration lock disables them); Live, Take a picture and Read do not (they save nothing).
- `cardEntry` for `role "ocr"`: `count_mode`, the picture settings, `station` and join settings when triggered, `triggers`, and `ocr: TextReader.readCard(card)`. `saveLineSetup` refuses with a toast: a field with Find YOLO class and no YOLO model ("Camera N (name): choose the YOLO model its fields read in."), a field without a format ("... text field 'Lot': give it a format.").

- [ ] **Step 1: Write the failing node tests**

```python
def test_script_is_valid_and_loaded(): ...          # node --check; dashboard.html has the script tag before production_lines.js

def test_suggest_format_matches_the_server(): ...   # "LOT 12345" -> "LOT 99999", "EXP 03/2027" -> "EXP 99/9999", " a1 " -> "A9"

def test_region_from_a_drag():
    assert _api("api.regionFrom(500, 300, 100, 100, 1000, 500)") == [0.1, 0.2, 0.5, 0.6]
    assert _api("api.regionFrom(-50, 10, 5, 12, 1000, 500)") == [0, 0.02, 0.02, 0.04]

def test_values_to_settings():
    assert _api('api.ocrFromValues({model: "tiny", locator: "", upside_down: false, join_dots: "0", ink: "dark", '
                'min_confidence: "0.5", fields: [{name: "Lot", find: "region", region: [0.1,0.2,0.5,0.6], format: "LOT A99999", yolo_class: ""}]})') == {
        "model": "tiny", "locator_model_id": None, "upside_down": False, "join_dots": 0, "ink": "dark", "min_confidence": 0.5,
        "fields": [{"name": "Lot", "find": "region", "region": [0.1, 0.2, 0.5, 0.6], "format": "LOT A99999"}]}

def test_the_fold_has_four_fields_at_most_and_help_only_where_needed():
    html = _api('api.foldHtml({fields: [...5 fields...]}, {models: ["small"], newModel: "small", yoloModels: [], readOnly: false})')
    assert html.count('data-text-field=') == 4 and html.count("data-help=") == 2
    assert 'data-f="ocr_format"' in html and 'data-f="ocr_join_dots"' in html

def test_a_new_reader_gets_the_new_model():
    assert 'value="tiny" selected' in _api('api.foldHtml(null, {models: ["tiny", "small"], newModel: "tiny", yoloModels: [], readOnly: false})')
```

And in `tests/test_line_setup_markup.py`-style checks (string search in `production_lines.js`): `JOBS` has `text`, `jobOf` maps `role === "ocr"`, `cardEntry` sends `ocr: TextReader.readCard(card)`.

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** `assets/text_reader.js`, then the `production_lines.js` changes. The preview: after each change (debounced 300 ms) and each Live picture (every 1 s while Line setup is shown and the fold is open), `POST /api/v1/ocr/preview/camera/{id}` with `{settings: cardEntry(card), held}`; draw on the canvas, scaled to the shown picture: areas grey, field boxes green / red with `name: text`, other lines thin blue `(0, 120, 255)`; the result and ms below. Read lists the preview's `lines`; clicking one sets the selected field's format to `suggestFormat(text)` (or adds a field). Dragging on the picture with a Region field selected sets its region with `regionFrom`.
- [ ] **Step 4: Run the tests and `tests/test_line_setup_markup.py tests/test_help_tips.py tests/test_camera_cards.py tests/test_trigger_card_markup.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Text reader: the card in Line setup`)

---

### Task 11: The Line dashboard and the Records page

**Files:**
- Modify: `assets/text_reader.js` (panel renderers), `assets/production_lines.js` (`renderLineExtras`, the reads refresh, `tileDetail`, `REJECT_REASON`, `recordCode`), `assets/dashboard.css`
- Test: `tests/test_text_reader_dashboard.py` (node)

**Interfaces:**
- Consumes: `GET /api/v1/lines/{id}/ocr/recent` (Task 5); line status camera rows with `role "ocr"` (they already list every camera); `trigger_cameras` rows (Task 7)
- Produces (`window.TextReader`): `readsPanelHtml() -> string` (the "Latest text reads" card with `<tbody id="plTextBody">`); `readsRowsHtml(reads: object[]) -> string` (columns Time, Camera, Text, Product result: each field as `name: text` followed by "fits", "does not fit" or "no text"; the product result and reason for a triggered read, "shown only" for a continuous one; all text escaped)
- `REJECT_REASON` gains `text_wrong: "text does not fit"`, `no_text: "no text"`, `reader_error: "text reader error"`; `tileDetail` for `role "ocr"`: `"Text reader · one picture per trigger"` / `"Text reader · reads continuously"` (+ station as for a vision camera); the Records page shows `details.text` as `name: text` pairs after the code in the Code cell.

- [ ] **Step 1: Write the failing node tests** (`readsRowsHtml` on two reads: a shown-only good read and a triggered reject; escaping of a text with `<b>`; `readsPanelHtml` has `plTextBody`; the records cell shows `Lot: A12345`)
- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement**; the panel is shown when a camera row of the line has `role === "ocr"`, and refreshed with the code reads (same interval)
- [ ] **Step 4: Run the tests and `tests/test_line_status_layout.py tests/test_line_dashboard_video_controls.py tests/test_trigger_view_markup.py`** — Expected: all pass
- [ ] **Step 5: Commit** (`Text reader: Line dashboard panel and Records page`)

---

### Task 12: Docs, Docker, and the live check

**Files:**
- Modify: `README.md` (a "Text reader (OCR)" section: what it reads, fields and the format notation, Join dots, models and `python -m app.engines.ocr_models --fetch` in the Windows install steps, speeds measured; the `OCR_MODEL_DIR` row in the settings table), `Dockerfile` (build stage, after the pip install: `RUN OCR_MODEL_DIR=/opt/ocr-models python -m app.engines.ocr_models --fetch` with the app's `app/engines` and `app/config.py` copied in first; runtime stage copies `/opt/ocr-models` and sets `ENV OCR_MODEL_DIR=/opt/ocr-models`), `.env.example`, `API_KEY_QUICKSTART.md` (`/lines/{id}/ocr/recent` under monitor; the setup routes are JWT-only)
- Test: the live check (no repo test); the full suite

- [ ] **Step 1: Write the docs and the Docker steps**; `docker build --build-arg ACCEL=cpu .` and confirm `/opt/ocr-models` holds the four `PP-OCRv6_*` files (PyPI is slow here: retry a failed build)
- [ ] **Step 2: Full suite** in the worktree (Global Constraints). Expected: all pass, apart from the known flaky `test_camera_roi_and_qr.py::test_motion_blurred_code_reads_in_the_second_pass[1]` (passes alone)
- [ ] **Step 3: Live check on a scratch copy** (never the real `data/`): the scratch-tools launcher on port 8765 with its own `data/`, the owner's test streamer's picture channel (8557) or a fake camera showing the probe's printed and dot-matrix pictures, headless Chrome as in `check_data_live.py`:
  1. Line setup: add a Text reader, drag a region on a held picture, Read, click the LOT line (format `LOT A99999` suggested), save; 1280 and 375 px, no side scroll, no page errors.
  2. Continuous: the video shows `TEXT READER`, the green box and the corner picture; Latest text reads lists it once within the hold time.
  3. One picture per trigger, own: `POST /lines/{id}/trigger` → a good product; a picture with `LOT A1234` → `text_wrong`; the trigger picture shows the boxes.
  4. Joined to a counting camera: the product gets the reader's reason.
  5. Records page and CSV export show the text; a send card with `{text.Lot}` sends it.
  6. CPU and DirectML (`INFERENCE_DEVICE=cpu` / `auto`), with a YOLO counting camera running at the same time on DirectML: no crash.
- [ ] **Step 4: Record in the vault** (decisions note: what was built, the live check, the memory numbers) and commit (`Text reader: docs and Docker`)
