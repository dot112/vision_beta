# OCR text reader, Part 1: camera job, settings and live view

Date: 2026-10-27. Branch `claude/ocr-reader` (from `main` at `234b44d`).

The owner wants more flexibility: a camera on a line that reads printed text (dates and lot codes),
checks it and gives good or reject, set up in Line setup with a live picture, and shown live on the
Line dashboard. Training the reader on the owner's own pictures is Part 2, with its own spec; Part 1
is everything else, and teaching without a training run.

## Owner's decisions (2026-10-27)

- **Library:** RapidOCR 3.10 (Apache-2.0): PaddleOCR's PP-OCR models on ONNX Runtime, the runtime
  the server already uses for YOLO.
- **Text:** dates and lot codes (short printed codes: letters, digits, `/ - . :`).
- **Finding the text:** per field, one of: a region drawn on the picture, the boxes of one class of
  a YOLO model (like the owner's `datebox` model: `dateBox`, `datepage`), or the whole picture.
- **Check:** the format only (no date range, no batch or product comparison in Part 1).
- **Line result:** like an inspection station: its own products, or joined to the counting
  camera's product by travel time. Reading continuously only shows the text.
- **Training:** Part 1 teaches without a training run (fields, formats, look-alike fixes). Part 2
  fine-tunes the recognition model in a separate trainer (Python 3.13 + PaddlePaddle; PaddlePaddle
  has no Python 3.14 wheels), and the server loads the trained model.

### Libraries looked at

| Library | Why not (or why) |
|---|---|
| **RapidOCR 3.10** (chosen) | ONNX Runtime (CPU, DirectML, CUDA; TensorRT engine for Jetson); `opencv-python-headless`, like the server; default models inside the 27 MB wheel (offline); line boxes, text, confidence, character boxes; can read a cut-out without searching. All dependencies have Python 3.14 wheels (omegaconf 2.4 no longer needs antlr4). |
| PaddleOCR 3.7 | Needs PaddlePaddle at run time: no Python 3.14 wheels (3.3.1 stops at 3.13), heavy. Used for training in Part 2. |
| EasyOCR 1.7 | Needs PyTorch at run time (the Docker image leaves it out); slower. |
| Tesseract | A separate program; weak on dot-matrix print (about 55% of characters right on dot-matrix dates before retraining, per an open-source project). |
| docTR / OnnxTR | Good and trainable in PyTorch, but made for documents. |

## Measurements (throwaway probe, 2026-10-27, this PC: 8 threads, GTX 1650)

Pictures 1280 x 720 drawn by the probe: two lines, `EXP 12/03/2027` and `LOT A12345`, printed
(solid strokes) or dot-matrix (dots 6 px wide, 9 px apart, like an inkjet coder). Times are the
median of 6–8 calls after a warm-up, PP-OCRv6 *small* (the model in the wheel).

| Case | CPU | DirectML | Read |
|---|---|---|---|
| Whole picture, RapidOCR defaults | 800–950 ms | 136–157 ms | printed: right (also turned 8° and 180°) |
| Whole picture, detection size `max 640` | 225–285 ms | | printed: right |
| Whole picture, detection size `max 480` | 205–225 ms | | printed: right |
| One line cut out, reading only (no search) | 19–25 ms | 44 ms | |
| Dot-matrix, as it is | | | **nothing found** |
| Dot-matrix, dots joined (grey + opening, 5–9 px) | 205–730 ms | | `LOT812345`, `EXP12/03/2821`, `EXI 12103/2821` |
| Dot-matrix, blurred and turned 8° | 630–810 ms | | `ENP 12`, `LOTT A123` |

The three PP-OCRv6 sizes, detection size `max 640`, no orientation step (*tiny* and *medium*
fetched from ModelScope in 14 s and 135 s):

| Model | Files | Printed, CPU | Printed, DirectML | Dot-matrix, dots joined |
|---|---|---|---|---|
| *tiny* | 1.8 + 4.4 MB | 49–51 ms | 30 ms | both lines found in all 6 cases, characters misread (`L0T 512345`) |
| *small* (in the wheel) | 31 MB | 168–172 ms | 65 ms | a line missed in 3 of 6 cases |
| *medium* | 59 + 73 MB | 2.6 s | 75–99 ms | a line cut short (`1222`) |

What this decides:

- The default detection (`limit_type: min`, 736) scales every picture *up* to at least 736 px on its
  short side, which is why a 640 x 360 picture took as long as a 1280 x 720 one. The reader uses
  `limit_type: max`, 640: three to four times faster on the CPU, no loss on printed text.
- Reading a cut-out without searching is about ten times faster than searching a whole picture.
- *tiny* is three times faster than *small* on the CPU, reads printed text as well, and finds
  dot-matrix lines more often: it is the model a new text reader gets when the server has it.
  *medium* is for a GPU only.
- Clean dot-matrix print is not even found: joining the dots first is needed, so Part 1 has one
  setting for it (**Join dots**). This is not the image processing the owner removed from the code
  reader on 2026-10-25 (zxing did that work itself; RapidOCR does not, as measured). Even joined,
  the pretrained model misreads dot-matrix digits: dot-matrix accuracy is what Part 2's training is
  for, and the real check is on the owner's own pictures.
- The multilingual model reads Chinese characters into dot-matrix misreads (`日`, `上`). Limiting the
  reader to the characters the formats allow removes them (measured); it does not fix a wrong digit.
- RapidOCR keeps per-call switches (`use_det`, `use_cls`) for the next calls: a call with
  `use_det=False` turned searching off for the calls after it. The engine passes every switch on
  every call.

## 1. New and changed units

| File | Does |
|---|---|
| `app/engines/text_format.py` (new) | The format notation: parse, look-alike fixes, fit, the characters a format allows, a format suggested from a reading. No runtime imports. |
| `app/engines/ocr_engine.py` (new) | Wraps RapidOCR: one engine per reader model and device, shared by all text readers, one call at a time. Reads a picture or cut-outs, with the character limit and Join dots. |
| `app/services/ocr_service.py` (new) | One reader thread per text reader camera (like `_QRReaderWorker`): finds each field's area, reads, matches lines to fields, keeps the latest result and the corner picture, draws the video overlay. |
| `app/services/line_config.py` | Role `ocr`: `normalize_camera` validates its settings; station, join, picture and trigger settings shared with triggered inspection stations; kept keys. |
| `app/services/line_service.py` | A text reader's continuous reads (shown only) and triggered results (own product, or joined); the line's latest text reads. |
| `app/services/trigger_inspection.py` | A trigger for a text reader takes its picture the same way, and hands it to the reader instead of a YOLO model. |
| `app/services/vision_service.py` | The frame runner feeds a continuous text reader; the stream draws its overlay. |
| `app/services/event_values.py`, `production_records_service.py`, `records_export.py` | `text.<field>` and `text_status` values; the texts in the product record and the export. |
| `app/engines/ocr_models.py` (new) | The reader models on disk, and `python -m app.engines.ocr_models --fetch`, which fetches *tiny* and *medium* once, for an offline server. |
| `app/routes/v1/ocr.py` (new) | Preview and held picture for Line setup; the reader models on offer. |
| `app/routes/v1/lines.py` | `GET /lines/{id}/ocr/recent`: the line's latest text reads. |
| `assets/text_reader.js` (new), `assets/production_lines.js`, `dashboard.html`, `assets/dashboard.css` | The Text reader card part in Line setup; the Latest text reads panel and tile on the Line dashboard. |
| `Dockerfile`, `README.md` | The build and the Windows install steps fetch the models. |
| `requirements.txt` | `rapidocr==3.10.*` (pinned to the minor version: the character limit hooks its recognizer's decoder). |

## 2. Settings of a text reader camera

A camera's entry on a line (`system_state.json`, `lines[].cameras[]`). The settings file version
stays 9: a new role and new keys need no upgrade step.

```json
{
  "camera_id": "c1",
  "role": "ocr",
  "qr_hold_ms": 1500,
  "ocr_read": "trigger",
  "station": "own",
  "picture_source": "system", "picture_delay_ms": 0, "picture_wait_ms": 1000, "notice_trigger": null,
  "triggers": [1],
  "ocr": {
    "model": "small",
    "locator_model_id": "7f3c...",
    "upside_down": false,
    "join_dots": 0,
    "ink": "dark",
    "min_confidence": 0.5,
    "fields": [
      {"name": "Expiry", "find": "region", "region": [0.25, 0.30, 0.75, 0.45], "format": "EXP 99/99/9999"},
      {"name": "Lot", "find": "yolo", "yolo_class": "dateBox", "format": "LOT A99999"}
    ]
  }
}
```

- `ocr_read`: `continuous` (shows the text, never decides) or `trigger` (one picture per trigger,
  decides). Default `continuous`.
- With `trigger`: `station` `own` or `join` (default `own`); with `join`, `join_offset_ms`,
  `join_window_ms`, `join_missing` as for a joined inspection station; the picture settings
  (`picture_source`, `picture_delay_ms`, `picture_wait_ms`, `notice_trigger`) and `triggers` as for
  a triggered inspection station, with the same limits and messages.
- `qr_hold_ms` (every camera has it): in continuous reading, the same text of a field within this
  time is one read.
- `ocr.model`: `tiny`, `small` (in the wheel) or `medium` (section 7). Left out, `small`, which is
  always there; Line setup gives a new text reader `tiny` when the server has it.
- `ocr.locator_model_id`: the YOLO model (AI models page) whose boxes `yolo` fields read in; needed
  when any field finds by `yolo`, and only then.
- `ocr.upside_down`: also read text turned 180° (RapidOCR's orientation step; slower). Default off.
- `ocr.join_dots`: 0 (off) or an odd size 3–15 px; `ocr.ink` `dark` (dark print on a light
  surface, the default) or `light`.
- `ocr.min_confidence`: 0.30–0.99, default 0.50: a line read with less is ignored.
- `ocr.fields`: 1 to 4 fields, in order.
  - `name`: a value name (`[A-Za-z][A-Za-z0-9_]{0,31}`, the rule of product field names), unique on
    the camera, ignoring case.
  - `find`: `region`, `yolo` or `all`.
  - `region` (with `region`): `[x1, y1, x2, y2]`, fractions 0–1 of the camera's picture (after
    its ROI, the picture the video shows), `x1 < x2`,
    `y1 < y2`, at least 0.02 wide and high. Fractions keep a region right when the camera's size
    or ROI changes.
  - `yolo_class` (with `yolo`): one class name of the locator model.
  - `format`: 1–64 characters (section 3).
- A client that leaves `ocr`, `ocr_read` or a picture setting out keeps what is saved (kept keys,
  as for vision cameras). A camera's role can change between jobs; a role change drops the old
  role's settings, as now.
- A locator model in use by a text reader cannot be deleted (the model-in-use check counts it).

## 3. Format and the check

`app/engines/text_format.py`.

- **Notation:** `9` a digit, `A` a letter A–Z, `X` a letter or a digit, `*` any one character; any
  other character must be that character. Examples: `EXP 99/99/9999`, `LOT A99999`, `L99-9999`.
- **Spaces** are left out on both sides before comparing (printers and the reader both move them).
- **Case:** the reading is put in capitals (dates and lot codes); literal letters compare in
  capitals.
- **Length:** without spaces, the reading must have as many characters as the format.
- **Look-alikes**, fixed only where the format asks for one kind: at a `9`, `O→0 Q→0 D→0 I→1 L→1
  S→5 B→8 Z→2 G→6`; at an `A`, the other way round (`0→O 1→I 5→S 8→B 2→Z 6→G`). Nothing is changed
  at `X`, `*` or a literal.
- **Fits** when every position fits after the fixes. The text kept is the fixed reading with its
  spaces as read.
- **Characters allowed** (the reader's limit): the union over the camera's formats of `0–9` (`9`,
  `X`), `A–Z` (`A`, `X`) and the literal characters, plus space. No limit when a format has `*`.
- **Suggested format** from a reading: each digit becomes `9`, every other character stays as read.
  `LOT 12345` gives `LOT 99999`; `EXP 03/2027` gives `EXP 99/9999`.

## 4. Reading one picture

`ocr_service` with `ocr_engine`:

1. **Areas.** Each field's area: its region cut out of the picture; for `yolo`, every box of its
   class that the locator model finds in this picture (the model runs once per picture, on demand,
   through the same model loading and GPU lock as vision cameras); for `all`, the whole picture.
2. **Join dots** (when on), on each area: grey, then a morphological opening (dark ink) or closing
   (light ink) with a round kernel of that size.
3. **Read** each area: search for lines and read them (`limit_type: max`, 640; orientation step
   only with `upside_down`), with the character limit. The engine keeps lines down to 0.30; lines
   under the camera's `min_confidence` are dropped.
   Boxes are mapped back to the camera picture.
4. **Match** lines to fields, in field order, each line to one field at most: a field takes, among
   the lines in its area, the one that fits its format (the highest confidence when several do);
   when none fits, the line with the fewest differences from the format (positions that do not
   fit after the look-alike fixes, plus the difference in length; to show what is wrong);
   when its area has no line, nothing.
5. **Result** per field: `text`, `raw` (as read), `fits`, `confidence`, `box`. The picture is
   **good** when every field fits; otherwise **reject** with the first field's problem in field
   order: `text_wrong` (its line does not fit) or `no_text` (no line).

## 5. On the line

### Continuously
The frame runner gives the reader the newest frame whenever it is free (frames in between are
skipped). Each new reading is shown: on the video, and in the line's Latest text reads (a field's
same text within `qr_hold_ms` is not listed again). Nothing else: no product, count, record,
output card or message, like a continuous code reader.

### One picture per trigger
The same triggers and picture rules as a triggered inspection station (`trigger_inspection`): a
"Take a picture" input with one of the camera's trigger numbers, `POST /lines/{id}/trigger`,
`/control/trigger/{camera}`, the camera's own hardware trigger (with or without a notice), and the
software trigger on an industrial camera. The picture goes to the text reader instead of a model.

- **Own:** each picture is a product of this camera: class `text ok`, `text wrong`, `no text` or
  `reader error` (a trigger with no picture is `no picture`, as now);
  good or reject; counted, recorded, and it fires the output cards and messages that name the
  camera, as an own station's product does.
- **Join:** the picture's result joins the counting camera's product by travel time, as a joined
  station's result does; a product with no result from it follows `join_missing`. A joined reject
  keeps its own reason (`text_wrong`, `no_text`) and the reader as the rejecting camera.
- **New reject reasons:** `text_wrong`, `no_text`, `reader_error` (the reader could not read: not
  ready, failed, or not finished within the 10 s a trigger waits for a model). `no_picture` stays
  for a trigger that got no picture.
- **Records:** `details.text` = `{field name: text}` (only fields with a line),
  `details.text_status` = `good`, `wrong` or `none`. The CSV/XLSX export gets a `text` column
  (`Expiry=12/03/2027; Lot=A12345`); the Records page shows it with the row.
- **Values** for output cards, messages and only-when rules: `text.<field>` (a field with a line) and
  `text_status`. A missing field is missing, not empty, as with other values.
- **Latest picture:** the last triggered picture with its field boxes, served by the existing
  `GET /lines/{id}/trigger/capture?camera_id=` like a triggered station's.

## 6. Live view

### Line dashboard
- **Video** of a text reader:
  - a **TEXT READER** bar, drawn like the code reader's: `LINE STOPPED` / `NOT ON A LINE` /
    `READER NOT READY` / `WAITING FOR A TRIGGER` when that applies, else `FIELDS 2/2 OK`, then the
    read time in ms;
  - regions outlined thin and grey; each field's line in a box, green when it fits, red when not,
    with the field name and its text above the box;
  - the **corner picture** of the code reader, for text: the first field's line cut out straight,
    with its text, kept until the next reading, hidden while the line is stopped.
- **Latest text reads** panel (beside Latest code reads, shown when the line has a text reader):
  time, camera, each field's text with a tick or what is wrong (`expected LOT A99999`), and the
  product's result for a triggered reader. From `GET /lines/{id}/ocr/recent` (Operator).
- **Strip tile** of the camera: "Text reader", and for a triggered reader its last picture, like a
  triggered station.

### Line setup: the Text reader card
- Job list: a new job **Text reader** (`role: "ocr"`).
- **When to read:** Continuously / One picture per trigger; with a trigger, Own / Join and the join
  settings, the picture settings and triggers, the same controls as a triggered station.
- **Reader:** model (only models on disk are offered), Text can be upside down, Join dots (Off,
  3–15 px) with ink Dark on light / Light on dark, Lowest confidence, and the YOLO model when a
  field finds by YOLO class.
- **Fields:** up to 4 rows: name, Find (Region / YOLO class / Whole picture), the class (from the
  locator model's classes), Format, remove. Add field.
- **Picture:** the camera's picture, **Live** (a new picture every second) or **Held** (Take a
  picture keeps one still). The selected field's region is drawn by dragging on it, and moved or
  resized by its corners.
  - The result of the settings as they are on the card, saved or not, is drawn over it: lines
    found, field boxes green or red, texts, read time. It updates after each change and, while
    Live, with each picture.
  - **Read** lists every line found; clicking one fills the selected field's format with the
    suggested format (or adds a field when none is selected).
- Help icons only on Format (the notation) and Join dots.

### API (new router `ocr`, Supervisor, allowed while the configuration is locked: they save nothing)
- `POST /api/v1/ocr/picture/camera/{camera_id}`: keeps the camera's newest picture as the held
  picture (one per camera, dropped after 10 minutes unused); returns it as JPEG.
- `GET /api/v1/ocr/picture/camera/{camera_id}?held=0|1`: the held picture, or a new one, as JPEG.
- `POST /api/v1/ocr/preview/camera/{camera_id}`: body `{"settings": <the camera's entry>, "held":
  true}`; reads that picture with those settings (validated as a save would; 400 with the same
  message when not valid) and returns `{width, height, ms, lines: [{text, confidence, box}],
  fields: [{name, text, raw, fits, box, area}], result, reason}`.
- `GET /api/v1/ocr/models`: the reader models on disk, and whether the reader can run
  (`{"models": ["small", "tiny"], "new": "tiny", "ready": true, "error": null}`).
- API keys: the preview and picture routes are JWT-only (scope none), like the other setup routes.

## 7. Engine, models and device

- One engine per (model, device), created on first use and shared by every text reader; one call
  at a time (a lock), the switches passed on every call. A camera's settings (character limit,
  Join dots, orientation step) apply per call.
- **Models:** `small` is inside the wheel. `tiny` and `medium` are fetched from ModelScope by
  `python -m app.engines.ocr_models --fetch` into `OCR_MODEL_DIR` (new setting, default
  `model_store/ocr`); the Docker build runs it into the image, and the README's Windows install
  steps run it once, so a server never downloads at run time. A model not on disk is not offered,
  and a camera saved with one is READER NOT READY. Part 2's trained models go in the same folder.
- `GET /api/v1/ocr/models` also names the model a new reader gets (`"new": "tiny"`, or `"small"`
  when *tiny* is not on disk).
- **Device:** `INFERENCE_DEVICE`, as for YOLO: on Windows with DirectML the reader's calls hold the
  same lock as YOLO's DirectML calls (two DirectML sessions running at once crash the process); on
  the `nvidia` image, CUDA; otherwise the CPU. A GPU session that cannot start falls back to the
  CPU, with a log line.
- The engine's memory is measured in the first build task.

## 8. Errors

| What | Then |
|---|---|
| RapidOCR missing, a model file missing or broken | Video `READER NOT READY`; alarm `camera.model_unavailable` (its message says the text reader); triggered products reject `reader_error`; the setup preview shows the message. |
| A read fails (an exception) | Logged (throttled); continuous: that frame is skipped; triggered: `reader_error`. |
| A read takes longer than 10 s | `reader_error` (the trigger stops waiting; the reader finishes and is free again). |
| The locator model is missing or cannot run | `yolo` fields: `reader_error` for the picture; `READER NOT READY` on the video. |
| No picture for a trigger | `no_picture`, as now. |
| The camera is not on a running line | Not read, as for other cameras. |

## 9. Not in Part 1

Training (Part 2); a date range check; the text compared with the batch or a product's value;
reading on a wire line crossing; Arabic and other scripts; more than 4 fields; image processing
other than Join dots.

## 10. Testing

- **Unit:** the format notation (fit, look-alike fixes by position, spaces, case, length, allowed
  characters, suggestion); field matching; settings validation and kept keys; product results with
  the text reasons; values and record details.
- **Engine:** the real RapidOCR (`small`) on a few pictures the tests draw with OpenCV (printed
  text, the character limit, Join dots, the per-call switches); everything else uses a fake engine.
- **Line:** continuous reads shown only; own and join products from triggers (good, `text_wrong`,
  `no_text`, `reader_error`, `no_picture`); records, export and values.
- **API:** preview, held picture, models; Supervisor; allowed while locked; JWT-only.
- **Dashboard:** node tests for the card (build, save body, suggested format, region dragging
  maths) and the Latest text reads panel.
- **Live check** on a scratch copy (never the real `data/`), with the test streamer's picture
  channel (8557) showing printed and dot-matrix dates: setup by dragging a region, Read and the
  suggested format, a triggered own product and a joined one, the video overlay and corner
  picture, the panel; 1280 and 375 px wide; CPU and DirectML. The owner's real product pictures,
  when they come, are checked the same way.
