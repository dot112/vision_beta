# Code reader image processing

> **Removed on 2026-10-25** at the owner's request: zxing-cpp already binarizes, inverts and turns the picture while it reads, so this stage only made reading slower. What stays: the corner picture (now always cut out of the camera's picture) and the CODE READER banner. See `tests/test_code_processing_removed.py`.

Date: 2026-10-25. Branch `claude/code-processing` (from `main` at `a2ff0b2`).

The owner wants the code reader to process the picture before it looks for codes
(perspective, rotation, scale, grayscale, denoise, contrast, sharpen, binarization), with
one switch to turn that stage off. The video keeps showing the camera's own picture; the
processing happens only on the copy that is read. After a read, the code is cropped with a
margin and the cropped, processed picture of it is shown in the top-left corner of the
camera's video. A live preview in Line setup shows what the steps do while they are set.
The banner on the reader's video says "CODE READER" instead of "QR READER", since it reads
barcodes too.

## Owner's decisions (2026-10-25)

- **Steps:** perspective (rectification), rotation, scale, grayscale, denoise, contrast,
  sharpen, binarization (with invert), all with a live picture of the result.
- **Switch:** in Line setup only, beside the reader's other settings (so the configuration
  lock covers it). No switch on the Line dashboard.
- **No read in the processed picture:** the original picture is read too, so reads are never
  worse than today.
- **Corner picture:** stays until the next read; hidden while the line is stopped.
- **Rectification:** both: the whole picture's perspective from four corners dragged on the
  preview, and each code found but not decoded is straightened automatically and read again.
- **Rotation:** any angle, -180° to 180°.
- Presented in the design and not objected to: other "QR" labels in Line setup ("QR hold time",
  "QR / barcode reader") stay as they are; the corner picture shows the plain crop when
  processing is off.

## 1. The steps

They run in this fixed order, each with its own on/off. All are off for a new camera, and a
camera entry without `code_processing` reads exactly as today.

