# Section 5: PLC inputs, batch, trigger inspection, reject confirmation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The PLC can send signals into the software (start/stop, product sensor, reset, batch, reject confirmation, alarms), vision cameras can take one picture per trigger, and every product carries the line's batch number.

**Architecture:** Every PLC driver gets a `read()`. A new `PLCInputService` polls each PLC channel's signal cards on the main loop, under the same per-channel lock the PLC action cards use, and turns edges into actions on the line. Sensor products and trigger inspections are made into the same "crossing" a tracked product is, so they are counted, recorded and sent by the code that exists.

**Tech Stack:** Python 3.12+, FastAPI, asyncio, SQLite; one HTML dashboard with plain JavaScript. No new library.

**Spec:** [docs/progress/section-5-plc-inputs.md](../../progress/section-5-plc-inputs.md), with the rules and the project map in [docs/progress/README.md](../../progress/README.md). Read both before a task.

## Global Constraints

- Branch: `claude/section-5-plc-inputs`, started from `origin/main` (Sections 1 to 4 are merged there).
- **Never break an existing line.** A line without signal cards, without a batch and with cameras in track mode behaves exactly as before, down to the keys of its messages.
- `SCHEMA_VERSION` stays **8**. No upgrade step: every new key is optional and read with a default (`[]`, `""`, `"track"`).
- A client that does not send a new field must not wipe it: cameras through `CAMERA_KEPT_KEYS`, PLC cards through `PLC_CARD_KEPT_KEYS`, line keys kept in `normalize_line` / `save_line`.
- **Never start the server from the repository folder.** Use the scratch copy (`backup/scratch-tools/launcher.py`, port 8765).
- Line endings LF. Keep a file's UTF-8 BOM where it has one.
- Tests need no hardware. Run them with the repository's own Python:
  `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider` and `.venv/Scripts/python.exe -m ruff check .`
- Test baseline on this Windows machine: 8 known failures that pass in Linux CI (`test_error_alarms::test_dispatch_to_unreachable_plc_raises_both_alarms`, six `TestModbusTCPDriver` tests, `test_plc_protocols::TestDispatcherHardening::test_bad_address_is_not_retried`) and a few timing tests that fail under load. Compare with that, not with zero. Linux CI must be fully green (780 tests before this section).
- Dashboard changes are checked at 1440 px and 375 px.
- Docs and UI text use the plain wording of the existing docs. Never put a model name in the repository.
- Commit after each task. Push only when the owner says so.

## Decisions this plan makes (the spec left them open, or the code forced them)

| # | Decision | Why |
|---|---|---|
| D1 | `plc_inputs` and `batch` are kept **inside the line's entry for every line, Line 1 too**. | Only `action_trigger`, `plc_actions` and `send_actions` are at the top level, for version 1. New keys have no version 1 reader. |
| D2 | `batch` is kept by the server (like `counts_reset_at`): `PUT /lines/{id}` ignores it; only `PUT /lines/{id}/batch`, a `set_batch` signal and the dashboard field change it. | One audited way in. |
| D3 | A signal card has **no `card_id`**. The link for reject confirmation is `confirm_input_id` on the PLC action card only. The signal dialog shows which actions name it. | Two places for one link can disagree. |
| D4 | `product_sensor` is **refused on a line that has a vision camera** (the counting camera counts the products there). Use `capture` to trigger a camera instead. | The spec only describes it for lines without one; with one, every product would count twice. |
| D5 | A sensor product always goes through `finish_crossing` with a crossing dict (through `_on_crossing` when Sync is on). `count_product` is not used. Its track id is a per-line sensor counter, not `None`. | Section 4's note: such a product is then recorded with nothing else to do. A `None` track id repeats the PLC event id after a counter reset, and a "once per event" card would skip products. |
| D6 | On a line with a product sensor and no vision camera, the sensor is the product source: Sync is allowed (sensor + code reader), a camera that checks codes forces Sync on, and "when no code is read" keeps its setting. | Otherwise each code read would count a second product. |
| D7 | New code reader capture choice `qr_trigger: "signal"` (one picture per PLC signal). A `capture` signal must name a reader set to it, or a vision camera in trigger mode. | A reader left on "continuous" would decode every frame as well and report each code twice. |
| D8 | PLC counter: the increase is `(new - old) mod 65536`. An increase above **1000** between two reads is taken as the PLC resetting its counter: nothing is counted and the new value is the starting point. | A reset from 500 to 0 would otherwise count 65036 products. |
| D9 | The first read of a signal is its starting point and runs no action. Exception: `alarm`, which follows the level and is raised at once when the value is true. | A start bit that is already on must not start a line when the server boots. |
| D10 | A PLC that cannot be reached is retried at the channel's `reconnect_interval` (5 s by default), not at every poll. Its signals show "PLC not connected"; only the existing `plc.connect_failed` alarm is raised. | A 50 ms poll must not hammer a dead PLC. |
| D11 | The batch is added to a message and a PLC event **only when the line has one**. | Lines without a batch keep their messages as they are. |
| D12 | Trigger mode waits for a frame handed to the model **after** the trigger: when the worker is busy at the trigger, that result is skipped and the next one is used. Still at most 2 s. | The frame in flight was taken before the part was in place. |
| D13 | The **Test** button of a PLC action does not wait for the confirmation signal and raises no confirmation alarm. | Commissioning must not raise critical alarms; the signal card's live value shows whether the sensor works. |
| D14 | API key scopes: `…/plc-inputs/status` `monitor:read`; a body with `plc_inputs` and `…/plc-inputs/{id}/read` need `plc:configure`; `POST /lines/{id}/trigger` `inspection:trigger`; `PUT /lines/{id}/batch` `configuration:write` (the rule for every other change to a line). | The spec names the first two and the trigger scope. |
| D15 | Tests are split by subject into five files instead of one `tests/test_plc_inputs.py`. | One file would pass 1500 lines. |
| D16 | A line clone does not copy signal cards. | Two lines would be started and stopped by the same bit. |

## File Structure

| File | Responsibility |
|---|---|
| `app/hardware/plc/base.py` | `PLCDriver.read()` (lock, type check) and the default `_read()` |
| `app/hardware/plc/_common.py` | `decode_words()`, `READ_SUFFIX`, `coerce_read()` shared by the drivers |
| `app/hardware/modbus/client.py` | read discrete inputs (2) and input registers (4) |
| `app/hardware/plc/{modbus,s7,ethernet_ip,opcua,melsec,fins}_driver.py` | each driver's `_read()` |
| `app/services/line_config.py` | signal card shape and checks, `count_mode` / `no_product`, `nothing_detected`, batch text, kept keys |
| `app/services/settings_persistence_service.py` | keep the new keys in `save_line`, `set_line_batch`, confirmation link check |
| **`app/services/plc_input_service.py`** (new) | polling, debounce and edges, counter increase, status, alarms, actions |
| **`app/services/trigger_inspection.py`** (new) | one picture → one product for a camera in trigger mode |
| `app/services/counting_service.py` | `make_crossing`, `emit_crossing`, `batch`, trigger mode skips the tracker |
| `app/services/line_service.py` | `LineRuntime.batch`, `plc_inputs`, `sensor_product()`, signal readers, the reject reason of a crossing |
| `app/services/plc_dispatcher_service.py` | wait for the confirmation signal; batch fallback |
| `app/services/plc_failsafe_service.py` | `validate_confirmation` |
| `app/services/send_dispatcher_service.py` | `batch` field and samples |
| `app/routes/v1/lines.py`, `control.py` | status, read now, batch, trigger |
| `app/security/api_key_scopes.py`, `app/dependencies.py` | scopes (D14) |
| `app/events/alarm_events.py` | three new alarm codes |
| `main.py`, `tests/conftest.py` | start and stop the service; tests never start the poller |
| **`assets/plc_signals.js`** (new), `dashboard.html`, `assets/production_lines.js` | PLC signals step, confirmation fields, count mode, batch field |
| `tests/test_plc_read.py`, `test_plc_inputs.py`, `test_batch.py`, `test_trigger_inspection.py`, `test_reject_confirmation.py`, `test_plc_inputs_e2e.py` (all new) | see each task |

