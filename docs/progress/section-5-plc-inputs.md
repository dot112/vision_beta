# Section 5: PLC inputs, batch number, trigger inspection, reject confirmation

Read [README.md](README.md) first: it has the goal, the owner's decisions, the project map and the rules.

**Depends on Sections 2 and 4** (the camera cards for the UI, and the records that carry the batch). Start from the latest section branch in the table in README.md.

## Status

Not started.

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
- **PLC card "Wait for ACK"** was removed in Section 1 (P1), because it did nothing.
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
   - added to every product event and payload (`batch`), to every product record (Section 4's `batch` column), to send card fields (`MESSAGE_FIELDS["batch"]`, the template placeholder from Section 1) and to the PLC value source `batch` (Section 1 P2).

   Optional: a Sparkplug tag `Line/Batch` (it needs a rebirth when added; only if simple).
6. **Trigger inspection** (vision camera count mode, set on the camera card, Counting section: **Moving products cross count lines** (default) / **One picture per trigger**). Camera field `count_mode`: `"track"` | `"trigger"`; add it to `CAMERA_KEPT_KEYS`.
   - **Trigger sources:** a PLC input with action `capture` naming the camera; `POST /api/v1/lines/{id}/trigger` (`{"camera_id": optional}`; the line's trigger-mode cameras by default; scope `inspection:trigger`); and `/api/v1/control/trigger/{camera_id}`, which now does the same when the camera is in trigger mode (keep its old answer shape and add the product's result).
   - **On a trigger:**
     - wait for the camera's **next** processed frame (the inference worker's `_processed_frame_id` changes), at most 2 s, else no result;
     - take its detections; any detection of a defect class (`is_defect_class` with the camera's name rule) → reject;
     - any product class (or any detection when the product list is empty) → good;
     - nothing found → the camera's `no_product` setting: `ignore` (no product), `reject` (reason `nothing_detected`) or `good`.
   - Then make **one** crossing (track id = a trigger counter) and finish it like a tracked product: through `_on_crossing` when the line has Sync or joined cameras, else `finish_crossing`. It is then counted, recorded and sent.
   - In trigger mode the tracker's crossings for that camera are ignored; `ContinuousVisionRunner` keeps feeding frames so the picture is fresh.
7. **Reject confirmation** (replaces the removed "Wait for ACK"):
   - A PLC action card can name `confirm_input_id` (a signal card of the same line with action `confirm_card`) and `confirm_timeout_ms` (50 to 10000).
   - After the card fires successfully, the dispatcher waits for that input's edge within the timeout:
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
9. **Docs:** `README.md` gets a new part "PLC signals (inputs)", plus batch, trigger mode and reject confirmation in the Line setup steps. `API_KEY_QUICKSTART.md` gets the new endpoints. Update `FILE_TREE.md` and `PROJECT_DESCRIPTION.md` ("the PLC is output-only" is no longer true).

## Tests to add (`tests/test_plc_inputs.py`)

- `read()` for each driver against the fakes or simulators the existing driver tests use (`tests/test_plc_drivers.py`, `tests/test_plc_protocols.py`): bits, words, the Modbus input ranges, REAL decode, and "not supported" for Generic TCP.
- Edges (rising, falling, change) and debounce. The PLC counter delta, including wrap-around.
- start / stop / run_level / reset / batch / alarm actions, with audit entries named `plc:<channel>`.
- A sensor product on a reader-only line with Sync pairs with its code and gets one result.
- Trigger mode: a reject, a good, nothing found (all three settings), a timeout with no frame, the API trigger, and `/control/trigger` still answering.
- Reject confirmation: confirmed in time, not confirmed (alarm raised and then cleared), two fires waiting at once.
- A read error raises and clears `plc.input_failed`; reads never interleave with writes on one endpoint (hold the lock in a fake driver and check the order).
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
