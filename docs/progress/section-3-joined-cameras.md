# Section 3: Several vision cameras: own station or joined product result

Read [README.md](README.md) first: it has the goal, the owner's decisions, the project map and the rules.

**Depends on Section 2** (the camera cards and the `station` field). Start from the branch Section 2 pushed; it is in the table in README.md.

## Status

**Done** on branch `claude/section-3-joined-cameras-mg2wnm` (started from `main` after Section 2 was merged).

- **Settings** (`line_config.py`): `join_offset_ms` (-60000 to 60000, default 0), `join_window_ms` (50 to 10000, default 500) and `join_missing` (`reject` / `ignore`, default `ignore`) are stored for a `station: "join"` camera only (`_join_settings`), and are in `CAMERA_KEPT_KEYS` (`JOIN_KEYS`). Leaving `join` drops them; the counting camera drops `station` and the three keys. A QR camera's `station` is ignored. Without a camera marked Counting, the first vision camera that does **not** join becomes the counting camera; when every vision camera joins, the save is refused ("Camera 1 joins the product result of the counting camera, but the line has no counting camera…"). `join_settings(cam)` gives `(offset ms, window ms, missing)` with the defaults, so v8 files without the keys load (no upgrade step; `SCHEMA_VERSION` stays 8). `joined_cameras(cameras)` lists them in card order.
- **Verdict**: `REASON_STATION_NO_RESULT = "station_no_result"` (in `REJECT_REASONS`, code 5 in `REJECT_REASON_CODES` through the constant), `CODE_REASONS`. `product_result(vision_reject, read, checks, stations, missing, counting_camera_id)` returns `(reject, reason, reject_camera_id)`; `product_verdict(...)` takes the same `stations` / `missing` keywords and still returns `(reject, reason)`, so old callers are unchanged. Order: counting camera, joined cameras in card order (`vision_class`), code (`code_not_in_list`, `code_in_reject_list`, `no_code`), joined camera with no result and `missing == "reject"` (`station_no_result`). `reject_camera_id` is the counting camera, the joined camera, the code camera (the read's camera, or the first check with no-read Reject) or the joined camera that saw nothing.
- **Runtime** (`line_service.py`): `JoinedStation` and `ProductAssembly` (own matching, not `SyncPairer`: `SyncPairer.add_*` expires with the item's own time, which expires products early once times are shifted by the travel offset). The counting counter's `event_sink` is `_on_crossing` when Sync is on **or** the line has joined cameras; each joined camera's counter gets `_joined_sink(cid)` and counts nothing. A product waits for its code (Sync) and for each joined camera until it answers or `t_c + offset + window` passes (finished early when all answered; an upstream camera whose window ends before `t_c` is already decided). A joined crossing matches the closest product with `|t_j - offset - t_c| <= window`; an unclaimed one waits until `t_j - offset + window`, then counts as `unmatched`. A product keeps the station settings it crossed with. One timer (`_schedule_expiry`) for the earliest of the pairer's and the assembly's deadlines. `_process` was split into `_finish_product(crossing, kind, read, product)`: lines without joined cameras go through exactly the old code path (same fields, `delay_from_crossing`, `qr_stats`).
- **Event**: payload `stations` and `reject_camera_id`; PLC event `reject_camera_id` and `joined_cameras`; `delay_from_crossing=True` on every product of a line with joined cameras. Lines without joined cameras get none of these keys. `card_triggers.camera_matches`: a card naming a joined camera fires for the products it takes part in (`joined_cameras`).
- **Messages**: `MESSAGE_FIELDS` gains `stations` and `reject_camera_id` (two single-key fields: a multi-key field name must never be a message key, `test_a_message_holds_the_fields_its_card_picked`); `send_actions.FIELD_LABELS` and the dashboard's `_sendMessageFields` list them. Placeholder `{reject_camera}` (`TEMPLATE_FIELDS`, `template_values`, `_sendPlaceholders`, `sendTemplateValues`): the rejecting camera's name, else its id; without joined cameras the vision camera when the reason is `vision_class`.
- **Warnings** (`line_warnings(line, models, products_per_minute=0)`): one warning per reject card shorter than the latest result time, the max of the Sync window (with a code check) and every joined `offset + window`; the Sync-window text is unchanged, the joined one names the card and the camera ("Camera 2 ('Side camera') joins the product result 400 ms after…"). With the live rate (`routes/v1/lines.py _view` passes the running line's `products_per_minute`), a joined camera whose `2 × window` is more than the gap between products is warned about.
- **UI** (`assets/production_lines.js`): Job fold of a joined card: **Travel time from the counting camera (ms)** (negative = before), **Match window (ms)**, **When no result** (Ignore / Reject), the explanation, the latest result time and the 2 × window rule (`syncJoin`), and a "Fires late" box naming the line's reject PLC actions whose travel delay is too short. `cardEntry` sends the three keys for join cards. Line dashboard strip: a joined tile shows "Joined +400 ms" (whole settings in its tooltip) and Matched / Unmatched / Rejects / No result (`station_stats` in the summary row, with `join_offset_ms`, `join_window_ms`, `join_missing`; a joined row has no `counts`).
- **Docs**: `README.md` (Job fold text, new "Several cameras on one product" with the top + side camera example, strip text, placeholders, reason code 5, API fields), `PROJECT_DESCRIPTION.md`, `FILE_TREE.md` (also added Section 2's missing `test_camera_cards.py`).
- **Tests**: `tests/test_joined_cameras.py`, 33 tests: the verdict table and reason order, the new reason/placeholder/fields, settings defaults and validation, the all-join refusal, kept keys, v8 settings without the keys, a line without joined cameras unchanged, downstream +400 ms reject (one message, one PLC event, `reject_camera_id`, the joined counter counts nothing, cards naming the joined camera), upstream -300 ms, no result with reject / ignore (PLC reason code 5), two products 1 s apart with two joined cameras and 300 ms windows, unmatched crossings and the summary row, joined + Sync + code check, own station unchanged, settings changed while a product waits, message fields and `{reject_camera}`, the travel-delay and match-window warnings, the API, Line 1. No existing test was changed.
- **Checks**: `pytest` 749 passed, `ruff check .` clean. UI checked with Playwright against a server run from a temporary copy with four fake MJPEG network cameras (Top counting, Side joined +400/300/reject, Under joined -200/150, Own station) and a 500 ms reject gate: join fields only on joined cards, the "Fires late" box on the side card only, own → joined shows the fields, travel time changed to -250 and When no result to Reject, saved, reloaded, kept; strip tiles; no horizontal scroll at 375 px; no page errors. Screenshots: [`screenshots/section-3/`](screenshots/section-3/) (Line setup, the joined card and the Line dashboard at 1440 and 375 px).

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
   - Update the docstring table. Add `station_no_result` to `REJECT_REASONS` with a `REASON_*` constant, beside `REASON_VISION` and the others. `REJECT_REASON_CODES` (Section 1) already maps `"station_no_result"` to 5; use the new constant there.
4. **The event** (`finish_crossing` gets the extra fields through `fields` / `plc_fields`):
   - the payload gains `stations: [{camera_id, camera_name, result, class_name, confidence}]` and `reject_camera_id`;
   - the PLC event gains `reject_camera_id`;
   - `delay_from_crossing=True` whenever the line has joined cameras.

   Add `stations` to `MESSAGE_FIELDS` in `send_dispatcher_service.py`. Section 1 added text templates, so also add a placeholder (for example `{reject_camera}`): it goes into `TEMPLATE_FIELDS` in `line_config.py` (`parse_template` refuses a name that is not there), gets its value in `send_dispatcher_service.template_values()`, and is added to the dashboard's fallback list `_sendPlaceholders` and to `sendTemplateValues()` in `dashboard.html`. The PLC value source needs no change: `REJECT_REASON_CODES` already gives `station_no_result` code 5, and the PLC dialog's hint already says so. The `class_index` source (`PLCDispatcherService._class_index`) looks up `detected_classes[0]` in the lists of the PLC event's `camera_id`, so it writes the counting camera's class; an event that carries its own `class_index` (for example through `plc_fields`) is written as it is (`write_value`).
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
- Lines without joined cameras behave exactly as before: run the whole suite, especially `test_reader_actions.py`, `test_send_cards.py`, `test_qr_trigger.py` and `test_messages_and_plc_values.py` (703 tests pass after Section 1).

## Notes for the next section

- **Start from** `claude/section-3-joined-cameras-mg2wnm` (or `main` once it is merged; see README.md).
- **The exact shape of `stations`** (payload key of a product's `WIRELINE_OBJECT_CROSSED` event, built by `LineRuntime._station_rows`). Present **only** on lines with at least one joined camera, one entry per joined camera in card order, never the counting camera (its result is the event's own `vision_result`, `class_name`, `confidence`):

  ```json
  "stations": [
    {"camera_id": "ipcam-599f59e0", "camera_name": "Side camera", "result": "reject",
     "class_name": "scratch", "confidence": 0.8731, "travel_ms": 402},
    {"camera_id": "ipcam-d8e442b4", "camera_name": null, "result": "no_result",
     "class_name": null, "confidence": null, "travel_ms": null}
  ],
  "reject_camera_id": "ipcam-599f59e0"
  ```

  - `result`: `"good"`, `"reject"` or `"no_result"` (saw nothing in its window).
  - `camera_name`: the connected driver's name (`app_state.cameras`), `null` when the camera is not connected.
  - `confidence`: rounded to 4 places; `class_name`, `confidence`, `travel_ms` are `null` for `no_result`.
  - `travel_ms`: measured time from the counting camera's crossing to this camera's (negative: before it), whole ms.
  - `reject_camera_id`: the camera whose result rejected the product (see Status), `null` for a good product. Also on the PLC event, with `joined_cameras: [camera_id, …]`.
  - `reject_reason` may now be `"station_no_result"`.
- **Records (Section 4)**: one product = one `finish_crossing` call, so one record per product as before; take `stations` and `reject_camera_id` from the payload into `details` when present. A joined camera's own counter stays at 0; its matched / unmatched / rejects / no_result are `runtime.assembly.stats(camera_id)` (reset with the line's counts, not persisted).
- **Where things are**: `line_config.join_settings`, `joined_cameras`, `product_result`; `line_service.ProductAssembly`, `LineRuntime._handle_crossing`, `_handle_joined_crossing`, `_finish_product`, `_station_rows`; summary rows' `station_stats`.
- **Known limits**: the match uses the time a frame's result reached the counter (inference latency differs per camera; the window absorbs it). A product finishes when its last camera answers, so two products can finish out of crossing order when one waits for a missing camera. Section 2 lines that already had `station: "join"` (shown then as "comes with the next update") now really join, with the defaults (0 ms, 500 ms, ignore).
- **Found, not fixed** (outside this section): `PLCDispatcherService._class_index` calls `runtime.counting_camera_id()`, but it is a property; it raises when the event's camera is not on the line (for example Line 1 fed by a free camera). Suggested as a separate task.
- **Test count** after this section: 749.
