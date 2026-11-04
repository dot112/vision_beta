# Pattern check: a camera job that compares each product with trained good samples

Date: 2026-11-03. Status: approved by the owner (2026-11-03); refined by the plan
(`docs/superpowers/plans/2026-11-03-pattern-check.md`: two-level `match` values in formulas, a picture of
another size, grey pictures). Branch `claude/pattern-check` (worktree `.claude/worktrees/pattern-check`), from local
`main` `6f155aa`.

## Why

The owner asked (in their words): "add another camera job mode in a vision system that uses template
matching, edge detection, mask, and add the features needed to make it accurate; check for product reject
and accept / I can train the camera in camera settings by taking a picture, crop and so on / the system
shows the confidence of the compared image and rejects by a threshold".

So: a fourth camera job beside AI vision, Code reader and Text reader, that inspects each product with
classic machine vision instead of an AI model. It is trained on the camera's page in Line setup, gives a
score for each product, and accepts or rejects it by thresholds the user sets.

## Owner's decisions (2026-11-03)

- **What it catches: both.** It finds the reference on each product (right product, right part, in its
  place) and compares it with the good samples (smears, missing print, scratches, marks).
- **Position:** products shift on the belt and turn a little: the search covers the whole search area
  and **±15°** by default.
- **Up to 4 checks per camera:** one main reference finds and lines up the product; up to 3 more areas
  (a cap, a logo, a date box) each with their own reference and thresholds. Good only when every check
  passes.
- **Changeover:** one set of references per camera; another product is trained again, or a saved line
  setup is loaded (Save / Load line setups, on `main` since `6f155aa`) and brings its references with it.
- **Thresholds: score + defect size** per check: a lowest **Match %** and a biggest **defect** (in picture
  pixels; no mm).
- **Colour:** a switch per check, **Grey** (default; shape and print) or **Colour** (a wrong-coloured cap
  rejects too).
- **Training:** one picture, its boxes and mask, then **5–20 more good samples**; the thresholds are
  suggested from them.
- **When it checks:** like the text reader: **One picture per trigger** decides (own or joined station);
  **Continuously** only shows.
- **Speed:** more than 300 products a minute: each picture well under 100 ms on the CPU.
- **Approach A** (below): edge-shape search + comparison with the good samples, OpenCV and numpy only.

### Approaches looked at
- **A. Edge-shape search + good-sample comparison (chosen).** Edge directions are compared (the
  "shape-based matching" industrial vision tools use): little change with light and background; masked
  parts left out. The lined-up picture is compared with the average of the good samples, each pixel
  allowed the variation the samples showed.
- **B. Plain template matching** (correlation of brightness) + the same comparison: simpler, but its score
  drops with uneven light, glare and busy backgrounds; Canny-edge matching breaks when a product is a
  pixel off.
- **C. Keypoint features** (ORB/AKAZE) to line up: any angle and size, but needs texture (plain shapes and
  smooth caps give too few points) and its time varies; not needed for ±15°.

## Measurements (throwaway probe, 2026-11-03, this PC: 8 threads, OpenCV 5.0.0)

Scratchpad `match_probe/probe_fast.py`, not in the repo. A 1280 × 960 picture with a 360 × 240 label
turned up to ±14°, shifted, light × 0.6–1.2 and ± 30 grey levels, camera noise; the defect is a 12 px spot
of missing print and a scratch.

| | Good label | Defect label | Wrong label | Blank label / no product |
|---|---|---|---|---|
| Edge score (raw) | 0.70–0.78 | 0.73–0.81 | 0.45 | 0.03 / 0.00 |
| Whole-label correlation after lining up | 0.97–1.00 | 0.96–0.99 | – | – |
| Biggest spot that differs from 8 good samples | **0 px** | **185–197 px** | – | – |

- Found every time: angle within 0.25°, position within 1.5 px.
- **48–58 ms** per picture for the search (Python, untuned, whole picture, ±15°), **about 5 ms** for the
  comparison; the edge model of the reference is built in 0.2 s.
- The whole-label correlation does not tell the defect apart (0.99 against 0.98); the biggest spot does
  (0 against about 190). This is why each check has a defect size as well as a score.
