# Section 3: Several vision cameras: own station or joined product result

Read [README.md](README.md) first: it has the goal, the owner's decisions, the project map and the rules.

**Depends on Section 2** (the camera cards and the `station` field). Start from the branch Section 2 pushed; it is in the table in README.md.

## Status

Not started.

## Goal (owner's decision)

Every vision camera that is not the counting camera chooses, on its card:

- **Own station** (default; what the second camera does today): it counts on its own and fires only the PLC cards and send cards that name it. Its products do not add to the line totals.
- **Joins the product result:** it looks at the same products as the counting camera, before or after it on the belt.
  - Its crossings are **matched to the counting camera's products** by travel time.
  - The product gets **one** result: reject when the counting camera, **any joined camera** or the code check rejects it.
  - It is counted once and sends one event.
  - A joined camera that sees nothing for a product in its window does what its **When no result** setting says: **Reject** (reason `station_no_result`) or **Ignore**.

## How it works today (what to build on)

- `CountingService.process_frame` turns tracker events into *crossings* (`_crossing()`, a dict with `track_id`, `class_name`, `is_defect`, `confidence`, `camera_id`, `time`, `crossed_at` (monotonic) and so on). It calls `self.event_sink(crossing)` when a sink is set, else `finish_crossing(crossing)`.
- `LineRuntime` (`app/services/line_service.py`):
  - With **Sync** on, the counting camera's counter has `event_sink = self._on_crossing`. The crossing goes through `SyncPairer` (`add_crossing` / `add_read` / `expire` / `next_deadline`, with key functions so a triggered QR capture pairs only with its own product's `track_id`).
  - When a pair is ready or the window ends, `_process()` decides with `product_verdict(vision_reject, read, code_checks)` (`line_config.py`) and calls `counter.finish_crossing(crossing, reject=, reason=, fields=, plc_fields=, delay_from_crossing=)` **once**.
  - `_schedule_expiry()` keeps one `loop.call_later` timer for the next deadline.
- **Second vision cameras** have their own `CountingService` in `aux_counters` (created in `configure()`); `camera_id` is set, so `plc_event["counting_camera"] = False`, and `card_triggers.camera_matches` fires only cards naming that camera.
- **PLC timing:** `delay_from_crossing=True` makes a reject card's travel delay run from `crossed_at`, not from when the result was decided (`PLCDispatcherService._dispatch`).
- `line_warnings()` already warns when a reject card's travel delay is shorter than the Sync window.

## What to build

1. **Settings** (`normalize_camera` / `normalize_line` in `line_config.py`; Section 2 added `station`). Add for `station == "join"`:
   - `join_offset_ms`: the travel time from the counting camera to this camera, -60000 to 60000. Negative = this camera is **before** the counting camera.
   - `join_window_ms`: 50 to 10000, default 500. How far from the expected time a crossing still matches.
   - `join_missing`: `"reject"` or `"ignore"` (default `"ignore"`).

   Validation:
   - a `join` camera needs a counting camera on the line;
   - the counting camera cannot be `join`;
   - a QR-only camera has no `station`.

   Add the three keys to `CAMERA_KEPT_KEYS`.
2. **Runtime** (`LineRuntime.configure` and new methods; consider a small class `ProductAssembly` in `line_service.py` if `_process` gets crowded):
   - A joined camera's counter gets `event_sink = lambda c: self._on_joined_crossing(camera_id, c)`. It does **not** count or send events by itself.
   - The counting camera's counter has the line's sink when Sync is on **or** the line has joined cameras.
   - **One pending product per counting-camera crossing.** It waits for the code (when Sync is on) and for each joined camera. Matching a joined crossing at time `t_j` to a product crossed at `t_c` means `|t_j - offset_j - t_c| <= window_j`. Reuse `SyncPairer`: one per joined camera, adding the joined camera's times shifted by `-offset_j`.
   - The product is finished when every source has answered **or** its deadline passes: `t_c + max(sync window, offset_j + window_j for each joined camera)`, and never before `t_c`.
   - A joined crossing that matches no product within its window is dropped. Count it in `qr_stats`-like station stats, `station_unmatched`, shown on the Line dashboard.
   - Keep using one timer (`_schedule_expiry`) for the earliest deadline across all pending products.
3. **The verdict** (`product_verdict` in `line_config.py`): add `stations` = `{camera_id: {"is_defect", "class_name", "confidence"} or None}` and the join rules.
   - **Reject** when the counting camera rejects, any joined camera rejects (reason `vision_class`, plus `reject_camera_id`), a joined camera has no result and its `join_missing` is `"reject"` (reason `station_no_result`), or the code check rejects (as today).
   - **Order of reasons:** vision of the counting camera, then joined cameras in card order, then code, then missing.
   - Update the docstring table. Add `station_no_result` to `REJECT_REASONS`.
4. **The event** (`finish_crossing` gets the extra fields through `fields` / `plc_fields`):
   - the payload gains `stations: [{camera_id, camera_name, result, class_name, confidence}]` and `reject_camera_id`;
   - the PLC event gains `reject_camera_id`;
   - `delay_from_crossing=True` whenever the line has joined cameras.

   Add `stations` to `MESSAGE_FIELDS` in `send_dispatcher_service.py`, and to the text template placeholders if Section 1 added them (for example `{reject_camera}`). Add `station_no_result` to the reject reason codes of the PLC value source (Section 1 P2: code 5).
5. **Warnings** (`line_warnings`): a reject card whose travel delay is shorter than the latest result time (`max(offset_j + window_j)`, and the Sync window) gets a warning naming the card and the camera. Also warn when two joined cameras' windows overlap the next product. As a rule of thumb, products must be further apart than `2 × window`; say so in the card's help text.
6. **Line setup UI** (the camera card from Section 2, Job section): when "Joins the product result" is chosen, show:
   - **Travel time from the counting camera (ms)**, with a note that a negative value means "before the counting camera";
   - **Match window (ms)**;
   - **When no result**: Reject / Ignore;
   - a short explanation and the reject-delay warning.
7. **Line dashboard:** in the camera strip, a joined camera shows "joined" and its station stats (matched / unmatched / rejects).
8. **Docs:** in `README.md`, a short part under Production lines: "Several cameras on one product" with an example (top camera + side camera 400 ms later).

## Tests to add (`tests/test_joined_cameras.py`)

Use the fake tracker / counter helpers from `tests/test_reader_actions.py` and `tests/test_send_cards.py` (`_CrossingTracker`, `_cross`, `_with_loop`, `lines`, `listeners`).
- A downstream joined camera (+400 ms) rejects → the product is rejected once, with `reject_camera_id`, **one** message and one PLC event.
- An upstream joined camera (-300 ms, it crosses first) is matched the same way.
- A joined camera with no crossing, `join_missing: reject` → reject `station_no_result`; with `ignore` → good.
- Two products 1 s apart with a 300 ms window each get their own station results (no mix-up).
- Joined + Sync with a code check: one result from all three.
- An own-station camera still counts on its own and fires only cards that name it (the existing behavior stays).
- The travel-delay warning is shown.
- The v8 settings load without the new keys (defaults).

## Acceptance

- `pytest` and `ruff check .` are green.
- Lines without joined cameras behave exactly as before: run the whole suite, especially `test_reader_actions.py`, `test_send_cards.py` and `test_qr_trigger.py`.

## Notes for the next section

(Fill in when done: branch, commits, the exact shape of `stations` that Section 4 stores in `details`.)
