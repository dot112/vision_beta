# Section 5: PLC inputs, batch number, trigger inspection, reject confirmation

Read [README.md](README.md) first: it has the goal, the owner's decisions, the project map and the rules.

**Depends on Sections 2 and 4** (the camera cards for the UI, and the records that carry the batch). Start from the latest section branch in the table in README.md.

## Status

**In progress** on branch `claude/section-5-plc-inputs` (started from `main` after Section 4 was merged). The branch is local: it has not been pushed yet.

The work follows the plan in [docs/superpowers/plans/2026-10-12-section-5-plc-inputs.md](../superpowers/plans/2026-10-12-section-5-plc-inputs.md): nine tasks, each with its tests first.

| Task | What | State |
|---|---|---|
| 1 | `read()` for every PLC driver | **Done** (`504f4a9`) |
| 2 | Signal cards in the line settings | **Done** (`e2e93a8`) |
| 3 | The polling service | **Done** (`bdb629a`) |
| 4 | What a signal does, and its API | **Done** (`adf6b9c`) |
| 5 | Batch number | **Done** (`9df924a`) |
| 6 | Trigger inspection | **Done** (`d6e33e6`) |
| 7 | Reject confirmation | **Done** (`c45d4ca`) |
| 8 | Dashboard | Not started |
| 9 | End-to-end test, docs, final pass, push and CI | Not started |

So the whole backend is done and tested; the dashboard and the docs are left. Until Task 8, the new settings can only be set through the API.

**Tests:** 1000 pass on the Windows development machine with `bash .superpowers/sdd/2026-10-12-section-5-plc-inputs/pt.sh`, which leaves out 11 tests that fail there before this section too (8 need Linux's fast refused connections, 3 need the `tzdata` package). `ruff check .` is clean. The branch has not run on Linux CI yet.

### To go on (Tasks 8 and 9)

1. `git checkout claude/section-5-plc-inputs`. Read this file, the plan (its Global Constraints and decisions D1 to D16, then Tasks 8 and 9) and "Decisions made while building" below.
2. The plan's progress ledger is in `.superpowers/sdd/2026-10-12-section-5-plc-inputs/progress.md` (not in git; it is on the development machine). It has every task's test run and decision. The same folder has `pt.sh` (the test run above) and `mutcheck.py` (breaks one piece of code on purpose and checks that a test notices).
3. Do Task 8, then Task 9. Ask the owner before pushing.

### What the backend gives the dashboard (for Task 8)

- **Line** (`GET /api/v1/lines/{id}`): `plc_inputs` (the signal cards; missing on a line that never had one), `batch` (only while the line has one) and `status.batch` (text or `null`).
- **Signal card:** `id` (`plcin_` + 8 hex digits; made by the server when left out), `name`, `enabled`, `plc_endpoint_id`, `address`, `data_type` (`auto`, `bool`, `int16`, `uint16`, `int32`, `real`, `string`, `counter`), `poll_ms` (50 to 10000, default 100), `edge` (`rising`, `falling`, `change`), `debounce_ms` (0 to 5000), `action`; `camera_id` for `capture`; `alarm_text` and `alarm_severity` (`info`, `warning`, `critical`) for `alarm`. `counter` is only for `product_sensor`, `string` only for `set_batch`.
- **Actions:** `start_line`, `stop_line`, `run_level`, `reset_counters`, `product_sensor`, `capture`, `set_batch`, `confirm_card`, `alarm`. The edge is used by `start_line`, `stop_line`, `reset_counters`, `product_sensor` (not with `counter`), `capture` and `confirm_card`.
- **Save:** `PUT /api/v1/lines/{id}` with `{"plc_inputs": [...]}`. A bad card is a 422 whose text names the card. At most 32 cards.
- **Status:** `GET /api/v1/lines/{id}/plc-inputs/status` → `{"line_id", "running", "inputs": [{"id", "name", "enabled", "action", "value", "changed_at", "last_read_at", "read_count", "error"}]}`. The times are seconds since 1970; `running` is false when the server is not reading signals.
- **Read now:** `POST /api/v1/lines/{id}/plc-inputs/{input_id}/read`, with `{"input": {…card…}}` for a card that is not saved yet → `{"ok", "value", "message"}`.
- **Batch:** `PUT /api/v1/lines/{id}/batch` with `{"batch": "…"}` (Supervisor; `""` clears it; 64 characters at most) → `{"line_id", "batch"}`.
- **Camera card:** a vision camera has `count_mode` (`track` / `trigger`; sent as `track` it is not saved) and, in trigger mode, `no_product` (`ignore` / `reject` / `good`). A code reader's `qr_trigger` has a fourth choice, `signal`.
- **Trigger:** `POST /api/v1/lines/{id}/trigger` with an optional `{"camera_id"}` → `{"line_id", "results": [...]}`; 409 when the line is stopped or has no camera in trigger mode.
- **PLC action card:** `confirm_input_id` (the id of a signal card with the action `confirm_card`; `""` for none) and `confirm_timeout_ms` (50 to 10000, default 1000). New statuses `confirmed` and `not_confirmed`; `last_result.confirmed` is true or false.
- **Values to keep in step:** reject reason code **6** is `nothing_detected` (`PLC_VALUE_SOURCE_HINTS.reject_reason_code`); the sample message's batch is `"B-1001"` (`sample_payload` and the dashboard's `sendSampleMessage`); the PLC Test button writes batch `1`.
- **Refused when a line is saved** (the dashboard should not offer them): a product sensor on a line with a vision camera; a `capture` signal whose camera is neither a reader on `signal` nor a vision camera in trigger mode; a reader on wire line 1 or 2 when the counting camera is in trigger mode; deleting (or changing the action of) a signal that a PLC action still names as its confirmation.