- Raw edge scores of good products are 0.70–0.80, not 1.0 (the picture's interpolation and noise): the
  Match % shown is scaled by the good samples' typical score (section 4).

## 1. New and changed units

| File | Does |
|---|---|
| `app/engines/pattern_find.py` (new) | The edge model of a reference (per pyramid level and angle) and the coarse-to-fine search: position, angle and edge score. No runtime imports. |
| `app/engines/pattern_check.py` (new) | Checking one picture with a reference set and a camera's settings: find, line up, each check's score and defects, the result. No thread, no line. |
| `app/engines/pattern_training.py` (new) | Building a reference set from a draft: line up the samples, the average good picture and its spread, typical scores, suggested thresholds. |
| `app/services/match_references.py` (new) | The files: reference sets (made once, never changed, kept in memory once loaded), drafts, the clean-up. |
| `app/services/match_service.py` (new) | One checker thread per pattern check camera (like the text reader's reader thread): checks with `pattern_check`, keeps the latest result, draws the video overlay. |
| `app/services/held_pictures.py` (new) | The held picture per camera, moved out of `app/routes/v1/ocr.py` so the text reader and the pattern check share it (same 10-minute life). |
| `app/services/line_config.py` | Role `match`: settings validation, kept keys, the new reasons and their numbers; `picture_per_trigger` and the station paths take it. |
| `app/services/line_service.py` | Pattern products (own, joined); the line's latest pattern checks; the alarm. The line save checks the reference set (section 2). |
| `app/services/trigger_inspection.py`, `app/services/vision_service.py` | A trigger's picture goes to the checker (`picture_worker` by job); the frame runner feeds a continuous pattern check; the stream draws its overlay. |
| `app/services/event_values.py`, `app/services/formula.py`, `production_records_service.py`, `records_export.py` | `match.*` values and `match_status`; formulas accept `match`; the record details and the export column. |
| `app/routes/v1/match.py` (new), `app/routes/v1/lines.py` | Setup API (section 7); `GET /lines/{id}/match/recent`. |
| `assets/pattern_check.js` (new), `assets/camera_pages.js`, `assets/production_lines.js`, `dashboard.html`, `assets/dashboard.css` | The Pattern and checks page; the job and its pages; the Latest pattern checks panel and tile. |
| `README.md`, `FILE_TREE.md`, `PROJECT_DESCRIPTION.md`, `API_KEY_QUICKSTART.md` | A "Pattern check" section; the new files; the new routes and values. |

No new package: `opencv-python-headless` and `numpy` are already required. Nothing changes in the Docker
image (reference sets live in `data/`, which is already a volume).

## 2. Settings of a pattern check camera

A camera's entry on a line (`system_state.json`, `lines[].cameras[]`). The settings file version stays 9:
a new role with new keys needs no upgrade step.

```json
{
  "camera_id": "c1",
  "role": "match",
  "qr_hold_ms": 1500,
  "count_mode": "trigger",
  "station": "own",
  "picture_source": "system", "picture_delay_ms": 0, "picture_wait_ms": 1000, "notice_trigger": null,
  "triggers": [1],
  "match": {
    "reference_id": "ref-1a2b3c4d",
    "search_area": [0.10, 0.05, 0.90, 0.95],
    "angle": 15,
    "checks": [
      {"name": "Label", "min_score": 85, "max_defect_px": 40, "colour": false, "sensitivity": "normal"},
      {"name": "Cap",   "min_score": 80, "max_defect_px": 25, "colour": true,  "sensitivity": "normal"}
    ]
  }
}
```

- `count_mode`, `station`, the join settings (`join_offset_ms`, `join_window_ms`, `join_missing`), the
  picture settings and `triggers`: exactly as for a text reader (`_text_reader_reading`): `trigger` decides
  (own by default, or joined); left out (or `track`) checks continuously and only shows.
- `match.reference_id`: the trained reference set (section 3), `ref-` and 8 hex characters. Required: a
  Pattern check camera without one cannot be saved.
- `match.search_area`: `[x1, y1, x2, y2]`, fractions 0–1 of the camera's picture (after its ROI), the
  same rules as a text field's region (`x1 < x2`, `y1 < y2`, at least 0.02 wide and high), or `null`
  (the whole picture; the default).
- `match.angle`: whole degrees 0–45, default 15: the main reference is searched turned from −angle to
  +angle. 0 searches without turning (fastest).
- `match.checks`: 1 to 4, in the order of the reference set's checks; check 1 is the main reference.
  - `name`: a value name (`[A-Za-z][A-Za-z0-9_]{0,31}`, the rule of product field and text field names),
    unique on the camera ignoring case, and not `score` (`match.score` is the main reference's score).
  - `min_score`: whole % 0–100, default 80: the lowest Match %.
  - `max_defect_px`: whole picture pixels 0–1 000 000, default 30: the biggest defect spot allowed.
  - `colour`: `false` (Grey, the default) or `true` (Colour).
  - `sensitivity`: `low`, `normal` (the default) or `high`: how different a pixel must be to count as a
    defect (section 4).
- **`normalize_camera`** checks the shape only (it reads no file). **The line save** also checks that the
  named reference set exists and has as many checks as `match.checks`, and that the search area, when
  set, is at least as wide and high as the reference box; otherwise the save is refused with the
  camera's name and "train its reference on Pattern and checks" (or which box is too small). A setup
  load goes through the same save, so a saved setup whose set was removed by hand says so.
- A client that leaves `match`, `count_mode` or a picture setting out keeps what is saved (kept keys, as
  for the text reader). A role change drops the old role's settings, as now.
- A reference set named by any line's camera, any saved line setup or a draft is never removed (section 3).

## 3. Reference sets and drafts

### Reference set
What Train makes. A folder `data/match-references/<id>/`, written once (into a `.partial` folder, then
renamed) and never changed:

- `set.json`: the id, when and by whom it was made, the camera it was trained on, the training picture's
  size, the reference box (picture pixels of the training picture), each check's box (picture pixels,
  measured from the reference box's centre, so it follows the product), the number of samples, and each
  check's typical score (section 4).
- `mask.png`: the mask over the training picture, white = ignore.
- Per check `n` (1–4): `check-n-mean.png` (the average good picture of that box, lined up, in colour) and
  `check-n-spread.npy` (how much each pixel varied across the samples, per channel of Lab colour,
  float16). Both Grey and Colour are worked out from them, so the switch needs no new training.
- `samples/NN.jpg`: the good samples' whole pictures (JPEG quality 95; at most 20), so a set can be opened
  again in a draft. A set is a few MB at most.

Training again always makes a new set with a new id. A loaded set is kept in memory with its edge models
(about 0.2 s to build), one copy per set, shared by every camera that names it.

**Clean-up** (at start-up and after each line save, each line setup delete and each Train): a set is
removed when no line's camera names it, no saved line setup (`data/line-setups/**.json`, its `line.cameras`)
names it, no draft names it as its last trained set, and it was made more than 24 hours ago.

### Draft
What the page is working on, per camera: `data/match-references/drafts/<camera_id>/` with `draft.json`
(the boxes and search area as fractions of the training picture, the last trained set), `picture.png`
(the training picture), `mask.png` and `samples/NN.jpg`. It survives a page reload and a restart.

- The draft starts when the first box is drawn on a held picture (drawing on a Live picture holds it
  first): that picture becomes the **training picture** and **sample 1**. Boxes and the mask are drawn on
  it (the page shows it while a draft exists).
- **Add as good** adds the held picture as the next sample (at most 20).
- **Edit training** makes a draft from the camera's current set (its training picture, boxes, mask,
  samples). **Start over** removes the draft.
- A draft left unused for 30 days is removed by the clean-up.
- Two browsers on the same camera: the last change wins.

## 4. Checking one picture

`pattern_check` with `pattern_find`:

1. **Size:** a picture whose size is not the training picture's (the camera's resolution or ROI changed)
   is `pattern_error`: "Picture is W x H, trained on w x h: train it again" (plain ASCII: the video's
   bar draws it whole, also on One picture per trigger and on the trigger's picture).
   **Search area:** cut it out of the picture (after the camera's ROI); none set: the whole picture.
2. **Find the main reference by its edges.**
   - Edges: Sobel gradients (3 × 3, on grey 0–255). A reference point is an edge point where the gradient
     is at least 20 and it lies on the edge's centre line (Canny's thin edges, so a product half a pixel
     off still scores the same); a picture point counts where it is at least 10. The reference's edge
     points under the mask and on the box's 2 px rim are left out, at every angle.
   - Score at a place and angle: the mean, over the reference's edge points, of how well the picture's
     edge direction agrees there (the cosine of the angle between the two directions; 0 where the picture
     has no edge). Polarity counts: light-on-dark is not dark-on-light. It is worked out for every place
     at once with two correlations of the unit gradient pictures (`cv2.matchTemplate`, `TM_CCORR`).
   - Coarse to fine: a pyramid down to where the reference's shorter side is about 24 px (at most 4
     levels). At the top every angle from −angle to +angle, in steps that move the reference's far corner
     about 1.5 px; at each finer level only the angles next to the best one, in a window of ±4 px around
     it; at full size the best place and angle.
   - Edges are worked out only inside the windows at the finer levels.
3. **Match %** = the edge score ÷ the check's **typical score** × 100, rounded, at most 100. The typical
   score is the median score of the good samples (section 5), so 100 % means "as good as your good
   samples".
4. **The main reference's Match % below its `min_score`:** the result is reject, reason
   `pattern_not_found`, and the other checks are not done (nothing to line them up by).
5. **Line up:** each check's box is cut out of the picture turned back by the found angle around the
   found place (`cv2.warpAffine`, linear), so it sits as in the training picture. Checks 2–4 then search
   ±6 px and ±1° around where the main reference puts them: their own place and Match %. A check box
   partly outside the picture: the part outside counts as masked; less than half inside: Match 0 %.
6. **Level the light** over the box's unmasked pixels:
   - Grey: brightness and contrast (mean and spread) matched to the average good picture.
   - Colour (Lab): lightness levelled the same way; the two colour channels compared as they are.
   - A grey (one-channel) picture, as industrial mono cameras give, is checked as Grey whatever the
     Colour switch says.
7. **Defects:**
   - A pixel is different when it is further from the average good picture than its tolerance: the
     samples' spread at that pixel × k, and at least a floor (grey levels):

     | Sensitivity | k | Floor |
     |---|---|---|
     | Low | 5 | 35 |
     | Normal | 4 | 25 |
     | High | 3 | 15 |

     In Colour a pixel is different when its lightness or either colour channel is.
   - The tolerance is widened by one pixel around edges (a 3 × 3 maximum), so an edge one pixel off is not
     a defect. Masked pixels and a 2 px border of the box are skipped.
   - Specks are removed (a 3 × 3 opening); touching pixels make a spot. The **biggest defect** is the
     largest spot's area in pixels (the lined-up box has the picture's scale); the 5 largest spots are
     kept for drawing.