## Review Focus

Each of these is implied by the spec but named by none of its tests. Each has a test in the task that owns the code.

1. **A PLC that is down, polled every 50 ms**: it is retried at its reconnect interval, raises one connect alarm, and the other channels keep polling (Task 3).
2. **The PLC resets its own counter** (500 → 0): nothing is counted (Task 3).
3. **A start bit that is already on when the server starts**: the line is not started (Task 3).
4. **A signal card is removed or switched off while its alarm is active**: its `plc.input_alarm` and `plc.input_failed` are cleared (Task 3).
5. **A batch from the PLC that is not clean text** (REAL `1001.0`, 200 characters, a control character): `"1001"`, cut to 64, refused; the poller keeps running in each case (Task 5).

---

### Task 1: `read()` for every PLC driver

**Files:**
- Modify: `app/hardware/plc/base.py`, `app/hardware/plc/_common.py`, `app/hardware/modbus/client.py`, `app/hardware/plc/modbus_driver.py`, `s7_driver.py`, `ethernet_ip_driver.py`, `opcua_driver.py`, `melsec_driver.py`, `fins_driver.py`
- Test: `tests/test_plc_read.py` (fake PLC servers: subclass the ones in `tests/test_plc_protocols.py`)

**Interfaces:**
- Produces:
  - `base.READ_DATA_TYPES = ("auto", "bool", "int16", "uint16", "int32", "real", "string")`
  - `PLCDriver.read(address: str, data_type: str = "auto") -> Tuple[bool, Any, str]`: lower-cases the type, refuses an unknown one, takes `_operation_lock`, calls `_read`, and turns a `ValueError` into `(False, None, message)`. The value is a `bool` for a bit, an `int`, a `float` for `real`, a `str` for `string`.
  - `PLCDriver._read(address, data_type)`: the default returns `(False, None, "Reading is not supported by <class name>")`. Generic TCP keeps the default.
  - `_common.READ_SUFFIX = {"int16": "INT", "uint16": "UINT", "int32": "DINT", "real": "REAL"}`
  - `_common.decode_words(words: List[int], data_type: str) -> Union[int, float]`: the reverse of `encode_words` (low word first; `""` is an unsigned word).
  - `_common.coerce_read(value, data_type) -> Any`: `"bool"` makes a number `value != 0`; `"auto"` returns the value as it is.
  - `ModbusTCPClient.read_discrete_inputs(address, count=1)` and `read_input_registers(address, count=1)`, each `-> Tuple[bool, list, Optional[str]]`. Simulation mode reads `_inputs` / `_input_registers` (512 each).
  - `modbus_driver._parse_modbus_read_target(address) -> Tuple[str, int, str]` with kind `"coil" | "discrete" | "holding" | "input"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_plc_read.py`. One test per line of this table; each starts the fake server, puts the value in it and asserts `await drv.read(address, data_type)`:

| Driver | Address, type | Fake holds | `read` returns |
|---|---|---|---|
| Modbus TCP | `"C5"`, auto | coil 5 on | `(True, True, …)` |
| | `"10003"`, auto | discrete input 2 on | `True` |
| | `"100003"`, auto | discrete input 2 on | `True` |
| | `"DI2"`, auto | discrete input 2 on | `True` |
| | `"40011"`, auto | holding 10 = 65535 | `65535` |
| | `"40011"`, `int16` | holding 10 = 65535 | `-1` |
| | `"R20:DINT"`, auto | 20 = 0xFFFF, 21 = 0xFFFE | `-2` |
| | `"R30"`, `real`, `modbus_word_order: "low_first"` | 1.5 with the words swapped | `1.5` |
| | `"30006"`, auto | input register 5 = 7 | `7` |
| | `"IR5:INT"`, auto | input register 5 = 0xFFFB | `-5` |
| | `"R10"`, `bool` | holding 10 = 3 | `True` |
| | `"R10"`, `string` | — | `(False, None, …)` |
| Modbus, simulation mode | `"R3"` after `write("R3", 9)` | — | `9` |
| Modbus RTU over TCP | `"R4"` | register 4 = 12 | `12` |
| S7 | `"I0.2"` | byte 0 = `0b100` | `True` |
| | `"MB3"` | byte 3 = 200 | `200` |
| | `"MW10"` auto / `int16` | bytes `FF FE` | `65534` / `-2` |
| | `"DB1.DBD8"` auto / `int32` | `FF FF FF FE` | `4294967294` / `-2` |
| | `"DB1.DBD8:REAL"` | `struct.pack(">f", 2.5)` | `2.5` |
| | `"MW10"`, `int32` | — | `(False, None, …)` needs a double word |
| EtherNet/IP | `"Reject"` | BOOL `FF` | `True` |
| | `"Count"` | DINT 70000 | `70000` |
| | `"Speed"` | REAL 12.5 | `12.5` |
| | `"Batch"`, auto and `string` | Logix STRING `"LOT-7"` (type `0x02A0`, handle `0x0FCE`, DINT length, bytes) | `"LOT-7"` |
| | `"Missing"` | — | `(False, None, …)` with the CIP status text |
| OPC UA | node values `True`, `7`, `1.5`, `"LOT-7"`; `7` as `bool` | a fake client put on `drv._client` whose `get_node(id).read_value()` returns them | the same; `True` |
| MELSEC | `"M5"` | bit on | `True` |
| | `"D100"` / `"D100"`, `int16` | word 0xFFFF | `65535` / `-1` |
| | `"D100:DINT"` | 70000 as two words | `70000` |
| | `"D100:REAL"` | 1.5 | `1.5` |
| Omron FINS | `"CIO 0.05"` | bit on | `True` |
| | `"D100"` | word 1234 | `1234` |
| | `"D100:DINT"` | 70000, low word in D100 | `70000` |
| Generic TCP | any | — | `(False, None, "Reading is not supported by GenericTCPDriver")` |

  Also:
  - `test_a_write_to_a_read_only_modbus_range_is_still_refused`: `await drv.write("30001", 1)` returns `ok is False` with "read-only" in the message.
  - `test_a_read_waits_for_the_drivers_operation_lock`: hold `drv._operation_lock`, start `read`, assert it is not done after 50 ms, release, assert it completes.
  - `test_an_unknown_data_type_is_refused`: `await drv.read("C1", "word64")` returns `(False, None, …)` and sends nothing to the fake.

- [ ] **Step 2: Run them.** `.venv/Scripts/python.exe -m pytest tests/test_plc_read.py -q -p no:cacheprovider`. Expected: every test fails (`read` does not exist).