### Decisions made while building (beyond the plan's D1 to D16)

- **Product sensor cards count for the line whether switched on or not.** A line whose sensor card is switched off stays a sensor line: Sync keeps its setting and a code read is not a product of its own. It counts nothing until the card is on again.
- **Two extras on signal cards:** a line warning for a reader set to `signal` that no switched-on `capture` card names; and a PLC channel a signal reads counts as used, so it is not deleted without a warning.
- **The poller starts with the other PLC services** (`SettingsPersistenceService.restore_on_startup`) and stops first in the server's shutdown.
- **`set_batch` also acts on the first read** (the plan excepted only `alarm`): the batch the PLC holds is the line's batch, also when it changed while the server was down.
- **A read that also drops the PLC link** raises `plc.input_failed` only when it happens twice in a row; the reconnect then waits for the channel's reconnect interval.
- **A start or stop from a signal runs one at a time per line.** A reset signal does nothing when nothing was counted since the last reset. A capture or sensor signal on a stopped line does nothing.
- **A batch that is too long is refused, not cut** (a cut batch is a wrong batch in the records). So is a bit, or text with a control character. A signal card keeps the reason its last action failed until one succeeds, and shows it as its error.
- **Trigger mode:** a trigger is refused while the camera's model is not loaded; two triggers at once on one camera take two pictures; the video of a camera in trigger mode shows no count lines; `/control/trigger` for such a camera writes no detection history row.
- **Reject confirmation:** a fire whose card is deleted or switched off while it waits ends quietly, with no alarm.

## Goal (owner's decision: "Yes, add PLC inputs")

The PLC can **send signals into** the software, per line:

- start / stop the line;
- a product sensor counts products or triggers a picture;
- reset the counters;
- set the batch (work order) number;
- confirm that a reject really happened;
- raise a user-defined alarm.

Vision cameras get a **trigger mode** (one picture per trigger instead of tracking moving products), for indexing machines, rotary tables and parts that stand still.

## What exists today

- **PLC is output-only.**
  - `PLCDriver` (`app/hardware/plc/base.py`) has `connect`, `disconnect`, `set`, `reset`, `pulse`, `write`, `toggle` and `execute_operation`.
  - `PLCDriverFactory.get_driver(endpoint)` pools one driver per endpoint id.
  - The dispatcher serialises operations per endpoint with `PLCDispatcherService._endpoint_locks[ep_id]` (an `asyncio.Lock`), and each driver has its own `_operation_lock`.