8. **Result:** good when every check's Match % is at least its `min_score` and its biggest defect at most
   its `max_defect_px`. Otherwise reject, with the first problem in check order: `pattern_not_found`
   (check 1's Match % too low), `pattern_mismatch` (a check 2–4's Match % too low), `pattern_defect` (a
   spot too big). `pattern_error` when it could not check (section 9).
9. **Out:** `{width, height, ms, found: {x, y, angle, score}, checks: [{name, score, defect_px, spots:
   [box], box: [4 corners], fits}], result, reason}`; checks not done (after `pattern_not_found`) are left
   out.

Speed: the target is **≤ 60 ms** for a 1280 × 960 picture at ±15° on this PC's CPU (measured in the first
build task; tune the angle steps and windows if it is slower). A search area and a smaller picture are
faster. The checker runs on the CPU only: it never waits for the DirectML lock of YOLO and the text reader.

## 5. Training

`pattern_training`, from a draft, with each check's Colour and Sensitivity as they are on the card:

1. **Refused** when: no draft or no reference box; a box under 16 px wide or high; the reference has too
   few edges ("Draw the reference around printing or a shape": fewer than 100 edge points at full size or
   20 at the top level); the disk is below the data safety free-space floor (the storage message).
2. **Line up the samples:** a first edge model from the training picture's reference box finds the
   reference in each sample; each check's box is cut out lined up (as in section 4). A sample where the
   reference is not found (its edge score under 0.8 of the training picture's own score; a label with
   another big word scores about 0.77 of it, good ones 0.95 or more) is named in the refusal.