| # | Step | Setting | What it does |
|---|------|---------|--------------|
| 1 | Perspective | four corners, fractions 0–1 of the picture | the area inside the corners becomes a flat rectangle |
| 2 | Rotate | angle -180 to 180 (° clockwise, as the Cameras page's rotation) | turns the picture; the canvas grows to hold all of it |
| 3 | Scale | factor 0.25 to 4 | resizes the picture |
| 4 | Grayscale | | drops the colour |
| 5 | Denoise | strength 1–5 | median filter, kernel 2·strength+1 (3 to 11 px) |
| 6 | Contrast | strength 1–10 | CLAHE (local contrast), clip limit = strength, 8 × 8 tiles |
| 7 | Sharpen | strength 1–10 | unsharp mask, amount 0.3·strength, blur σ 2 |
| 8 | Binarize | Automatic, Adaptive or Fixed level; Invert | black and white only: Otsu; adaptive Gaussian (block size 3–255 px, odd, C 5); or a fixed level 0–255. Invert swaps black and white |

- The picture is the one the reader gets today: after the camera's own size, rotation, flips
  and ROI (Cameras page).
- **Steps 1–3 are one warp.** They make one 3 × 3 matrix (perspective, then rotation, then
  scale) and the picture is resampled once (cubic when it grows, area when it shrinks; the
  border is filled with the edge's own pixels). The matrix's inverse maps every code's
  outline back to the camera picture, so outlines on the video, the captured pictures and the
  records stay where the code is.
- The scaled picture's long side is at most 4096 px: a larger factor is reduced to fit.
- **Corners:** top-left, top-right, bottom-right, bottom-left, as fractions of the picture's
  width and height. They must make a convex area of at least 1% of the picture. The flat
  rectangle is as wide as the average of the top and bottom edges and as high as the average
  of the left and right edges (in pixels).
- **Steps 5–8 work on a grey picture:** when any of them is on, the picture is turned grey
  first, whether Grayscale is on or not.
- **Invert** belongs to Binarize and applies only while Binarize is on.
- **Crop margin** (`crop_margin_px`, 0–200, default 16): the margin around a code in its
  corner picture, in pixels of the picture the code was read in.

## 2. Reading

With processing **on** (the switch on and at least one step on), a picture is read like this,
each stage only when the ones before it read nothing:

1. **Processed picture:** the steps make the processed picture, which is read. The code
   library (zxing-cpp) is asked to report the codes it found but could not decode too
   (`return_errors=True`), with their corners. A read here is "processed".
2. **Straightened codes:** each code found but not decoded (at most 3) is cut out of the
   processed picture from its four corners plus the crop margin, warped upright, enlarged so
   its short side is at least 120 px (never more than 4×), and read again. A read here is
   "straightened". Without zxing-cpp (OpenCV fallback) this stage is skipped: OpenCV does not
   report codes it could not decode.
3. **Original picture:** the camera picture is read exactly as today. A read here is "original".
4. **Second pass:** the existing sharpen-and-deblur pass on the original picture, with the
   same time budget as today (40 ms continuously, 150 ms for a single picture).

With processing **off**, stages 1–2 do not run: the reader works exactly as today.

- The code type filter (Line setup's **Code type**) applies at every stage.
- The read time shown on the banner and in the records covers all stages.
- A continuously reading camera only takes a new frame when the last one is done, so a slower
  read means fewer frames read per second, never a queue.
- The decode answer (`BarcodeDecodeResponse`) does not change: its outlines are always in
  camera-picture coordinates. The engine also keeps, for its last decode, the picture the
  codes were read in, the matrix from the camera picture to it, and the stage that read
  ("processed", "straightened", "original", or "none"), for the corner picture and the preview.

## 3. Settings

A camera that reads codes (a code reader, or a vision camera with Read codes on) may carry
`code_processing` in its Line setup entry:

```json
{
  "enabled": false,
  "perspective": {"on": false, "corners": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]},
  "rotate": {"on": false, "angle": 0},
  "scale": {"on": false, "factor": 1.0},
  "grayscale": {"on": false},
  "denoise": {"on": false, "strength": 2},
  "contrast": {"on": false, "strength": 3},
  "sharpen": {"on": false, "strength": 3},
  "binarize": {"on": false, "method": "auto", "level": 128, "block": 31, "invert": false},
  "crop_margin_px": 16
}
```

- **Saving** (`PUT /lines/{id}`, `POST /lines`): a sent `code_processing` is checked and stored
  with every key filled in (missing keys take the defaults above, unknown keys are dropped).
  An even adaptive block size is raised by one. A value out of range answers `422` with
  `Camera N image processing: ...` (e.g. `Camera 2 image processing: the scale factor must be
  0.25 to 4`).
- **Left out:** a client that sends a camera without `code_processing` keeps the saved one
  (`READER_KEPT_KEYS`), as for the other reader settings. A camera that has never had it
  stores nothing and reads as today.
- A camera that stops reading codes (job changed to Counting or Inspection only) drops it.
- It is saved with Line setup, so the configuration lock covers it: no new route changes it.
- No schema upgrade: an old settings file has no `code_processing` and reads as today.

## 4. The corner picture

- **What:** after each picture where a code reads, the reader keeps a small picture of the
  code: cut from the processed picture for a "processed" or "straightened" read, else from the
  camera picture, from its four corners widened by the crop margin, warped upright, long side
  at most 240 px.
  - A 1D code whose outline is thinner than a third of its length is made taller, to half its
    length, centred on the outline, so its bars show.
  - Several codes in one picture: the first known code, else the first code.
- **Where:** the top-left of the camera's video, below its banner, with a 10 px gap (scaled
  with the video like the banner, `overlay_scale`). It fits in 160 × 160 px (scaled), and never
  more than 35% of the video's width or height.
  - A frame of 2 px (scaled): green for a known product, red for an unknown code, as the
    outlines.
  - Under it, a dark strip with the code's text (first 24 characters) and, while processing is
    on and the original picture gave the read, the tag `original`.
  - A vision camera with Read codes on: below its own status bar (the video publisher passes
    that bar's height).
- **How long:** it stays until the next picture where a code reads (a code still in view
  refreshes it every frame). It is hidden while the line is stopped, cleared when the line
  starts (a picture from before a Stop never shows as new), and starts empty after a restart.
- **Triggered readers** (one picture per product, or when an input asks): the same picture on
  the live video, and drawn in the top-left of the captured picture (below its CAPTURE banner)
  that the reads panel shows.
- With processing off it is the plain crop from the camera picture.

## 5. Banner

The bar across the top of a code reader's video says `CODE READER` instead of `QR READER`, in
all three of its states (stopped, waiting for a crossing, reading).

## 6. Preview route

`POST /api/v1/qr/preview/camera/{camera_id}`

Body: `{"processing": {code_processing}, "code_type": "all", "view": "processed" | "original"}`
(`code_type` and `view` optional; defaults `"all"` and `"processed"`).

- Takes the camera's latest picture (as the reader gets it), reads it as the reader would with
  the given, unsaved settings (all four stages, single-picture budget), and answers a JPEG:
  - `view=processed`: the processed picture (the original when processing is off), codes
    outlined in its own coordinates;
  - `view=original`: the camera picture, codes outlined.
  - Scaled down to at most 960 px wide, quality 80.
- Headers: `X-Codes` (how many codes read), `X-Read-By` (`processed`, `straightened`,
  `original` or `none`), `X-Read-Ms` (read time), `Cache-Control: no-store`.
- `409` "Camera {id} is not connected" when it is not connected or gives no picture; `422`
  "Image processing: ..." for bad settings (the save's checks, without the camera number).
- Any signed-in user, like the other `/qr/` routes; API keys need `inspection:trigger` (the
  existing `/api/v1/qr/` rule). It changes nothing and is not audited.
- **Allowed while the configuration is locked:** it is added to the lock's allowed list
  (`LOCK_FREE_ROUTES`, "tests and checks").
- It uses its own decoder under a lock, never the running reader's.

## 7. Line setup

- **New fold "Image processing"** in each camera card whose job reads codes, after **Codes**.
  - **Process the picture before reading** (the switch), with a note: "The video keeps showing
    the camera's own picture; only the reading uses the processed one. When the processed
    picture reads nothing, the original is read too."
  - One row per step: its checkbox and its value, as in section 1. Binarize has a method
    choice; Level shows for Fixed level, Block size for Adaptive; then Invert. Perspective has
    "Drag the four corners on the Original view" and a **Reset corners** link.
  - **Crop margin (px)**.
- **Live preview** below the steps, in the same 4:3 box as the count line picture:
  - a **Processed / Original** switch and a status line: `2 codes · read in the processed
    picture · 23 ms`, `No code read · 31 ms`, or `Camera not connected`;
  - on **Original** with Perspective on: the four corners drawn as an outline with round handles;
    dragging a handle moves that corner (clamped to the picture), and the next refresh shows
    the result;
  - it sends the values as they are typed (unsaved); **Save line setup** saves them.
- **Refresh:** while the fold is open, the card is on the page, Line setup is the shown tab and
  the browser tab is visible: one request at a time, the next at least 250 ms after the last
  answer. A 409 shows `Camera not connected` and tries again every 2 s. It stops when any of
  those stops being true.
- **Configuration lock:** the fields are inside Line setup, so they are disabled while locked
  (existing behaviour); dragging corners does nothing while locked; the preview keeps working.
- **Help icons** (owner's rule: only where the purpose is not plain): the switch ("Processes a
  copy of the picture before codes are read: the video keeps showing the camera's own
  picture.") and Perspective ("Drag the four corners around the label on the Original view:
  that area is made flat and straight before reading.").
- The fold's markup, values and preview live in a new `assets/code_processing.js`
  (`window.CodeProcessing`); `production_lines.js` adds the fold, puts `code_processing` in the
  camera entry it saves, and starts and stops the preview.

## 8. Tests

- **Steps:** each step changes the picture as stated (grey is one channel, binarized has only 0
  and 255, invert swaps them, scale and rotation sizes, the perspective rectangle's size);
  outline points mapped through perspective + 30° + 1.5× and back land within 2 px.
- **Settings:** defaults filled, out-of-range values refused with the camera's number, bad
  corners (not 4, out of 0–1, not convex, too small) refused, even block made odd; kept when a
  client leaves it out; dropped when the camera stops reading codes.
- **Reading:**
  - a 1D code turned 45° does not read in the camera picture (the reader only tries 90° turns)
    but reads with Rotate -45°, its outline mapped back onto the code in the camera picture;
    the same code under a perspective tilt reads with the four corners set around it;
  - steps that ruin the picture (Fixed level 255): the original still reads, "original";
  - straightening: a skewed code outline gives an upright crop of the expected size; a code
    reported with an error is read from its straightened crop (the code library's answer
    stubbed);
  - processing off: the same codes as today, and the library is not asked for errors.
- **Corner picture:** kept after a read, drawn in the top-left below the banner (pixel check),
  green or red frame, hidden while stopped, drawn on a triggered capture's picture, the
  `original` tag; below a vision camera's own bar.
- **Banner:** `CODE READER` in all three states.
- **Preview route:** a JPEG with the three headers; the processed view is twice as large with
  Scale 2 (before the 960 px cap); 409 when not connected; 422 for bad settings; works while
  locked; the route inventory test classifies it.
- **Dashboard:** `node --check assets/code_processing.js`; the fold shows for reader jobs only;
  the saved entry carries `code_processing` from the fields; the help tips test knows the two
  new help texts and the new file.
- **Live check** on the scratch server with a fake camera showing a tilted, low-contrast code:
  the fold's preview in both views, a corner drag, Save; on the Line dashboard the
  `CODE READER` banner and the corner picture; the read time with processing on and off; at
  1440 and 375 px wide; no page errors, no sideways scroll.

## Not in this change

- Processing for `POST /qr/decode` (uploads) and `POST /qr/decode/camera/{id}`: they read as today.
- Choosing the order of the steps, or more steps (morphology, gamma).
- Processing the picture the AI model sees.
- Renaming the other "QR" labels in Line setup.
