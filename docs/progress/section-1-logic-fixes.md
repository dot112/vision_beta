# Section 1: Logic fixes (vision, messages, PLC outputs)

Read [README.md](README.md) first: it has the goal, the owner's decisions, the project map and the rules.

## Status

- **Done.** Everything is on `claude/tender-cerf-qzfcle` (it starts from `claude/busy-babbage-h9j4ln`):
  - `5d46863`: two Linux-only test races fixed;
  - `c631fe0`: counting logic (vision part);
  - the next commit on `claude/tender-cerf-qzfcle`: M1 (TCP channels), M2 (text templates), P2 (PLC value sources and strobe), P1 (fake "wait for ACK" removed), docs.
- 703 tests pass (Python 3.13 and 3.12) and `ruff check .` is clean.
- Screenshots of the TCP panel (client and server mode), the send card dialog in text mode and the PLC dialog's WRITE part were checked at 1440 px and 375 px (no browser errors); they were shared in the session, not committed.

## Done (c631fe0): what changed and why

| Item | Change | Where |
|---|---|---|
| V1, defect by name | `is_defect_class(name, defect_classes, name_based)`. A class is a defect when it is ticked as one; the old rule (name contains defect/scratch/broken) applies only when the camera's `name_based_defects` is on. The tracker takes `name_based_defects=`. `CountingConfig.name_based_defects` (default `True` for API callers). | `app/engines/tracker.py`, `app/schemas/counting.py` |
| V1, keep old lines | Settings **v8** (`_upgrade_to_v8`) sets `name_based_defects: true` on every existing vision camera. `normalize_camera` writes the key only when a client sends it; a missing key means "old rule on" (`counting_config_from_dict` defaults to `True`). | `app/services/line_config.py`, `settings_persistence_service.py` |
| V2, exit edge | `exit_edge(orientation, direction, line1, line2)` and `exit_zone(w, h, edge)` replace the rotation-based `get_exit_line_zone`. The edge follows the flow (A→B forward); `direction == "both"` has no exit band. The tracker and `draw_annotations_mat` both use it. `camera_orientation()` and the three `int(rotation or 90)` copies in `vision_service.py` are removed. The `camera_rotation`/flip parameters are still accepted and ignored (older callers and tests pass them). | `tracker.py`, `inference_engine.py`, `vision_service.py`, `camera_service.py`, `counting_service.py` |
| V3, settings per camera | `counting_config_from_dict(line_trigger, camera)` takes `line1_position`, `line2_position`, `orientation`, `direction` and `tracking{}` from the camera, else the line. `normalize_camera` validates `direction` (`COUNT_DIRECTIONS`) and `tracking` (`TRACKING_LIMITS`: 7 keys with limits; whole numbers for `min_hits` and `max_missed_frames`). `CAMERA_KEPT_KEYS`: an older client that leaves these out keeps the saved values (`_fill_camera_models`). `LineRuntime.configure` no longer patches the second camera's count lines by hand. | `line_config.py`, `settings_persistence_service.py`, `line_service.py` |
| V3, "both" mode bug | A product crossing B and then A in `direction="both"` was never counted (an exact test against a predicted previous point). It now uses the same ±10 px band as the other modes. | `tracker.py` |
| V4, `passed` | `detect_live_camera` decides `passed` with the camera counter's defect classes and name rule. | `vision_service.py` |
| V5, counters | `products_per_minute` and `last_count_at` read under `_count_lock`. `rejects_by_class` is kept, so resetting one class takes its good and its rejected products off the totals. | `counting_service.py` |
| Tests | New `tests/test_logic_fixes.py` (7 tests). The exit-zone tests in `test_bytetrack_kalman.py` and `test_camera_orientation.py` were rewritten for the flow rule. `test_camera_models.py`, `test_lines.py` and `test_product_lists.py` expect `name_based_defects: True` after the upgrade. | `tests/` |

There is **no UI** yet for direction, tracking or the name switch; that is Section 2 (the camera cards).

## Done (second commit): what was built, and where it differs from the plan