3. **Average and spread** per check over the lined-up samples (Lab colour), and the **final edge model**
   from the average picture (less noise than one picture).
4. **Typical score** per check:
   - 3 samples or more: each sample scored against a set made from the others (leave one out); the
     median.
   - 1 or 2 samples: the samples turned by −angle/2 and +angle/2 and scored; the median.
5. **Suggested thresholds** per check:
   - 3 samples or more, from the leave-one-out scores: `min_score` = the lowest good Match % − 5,
     kept within 50–95; `max_defect_px` = 1.5 × the biggest good spot + 5, at least 10.
   - Fewer than 3 samples: 80 % and 30 px, and the answer says to add samples.
6. Write the set (section 3) and answer `{reference_id, suggested: [{min_score, max_defect_px}],
   samples: [{scores, defects}], note}`. Train takes a few seconds with 20 samples.

## 6. On the line

### Continuously
The frame runner gives the checker the newest frame whenever it is free (frames in between are skipped).
Each result is drawn on the video. Nothing else: no product, count, record, output card or message, and
not listed in the panel.

### One picture per trigger
The same triggers and picture rules as a triggered inspection station (`trigger_inspection`): a "Take a
picture" input with one of the camera's trigger numbers, `POST /lines/{id}/trigger`,
`/control/trigger/{camera}`, the camera's own hardware trigger (with or without a notice), the software
trigger of an industrial camera. The picture goes to the checker instead of a model. Triggers of one camera
are served one after the other, as now.