- **Read code the drivers already have** (to build `read()` on):
  - **Modbus TCP:** `app/hardware/modbus/client.py`: `read_coils(address, count)`, `read_registers(address, count)` (function 3, holding). Add discrete inputs (function 2) and input registers (function 4). `modbus_driver._parse_modbus_target` refuses 1xxxx/3xxxx addresses **for writes** (`_read_only_message`); reading must allow them. `:INT/:DINT/:REAL` suffixes and `word_order` exist for writes (`_encode_register_value`); add the decode.
  - **Modbus RTU:** `app/hardware/modbus/rtu_client.py` (the same request path, serial or TCP gateway).
  - **S7:** `s7_driver.read_bit(address)`, `_read_byte(parsed)`. Extend to byte, word, int, dint and real.
  - **EtherNet/IP:** `ethernet_ip_driver._read_tag(tag)` → `(type_code, data)`, `_read_bool`. Decode the type codes.
  - **OPC UA:** `self._node(address).read_value()` (see `toggle`).
  - **MELSEC SLMP:** `melsec_driver.read_bit`. Add a word read (batch read command).
  - **Omron FINS:** `fins_driver.read_bit`. Add a word read (memory area read).
  - **Generic TCP:** no read: answer "not supported".
- **PLC card "Wait for ACK"** was removed in Section 1 (P1), because it did nothing. The dialog has no ACK select and new cards carry no `ack_mode`; an old saved `ack_mode` stays in the file, does nothing and reports `sent` (`test_an_old_wait_for_ack_card_reports_sent`).
- **`/api/v1/control/trigger/{camera_id}`** (`app/routes/v1/control.py`) runs one detection and returns it. It counts nothing and fires no card.
- **Line start/stop:** `set_line_running(line_id, running, actor)` in `app/routes/v1/lines.py` (the actor needs `username`, `role` and `clearance_level`; Sparkplug passes a `CommandActor`, see `app/services/sparkplug_service.py`). Counter reset: `reset_line_counters(runtime, actor)` in `app/routes/v1/counting.py`.
- **Triggered QR picture:** `QRReaderPipeline.get_worker(camera_id).request_capture(track_id=, class_name=, wire_line=, delay=)` (`app/services/qr_service.py`).
- **Products without a vision camera:** `CountingService.count_product(name, reject)`. With Sync, a crossing goes through `LineRuntime._on_crossing(crossing)` and is paired with its code.

## What to build

1. **`PLCDriver.read(address, data_type="auto") -> (ok, value, message)`** in `base.py`:
   - The default returns `(False, None, "Reading is not supported by …")`.
   - Implement it for every driver listed above.
   - `data_type`: `bool`, `int16`, `uint16`, `int32`, `real`, `string` (OPC UA and EtherNet/IP only); `auto` uses the address's own suffix or the protocol's natural type.
   - Run under the driver's `_operation_lock`.