| Item | What was built | Where |
|---|---|---|
| M1 | `TcpChannels` (`send`, `apply`, `drop`, `sync`, `status`, `close_all`) and `frame()` / `decode_delimiter()`. Every TCP sender uses it: `deliver()`, `send_to_channels()` (phone scans), the flow engine's `_exec_tcp`, and `POST /comms/tcp/test`. `CountingService._dispatch_tcp` is gone. Kept connections and listeners live only on the dispatcher loop; `_on_loop()` hops there from any other loop. A kept connection reads (and drops) what the device sends, so it notices a closed connection before the next message. | `app/services/tcp_channels.py`, `send_dispatcher_service.py`, `counting_service.py`, `flow_engine.py`, `routes/v1/comms.py` |
| M1, settings | `add_or_update_endpoint` checks `mode`, `delimiter` (text, at most 32 characters), `timeout`, `keep_open`, and keeps the saved values a client leaves out. Save → `TcpChannels.apply`, delete → `drop`, startup (`connect_on_startup`) → `sync`. The **Test** of a server channel reports its listener ("Listening on …; N devices connected") instead of connecting to itself. | `settings_persistence_service.py` |
| M1, shutdown | `CountingService.shutdown()` waits for the queue to drain, then `TcpChannels.close_all()`, then stops the dispatcher (main.py's lifespan already calls it). | `counting_service.py` |
| M1, alarm | `send.channel_down` (scope `server`, `warning`, source `channel:<id>`), in `ALARM_CATALOG` and `AlarmCode.SEND_CHANNEL_DOWN`. Raised by a kept connection that fails and by a port that cannot be opened (retried with the same back-off); cleared when a message goes out, the listener opens, or the channel stops keeping a connection or listening. | `alarm_events.py`, `tcp_channels.py` |
| M1, old files | **Differs:** the v8 step sets every existing TCP channel to `delimiter "\n"`, `mode "client"`, `keep_open false`, because the old code ignored the saved delimiter and mode. A channel saved with "None" or "server" keeps sending what it really sent until someone changes it. The saved `timeout` is used as it is (5 s by default instead of the old fixed 2 s); a channel with no `timeout` key uses 2 s. | `line_config._upgrade_to_v8` |
| M1, extra | Delimiters also take `\xHH` (a byte in hex, e.g. `\x03` for ETX framing). | `tcp_channels.decode_delimiter` |
| M2 | Send card `format` (`json`/`text`) and `template` (at most 1024 characters, required for text). **Differs:** `TEMPLATE_FIELDS` and `parse_template()` live in `line_config.py` (it has no runtime imports and `normalize_send_card` checks the template); `send_dispatcher_service` imports them and has `template_values()` / `render_template()`. Placeholder names are matched after strip + lower case (`{ Code }` works). `\\` is also an escape. `GET /send/fields` returns `placeholders: [{name, label}]` beside `fields`. `send_test` sends a text card's rendered sample as it is (no `"test"` key; that is JSON only). Both sample payloads (server and browser) gained `camera_name: "Line camera"`. | `line_config.py`, `send_dispatcher_service.py`, `routes/v1/send_actions.py` |
| M2, webhook | As planned: text goes with `Content-Type: text/plain; charset=utf-8` unless the channel's headers set one. **Note:** the Connections dialog fills a new webhook channel's headers with `Content-Type: application/json`, so on such a channel a text card goes out as JSON content type; change the channel's header for plain text. | `deliver()` |
| M2, kept fields | `keep_card_fields(cards, saved, SEND_CARD_KEPT_KEYS)`: an older client that leaves `format`/`template` out keeps them, through `POST /send/actions/batch` and `PUT /lines/{id}` (`save_line` now passes the cards as the client sent them). | `line_config.py`, `settings_persistence_service.py` |
| P2 | `value_source` (`line_config.PLC_VALUE_SOURCES`), `strobe_address`, `strobe_pulse_ms`. `validate_write_value(card)` beside `validate_safe_state`, called in `replace_plc_actions`, `replace_line_plc_actions` (so every PLC route) and `save_line`, and in the Test route (422). `PLCDispatcherService.write_value(card, event)` works the value out after the travel delay. Counts come from the event, or from the line's counter for an event that is not a product (line start, alarm). `class_index` uses the event's camera, else the counting camera. `reject_reason_code` uses `line_config.REJECT_REASON_CODES` (5 = `station_no_result` is already mapped for Section 3). A value the event does not have fails the card ("Nothing was written: …") with the usual alarm. Kept fields: `PLC_CARD_KEPT_KEYS`. | `plc_dispatcher_service.py`, `plc_failsafe_service.py`, `line_config.py` |
| P2, strobe | Pulsed inside the endpoint lock after a successful operation (any operation, though the dialog offers it only for WRITE). A failed strobe fails the card and **stops the retries**, so the PLC never sees two strobes for one value. `last_result` gained `value` and `strobe`. | `PLCDispatcherService._dispatch` |
| P2, Test | `dispatch_manual` sends a sample event: reject, `vision_class`, good/reject/total count 1, class index 1. A `batch` card fails there until Section 5. | `dispatch_manual` |
| P1 | The ACK select is gone from the dialog, new cards carry no `ack_mode`, and `_dispatch` always reports `sent`. Old saved `ack_mode` values stay in the file and do nothing. | `dashboard.html`, `plc_dispatcher_service.py`, `schemas/plc.py` |
| Dashboard | TCP panel: mode first, host label "Listen on address" in server mode, a hint line ("Devices connect to this server at port …"), **Keep connection open** (client only), **Custom…** delimiter with its own box (older real control characters are shown escaped), timeout 1 to 30. The Connections list says "TCP Server" / "kept open". Send card dialog: **Message format**, template textarea, placeholder chips (`.send-chip`), live preview with the same parser as the server (`renderSendTemplate`, `sendTemplateValues`), problems shown under the textarea and blocking OK/Apply, summary "Text: …". PLC dialog WRITE part: **Value** select with a hint per source, the fixed number only for "Fixed number", **Then pulse strobe address** and **Strobe pulse (ms)**; changing the operation away from WRITE resets the source to fixed; the summary names the source and the strobe. | `dashboard.html`, `assets/dashboard.css` |
| Tests | `tests/test_messages_and_plc_values.py` (57 tests): every delimiter on the wire, the channel timeout, one connection per message, keep-open on one connection, reconnect with the alarm raised and cleared, a device closing a kept connection, server mode with two devices and with none, a busy port, settings checks and kept fields, the v8 step, the dispatcher loop, text templates (checks, rendering, TCP, MQTT, webhook Content-Type, Test button, API), every PLC value source, refused values, strobe order and lock, a failed strobe, the Test button's sample values, old `wait_ack`, PLC API checks. | `tests/` |

## The plan this section followed

### M1: TCP channels do what their settings say

**Today:** `send_dispatcher_service.deliver()`, `counting_service.send_to_channels()` and `CountingService._dispatch_tcp()` always send `json.dumps(message) + "\n"` on a new connection with a 2 s timeout. The channel's saved fields are ignored:
- `delimiter`: stored as *escaped text* from the select in `dashboard.html` (`tcpDelimiterInput`: the values are the 2-character string `\n`, `\r\n`, `\0`, or `""` for none; older files may hold a real newline);
- `timeout`: 1 to 30 s, `_bounded_int` in `settings_persistence_service.add_or_update_endpoint`;
- `mode`: `client` / `server`, select `tcpModeInput`.

**Build:** a new module `app/services/tcp_channels.py` (`TcpChannels`), used by `deliver()`, `send_to_channels()` and the flow engine's TCP output node (`_exec_tcp` in `app/engines/flow_engine.py`: check what it does today and make it use the same framing).

- **Framing:** `frame(text, endpoint) -> bytes` = UTF-8 text + the decoded delimiter. Decode `\r` `\n` `\t` `\0` `\\` escapes; a real control character stays as it is; `""` means no delimiter. Add a **Custom** option to the select (free text with the same escapes).
- **Timeout:** use the channel's `timeout` for the connect and the write.
- **Client mode:**
  - as today (connect, send, close);
  - plus a new channel option **Keep connection open** (`keep_open`, a checkbox in the TCP panel, saved and validated in `add_or_update_endpoint`). With it, one connection per channel is kept. It reconnects on the next message after a failure, with backoff (1 s doubling to 30 s), and raises a new alarm `send.channel_down` (add it to `ALARM_CATALOG`, scope `server`, severity `warning`) until a send succeeds again.
- **Server mode:**
  - The channel listens on `host:port` (`host` is the bind address, e.g. `0.0.0.0`), and every message goes to every connected client. With no client connected, the send fails with "no device is connected".
  - The listener starts when the channel is saved, at startup (hook in where MQTT channels are applied: `add_or_update_endpoint` and `SettingsPersistenceService.restore_on_startup`/`connect_on_startup`), and stops on delete (`delete_endpoint`) and shutdown (`main.py` lifespan).
  - A port that cannot be opened raises `send.channel_down`.
- **One event loop:**
  - Queued messages run on the telemetry dispatcher's own loop (`counting_service._telemetry_dispatcher._loop`, a separate thread), but `SendDispatcherService.send_test` awaits `deliver()` on the main loop. Persistent connections and listeners must live on **one** loop, so run all TCP work on the dispatcher loop: from any other loop, `await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, dispatcher_loop))`.
  - Keep messages to one destination in order (the dispatcher's lanes already do this: `_lane()`).
- **Dashboard:** in the TCP panel of the Connections dialog (`commPanelTcp`, saved by `saveCommEndpoint()`, loaded by `openEditCommModal()`):
  - add the **Keep connection open** checkbox (client mode only) and the **Custom** delimiter;
  - explain server mode in a hint ("devices connect to this server at port …").

### M2: Text messages (templates) on send cards

- **New send card fields** (validated in `normalize_send_card`, `line_config.py`):
  - `format`: `"json"` (default, as today) or `"text"`;
  - `template`: at most 1024 characters, required when `format == "text"`.
- **Placeholders** are `{name}`, with `{{` and `}}` for literal braces; `\r` `\n` `\t` escapes are allowed in the template. An unknown placeholder is refused at save with a message that names it.
- **Placeholder list** (one list, `TEMPLATE_FIELDS`, in `send_dispatcher_service.py`, also returned by `GET /api/v1/send/fields`):

  `line_id, line_name, line_state, event, timestamp, date, time, camera_id, camera_name, track_id, class_name, confidence, result, result_code (1 good / 2 reject), reject_reason, code, code_format, code_status, product_name, good_count, rejected_count, total_inspected, yield_percentage, products_per_minute, batch, alarm_code, alarm_message`

  - `batch` stays empty until Section 5.
  - `camera_name` comes from `app_state.cameras[camera_id].name`.
  - `date`/`time` are the server's local time.
  - Missing values render as an empty string.
- **Sending:**
  - `build_message()` returns the text for a text card.
  - `deliver()` sends text as it is: TCP = text + delimiter; MQTT = the text as the payload (check that `MQTTChannels.publish` / the client accepts `str`, and extend it if not); webhook = `content=text` with `Content-Type: text/plain; charset=utf-8` unless the channel's own headers set a Content-Type.
- **Dashboard** (send card dialog: `refreshSendModalBody`, `updateSendPreview`, `sendSampleMessage`, `saveSendActionFromModal` in `dashboard.html`):
  - add a **Message format** select (JSON / Text);
  - a template textarea with clickable placeholder chips;
  - the preview shows the template filled with the sample values (`sample_payload` on the server, `sendSampleMessage` in the browser: keep the two in step);
  - the card summary says "Text: …".

### P2: PLC WRITE value sources and a strobe

- **New PLC card fields:**
  - `value_source`, one of:
    - `fixed` (default: today's `write_value`);
    - `result_code` (1 good, 2 reject);
    - `good_count`, `reject_count`, `total_count`;
    - `class_index`: the 1-based position of the product's class in the camera's "Products to count" list followed by "Defects to reject"; 0 when not found;
    - `reject_reason_code`: 0 none, 1 `vision_class`, 2 `code_not_in_list`, 3 `code_in_reject_list`, 4 `no_code`, 5 `station_no_result` (Section 3), 9 other;
    - `batch`: a number only; Section 5 adds the batch. Until then, or for a batch that is not a number, the card fails with a clear message.
  - `strobe_address` (optional) and `strobe_pulse_ms` (default 100, 10 to 10000).
- **Validation:** add `validate_write_value(card)` next to `validate_safe_state` (`plc_failsafe_service.py`). It is called in `SettingsPersistenceService.save_line` (where `validate_safe_state` runs) and in the PLC card routes (`app/routes/v1/plc.py`). `value_source` other than `fixed` only for WRITE.
- **Dispatcher** (`PLCDispatcherService._dispatch`):
  - Work out the value from the event at the moment of the write. The event already has `good_count`, `reject_count`, `result`, `reject_reason` and `detected_classes`; for `class_index`, look up the camera's lists through `line_manager.get(line_id).camera_entry(camera_id)`, or the counting camera.
  - After a successful operation, pulse `strobe_address` **inside the same endpoint lock**, so no other card's write comes between the data and its strobe. A failed strobe makes the card fail (alarm as for any failure).
  - The **Test** button (`dispatch_manual`) uses sample values: result reject, counts 1, class index 1.
- **Dashboard:** PLC card dialog (`refreshPlcModalBody` in `dashboard.html`, the WRITE part):
  - a **Value** select;
  - the fixed number box only for "Fixed number";
  - **Then pulse strobe address** and its pulse length;
  - the card summary (`plcCardSummary`) names the source.

### P1: Remove the fake "Wait for PLC ACK"

- Remove the `ack_mode` select from the PLC card dialog (around `refreshPlcModalBody`, the "Fire & Forget / Wait for PLC ACK" options).
- `_dispatch` always reports `sent` on success. Do not write `ack_mode` on new cards (`addPlcActionCard`, `loadPlcActionCards` defaults).
- Leave old saved values alone: they do nothing.
- Section 5 adds the real **reject confirmation**.

### Docs for this section

`README.md`:
- Connections, TCP: delimiter, timeout, Keep connection open, server mode.
- Send results: Text format and the placeholders.
- PLC actions: value sources and strobe; the removed ACK option.
- The defect-name switch, the count direction and the flow-based exit band. This can be one paragraph now, completed by Section 2.

## Tests to add

In `tests/test_logic_fixes.py` or a new `tests/test_messages_and_plc_values.py`. Reuse `_Listener`, `listeners`, `lines` and `_set_cards` from `tests/test_send_cards.py`, and `tests/mqtt_broker.py`.
- **TCP:** each delimiter (`\n`, `\r\n`, `\0`, none, custom) as received by a local listener; the channel timeout is used; with `keep_open`, two messages on **one** connection; after the listener restarts, the channel reconnects and `send.channel_down` is raised then cleared.
- **Server mode:** two clients connect to the channel's port and both receive each message; with no client, the send fails with "no device is connected".
- **Templates:** an unknown placeholder is refused; rendering with missing values; a text card over TCP, MQTT and webhook (check `Content-Type`); `send_test` sends the rendered sample.
- **PLC:** with a fake driver that records operations, every `value_source`; the strobe pulses **after** the write and inside the lock (two cards on one endpoint do not interleave); a strobe failure fails the card; `ack_mode: wait_ack` reports `sent`.

## Acceptance

- `pytest` and `ruff check .` are green.
- An old settings file behaves exactly as before: JSON + `\n`, a new connection per message, the card's fixed value.
- Screenshots of the TCP panel, the send card dialog in text mode and the PLC dialog's WRITE part, at 1440 px and 375 px.

## Notes for the next section

- **Start from** `claude/tender-cerf-qzfcle`.
- **Section 2 (camera cards):** the camera settings from c631fe0 still have no UI: `direction`, `tracking{}`, per-camera `line1_position`/`line2_position`/`orientation`, and `name_based_defects` (missing = on, so every camera still rejects classes named like a defect until the switch is turned off). They are validated in `normalize_camera` and kept by `CAMERA_KEPT_KEYS`. The dashboard's native `<select>`s are replaced by custom dropdowns (`initCustomSelects`); in Playwright, scroll to an input next to a select, not to the select itself.
- **Section 3:** add `"station_no_result"` to `line_config.REJECT_REASONS` with a `REASON_*` constant; `REJECT_REASON_CODES` already maps it to 5.
- **Section 5:** put the line's `batch` into the product's message payload (for `{batch}` in templates) and into the PLC event (`PLCDispatcherService.write_value` reads `event["batch"]`; a non-number already fails with a clear message). The real reject confirmation replaces the removed ACK choice.
- **New settings this section added** (all with defaults, no new schema version): endpoint `keep_open`; send card `format`, `template`; PLC card `value_source`, `strobe_address`, `strobe_pulse_ms`. Clients that leave them out keep the saved values (`keep_card_fields`, `SEND_CARD_KEPT_KEYS`, `PLC_CARD_KEPT_KEYS`).
- **A flaky test seen once:** `tests/test_sparkplug_commands.py::test_what_is_not_a_command_changes_nothing[True]` timed out waiting for a metric in one full run on Python 3.13 and passed on every rerun and alone. It does not touch what this section changed; if it shows up again, look at the Sparkplug publish timing.