- **Own:** each picture is a product of this camera: class `pattern ok`, `pattern not found`,
  `pattern mismatch`, `pattern defect` or `pattern error` (a trigger with no picture is `no picture`, as
  now); good or reject; counted, recorded, and it fires the output cards and send cards that name the
  camera, as an own station's product does.
- **Join:** the result joins the counting camera's product by travel time, as a joined station's result
  does; a product with no result from it follows `join_missing`. A joined reject keeps its own reason and
  this camera as the rejecting camera.
- **New reject reasons** and their numbers for the PLC (`REJECT_REASON_CODES`): `pattern_not_found` 14,
  `pattern_mismatch` 15, `pattern_defect` 16, `pattern_error` 17. Labels: "pattern not found", "pattern
  mismatch", "pattern defect", "pattern check error".
- **Records:** `details.match` = `{"score": <main Match %>, "status": "good" | "not_found" | "mismatch" |
  "defect" | "error", "checks": {<name>: {"score": <%>, "defect_px": <px>}}}` (checks not done are left
  out). The CSV/XLSX export gets a `pattern` column (`Label 97% 0px; Cap 92% 48px`); the Records page
  shows it with the row.
- **Values** for output cards, send cards and only-when rules: `match.score`, `match.<check>.score`,
  `match.<check>.defect`, `match_status`. A check not done is missing, not 0, as with other values.
  `formula.ROOTS` gains `match`, which alone takes a second level (`match.<check>.score`,
  `match.<check>.defect`; other roots keep one); the card dialogs list these values.
- **Latest picture:** the last triggered picture with its drawing, served by the existing
  `GET /lines/{id}/trigger/capture?camera_id=` like a triggered station's.
- **Alarm:** on a running line, a pattern check camera whose reference set is missing or cannot be read
  raises `camera.model_unavailable`, its message naming "(pattern check)"; cleared when it can check
  again. Its triggered products reject with `pattern_error`.

## 7. Live view and setup

### Line dashboard
- **Video:** a **PATTERN CHECK** bar, drawn like the text reader's (`draw_status_bar`): `LINE STOPPED` /
  `NOT ON A LINE` / `NO REFERENCE` / `WAITING FOR A TRIGGER` when that applies; else `GOOD · MATCH 97%` or
  `REJECT · Cap: defect 48 px`, then the time in ms. Over the picture: the found outline turned to the
  product's angle; each check's box green or red with `Cap 92% · 48 px`; the defect spots outlined red.
- **Latest pattern checks** panel (beside Latest text reads, shown when the line has a pattern check
  camera): time, camera, each check's Match % and defect, the product's result and reason. Triggered
  results only. From `GET /lines/{id}/match/recent` (Operator).
- **Strip tile:** "Pattern check", and its last triggered picture, like a triggered station.

### Line setup
- **Job list:** **Pattern check** (`role: "match"`). Its camera page shows Camera · Job · **When to
  check** (the Counting page, titled so for this job: Continuously / One picture per trigger, Own / Join
  and the join settings, picture settings and triggers, the controls of a triggered station) · **Pattern
  and checks** · Image and video. A new Pattern check camera opens at Pattern and checks.
- **Card summary line:** `Camera 2 · IP · One picture per trigger` or `… · Shows pattern`.
- **Save problem** `no_reference` (no trained set) opens Pattern and checks.

### The Pattern and checks page (`assets/pattern_check.js`)