2. **PLC signal cards** (per line, saved as `plc_inputs` in the line entry; for Line 1, decide top level vs. entry and document it; validated by a new `normalize_plc_input` in `line_config.py`; extend `_upgrade_to_v8` only if old files need a default; `[]` when missing is enough):
   - **Fields:** `id`, `name`, `enabled`, `plc_endpoint_id`, `address`, `data_type`, `poll_ms` (50 to 10000, default 100), `edge` (`rising` / `falling` / `change`), `debounce_ms` (0 to 5000), `action`, and per action: `camera_id`, `card_id`, `alarm_text`, `alarm_severity`.
   - **Actions:**
     - `start_line`, `stop_line` (on the edge); `run_level` (the line runs while the value is true; act only on a change).
     - `reset_counters`.
     - `product_sensor`: each edge is one product. With `data_type` = **PLC counter** (`counter`), the *increase* of a counter register is the number of products, which handles sensors faster than the poll and the counter wrapping at 65536.
       - On a line **without** a vision camera: if a code reader with Sync is on the line, make a crossing (`{"track_id": None, "class_name": "product", "is_defect": False, …}`, built like `CountingService._crossing`) and hand it to `LineRuntime._on_crossing`, so it pairs with its code and gets one result.
       - Otherwise `count_product("product", reject=False)` and send its event like `_emit_code_product` does.
     - `capture`: a code reader (`request_capture`), or a vision camera in trigger mode (below).
     - `set_batch`: the value becomes the line's batch.
     - `confirm_card`: confirms a PLC action card (below).
     - `alarm`: raise `plc.input_alarm` (source `plc_input:<id>`, the card's own text and severity, `details.line_id`) while the value is true; clear it when false. Alarm cards can react to it.
3. **`app/services/plc_input_service.py` (`PLCInputService`):**
   - one asyncio task per endpoint, polling all that endpoint's inputs at the smallest `poll_ms` among them;
   - every read takes `PLCDispatcherService._endpoint_locks[ep_id]`, so reads never interleave with card writes on one connection;
   - edge detection with debounce; actions run as tasks on the main loop;
   - status per input: last value, last change time, last error, read count;
   - a read error raises `plc.input_failed` (add it to `ALARM_CATALOG`: scope `line`, `warning`), cleared on the next good read. A lost link uses the PLC connect alarm that already exists;
   - started and stopped in `main.py`'s lifespan; reloaded when a line is saved (`apply_line` in `routes/v1/lines.py`) or a channel changes.
   - **Audit:** start/stop/reset/batch actions go to the audit log as user `plc:<channel name>`, role `PLC` (like Sparkplug's `sparkplug:<name>`).
4. **API:**
   - `GET /api/v1/lines/{id}/plc-inputs/status`;
   - `POST /api/v1/lines/{id}/plc-inputs/{input_id}/read` (**Read now**, Supervisor);
   - the inputs are saved with the line (`PUT /lines/{id}` with `plc_inputs`).

   API key scopes: reading status is `monitor:read`; saving is `plc:configure`.
5. **Batch number:**
   - `LineRuntime.batch`, saved in the line's settings so it survives a restart;
   - set by `PUT /api/v1/lines/{id}/batch` `{"batch": "…"}` (Supervisor, audited), by a `set_batch` input, and on the Line dashboard (a field next to the line state; Supervisor);
   - added to every product event and payload (`batch`), to every product record (Section 4's `batch` column), to send card fields (`MESSAGE_FIELDS["batch"]`, for JSON cards).
   - Section 1 already reads the key `batch`: the `{batch}` placeholder (`line_config.TEMPLATE_FIELDS`, `send_dispatcher_service.template_values`, the dashboard's `_sendPlaceholders` and `sendTemplateValues`) and the PLC value source `batch` (`PLCDispatcherService.write_value` reads `event["batch"]` and refuses a missing or non-number batch). So put the batch in the message payload *and* in the PLC event; they are built apart (for example in `finish_crossing`). A line start or an alarm event has no batch: add it there too, or let `write_value` fall back to the line's batch, as it does for the counts.
   - Give the examples a batch: the sample event of `dispatch_manual` (until then a `batch` card's **Test** fails), and `sample_payload` and the dashboard's `sendSampleMessage` (keep the two in step).

   Optional: a Sparkplug tag `Line/Batch` (it needs a rebirth when added; only if simple).
6. **Trigger inspection** (vision camera count mode, set on the camera card, Counting section: **Moving products cross count lines** (default) / **One picture per trigger**). Camera field `count_mode`: `"track"` | `"trigger"`; add it to `CAMERA_KEPT_KEYS`.
   - **Trigger sources:** a PLC input with action `capture` naming the camera; `POST /api/v1/lines/{id}/trigger` (`{"camera_id": optional}`; the line's trigger-mode cameras by default; scope `inspection:trigger`); and `/api/v1/control/trigger/{camera_id}`, which now does the same when the camera is in trigger mode (keep its old answer shape and add the product's result).
   - **On a trigger:**
     - wait for the camera's **next** processed frame (the inference worker's `_processed_frame_id` changes), at most 2 s, else no result;
     - take its detections; any detection of a defect class → reject: `is_defect_class(name, config.defect_classes, config.name_based_defects)` with the camera's `CountingConfig` (`line_manager.counter_for_camera(camera_id).config`), as `detect_live_camera` does (a camera saved without `name_based_defects` keeps the name rule on);
     - any product class (or any detection when the product list is empty) → good;
     - nothing found → the camera's `no_product` setting: `ignore` (no product), `reject` (reason `nothing_detected`) or `good`.
       - `nothing_detected` is a new reason: add a `REASON_*` constant to `line_config.REJECT_REASONS` and a number to `REJECT_REASON_CODES` (for example 6; without one, the PLC value source `reject_reason_code` writes 9, "other"). Add that number to the dashboard's `PLC_VALUE_SOURCE_HINTS.reject_reason_code` and to `README.md`.
   - Then make **one** crossing (track id = a trigger counter) and finish it like a tracked product: through `_on_crossing` when the line has Sync or joined cameras, else `finish_crossing`. It is then counted, recorded and sent.
   - In trigger mode the tracker's crossings for that camera are ignored; `ContinuousVisionRunner` keeps feeding frames so the picture is fresh.
7. **Reject confirmation** (replaces the removed "Wait for ACK"):
   - A PLC action card can name `confirm_input_id` (a signal card of the same line with action `confirm_card`) and `confirm_timeout_ms` (50 to 10000). Check them beside `validate_write_value` (`plc_failsafe_service.py`; every PLC route and `save_line` already call it), and add them to `line_config.PLC_CARD_KEPT_KEYS` so a client that leaves them out keeps them.
   - After the card fires successfully (`_dispatch` reports `sent`; a card with a strobe only once its strobe is pulsed too), the dispatcher waits for that input's edge within the timeout:
     - it comes → status `confirmed`;
     - else status `not_confirmed` and a critical alarm `plc.action_not_confirmed` (add it to the catalog, scope `line`), cleared on the next confirmed fire.
   - Several fires waiting at once are confirmed in order (a FIFO per card).
8. **Line setup UI:** a new step **PLC signals (inputs)**, laid out like the PLC actions:
   - cards / list view, Add, Apply;
   - a dialog with the fields above;
   - the action's own fields shown only when they apply;
   - a **Read now** button with the live value;
   - status chips: last value, last change, error;
   - a note that Generic TCP channels cannot be read, and that sensors faster than the poll need the PLC counter type.

   The PLC action dialog gets **Confirmation signal** and **timeout**. The camera card (Section 2) gets **Count mode** and **When nothing is found**.
9. **Docs:** `README.md` gets a new part "PLC signals (inputs)", plus batch, trigger mode and reject confirmation in the Line setup steps. In the PLC actions paragraph Section 1 wrote (**PLC actions that write a value**), update *Batch number*, *Reject reason code*, the **Test** example values and the sentence on the removed *Wait for PLC ACK / Reply*. `API_KEY_QUICKSTART.md` gets the new endpoints. Update `FILE_TREE.md` and `PROJECT_DESCRIPTION.md` ("the PLC is output-only" is no longer true).

## Tests to add (`tests/test_plc_inputs.py`)

- `read()` for each driver against the fakes or simulators the existing driver tests use (`tests/test_plc_drivers.py`, `tests/test_plc_protocols.py`): bits, words, the Modbus input ranges, REAL decode, and "not supported" for Generic TCP.
- Edges (rising, falling, change) and debounce. The PLC counter delta, including wrap-around.
- start / stop / run_level / reset / batch / alarm actions, with audit entries named `plc:<channel>`.
- A sensor product on a reader-only line with Sync pairs with its code and gets one result.
- Trigger mode: a reject, a good, nothing found (all three settings), a timeout with no frame, the API trigger, and `/control/trigger` still answering.
- Reject confirmation: confirmed in time, not confirmed (alarm raised and then cleared), two fires waiting at once.
- A read error raises and clears `plc.input_failed`; reads never interleave with writes on one endpoint (hold the lock in a fake driver and check the order; `_SlowDriver` and the `slow_plc` fixture in `tests/test_messages_and_plc_values.py` already do this for the strobe).
- **End to end:** start `test codes/plc_simulator.py` (Modbus TCP) on a free port from a temporary copy of the server. Toggle a coil to start the line, step a register as a product counter, and check the counts and records.

## Acceptance

- `pytest` and `ruff check .` are green.
- Lines without signal cards behave exactly as before.
- Screenshots of the PLC signals step and its dialog, and of the batch field on the Line dashboard, at 1440 px and 375 px.
- A final pass over all five sections:
  - update `README.md` (the whole "Production lines" part reads as one story) and `FUTURE_FIXES.md` (what was done; what still needs a real line or PLC);
  - push, and make sure CI (lint, tests, Docker) is green.

## Notes

(Fill in when done.)