- [ ] **Step 3: Implement** `read` / `_read` in `base.py`, the helpers in `_common.py`, the two client reads, and each driver's `_read`:
  - **Modbus:** `_parse_modbus_read_target` accepts everything `_parse_modbus_target` does, plus `1xxxx` / `1xxxxx` and `DI<n>` (discrete input; `10001` is input 0) and `3xxxx` / `3xxxxx` and `IR<n>` (input register; `30001` is register 0). A type suffix is allowed on holding and input registers. `_parse_modbus_target` (writes) is not changed. Add `_decode_register_value(words, data_type, word_order)`, the reverse of `_encode_register_value`.
  - **S7:** `_read_bytes(parsed, size) -> Tuple[bool, Optional[bytes], str]` reads `size` bytes with transport size BYTE (`_address_spec(parsed, 0x02, count=size)`); `_read_byte` stays for `read_bit` and `toggle`. Big-endian decode.
  - **EtherNet/IP:** decode `_read_tag`'s `(type_code, data)` with `_CIP_TYPES`. For a string, read the tag without `_read_tag`'s structure check: Logix STRING (`0x02A0`, handle `0x0FCE`: DINT length, then the bytes) and the short string `0x00DA` (one length byte).
  - **OPC UA:** `self._node(address).read_value()`; on an exception call `_handle_failure` as `toggle` does.
  - **MELSEC:** batch read `0x0401` with subcommand `0x0000`, words little-endian.
  - **FINS:** memory area read `0x01 0x01` with the word area code, words big-endian, low word of a 32-bit value first.

- [ ] **Step 4: Run** `tests/test_plc_read.py`, `tests/test_plc_protocols.py` and `tests/test_plc_drivers.py`. Expected: all pass, except the six known Windows `TestModbusTCPDriver` failures and `test_bad_address_is_not_retried`.

- [ ] **Step 5: Commit** `Add read() to the PLC drivers`.

---

### Task 2: Signal cards in the line settings

**Files:**
- Modify: `app/services/line_config.py`, `app/services/settings_persistence_service.py` (only if `save_line` drops the key), `app/routes/v1/lines.py` (`clone_line` stays without signals)
- Test: `tests/test_plc_inputs.py`

**Interfaces:**
- Produces, in `line_config.py`:

```python
PLC_INPUT_ACTIONS = ("start_line", "stop_line", "run_level", "reset_counters", "product_sensor",
                     "capture", "set_batch", "confirm_card", "alarm")
PLC_INPUT_DATA_TYPES = ("auto", "bool", "int16", "uint16", "int32", "real", "string", "counter")
PLC_INPUT_EDGES = ("rising", "falling", "change")
PLC_INPUT_POLL_LIMITS_MS = (50, 10000)
DEFAULT_PLC_INPUT_POLL_MS = 100
PLC_INPUT_DEBOUNCE_LIMITS_MS = (0, 5000)
PLC_INPUT_ALARM_SEVERITIES = ("info", "warning", "critical")
MAX_PLC_INPUTS = 32

def new_plc_input_id() -> str                       # "plcin_" + 8 hex digits
def normalize_plc_input(raw: Any, index: int = 0) -> Dict[str, Any]
def normalize_plc_inputs(raw: Any) -> List[Dict[str, Any]]     # at most MAX_PLC_INPUTS, ids unique
def product_sensors(line: Dict[str, Any]) -> List[Dict[str, Any]]   # enabled cards with action product_sensor
```

  A normalized card has exactly: `id`, `name`, `enabled`, `plc_endpoint_id`, `address` (text, at most 128), `data_type`, `poll_ms`, `edge` (default `rising`), `debounce_ms` (default 0), `action`; plus `camera_id` for `capture`, and `alarm_text` (at most 200, default the card's name) and `alarm_severity` (default `warning`) for `alarm`.
- `QR_TRIGGERS = ("continuous", "line1", "line2", "signal")`.
- `normalize_line` keeps `plc_inputs` when the merged line has it (so a client that leaves it out keeps the saved cards) and checks the cards against the line's cameras.

- [ ] **Step 1: Write the failing tests:**

```python
def test_a_signal_card_gets_its_defaults():
    card = normalize_plc_input({"name": " Start ", "plc_endpoint_id": "ep1", "address": "10001", "action": "start_line"})
    assert card["id"].startswith("plcin_") and card["name"] == "Start"
    assert (card["enabled"], card["data_type"], card["poll_ms"], card["edge"], card["debounce_ms"]) == (True, "auto", 100, "rising", 0)
    assert "camera_id" not in card and "alarm_text" not in card and "card_id" not in card

@pytest.mark.parametrize("bad, word", [
    ({"action": "explode"}, "action"),
    ({"action": "start_line", "poll_ms": 10}, "between 50 and 10000"),
    ({"action": "start_line", "debounce_ms": 6000}, "between 0 and 5000"),
    ({"action": "start_line", "edge": "sideways"}, "edge"),
    ({"action": "start_line", "data_type": "counter"}, "product sensor"),
    ({"action": "start_line", "data_type": "string"}, "batch"),
    ({"action": "alarm", "alarm_severity": "loud"}, "severity"),
    ({"action": "capture"}, "camera"),
])
def test_signal_card_validation(bad, word):
    with pytest.raises(ValueError, match=word):
        normalize_plc_input({"address": "C1", "plc_endpoint_id": "ep1", **bad})
```

  And, with the `lines` fixture (copy the one in `tests/test_joined_cameras.py`):
  - `test_a_line_keeps_its_signal_cards_when_a_client_leaves_them_out`: save a line with one card, then `save_line({"id": line_id, "enabled": False})`; `get_line(line_id)["plc_inputs"]` still has the card. The same for `PRIMARY_LINE_ID`, where the card is in `state["lines"][0]`, not at the top level of the state.
  - `test_version_8_settings_load_without_signal_cards`: a state with no `plc_inputs` anywhere loads, `schema_version` stays 8, and `get_line(...).get("plc_inputs", [])` is `[]`.
  - `test_two_signal_cards_cannot_share_an_id` and `test_at_most_32_signal_cards`.
  - `test_a_product_sensor_is_refused_on_a_line_with_a_vision_camera` (D4): `ValueError` matching "vision camera".
  - `test_a_capture_signal_needs_a_reader_set_to_capture_on_a_signal` (D7): refused for a camera that is not on the line and for a reader on `continuous`; accepted for a reader with `qr_trigger: "signal"`.
  - `test_a_signal_reader_needs_no_vision_camera`: a line with only a QR reader on `qr_trigger: "signal"` saves; one on `line1` is still refused with "needs a vision camera".
  - `test_sync_works_with_a_product_sensor_and_a_code_reader` (D6): a reader-only line with a product sensor saves with `sync.enabled: True`; a reader with `accept_listed` forces Sync on and keeps `qr_no_read: "reject"`; without the sensor Sync is still refused.
  - `test_a_cloned_line_has_no_signal_cards` (through the API; D16).

- [ ] **Step 2: Run** `tests/test_plc_inputs.py`. Expected: fail (`normalize_plc_input` is not defined).

- [ ] **Step 3: Implement** the constants and functions, and in `normalize_line`: read `plc_inputs` from `merged`, normalize, run the camera checks, and use `vision or product_sensors(...)` wherever the Sync rules now ask for a vision camera. A disabled card is still validated. `counter` is only for `product_sensor`; `string` only for `set_batch`.

- [ ] **Step 4: Run** `tests/test_plc_inputs.py tests/test_lines.py tests/test_lines_api.py tests/test_qr_trigger.py tests/test_reader_actions.py tests/test_joined_cameras.py`. Expected: pass.

- [ ] **Step 5: Commit** `Add PLC signal cards to the line settings`.

---

### Task 3: The polling service

**Files:**
- Create: `app/services/plc_input_service.py`
- Modify: `app/events/alarm_events.py`, `main.py`, `app/routes/v1/lines.py` (`apply_line`), `app/services/settings_persistence_service.py` (channel saved or deleted), `tests/conftest.py`
- Test: `tests/test_plc_inputs.py`

**Interfaces:**
- Consumes: `PLCDriver.read` (Task 1), `normalize_plc_input` (Task 2), `PLCDispatcherService._endpoint_locks`, `PLCDriverFactory.get_driver`.
- Produces:

```python
COUNTER_MODULUS = 65536
COUNTER_MAX_STEP = 1000

def truthy(value: Any) -> bool
def counter_delta(previous: int, current: int) -> int          # 0 when the increase is above COUNTER_MAX_STEP
def edge_matches(edge: str, old: Any, new: Any) -> bool         # rising / falling on truthy(); change on !=

class SignalFilter:
    """The debounced value of one signal."""
    def __init__(self, debounce_s: float) -> None
    value: Any                                                  # the accepted value; unset before the first read
    def update(self, value: Any, now: float) -> Optional[Tuple[Any, Any]]
        # (old, new) when the accepted value changed. A new value is accepted once a read at least
        # debounce_s after it was first seen still shows it. The first value is accepted at once and returns None.

@dataclass(frozen=True)
class PLCActor:
    username: str               # "plc:<channel name>"
    role: str = "PLC"
    clearance_level: int = 2

class PLCInputService:
    @classmethod
    def start(cls) -> None                  # sets the running flag, then reload()
    @classmethod
    async def stop(cls) -> None
    @classmethod
    def reload(cls) -> None                 # rereads every line's cards; one task per channel while running; clears alarms of cards that are gone or off
    @classmethod
    async def poll_once(cls, endpoint_id: str, now: Optional[float] = None) -> None   # one pass over a channel's cards
    @classmethod
    def get_status(cls, line_id: str) -> List[Dict[str, Any]]
        # per card: id, name, enabled, action, value, changed_at, last_read_at, read_count, error
    @classmethod
    async def read_now(cls, line_id: str, card: Dict[str, Any]) -> Dict[str, Any]   # {"ok", "value", "message"}
    @classmethod
    async def _act(cls, card: Dict[str, Any], old: Any, new: Any, count: int = 1) -> None   # filled in by Tasks 4 to 7
    @classmethod
    def reset(cls) -> None                  # test helper
```

- `AlarmCode.PLC_INPUT_FAILED = "plc.input_failed"` (catalog: group PLC, scope line, warning), `PLC_INPUT_ALARM = "plc.input_alarm"` (scope line, warning; the card's own severity is used when raised), `PLC_ACTION_NOT_CONFIRMED = "plc.action_not_confirmed"` (scope line, critical). Alarm source of a card: `plc_input:<card id>`, with `details.line_id` and `details.input_id`.
- `reload()` only starts tasks after `start()`. `tests/conftest.py` patches `PLCInputService.start` to do nothing, as it does for the fail-safe watchdog, so API tests never poll.

- [ ] **Step 1: Write the failing tests** (pure helpers first):

```python
def test_a_rising_edge_fires_once():
    f = SignalFilter(0.0)
    assert f.update(False, 0.0) is None                 # the starting point
    assert f.update(True, 0.1) == (False, True)
    assert f.update(True, 0.2) is None
    assert edge_matches("rising", False, True) and not edge_matches("rising", True, False)
    assert edge_matches("falling", True, False) and edge_matches("change", 3, 4)

def test_debounce_ignores_a_value_that_does_not_last():
    f = SignalFilter(0.2)
    f.update(False, 0.0)
    assert f.update(True, 0.1) is None                  # first seen
    assert f.update(False, 0.15) is None                # gone again
    assert f.update(True, 0.2) is None
    assert f.update(True, 0.45) == (False, True)        # still there 0.2 s later

@pytest.mark.parametrize("previous, current, delta", [(10, 13, 3), (65530, 4, 10), (7, 7, 0), (500, 0, 0), (0, 1001, 0)])
def test_the_counter_increase_wraps_and_ignores_a_reset(previous, current, delta):
    assert counter_delta(previous, current) == delta
```

  With a fake driver that has a `values` dict and a `read` that records the order of calls (extend `tests/test_plc_failsafe.py`'s `FakeDriver` and its `plc` fixture), and `_act` replaced by a recorder:
  - `test_the_first_read_is_the_starting_point_and_runs_no_action` (Review Focus 3): value already `True`, two passes, no action.
  - `test_an_edge_runs_the_cards_action_once`: `False`, `True`, `True` over three passes → one call with `(False, True)`.
  - `test_a_falling_and_a_change_card`.
  - `test_a_counter_card_reports_the_increase` and `test_a_counter_reset_in_the_plc_counts_nothing` (Review Focus 2): values `10, 13, 13, 0, 2` → counts `3` then `2`.
  - `test_status_shows_the_value_the_change_time_and_the_read_count`.
  - `test_a_read_error_raises_plc_input_failed_and_the_next_good_read_clears_it`.
  - `test_a_plc_that_is_down_is_retried_at_the_reconnect_interval_not_every_poll` (Review Focus 1): unreachable driver with `reconnect_interval: 5`; 20 passes with `now` 50 ms apart call `connect` once; a pass at `now + 5` calls it again; no `plc.input_failed` alarm; the status error is "PLC not connected".
  - `test_reads_never_come_between_a_cards_write_and_its_strobe`: use `slow_plc` from `tests/test_messages_and_plc_values.py`; start a WRITE card with a strobe and `poll_once` together; the recorded order has the read before the write or after the strobe, never between.
  - `test_a_disabled_card_is_not_read`.
  - `test_removing_a_signal_clears_its_alarms` (Review Focus 4): raise both alarms for a card, save the line without it, `reload()`; neither is active. The same when the card is switched off.
  - `test_reload_starts_no_task_before_start` and `test_one_task_per_channel_at_the_smallest_poll` (cards of 200 ms and 500 ms on one channel → one task, interval 0.2 s).
  - `test_a_line_without_signal_cards_is_not_polled`: after `start()` with lines that have no cards there is no task and no driver is asked for.
  - `test_read_now_reads_a_card_that_is_not_saved`.

- [ ] **Step 2: Run** them. Expected: fail (the module does not exist).

- [ ] **Step 3: Implement** the service.
  - `poll_once`: for each enabled card of the channel, take `PLCDispatcherService._endpoint_locks.setdefault(ep_id, asyncio.Lock())` **per read**, connect when needed (D10), `await asyncio.wait_for(driver.read(address, wire_type), timeout + 1)`. `wire_type` is the card's `data_type`, with `counter` read as `uint16`. On a timeout, disconnect the driver. When the driver is not connected after a failed read, call `PLCFailsafeService.mark_link_lost(ep_id)`.
  - A good read updates the status and clears `plc.input_failed`. A counter card calls `_act(card, old, new, count=counter_delta(...))` when the increase is above 0. `alarm` follows the level (D9). Every other card passes the value through its `SignalFilter` and calls `_act` when `edge_matches`; `run_level` and `set_batch` act on any change of the accepted value.
  - Actions run as tracked tasks; an exception in one is logged and never stops the poll.
  - The task of a channel sleeps the smallest `poll_ms` of its cards between passes.
  - `main.py`: `PLCInputService.start()` after the saved settings are restored (step 4); `await PLCInputService.stop()` in shutdown before `PLCDispatcherService.shutdown()`.
  - `apply_line` and the save and delete of a channel call `PLCInputService.reload()`.

- [ ] **Step 4: Run** `tests/test_plc_inputs.py tests/test_plc_failsafe.py tests/test_plc_dispatcher.py tests/test_alarm_events.py tests/test_health.py`. Expected: pass.

- [ ] **Step 5: Commit** `Add the PLC signal polling service`.

---

### Task 4: What a signal does, and its API

**Files:**
- Modify: `app/services/plc_input_service.py` (`_act`), `app/services/line_service.py`, `app/services/counting_service.py`, `app/routes/v1/lines.py`, `app/security/api_key_scopes.py`, `app/dependencies.py`
- Test: `tests/test_plc_inputs.py`

**Interfaces:**
- Consumes: `set_line_running(line_id, running, actor)`, `reset_line_counters(runtime, actor)`, `QRReaderPipeline.get_worker(camera_id).request_capture(...)`.
- Produces:
  - `CountingService.make_crossing(class_name: str, is_defect: bool = False, track_id: Optional[int] = None, confidence: float = 1.0, bbox: Optional[dict] = None, camera_id: Optional[str] = None) -> Dict[str, Any]`: the dict `_crossing` builds, for a product no tracker saw.
  - `CountingService.emit_crossing(crossing) -> None`: hand it to `event_sink`, else `finish_crossing`. `process_frame` uses it.
  - `LineRuntime.plc_inputs: List[dict]`, `LineRuntime.signal_readers: set`, `LineRuntime.has_product_sensor: bool`. `qr_triggered(camera_id)` is also true for a signal reader. `sync_enabled` needs a vision camera **or** a product sensor.
  - `LineRuntime.sensor_product() -> None`: one product from a sensor (class `"product"`, good, track id from `self._sensor_seq`). Nothing on a stopped line.
  - `GET /api/v1/lines/{line_id}/plc-inputs/status` (Operator) → `{"line_id", "running", "inputs": [...]}`.
  - `POST /api/v1/lines/{line_id}/plc-inputs/{input_id}/read` (Supervisor), optional body `{"input": {…card…}}` to read a card that is not saved yet → `{"ok", "value", "message"}`.
  - `_act` for: `start_line`, `stop_line`, `run_level`, `reset_counters`, `product_sensor`, `capture` (code reader), `alarm`.

- [ ] **Step 1: Write the failing tests.** Service tests call `PLCInputService._act` or `poll_once` with the fake driver; API tests use `client` and `admin_headers`.
  - `test_a_start_signal_starts_the_line_and_a_stop_signal_stops_it`: the line's `enabled` changes; the audit log has `START_LINE` / `STOP_LINE` with `username == "plc:Line PLC"` and `role == "PLC"`; a start signal on a running line writes no second audit entry.
  - `test_run_level_follows_the_value`: `True` starts, `False` stops.
  - `test_a_reset_signal_resets_the_counters` with an audit entry `RESET_COUNTERS` by `plc:Line PLC`.
  - `test_an_alarm_signal_raises_and_clears_its_alarm`: code `plc.input_alarm`, source `plc_input:<id>`, the card's text and severity, `details.line_id`; cleared when the value is false; an Alarm PLC card of the line that picked `plc.input_alarm` fires.
  - `test_a_sensor_product_is_counted_recorded_and_sent`: a line with no camera and a sensor card; three edges → `total_inspected == 3`, three events with `event_type == "crossing"`, `result == "good"`, class `"product"` and three different `event_id`s; each calls `record_event(payload, "product", counted=True)`.
  - `test_a_counter_sensor_counts_the_increase`: `_act(card, 10, 14, count=4)` → four products.
  - `test_sensor_products_keep_new_event_ids_after_a_counter_reset` (D5).
  - `test_a_sensor_product_pairs_with_its_code_and_gets_one_result`: a reader-only line with Sync and `accept_listed`; a sensor product and a read of a listed code within the window → one product, good, with `qr_code`; a sensor product with no read → one product, `reject`, reason `no_code` (the reader is set to reject).
  - `test_a_sensor_on_a_stopped_line_counts_nothing`.
  - `test_a_capture_signal_takes_one_picture_with_the_readers_delay`: `request_capture` is called once with `delay == 0.25` for a reader with `qr_trigger_delay_ms: 250`; `ContinuousVisionRunner` does not decode that reader's frames (`runtime.qr_triggered(reader)` is `True`).
  - `test_the_status_endpoint_and_read_now`: 200 for an Operator; `read` 403 for an Operator, 200 for a Supervisor; 404 for a line or a card that does not exist.
  - `test_api_key_scopes_for_signals`: `required_api_key_scopes("GET", "/api/v1/lines/line-1/plc-inputs/status") == {"monitor:read"}`; the `read` path gives `{"plc:configure"}`; a `PUT /api/v1/lines/line-1` whose body has `plc_inputs` is refused for a key without `plc:configure`.
  - `test_saving_signal_cards_through_the_api`: `PUT /lines/{id}` with `plc_inputs` returns them; a card with a bad poll time is a 422 that names the card.

- [ ] **Step 2: Run** them. Expected: fail.

- [ ] **Step 3: Implement.** `_act` builds its `PLCActor` from the channel's name. Starting, stopping and resetting do nothing when the line is already in that state. `sensor_product()` runs `count` times for a counter card.

- [ ] **Step 4: Run** `tests/test_plc_inputs.py tests/test_send_cards.py tests/test_production_records.py tests/test_reader_actions.py tests/test_qr_trigger.py tests/test_sparkplug_commands.py tests/test_lines_api.py`. Expected: pass.

- [ ] **Step 5: Commit** `Let PLC signals start, stop, reset, count and capture`.

---

### Task 5: Batch number

**Files:**
- Modify: `app/services/line_config.py`, `settings_persistence_service.py`, `counting_service.py`, `line_service.py`, `send_dispatcher_service.py`, `plc_dispatcher_service.py`, `plc_input_service.py`, `app/routes/v1/lines.py`
- Test: `tests/test_batch.py`

**Interfaces:**
- Produces:
  - `line_config.BATCH_MAX_LENGTH = 64` and `clean_batch(value: Any) -> str`: text, stripped. A whole number given as a float becomes its integer text (`1001.0` → `"1001"`). `None` → `""`. Raises `ValueError` for a control character or more than 64 characters.
  - `SettingsPersistenceService.set_line_batch(line_id: str, batch: Any) -> str`: cleans, saves in the line's entry, returns what was saved. `KeyError` for an unknown line.
  - `routes.v1.lines.set_line_batch(line_id: str, batch: Any, actor: Any) -> str`: saves, writes the audit entry `SET_BATCH` (category `PRODUCTION`), sets the running line's batch. `PUT /api/v1/lines/{line_id}/batch` with `{"batch": "…"}` (Supervisor) → `{"line_id", "batch"}`; 422 for a bad value.
  - `CountingService.batch: str = ""` and `LineRuntime.batch: str = ""` (the runtime sets it on its counter and every own-station counter, in `configure` and when the batch changes).
  - `MESSAGE_FIELDS["batch"] = ("batch",)`; `LineManager.summary(...)["batch"]`.

- [ ] **Step 1: Write the failing tests:**

```python
@pytest.mark.parametrize("value, text", [("  LOT-7 ", "LOT-7"), (1001, "1001"), (1001.0, "1001"), (12.5, "12.5"), (None, ""), ("", "")])
def test_a_batch_is_cleaned(value, text):
    assert clean_batch(value) == text

@pytest.mark.parametrize("bad", ["x" * 65, "LOT\n7", "LOT\x007"])
def test_a_batch_that_is_too_long_or_has_a_control_character_is_refused(bad):
    with pytest.raises(ValueError):
        clean_batch(bad)
```

  - `test_the_batch_survives_a_restart`: set it, load the settings file again, the line still has it. A `PUT /lines/{id}` with `{"batch": "X"}` in the body does not change it (D2).
  - `test_a_product_carries_the_batch`: with batch `"LOT-7"`, `finish_crossing` returns a payload and a PLC event with `batch == "LOT-7"`, and `build_row(payload, "product", True)["batch"] == "LOT-7"`. So do a code read (`_qr_events`) and a no-read (`_emit_no_read`).
  - `test_a_line_without_a_batch_sends_its_messages_as_before` (D11): `"batch" not in payload` and `"batch" not in plc_event`.
  - `test_an_own_station_and_a_joined_product_carry_the_batch`.
  - `test_the_batch_placeholder_and_field`: `render_template("{batch}", payload) == "LOT-7"`; `build_message({"fields": ["batch"]}, payload) == {"batch": "LOT-7"}`.
  - `test_a_plc_card_writes_the_batch`: `write_value` with source `batch` gives `1001.0` from the event; for a line start event (no batch in the event) it reads the line's batch; a batch that is not a number is still refused, naming it.
  - `test_the_examples_have_a_batch`: `dispatch_manual` of a card with `value_source: "batch"` succeeds; `sample_payload(card)["batch"]` is set for every trigger.
  - `test_a_line_start_message_carries_the_batch`.
  - `test_the_batch_endpoint`: Supervisor 200 and an audit entry `SET_BATCH`; Operator 403; 65 characters 422; unknown line 404; `GET /lines/{id}` and `/lines/overview` show the batch.
  - `test_a_set_batch_signal_sets_the_batch` with the audit user `plc:Line PLC`.
  - `test_a_batch_from_the_plc_is_cleaned_or_refused` (Review Focus 5): `_act` with `1001.0` → `"1001"`; with a 200-character string → the batch is unchanged, the card's status error says why, and the next pass still reads; the same for a control character.

- [ ] **Step 2: Run** `tests/test_batch.py`. Expected: fail.

- [ ] **Step 3: Implement.** `normalize_line` copies `batch` from `base` only. `finish_crossing`, `_qr_events`, `_emit_no_read` and `SendDispatcherService._with_totals` add `batch` when the line has one. `write_value` falls back to the line's batch as it does for the counts. The sample event of `dispatch_manual` gets `"batch": "1"` and `sample_payload` gets `"batch": "B-1001"`.

- [ ] **Step 4: Run** `tests/test_batch.py tests/test_send_cards.py tests/test_messages_and_plc_values.py tests/test_production_records.py tests/test_joined_cameras.py tests/test_sparkplug_metrics.py`. Expected: pass.

- [ ] **Step 5: Commit** `Add the line's batch number to products, records, messages and PLC values`.

---

### Task 6: Trigger inspection

**Files:**
- Create: `app/services/trigger_inspection.py`
- Modify: `app/schemas/counting.py`, `app/services/line_config.py`, `settings_persistence_service.py` (`counting_config_from_dict`), `counting_service.py`, `line_service.py`, `vision_service.py`, `plc_input_service.py`, `app/routes/v1/lines.py`, `app/routes/v1/control.py`, `app/security/api_key_scopes.py`
- Test: `tests/test_trigger_inspection.py`

**Interfaces:**
- Consumes: `make_crossing`, `emit_crossing` (Task 4).
- Produces:
  - Camera keys `count_mode` (`"track"` | `"trigger"`; saved only when `"trigger"`) and `no_product` (`"ignore"` | `"reject"` | `"good"`, default `"ignore"`; saved only in trigger mode). Both in `CAMERA_KEPT_KEYS`. `line_config.COUNT_MODES`, `NO_PRODUCT`.
  - `CountingConfig.count_mode: str = "track"` and `no_product: str = "ignore"`.
  - `line_config.REASON_NOTHING_DETECTED = "nothing_detected"`, in `REJECT_REASONS`; `REJECT_REASON_CODES[REASON_NOTHING_DETECTED] = 6`.
  - `_CameraInferenceWorker.result_seq: int`: goes up by one with every processed frame (a frame id starts again when a camera reconnects).
  - In `trigger_inspection.py`:

```python
TRIGGER_TIMEOUT_S = 2.0
NOTHING_CLASS = "nothing detected"

def inspection_verdict(detections: List[Any], config: CountingConfig) -> Optional[Dict[str, Any]]
    # {"class_name", "is_defect", "confidence", "bbox", "reject_reason"}; None when nothing was found and no_product is "ignore".
    # A defect class wins. Else a product class (any detection when the product list is empty) is good.
    # Else no_product: "reject" -> is_defect True, class NOTHING_CLASS, reject_reason "nothing_detected";
    #                  "good"   -> is_defect False, class "product".

async def trigger_camera(runtime: LineRuntime, camera_id: str, timeout: float = TRIGGER_TIMEOUT_S) -> Dict[str, Any]
    # {"camera_id", "status": "inspected" | "nothing" | "no_frame", "result": "good" | "reject" | None,
    #  "class_name", "reject_reason", "track_id", "detections": int, "inference_time_ms", "waited_ms"}
    # Raises ValueError (a 409 in the routes) when the line is stopped, the camera is not connected,
    # or it is not a vision camera in trigger mode.

def trigger_cameras(runtime: LineRuntime) -> List[str]      # the line's vision cameras in trigger mode
```

  - `POST /api/v1/lines/{line_id}/trigger` (Supervisor; scope `inspection:trigger`), body `{"camera_id": optional}` → `{"line_id", "results": [...]}`; 409 when the line has no camera in trigger mode.
  - `/api/v1/control/trigger/{camera_id}`: for a camera in trigger mode it runs `trigger_camera` and answers `{"status": "triggered", "camera_id", "inspection_result": {…the same keys as before, from the judged frame…}, "product": {…trigger_camera's answer…}}`. For any other camera it answers exactly as before.
  - A crossing may carry `reject_reason`; `finish_crossing` and `LineRuntime._finish_product` use it in place of `vision_class` for that camera's reject.
  - `_act` for `capture` naming a vision camera in trigger mode.

- [ ] **Step 1: Write the failing tests:**

```python
def _det(name, conf=0.9): ...   # a DetectionItem

@pytest.mark.parametrize("names, expected, defects, no_product, verdict", [
    (["bottle"], ["bottle"], ["dent"], "ignore", ("bottle", False, None)),
    (["bottle", "dent"], ["bottle"], ["dent"], "ignore", ("dent", True, None)),
    (["cap"], [], ["dent"], "ignore", ("cap", False, None)),               # empty product list: any detection is a product
    (["scratch"], ["bottle"], [], "ignore", ("scratch", True, None)),      # the name rule, on by default
    ([], ["bottle"], ["dent"], "ignore", None),
    (["cap"], ["bottle"], ["dent"], "ignore", None),                       # neither a product nor a defect
    ([], ["bottle"], ["dent"], "reject", ("nothing detected", True, "nothing_detected")),
    ([], ["bottle"], ["dent"], "good", ("product", False, None)),
])
def test_what_one_picture_says(names, expected, defects, no_product, verdict): ...
```

  - `test_the_new_reason_has_a_number`: `REJECT_REASON_CODES["nothing_detected"] == 6` and it is in `REJECT_REASONS`.
  - `test_count_mode_is_kept_when_a_client_leaves_it_out`, `test_a_camera_in_track_mode_saves_as_before` (no `count_mode` key) and `test_count_mode_validation`.
  - `test_a_camera_in_trigger_mode_counts_nothing_from_its_tracker`: `process_frame` with a tracker that reports a crossing counts nothing; in track mode it counts one.
  - With a fake worker (`result_seq`, `is_busy`, `get_latest_inference`) patched into `CameraStreamPipeline.get_worker`:
    - `test_a_trigger_counts_one_good_product` and `test_a_trigger_counts_one_reject`: totals, the payload's `track_id` (1, then 2), the record row, and a `cross_line` / `reject` PLC card fires.
    - `test_nothing_found` for each of the three settings; the `reject` one has `reject_reason == "nothing_detected"` in the payload, the record and the PLC event.
    - `test_no_new_frame_in_two_seconds_gives_no_result`: `status == "no_frame"`, nothing counted (pass `timeout=0.1`).
    - `test_a_trigger_while_the_worker_is_busy_skips_the_frame_in_flight` (D12): the result used is the second after the trigger.
    - `test_a_trigger_on_a_stopped_line_or_a_disconnected_camera_is_refused`: `ValueError`, nothing counted.
    - `test_a_triggered_counting_camera_goes_through_sync_and_joined_cameras`: with Sync on, the product waits for its code and gets one result; a joined camera in trigger mode joins the counting camera's product.
    - `test_an_own_station_in_trigger_mode_counts_on_its_own_counter`.
  - `test_the_line_trigger_endpoint`: all trigger cameras by default, one by `camera_id`, 409 for a camera in track mode, Operator 403; `required_api_key_scopes("POST", "/api/v1/lines/line-1/trigger") == {"inspection:trigger"}`.
  - `test_control_trigger_still_answers_as_before` (a camera in track mode: the old keys only, nothing counted) and `test_control_trigger_counts_for_a_camera_in_trigger_mode` (the old keys plus `product`).
  - `test_a_capture_signal_triggers_a_vision_camera_in_trigger_mode`; and a `capture` card naming a vision camera in track mode is refused when the line is saved.

- [ ] **Step 2: Run** `tests/test_trigger_inspection.py`. Expected: fail.

- [ ] **Step 3: Implement.** In trigger mode `process_frame` returns before the tracker runs. `trigger_camera` notes `result_seq` (plus one when the worker is busy), waits in 10 ms steps until it is passed or the timeout, reads `get_latest_inference()`, and calls `counter.emit_crossing(counter.make_crossing(...))` with a track id from `runtime._trigger_seq`. The verdict uses `is_defect_class(name, config.defect_classes, config.name_based_defects)` from `app/engines/tracker.py`.

- [ ] **Step 4: Run** `tests/test_trigger_inspection.py tests/test_vision_pipeline.py tests/test_camera_cards.py tests/test_logic_fixes.py tests/test_joined_cameras.py tests/test_session_and_continuous_vision.py tests/test_api.py`. Expected: pass.

- [ ] **Step 5: Commit** `Add trigger inspection: one picture per trigger for vision cameras`.

---

### Task 7: Reject confirmation

**Files:**
- Modify: `app/services/plc_failsafe_service.py`, `line_config.py`, `settings_persistence_service.py`, `plc_dispatcher_service.py`, `plc_input_service.py`, `app/schemas/plc.py` (the status comment)
- Test: `tests/test_reject_confirmation.py`

**Interfaces:**
- Produces:
  - PLC card keys `confirm_input_id` (text; `""` = no confirmation) and `confirm_timeout_ms` (whole number, 50 to 10000, default 1000). Both in `PLC_CARD_KEPT_KEYS`.
  - `plc_failsafe_service.validate_confirmation(card: dict) -> None`, called wherever `validate_write_value` is called when cards are saved.
  - `SettingsPersistenceService._check_confirmation_links(cards, inputs) -> None`: `ValueError` when a card names a signal that is not on the line, or one whose action is not `confirm_card`. Run by `save_line` and `replace_line_plc_actions`, so deleting a signal that an action still names is refused, naming the action.
  - `PLCDispatcherService.confirm_signal(line_id: str, input_id: str) -> int`: confirms the oldest waiting fire of every card of that line that names the signal; returns how many.
  - Card statuses `confirmed` and `not_confirmed`; `last_result["confirmed"]: bool`.
  - `_act` for `confirm_card` calls `confirm_signal`.

- [ ] **Step 1: Write the failing tests** (the `plc` fixture and `FakeDriver` from `tests/test_plc_failsafe.py`):
  - `test_confirmation_settings_are_checked`: timeouts 49, 10001, `"soon"` and `1.5` are refused; 50 and 10000 pass; a card without the keys passes.
  - `test_a_card_keeps_its_confirmation_when_a_client_leaves_it_out`.
  - `test_a_card_must_name_a_confirm_signal_of_its_own_line`: a missing signal and a `start_line` signal are refused; `test_a_signal_an_action_names_cannot_be_deleted`.
  - `test_a_confirmed_fire`: fire the card, call `confirm_signal` 20 ms later → status `confirmed`, `last_result["confirmed"] is True`, no alarm.
  - `test_a_fire_that_is_not_confirmed_raises_the_alarm_and_the_next_confirmed_fire_clears_it`: `confirm_timeout_ms: 50`, no signal → status `not_confirmed`, alarm `plc.action_not_confirmed` (critical, source `plc_card:<id>`, `details.line_id`); fire again and confirm → cleared.
  - `test_two_fires_waiting_are_confirmed_in_order`: two fires, one signal → the first is `confirmed`, the second still waits; a second signal confirms it.
  - `test_a_signal_that_comes_during_the_pulse_counts`: a slow pulse; `confirm_signal` while the operation is still running → `confirmed`.
  - `test_a_failed_write_waits_for_no_confirmation`: status `failed`, no confirmation alarm, and a later signal confirms nothing.
  - `test_a_card_with_a_strobe_waits_only_after_its_strobe`.
  - `test_the_test_button_does_not_wait_for_the_signal` (D13): status `sent`, no alarm.
  - `test_an_old_wait_for_ack_card_still_reports_sent`.
  - `test_a_confirm_signals_edge_confirms_through_the_poller`: `poll_once` with the value going `False` → `True`.

- [ ] **Step 2: Run** `tests/test_reject_confirmation.py`. Expected: fail.

- [ ] **Step 3: Implement.** `_dispatch` puts a future in the card's queue **before** it runs the operation, takes it out again when the operation fails, and after a success waits for it up to `confirm_timeout_ms`. The failure alarms are cleared as for `sent`; the confirmation alarm is raised and cleared on its own. A card that was deleted or switched off has its waiting fires dropped in `set_cards`.

- [ ] **Step 4: Run** `tests/test_reject_confirmation.py tests/test_plc_dispatcher.py tests/test_plc_failsafe.py tests/test_plc_action_persistence.py tests/test_messages_and_plc_values.py tests/test_error_alarms.py`. Expected: pass (but the known Windows failure in `test_error_alarms`).

- [ ] **Step 5: Commit** `Add reject confirmation to PLC action cards`.

---

### Task 8: Dashboard

**Files:**
- Create: `assets/plc_signals.js`
- Modify: `dashboard.html` (the PLC signals panel, the script tag, the PLC action dialog, `PLC_VALUE_SOURCE_HINTS`, `sendSampleMessage`), `assets/production_lines.js` (camera card, Line dashboard), `assets/dashboard.css` (only if a class is missing)
- Test: by hand in the scratch copy, with screenshots in `docs/progress/screenshots/`

**Interfaces:**
- Consumes: `GET /lines/{id}` (`plc_inputs`, `batch`), `PUT /lines/{id}` with `{plc_inputs}`, `GET …/plc-inputs/status`, `POST …/plc-inputs/{id}/read`, `PUT …/batch`.
- Produces: `window.PlcSignals = { load(lineId), render(), add(), apply(), openDialog(id), readNow(id), confirmSignals() }`. `confirmSignals()` gives the PLC action dialog the line's `confirm_card` signals as `[{id, name}]`.

- [ ] **Step 1: The PLC signals step.** A panel after "PLC actions", titled **PLC signals (inputs)**, built like it: Cards / List, **Add signal**, **Apply**, and the same "Unsaved changes / Applied to server" label. Each card shows the name, channel, address, action and three chips: last value, last change, error (from the status call, every second while the tab is shown and the page is visible). The dialog has every field of Task 2; the camera select shows only for **Take a picture**, the text and severity only for **Raise an alarm**, the edge only for the actions that use one. It has a **Read now** button that shows the value or the error, and two notes: a Generic TCP channel cannot be read; a sensor faster than the poll time needs the **PLC counter** type. For **Confirm a PLC action** the dialog lists the actions that name the signal.

  Names of the actions, in this order: Start the line, Stop the line, Run the line while on, Reset the counters, Product sensor, Take a picture, Set the batch number, Confirm a PLC action, Raise an alarm.

- [ ] **Step 2: The PLC action dialog** gets **Confirmation signal** (None, or one of `confirmSignals()`) and **Confirm within (ms)** (50 to 10000, shown only with a signal). `PLC_VALUE_SOURCE_HINTS.reject_reason_code` gains "6 nothing detected". The card and list views show `confirmed` and `not confirmed`.

- [ ] **Step 3: The camera card** (Counting fold of a vision camera) gets **Count mode**: *Moving products cross count lines* (default) / *One picture per trigger*, and under it, only in trigger mode, **When nothing is found**: *Ignore* / *Reject* / *Good*. In trigger mode the count lines, direction and tracking fields are hidden. A reader's **Capture** select gets *When a PLC signal asks*. `cardEntry` sends `count_mode` and `no_product`.

- [ ] **Step 4: The Line dashboard** shows **Batch** next to the line state: the text for an Operator, a field with **Set** for a Supervisor. `sendSampleMessage` gets `batch: "B-1001"`, the same as `sample_payload`.

- [ ] **Step 5: Check it in the scratch copy** (see the memory note on live dashboard testing; never from the repository folder), with `test codes/plc_simulator.py` on a free port as a Modbus TCP channel:
  - add a start signal on a coil, Apply, write the coil with a Modbus client, and see the line start and the chips change;
  - Read now shows the coil's value; on a Generic TCP channel it shows "Reading is not supported";
  - set the batch on the Line dashboard and see it on the Production records page after a product;
  - save a camera in trigger mode and reload: both fields are kept;
  - a line with no signal cards looks and saves as before.

  Take screenshots at 1440 px and 375 px of: the PLC signals step, its dialog, the batch field. Save them as `docs/progress/screenshots/s5-*.png`.

- [ ] **Step 6: Run** the whole suite and `ruff check .`. Commit `Add the PLC signals step, count mode and the batch field to the dashboard`.

---

### Task 9: End to end, docs and the final pass

**Files:**
- Create: `tests/test_plc_inputs_e2e.py`
- Modify: `README.md`, `API_KEY_QUICKSTART.md`, `FILE_TREE.md`, `PROJECT_DESCRIPTION.md`, `FUTURE_FIXES.md`, `docs/progress/README.md`, `docs/progress/section-5-plc-inputs.md`

- [ ] **Step 1: Write `test_a_coil_starts_the_line_and_a_register_counts_products`.** Start `test codes/plc_simulator.py` as a subprocess on a free port (wait until the port accepts a connection; stop it in a `finally`). With the `lines` fixture, a real `ModbusTCPDriver` channel, a line with a `start_line` card on `C5` and a `product_sensor` card on `R10` with type `counter`, and a recorder with a temporary database: drive `PLCInputService.poll_once`; write the coil and the register from the test with a second `ModbusTCPClient`. Assert: the line is stopped, then running after the coil goes on; after the register goes 0 → 3 → 5 the line's total is 5 and five `product` rows with `counted` true are in `product_records`, each with the batch set before the run.

- [ ] **Step 2: Run** it. Expected: pass. Then run the whole suite and `ruff check .`; compare the failures with the Windows baseline.

- [ ] **Step 3: Live run from the scratch copy** with the simulator: toggle the coil and step the register from a Modbus client, watch the counts on the Line dashboard, and export the records for the last hour as CSV; the rows have the batch.

- [ ] **Step 4: Docs.**
  - `README.md`: a new part **PLC signals (inputs)**; batch, trigger mode and reject confirmation in the Line setup steps; in **PLC actions that write a value**, update *Batch number*, *Reject reason code* (6), the **Test** example values and the sentence on the removed *Wait for PLC ACK / Reply*. Then read the whole "Production lines" part once more so it reads as one story.
  - `API_KEY_QUICKSTART.md`: the four new endpoints and their scopes (D14).
  - `FILE_TREE.md`: the new files. `PROJECT_DESCRIPTION.md`: the PLC is no longer output-only.
  - `FUTURE_FIXES.md`: what the five sections did; what still needs a real line or PLC (each driver's read against real hardware, the poll time on a loaded PLC, trigger timing on a real indexing machine, confirmation on a real reject gate).
  - `docs/progress/section-5-plc-inputs.md`: Status, and Notes with decisions D1 to D16 and the known limits. `docs/progress/README.md`: Section 5 **Done** with the branch; the map rows for the new files; the test count.
  - Optional: a Sparkplug tag `Line/Batch`, only if it is one more metric in the line's list and the rebirth happens by itself; otherwise note it in `FUTURE_FIXES.md`.

- [ ] **Step 5: Commit** `Section 5: end-to-end test and docs`. Ask the owner before pushing; after the push, check that GitHub Actions (lint, tests, Docker) are green.