```
Pattern and checks
[Live] [Take a picture]       Draw: (●Reference) (Check) (Search area) (Mask)
┌─────────────── picture, at most 640 px wide ───────────────┐
│  ┌ Search area (dashed) ───────────────────────────┐       │
│  │   ┌ Label (reference) ──────┐   ┌ Cap ┐         │       │
│  │   │  ORANGE  ░░mask░░       │   └─────┘         │       │
│  │   └─────────────────────────┘                   │       │
│  └─────────────────────────────────────────────────┘       │
│ GOOD · Label 97% 0 px · Cap 92% 4 px · 38 ms               │
└────────────────────────────────────────────────────────────┘
Mask brush: (Paint) (Erase)   Size [S M L]
Good samples (8): [1 100%][2 98%][3 99%] … [8 97%] ×      [Add as good]
[Train]  5–20 good samples give better thresholds.   [Edit training] [Start over]
Search: angle ±[15]°
Checks:  Name    Min match   Max defect   Colour   Sensitivity
         Label   [85] %      [40] px      Grey     Normal
         Cap     [80] %      [25] px      Colour   Normal         ×
```

- **Picture:** **Live** (a new picture every second) or held (**Take a picture** keeps one still), as on
  the Text fields page. While a draft exists, the training picture is shown for drawing.
- **Draw:** the selected tool's box is drawn by dragging, moved by dragging inside it and resized by its
  corners (the ROI box's and text regions' handling). **Reference** is check 1; **Check** adds a box (at
  most 3) and a row in the checks table (named `Check2`… until renamed); **Search area** is dashed;
  **Mask** paints (or erases) with a round brush (S / M / L), shown tinted red; the mask is sent as a PNG
  at the training picture's size.
