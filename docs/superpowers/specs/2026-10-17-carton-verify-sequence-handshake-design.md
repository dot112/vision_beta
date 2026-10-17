# Carton line: product sequence, result handshake, sorting verification

Date: 2026-10-17. Branch `claude/carton-verify-sequence-handshake` (from `main` at `10b60b7`).
Background: vault note "Carton line test design and missing features" (features 1, 2, 4 and 5 there).

The owner's carton line stops the conveyor for each carton. The PLC raises
`CaptureRequest` (an input with the action "Take a picture", trigger 1) and holds
`ProductSequence` (UINT). Camera 1 reads the QR code "When an input asks", Camera 2
is the counting vision camera "One picture per trigger", Sync pairs them into one
product. Output actions SET `InspectionDone` / `PassRequest` / `RejectRequest` and
write `QRValue`; the PLC answers `ResultAck`, and three output actions RESET the
flags on "Input changed: on" of `ResultAck`. Camera 3 watches the two outfeeds.

Three features, all optional per line; a line without the new settings behaves exactly as before.

## 1. Product sequence per carton

- Line setting `sequence_input_id`: the id of one of the line's inputs. Its value is
  the carton's sequence number (any data type). Refused on save when the line has no
  such input. A client that leaves the key out keeps the saved one; `null` or `""` clears it.
- When a "Take a picture" input fires (from the PLC, or `POST /lines/{id}/trigger
  {"trigger_no": N}`), the server reads the sequence input **once, fresh, from the PLC**
  before it takes the pictures, and that value travels with the capture request: into
  each code reader's capture (`request_capture(sequence=...)`, the capture record, the
  read) and into each triggered vision camera's product (`trigger_camera(sequence=...)`,
  the crossing). This is the FIFO: each carton keeps its own sequence even if the PLC
  changes the value before the result.
  - Decision (beyond the brief): a fresh read, not the last polled value. The poller
    reads a channel's inputs one after the other, and the capture action starts while
    the sequence input of the same pass may not be read yet; the PLC sets
    `ProductSequence` and `CaptureRequest` in the same scan, so the polled value could
    be the previous carton's. When the fresh read fails, the last polled value stands
    in only while the input itself reads fine (an old value must not stand in for one
    nobody can see, as `PLCInputService.values` already does); else the carton has no sequence.
- Which value wins for a product: the vision camera's crossing (the counting camera
  made the product), else the paired code read, else (a product made without a
  trigger: a moving line, `POST /trigger` by camera) the sequence input's last polled
  value at the moment the product is made. A line without `sequence_input_id` adds nothing.
- Carried as `sequence` in: the product's message and PLC event, the value names
  (`event_values`, formula `PLAIN_NAMES`, `card_values.js` `VALUE_NAMES`), message
  fields (`MESSAGE_FIELDS["sequence"]`), the template placeholder `{sequence}`, product
  checks (rules see `sequence`), product records (`details.sequence`) and the CSV/XLSX
  export (new column `sequence`, after `product_check`), the Line dashboard's recent reads.
- Duplicate: a second capture with the same sequence while that sequence is still in
  the handshake (feature 2: waiting for its result or for the ack) is ignored: no
  picture, counted as `duplicates`, logged. Without a handshake there is no duplicate check.

## 2. Result handshake

- Line setting `handshake` (absent = off):
  `{enabled, capture_input_id, ack_input_id, result_timeout_ms, ack_timeout_ms, on_timeout}`.
  `capture_input_id` must be one of the line's "Take a picture" inputs; `ack_input_id`
  any other input of the line (usually the action "Only fires cards").
  `result_timeout_ms` 100..60000 (default 3000), `ack_timeout_ms` 100..60000 (default
  5000), `on_timeout` `"reject"` (default) or `"fault"` - never a pass. Checked in
  `normalize_line`; a disabled handshake keeps its settings and is not checked against the inputs.
- State machine `app/services/handshake.py` (`Handshake`), one per line, pure
  bookkeeping with timers given by the caller (the line uses `loop.call_later`; no new threads):

  | state | event | next | effect |
  |---|---|---|---|
  | idle | capture | waiting_result | sequence, request id, result timer |
  | waiting_result | product result (its request) | result_ready | ack timer; clears `line.inspection_timeout` |
  | waiting_result | result timeout, reject | result_ready | a rejected product `inspection_timeout`, counted once |
  | waiting_result | result timeout, fault | result_ready (`last_result` "fault") | alarm `line.inspection_timeout`, event "Handshake fault" |
  | result_ready | ack on | idle | clears `line.ack_timeout`, `line.handshake_fault`, `line.sorting_mismatch` |
  | result_ready | ack timeout | result_ready | alarm `line.ack_timeout` |
  | result_ready | capture (new sequence) | waiting_result | alarm `line.handshake_fault` (the PLC skipped the ack), then the new capture |
  | waiting_result | capture (new sequence) | waiting_result | alarm `line.handshake_fault`; the older request no longer drives the handshake (its product, if it comes, is still counted) |
  | waiting / ready | capture, same sequence | unchanged | duplicate: ignored, counted |
  | idle / waiting | ack on | unchanged | stale ack: ignored for the handshake, logged, counted (`stale_acks`), shown |

  - Decision: after a "fault" timeout the handshake waits for the ack too (`result_ready`
    with `last_result: "fault"`), so the PLC acknowledges the fault the same way and the
    owner's RESET cards reset the fault tag.
  - Decision: a stale ack still fires the input's own cards (the owner's RESET cards):
    resetting flags that are already off does no harm, and the handshake must not hide an input.
  - Decision: `line.inspection_timeout` stays until a later product's result comes in time.