- **Good samples:** thumbnails of the draft's samples, each with its raw score as a % and its × (not
  sample 1), so an odd one can be taken out; **Add as good** sends the held picture and shows where the
  reference was found in it, or "Reference not found in this picture" (under 0.8 of the training
  picture's own edge score, as in Train). The thumbnail's % is that share.
- **Train** builds the set, puts its id into the card and fills each check's Min match and Max defect
  with the suggested values ("suggested from 8 samples"; with fewer than 3, the note to add samples). The
  card shows Unsaved changes; **Save** keeps it. Colour and Sensitivity change the defects found: the
  page says "Train again for new suggested thresholds" after either changes.
- **Result:** after training, every picture (Live or held) shows the result of the card's settings as they
  are, saved or not (`POST /api/v1/match/preview/camera/{id}`), drawn over it; a change of a threshold,
  the angle or the search area previews again (after 300 ms).
- **Help icons** only on Sensitivity and the mask brush.
- **Levels and lock:** Level 2, like Line setup. Live, Take a picture and the preview work while the
  configuration is locked (they save nothing); the draft's changes, Add as good, Train, Edit training
  and Start over are refused while locked, with the lock's message, and their buttons carry `data-lock`.

### API (new router `match`, Supervisor (Level 2), JWT only: API keys cannot call it)

| Route | Does | While locked |
|---|---|---|
| `POST /api/v1/match/picture/camera/{id}` | Keeps the camera's newest picture as the held picture (`held_pictures`, shared with the text reader); returns it as JPEG | allowed |
| `GET /api/v1/match/picture/camera/{id}?held=0\|1` | The held picture, or a new one, as JPEG | allowed |
| `POST /api/v1/match/preview/camera/{id}` | Body `{"settings": <the camera's entry>, "held": true}`; checks that picture with those settings (validated as a save would; 400 with the same message) and returns section 4's result | allowed |
| `GET /api/v1/match/draft/camera/{id}` | The draft: boxes, search area, samples (with where each was found), last trained set; 404 when none | allowed |
| `GET /api/v1/match/draft/camera/{id}/picture`, `…/mask`, `…/samples/{n}` | The training picture (JPEG), the mask (PNG), a sample (JPEG) | allowed |
| `PUT /api/v1/match/draft/camera/{id}` | Body `{"reference": [x1,y1,x2,y2], "checks": [[…], …], "search_area": […] \| null}` (fractions); starts the draft from the held picture when there is none | refused |
| `PUT /api/v1/match/draft/camera/{id}/mask` | The mask, PNG at the training picture's size | refused |
| `POST /api/v1/match/draft/camera/{id}/samples` | Adds the held picture as a good sample; returns where it was found | refused |
| `DELETE /api/v1/match/draft/camera/{id}/samples/{n}` | Removes a sample (not sample 1) | refused |
| `POST /api/v1/match/draft/camera/{id}/train` | Body `{"angle": 15, "checks": [{"colour", "sensitivity"}]}`; section 5 | refused |
| `POST /api/v1/match/draft/camera/{id}/edit` | Body `{"reference_id": …}`: a draft from that set | refused |
| `DELETE /api/v1/match/draft/camera/{id}` | Start over | refused |
| `GET /api/v1/lines/{id}/match/recent` | The line's latest triggered pattern checks (Operator) | allowed |

## 8. Engine and threads

- One checker thread per pattern check camera (`match_service`, like the text reader's reader thread):
  the frame runner (continuous) and the trigger path (`picture_worker`) give it pictures; it keeps the
  last checked frame and result for the overlay and the trigger capture.
- OpenCV's own threads are used inside one check; several cameras check at the same time.
- The 10 s a trigger waits for a model (`TRIGGER_INFERENCE_TIMEOUT_S`) also applies: a check that takes
  longer gives `pattern_error`, and the checker is free again when it finishes.

## 9. Errors

| What | Then |
|---|---|
| No reference set, or a file of it missing or broken | Video `NO REFERENCE`; the alarm; triggered products `pattern_error`; the preview shows the message. |
| A check raises an exception | Logged (at most once a minute per camera); continuous: that frame is skipped; triggered: `pattern_error`. |
| A check takes longer than 10 s | `pattern_error` (the trigger stops waiting). |
| A sample where the reference is not found | Add as good refused: "Reference not found in this picture". |
| A reference with too few edges | Train refused: "Draw the reference around printing or a shape". |
| A box under 16 px; a search area smaller than the reference | Refused, naming the box. |
| The camera's picture size changed after training (resolution or ROI) | `pattern_error` "… trained on w × h: train it again"; the video says it. |
| The disk is below the free-space floor | Train and Add as good refused with the storage message. |
| No picture for a trigger | `no_picture`, as now. |
| The camera is not on a running line | Not checked, as for other cameras. |

## 10. Not in this part

More than 4 checks; references per product (picked by the line's product); finding products on a moving
belt without a trigger; size changes (the camera must stay at a fixed distance) and turns beyond ±45°;
measuring in mm; reference sets in the nightly backup and the second folder (AI models in `model_store/`
are not backed up either); a Software-trigger hint for a continuous industrial pattern check.

## 11. Testing

- **Engine** (`pattern_find`, `pattern_check`, `pattern_training`) on pictures the tests draw with OpenCV:
  a label turned within ±15°, shifted, light × 0.6–1.2 and ± 30 grey levels, noise; a spot and a scratch;
  a wrong label; a blank label; no product. Checked: position within 2 px and angle within 0.5°; Match %
  and biggest defect tell good from bad; the mask hides a date that changes; Colour rejects a
  wrong-coloured cap that Grey passes; a check box partly outside the picture; leave-one-out suggestions;
  the time per picture is measured and printed, and the test only fails over 300 ms (timing tests on a
  busy PC are known to be flaky here).
- **Settings and files:** `normalize_camera` for `match` (limits, names, kept keys, role change); the line
  save refusing a missing set, a wrong check count, a small search area; a set is written once and never
  changed; the clean-up keeps sets named by a camera, a saved line setup or a draft, and removes the
  others after 24 hours; the draft survives a restart.
- **Line:** continuous checks drawn only; own and joined products from triggers (good, each reason,
  `no_picture`); records, export, values, formulas; the alarm.
- **API:** each route; Level 2; the locked refusals; JWT only.
- **Dashboard** (node): the page's build, box and mask maths, what Save sends, the suggestions filled in
  after Train, the "Train again" note; `camera_pages` for the new job (pages, titles, start page, summary,
  `no_reference`).
- **Live check** on a scratch copy (never the real `data/`): a new fake camera (`fake_label_cam.py` in
  `backup/scratch-tools`) that shows a label shifted, turned and lit differently, with a defect every few
  pictures; train through the page (picture, boxes, mask, 8 samples, Train, Save); triggers at 5 a second
  for 60 s through `POST /lines/{id}/trigger`: time per picture, no missed triggers, the right
  rejects; the video overlay, the panel, the records; screenshots at 1280 and 375 px; no page errors.
- **Not tested** on a real line or with the owner's real products; the report says so plainly.