- Results: the line hands every finished product (counting counter's `finish_crossing`,
  and a code-only line's product) to the handshake with its request id. A product made
  for a request that already timed out is **not counted again** (the timeout product
  was its one result, or the fault was): it is dropped, logged and counted as `late`.
  A product without a request id (no trigger) is taken as the waiting request's result.
- Exactly one of pass / reject per product: one `finish_crossing` per product, and the
  timeout product is the request's only product. Tests assert the good and reject cards
  never both fire for one product.
- Restart: the state lives in memory and starts idle. The PLC should drop its pending
  flags after a server restart (the flags are SET, so they stay set in the PLC).
- PLC disconnected: the timers do not depend on reads. When the capture input cannot
  be read while waiting, the result timeout still runs and its policy applies; an ack
  that cannot be read ends in the ack timeout alarm.
- View: `GET /lines/{id}/handshake` -> `{enabled, state, sequence, since, last_result,
  stale_acks, timeouts, duplicates, late, last_error, settings}` (`since`: ISO time of
  the last change of state). Line dashboard panel "Result handshake" (polled with the
  line's figures) and a Line setup section to configure it.

## 3. Camera 3 sorting verification

- Camera setting `sorting_lane`: `null` | `"accepted"` | `"rejected"`, for a camera
  that reads codes; one camera per lane per line. Kept for older clients
  (`READER_KEPT_KEYS`). A sorting camera cannot check codes (save refuses a code check
  on it), never makes a product, never counts, is left out of Sync pairing, code checks,
  the "Sync needs a triggered reader" checks and the batch-from-code option.
- Decision: a sorting camera on "Continuously" verifies (the exception to "continuous
  only shows"), because it reports, it does not decide; it also clears the version 9
  `qr_trigger_review` flag. Its reads come once per code per hold time (`qr_hold_ms`):
  keep the hold time shorter than the gap between two cartons with the same code.
  A triggered sorting camera (wire line or "When an input asks") verifies each picture.
- Memory per line: the last 200 products that had a code (code, sequence, result,
  time, verified lane). On a verification read: the **oldest** product with that exact
  code (whole code text) that is not verified yet (FIFO: cartons leave in the order
  they were inspected; all six test cartons can carry the same QR). Expected lane:
  accepted for a good product, rejected for a reject.
  - match: the product is marked verified; counter `verified`; event `SORTING_VERIFIED`
    (card trigger "Sorting verified"). No record.
  - mismatch: marked verified (with the lane seen); counter `mismatches`; alarm
    `line.sorting_mismatch` (code, sequence, expected, seen); event `SORTING_MISMATCH`
    (card trigger "Sorting mismatch", PLC and send cards); a record row kind
    `"verification"`, result reject, reason `sorting_mismatch`, details
    `{sequence, expected_lane, seen_lane}`.
  - no product with that code waiting: counter `unmatched`, event `SORTING_UNMATCHED`
    on the event bus only, no alarm.
  - A carton without a code (BOX-003) cannot be verified: it is not in the memory and
    Camera 3 reads nothing. A picture without a code on a sorting camera is only shown.
  - A carton Camera 3 misses stays unverified, and the next carton with the same code
    is matched to it (FIFO): reset the line's counters to empty the memory.
- `line.sorting_mismatch` is cleared by the handshake's ack (the PLC acknowledged) and
  by a reset of the line's counters.
- Line summary `sorting: {verified, mismatches, unmatched}`; Line dashboard shows them;
  Line setup reader cards get "Sorting verification" (off / accepted outfeed / rejected outfeed).
- dashboard.html output action / send card triggers gain "Sorting mismatch",
  "Sorting verified" and "Handshake fault". README: a SET card on "Sorting mismatch"
  writes `VerificationFault`, a RESET card on "Input changed: on" of `ResultAck` resets it.

## New names

- Reject reason `inspection_timeout`, PLC reason code 8.
- Alarms (scope line): `line.inspection_timeout`, `line.ack_timeout`, `line.handshake_fault`, `line.sorting_mismatch`.
- Card triggers / event types: `sorting_mismatch`, `sorting_verified`, `handshake_fault`
  (each fires only its own cards, like "Line started / stopped").
- Records filter `kind` accepts `verification`.

## Decisions made while building

- Sync pairing: a capture request's read and the product of the same request carry the
  handshake's request id; Sync pairs only equal request ids when both sides have one
  (keys of different kinds, a request and a track, leave it to the time as before).
  Without this a late product of a timed-out request could take the next carton's code.
- "Handshake fault" fires for: a result timeout under "fault" (`fault: inspection_timeout`),
  an ack timeout (`ack_timeout`), a capture that skipped the ack (`skipped_ack`) or came
  before the last result (`no_result`). The event carries `fault` and `sequence`.
- `line.sorting_mismatch` is cleared by any "on" edge of the handshake's ack input
  (also a stale one), not only a completed cycle.
- A sorting camera's picture without a code (triggered) sends nothing (no `QR_CODE_NO_READ`).
- The timeout reject shows in the recent reads as status `no_result`.
- `line.inspection_timeout` is critical, `line.sorting_mismatch` critical, `line.ack_timeout`
  and `line.handshake_fault` warnings.

## Not changed

- No settings version bump: the new keys are optional and kept for older clients.
- The sequence itself does not pair codes with products.
