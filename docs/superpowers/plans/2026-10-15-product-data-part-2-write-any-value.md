# Part 2: Write any value. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An output action can write any of these:
- a field of the item's product;
- the code;
- the product name;
- the last value an input read;
- the result of a small formula.

When there is no value, it skips the write or writes a default. Text goes to OPC UA String tags.

**Architecture:**
- `app/services/formula.py` is a small expression language. It is parsed with `ast` against a whitelist and computed by a tree walker; no user text is ever passed to `eval` or `exec`.
- `app/services/event_values.py` gives every event the spec's one set of value names.
- `PLCDispatcherService.write_value` reads its value from those names.
- A missing value raises `MissingValue`, which `_dispatch` turns into "skipped" or the default.
- Input cards get an optional `value_name`, so their last value can be written.
- The OPC UA driver writes String nodes. Every other driver refuses text.

**Tech stack:** Python 3.12+ (`ast`, `tokenize`), FastAPI, asyncua; plain JavaScript in the dashboard.

**Spec:** [docs/superpowers/specs/2026-10-15-product-data-values-rules-design.md](../specs/2026-10-15-product-data-values-rules-design.md), section 1 (values, formula language) and section 3. This part needs [part 1](2026-10-15-product-data-part-1-products-with-data.md) to be done first: it uses `event["product"]`, `VALUE_NAME_RE`, `flat_product_values` and `ProductCatalog.fields`. [Part 3](2026-10-15-product-data-part-3-when-then-rules.md) builds on `formula.compare` and `event_values.values_for`.

## Global Constraints

**Branch and commits**
- Work on the branch `claude/product-data-rules`, after part 1.
- Commit after each task. Never push.

**Never break an existing line**
- A card without the new settings writes exactly what it writes today.
- Existing value sources fail exactly as they do today: `failed`, with an alarm.
- `SCHEMA_VERSION` stays **8**.

**Never `eval`, `exec` or `compile`** a formula or any text from a user.

**Limits (from the spec)**
- A formula is at most **200** characters.
- Value names follow `VALUE_NAME_RE`: letters, digits and `_`, starting with a letter, at most **32** characters, case-sensitive.
- yes / no values are written as **1 / 0**.
- Text is written to **OPC UA String nodes only**. Other controllers refuse it with "This controller type writes numbers only".

**Running tests**
- Full suite: `bash backup/scratch-tools/pt.sh`
- One file: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/<file>.py`
- Lint: `.venv/Scripts/python.exe -m ruff check .`
- Mutation checks: `backup/scratch-tools/mutcheck.py`

**Files and editing**
- Keep each file's line endings and any BOM. Check `git diff --stat`.

**Live testing and data safety**
- Never start the server from the repository folder. Use the scratch copy on 127.0.0.1:8765, and stop it by its PID.
- The owner's PLCSIM (`opc.tcp://192.168.0.1:4840`) is **read only**. Every write test against it needs the owner's OK, asked each time. It also needs an Int tag and a String tag that the owner adds in TIA Portal.
- Leave the owner's MQTT broker on port 1883 running.

**Dashboard**
- Put user values into the page with `esc` / `escapeHtml` or `data-` attributes, never inside quoted strings in `on*=` handlers. Use `encodeURIComponent` in URLs.
- Raise the `?v=` of changed assets to `2.8.0`.
- Check pages at 1440 px and at 375 px, with no page errors and no CSP violations.

**Text and docs**
- Use plain, short English. Never put a model name in the repository. Commit messages end with the session's attribution lines.
- Update the parts of the docs that a change touches.

## Decisions this plan makes

| # | Decision | Why |
|---|---|---|
| W1 | Python keywords such as `if` and `class` cannot be parsed as names. Before parsing, each NAME token that is a Python keyword (except `and`, `or` and `not`) is renamed to `_kw_<word>`. `_kw_if` is the `if()` function and `_kw_class` is the value `class`. Any other `_kw_` name is "unknown value '<word>'". A name the user typed with a leading `_` is refused. Error positions are given in the text as it was typed. | The spec wants `if(...)` and `class`. `product.from` works the same way. |
| W2 | **Tokens allowed:** NAME, NUMBER (plain decimal, with an optional exponent), STRING (double quotes, no prefix), OP (`+ - * / % == != < <= > >= ( ) , .`), NEWLINE and ENDMARKER. **AST nodes allowed:** `Expression`, `BoolOp` (And / Or), `UnaryOp` (Not / USub / UAdd), `BinOp` (Add / Sub / Mult / Div / Mod), `Compare` (one operator only), `Call` (a known function, positional arguments only), `Name`, `Attribute` (root `product` or `input`, one level), `Constant`. Anything else is refused as "… is not allowed". A formula may contain **at most 64 nodes**. | A whitelist at both levels. 64 nodes is ample within 200 characters. |
| W3 | **Arithmetic** needs numbers; yes / no counts as 1 / 0. Text `+` text joins the two texts. Any other mix, division or `%` by zero, or a result that is not finite gives **MISSING**. | This is the spec's "type mismatch makes the result missing". Joining texts builds codes. |
| W4 | **One comparison rule,** `compare(left, op, right)`, for formulas and for part 3's rules: <br>• numbers, or text that reads as a decimal number, compare as numbers; <br>• yes / no against `yes` / `no` / `true` / `false` / `1` / `0` compares as yes / no (ordering is false); <br>• everything else compares as text, ignoring case; <br>• a MISSING side gives MISSING. | One meaning for `=` everywhere. The spec makes rules ignore case, so formulas do too. |
| W5 | `and`, `or` and `not` give true / false from truthiness. A MISSING operand gives MISSING. `if()` computes only the branch it takes. | This is predictable. |
| W6 | A MISSING value is skipped or replaced with the default **only for the five new sources.** The existing sources keep their errors (`failed` plus an alarm). | That is how existing cards behave today. |
| W7 | A skipped write gives the card status `"skipped"`, with `last_result = {"success": False, "skipped": True, "message": "Skipped: no value for product.lane"}`. It raises no alarm and sends nothing. | Spec, section 3. |
| W8 | Text refusal happens in **one place**: `PLCDriver.execute_operation` raises `ValueError("This controller type writes numbers only")` when the driver's `writes_text` is false. Only `OPCUADriver` sets it to true. The dispatcher's existing `ValueError` branch shows the message and does not retry. | One check covers every driver. |
| W9 | **OPC UA String node:** text is written as a String. A number is written as its text (`3`, not `3.0`). Text longer than **254** characters is refused before writing (a Siemens STRING holds at most 254). If the node is shorter, the server refuses and its answer is shown. Text to a numeric or Boolean node is refused: "{address} is Int16; text cannot be written to it". | The spec only says "length checked"; 254 is the hard limit on the owner's PLCs. |
| W10 | `value_name` is part of **this** part, not part 3: it is an optional setting on any input card except the PLC counter type. `PLCInputService.values(line_id)` returns each card's last **accepted** (debounced) value. | The `input` value source needs it. |
| W11 | The Test button takes an optional `code`. The example event then carries that code's product: the first list of the line's code-reading cameras that has the code, in camera order. | Otherwise a product-field card could only ever test as "skipped". |
| W12 | `POST /api/v1/lines/{line_id}/formula/check` (supervisor) checks a formula. It answers `200 {"ok": true, "names", "warnings"}` or `200 {"ok": false, "error", "position"}`. The warnings name field keys that are in no list of the line's code readers, and input names the line does not have. Saving refuses only errors; the warnings show only here. | Spec: unknown fields are a warning, because lists change. |

## Review Focus

1. **A field is deleted or retyped after a card that writes it was saved.** Expected: the card shows "Skipped: no value for product.lane". There is no alarm and no crash. Test: Task 4 `test_a_missing_value_skips_the_write_without_an_alarm`.
2. **Text sent to a numeric node, to a Modbus or S7 controller, or text longer than 254 characters.** Expected: `failed`, with a clear message, no retry and nothing written. Tests: Task 4 `test_text_goes_only_to_a_controller_that_writes_text`; Task 5 `test_string_writes`.
3. **Formula text that tries Python tricks**: `__import__`, `().__class__`, `"a" * 10**9`, f-strings, single quotes, `lambda`, a newline. Expected: refused when the card is saved, and the message names the position. Test: Task 1 `test_hostile_or_wrong_text_is_refused`.
4. **A fraction written to an integer node**, such as `product.lane / 2` = 1.5 to an Int16. Expected: the existing "requires an integer value" refusal. Test: Task 5 `test_string_writes` (the Int16 part).
5. **An input that has not been read yet, or whose PLC is down.** Expected: the `input` source is missing, so the card skips or writes its default. Test: Task 4 `test_an_unread_input_is_missing`.

---

### Task 1: The formula language (`formula.py`)

**Files:**
- Create: `app/services/formula.py`
- Test: `tests/test_formula.py` (new)

**Interfaces:**
- Produces:
  - `MISSING`: a singleton whose repr is `"missing"`.
  - `FormulaError(ValueError)` with `.position: Optional[int]` (a 1-based column).
  - `MAX_FORMULA_LENGTH = 200`, `MAX_NODES = 64`.
  - `PLAIN_NAMES`: `result`, `result_code`, `reject_reason`, `class`, `confidence`, `code`, `code_status`, `good_count`, `reject_count`, `total_count`, `batch`, `line`, `camera`.
  - `ROOTS = ("product", "input")`.
  - `FUNCTIONS`: `if`, `round`, `min`, `max`, `abs`, `int`, `text`.
  - `parse(text: str) -> Formula`, cached with `functools.lru_cache(maxsize=256)`.
  - `Formula.names: FrozenSet[str]`, such as `{"class", "product.lane", "input.speed"}`.
  - `Formula.evaluate(values: Mapping[str, Any]) -> Any` (it can return `MISSING`).
  - `compare(left, op: str, right) -> Union[bool, MISSING]` for `op` in `== != < <= > >=` (decision W4).

  Part 3 uses `compare`.

- [x] **Step 1: Write the failing tests.**

  ```python
  V = {"result": "good", "class": "Apple", "confidence": 0.93, "good_count": 7, "reject_count": 1, "code": "J-1",
       "product.lane": 3, "product.recipe": "R-12", "product.fragile": True, "input.speed": 12.5}

  @pytest.mark.parametrize("text, value", [
      ('if(result == "good", product.lane, 9)', 3), ('if(result == "reject", product.lane, 9)', 9),
      ("product.lane * 10 + 1", 31), ("good_count / 2", 3.5), ("good_count % 4", 3), ("-product.lane", -3),
      ("round(confidence * 100)", 93), ("round(input.speed / 4, 1)", 3.1), ("min(product.lane, 2, 5)", 2),
      ("max(1, good_count)", 7), ("abs(0 - 4)", 4), ("int(input.speed)", 12), ('int("42")', 42),
      ('text(product.lane) + "-" + product.recipe', "3-R-12"), ('class == "apple"', True),
      ("product.fragile == true", True), ("not product.fragile", False), ("product.fragile + 1", 2),
      ("good_count > 5 and reject_count < 2", True), ('code != "J-2"', True), ('"10" > 9', True),
  ])
  def test_formulas_compute(text, value):
      assert parse(text).evaluate(V) == value

  @pytest.mark.parametrize("text", ["product.missing + 1", "good_count / 0", "good_count % 0", "product.recipe * 2",
                                    '"a" + 1', "int(product.recipe)", "if(product.missing > 1, 1, 2)", "1e308 * 10",
                                    "product.missing and true"])
  def test_missing_values_and_bad_types_give_missing(text):
      assert parse(text).evaluate(V) is MISSING

  def test_if_only_computes_the_branch_it_takes():
      assert parse('if(result == "good", 1, product.missing)').evaluate(V) == 1

  def test_names_and_keywords():
      assert parse("if(class == product.expected_class, input.speed, 0)").names == frozenset(
          {"class", "product.expected_class", "input.speed"})
      assert parse("product.from + 1").evaluate({"product.from": 1}) == 2

  @pytest.mark.parametrize("text, word", [
      ('__import__("os")', "not a value"), ("().__class__", "not a value"), ("(1).real", "only product"),
      ("product.lane.real", "one level"),
      ("speed + 1", "unknown value 'speed'"), ("eval(1)", "unknown function 'eval'"), ("2 ** 8", "not allowed"),
      ("'x'", "double quotes"), ('f"{1}"', "not allowed"), ("lambda: 1", "not allowed"), ("[1][0]", "not allowed"),
      ("1 < good_count < 3", "two values"), ("round(1, digits=1)", "not allowed"), ("round()", "takes"),
      ("if(1, 2)", "takes"), ("1\n+1", "one line"), ("", "empty"), ("1" * 201, "200 characters"),
      ("True", "unknown value 'True'"), ("_kw_if(1, 2, 3)", "not a value"), ("1 +", "position"),
  ])
  def test_hostile_or_wrong_text_is_refused(text, word):
      with pytest.raises(FormulaError, match=re.escape(word)):
          parse(text)

  def test_an_error_names_the_position_as_typed():
      with pytest.raises(FormulaError) as caught:
          parse("good_count + 'x'")
      assert caught.value.position == 14

  @pytest.mark.parametrize("left, op, right, want", [
      (3, "==", 3.0, True), ("3", "==", 3, True), ("Apple", "==", "apple", True), (True, "==", "yes", True),
      (False, "==", "No", True), (True, "<", 2, False), ("10", ">", "9", True), ("b", ">", "A", True),
      (MISSING, "==", 1, MISSING), (" good", "==", "good", False),
  ])
  def test_one_comparison_rule(left, op, right, want):
      assert compare(left, op, right) == want if want is not MISSING else compare(left, op, right) is MISSING
  ```

- [x] **Step 2: Run the tests and make sure they fail.**

  Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_formula.py`

  Expected: FAIL (`ModuleNotFoundError`).

- [x] **Step 3: Implement it** with decisions W1 to W5.
  - **Order of checks.** First the length, the newline and the empty text. Then the tokens (with exact positions). Then the keyword renaming, and building the text that `ast.parse(mode="eval")` reads, with a column map from the new text back to the typed text. Then the walk over the node whitelist, which also checks names and function argument counts.
  - **Argument counts.** `if` takes 3. `round` takes 1 or 2, with digits from 0 to 6. `min` and `max` take 1 or more. `abs`, `int` and `text` take 1.
  - **`text()`.** A whole number has no `.0`. `true` and `false` stay as they are.

- [x] **Step 4: Run the tests and make sure they pass.** Run the same command. Expected: PASS.

- [x] **Step 5: Commit.** Message: "A small formula language for written values (no eval)".

### Task 2: One set of values per event, and input value names

**Files:**
- Create: `app/services/event_values.py`
- Modify: `app/services/counting_service.py` (`finish_crossing`: add `"confidence"` to `plc_event`), `app/services/line_config.py` (`normalize_plc_input` and `normalize_plc_inputs`: `value_name`), `app/services/plc_input_service.py` (`values`)
- Test: `tests/test_event_values.py` (new), `tests/test_plc_inputs.py`

**Interfaces:**
- Consumes: part 1 `flat_product_values` and `VALUE_NAME_RE`.
- Produces:
  - `values_for(event: Dict[str, Any]) -> Dict[str, Any]`. It has only the names that have a value; anything else is missing. It follows the spec's table:
    - `result` and `result_code` only when `event["result"]` is `good` or `reject`;
    - `reject_reason` only for a reject;
    - `class` from `class_name`, or else from `detected_classes[0]`;
    - `confidence`;
    - `code` from `qr_code`, and `code_status` from `qr_status`;
    - `good_count` and `reject_count` from the event, or else from the line's counter, and `total_count` as their sum;
    - `batch` from the event, or else from the line;
    - `line`, the line's name;
    - `camera`, the camera's name, or its id when it has no name;
    - the `product.*` names;
    - `input.<value_name>` from `PLCInputService.values(line_id)`.
  - Input cards get `value_name`. It is optional, follows `VALUE_NAME_RE`, and is unique within a line. It is refused for the `counter` data type. The names `value` and `previous` are refused too, because part 3 uses `input.value` and `input.previous` for the input that changed.
  - `PLCInputService.values(line_id) -> Dict[str, Any]`, shaped `{value_name: last accepted value}`.

  Part 3 adds `input.value` and `input.previous`.

- [x] **Step 1: Write the failing tests.**

  ```python
  def test_a_product_event_gives_every_value(lines, monkeypatch):           # lines: tests/test_send_cards.py
      line = SettingsPersistenceService.save_line({"name": "Values", "cameras": [...one vision camera...]})
      lines.apply_state(); runtime = lines.get(line["id"]); runtime.set_batch("B-7")
      monkeypatch.setattr(PLCInputService, "values", classmethod(lambda cls, line_id: {"speed": 12}))
      event = {"event_type": "crossing", "line_id": line["id"], "camera_id": "top", "result": "reject",
               "reject_reason": "vision_class", "good_count": 4, "reject_count": 2, "detected_classes": ["Apple"],
               "confidence": 0.91, "qr_code": "J-1", "qr_status": "known", "product": WIDGET}
      assert values_for(event) == {"result": "reject", "result_code": 2, "reject_reason": "vision_class", "class": "Apple",
          "confidence": 0.91, "code": "J-1", "code_status": "known", "good_count": 4, "reject_count": 2, "total_count": 6,
          "batch": "B-7", "line": "Values", "camera": "top", **flat_product_values(WIDGET), "input.speed": 12}

  def test_a_line_event_has_the_lines_values_and_no_result(lines): ...
      # line_state_event(...): no "result"/"result_code"/"class"/"code"/"product.*"; counts from the counter.

  def test_a_good_product_has_no_reject_reason(lines): ...

  def test_finish_crossing_puts_the_confidence_on_the_plc_event(): ...       # 0.87 from a _CrossingTracker crossing

  def test_an_input_value_name_is_checked_and_unique():
      assert normalize_plc_input({"action": "start_line", "value_name": "speed"})["value_name"] == "speed"
      assert "value_name" not in normalize_plc_input({"action": "start_line", "value_name": ""})
      for bad, word in (({"action": "start_line", "value_name": "1x"}, "value name"),
                        ({"action": "product_sensor", "data_type": "counter", "value_name": "n"}, "counter"),
                        ({"action": "start_line", "value_name": "previous"}, "previous")):
          with pytest.raises(ValueError, match=word):
              normalize_plc_input(bad)
      with pytest.raises(ValueError, match="speed"):
          normalize_plc_inputs([{"action": "start_line", "value_name": "speed"}, {"action": "stop_line", "value_name": "speed"}])

  def test_values_gives_the_last_accepted_value(signals):                   # tests/test_plc_inputs.py fixture
      # A card with value_name "speed" and debounce 0: first read 5, then 7 -> values(line) == {"speed": 7};
      # a card without a value name is not listed; a card not read yet is not listed.
  ```

- [x] **Step 2: Run the tests and make sure they fail.** Expected: FAIL.

- [x] **Step 3: Implement it.**
  - Read the line through `line_service.line_manager.get(line_id)`. Import it inside the function, as `plc_dispatcher_service` does. When the line is not found, the line's values are simply missing; that is not an error.
  - Read the camera name through `send_dispatcher_service._camera_name`.
  - Keep `value_name` out of `_signature`, so naming a value does not reset the input's state.

- [x] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_event_values.py tests/test_plc_inputs.py tests/test_production_records.py`. Expected: PASS.

- [x] **Step 5: Commit.** Message: "One set of value names for every event; inputs can name their value".

### Task 3: Card settings, save-time checks and the formula check

**Files:**
- Modify:
  - `app/services/line_config.py`: `PLC_VALUE_SOURCES` gains `"product_field", "code", "product_name", "input", "formula"`; new `IF_MISSING = ("skip", "default")`; `PLC_CARD_KEPT_KEYS` gains `"value_field", "value_input", "formula", "if_missing", "default_value"`.
  - `app/services/plc_failsafe_service.py`: `validate_write_value`.
  - `app/services/settings_persistence_service.py`: `_check_input_links`, called in the 3 places that call `_check_confirmation_links`, with the endpoints passed to `validate_write_value`.
  - `app/routes/v1/lines.py`: the formula check route.
- Test: `tests/test_write_any_value.py` (new)

**Interfaces:**
- Consumes: Task 1 `parse` and `FormulaError`; part 1 `ProductCatalog.fields` and `VALUE_NAME_RE`.
- Produces:
  - `validate_write_value(card: dict, endpoints: Optional[List[dict]] = None) -> None`. The rules:
    - `value_field` and `value_input` must match `VALUE_NAME_RE`.
    - `formula` must parse.
    - `if_missing` must be in `IF_MISSING` (default `skip`).
    - With `default`, `default_value` must be a finite number, or text of 1 to 256 characters. Text is refused only when the card's endpoint is found in `endpoints` and is not OPC UA (`plc_sub_protocol` not `opc_ua` / `opcua`).
    - The new sources need `operation == "WRITE"`, as the existing ones do.
  - `SettingsPersistenceService._check_input_links(cards, inputs) -> None`: a card with `value_source == "input"` must name a `value_name` of an input on the same line. Part 3 adds the `input_id` of the input trigger.
  - `POST /api/v1/lines/{line_id}/formula/check` (decision W12).

- [x] **Step 1: Write the failing tests.**

  ```python
  W = {"id": "w1", "name": "Sort", "operation": "WRITE"}

  @pytest.mark.parametrize("extra, word", [
      ({"value_source": "product_field"}, "product field"), ({"value_source": "product_field", "value_field": "1x"}, "product field"),
      ({"value_source": "input", "value_input": ""}, "input"), ({"value_source": "formula", "formula": "1 +"}, "formula"),
      ({"value_source": "code", "if_missing": "explode"}, "no value"), ({"value_source": "code", "if_missing": "default"}, "default value"),
      ({"value_source": "code", "if_missing": "default", "default_value": float("inf")}, "default value"),
  ])
  def test_bad_write_settings_are_refused(extra, word):
      with pytest.raises(ValueError, match=word):
          validate_write_value({**W, **extra})
      with pytest.raises(ValueError, match="Write"):
          validate_write_value({**W, "operation": "PULSE", "value_source": "formula", "formula": "1"})

  def test_a_text_default_needs_an_opc_ua_controller():
      card = {**W, "value_source": "code", "if_missing": "default", "default_value": "NONE", "plc_endpoint_id": "ep1"}
      with pytest.raises(ValueError, match="OPC UA"):
          validate_write_value(card, [{"id": "ep1", "plc_sub_protocol": "modbus_tcp"}])
      validate_write_value(card, [{"id": "ep1", "plc_sub_protocol": "opc_ua"}])
      validate_write_value(card)

  def test_the_new_settings_survive_an_older_client(lines): ...
      # Save a line whose PLC card has value_source "formula" and a formula, then save the card again without
      # value_source / formula / if_missing: the saved card still has them (PLC_CARD_KEPT_KEYS).

  def test_an_input_value_must_exist_on_the_line(lines): ...
      # Card value_source "input", value_input "speed": refused (the error names the card and 'speed');
      # with an input card value_name "speed": saved; then saving the line with plc_inputs=[] is refused, naming the card.

  def test_the_formula_check(client, admin_headers, juice_line):            # a line whose QR reader's list has field lane
      ok = client.post(f"/api/v1/lines/{juice_line}/formula/check", json={"formula": "product.lane + product.brix"},
                       headers=admin_headers).json()
      assert ok["ok"] is True and ok["names"] == ["product.brix", "product.lane"] and "brix" in " ".join(ok["warnings"])
      bad = client.post(..., json={"formula": "1 +"}).json()
      assert bad["ok"] is False and isinstance(bad["position"], int) and bad["error"]
      assert "speed" in " ".join(client.post(..., json={"formula": "input.speed"}).json()["warnings"])
  ```

- [x] **Step 2: Run the tests and make sure they fail.** Expected: FAIL.

- [x] **Step 3: Implement it.** The error texts name the card, as today ("PLC action card 'Sort': …"). The route lives in `lines.py`, next to the other line routes, with `require_supervisor`.

- [x] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_write_any_value.py tests/test_messages_and_plc_values.py tests/test_plc_action_persistence.py tests/test_lines_api.py`. Expected: PASS.

- [x] **Step 5: Commit.** Message: "Output actions: new value sources, If no value, and a formula check".

### Task 4: Writing the value (skip, default, text, Test with a code)

**Files:**
- Modify:
  - `app/services/plc_dispatcher_service.py`: `write_value`, `_dispatch`, `dispatch_manual`; new `MissingValue`.
  - `app/hardware/plc/base.py`: `writes_text = False`; the text check in `execute_operation`; the type hint `write_value: Union[float, str]`.
  - `app/hardware/plc/opcua_driver.py`: `writes_text = True` only. The String write itself is Task 5.
  - `app/schemas/plc.py`: `PLCActionTestRequest.code: Optional[str]`, at most 256 characters.
  - `app/routes/v1/plc.py`: pass `code` through.
- Test: `tests/test_write_any_value.py`

**Interfaces:**
- Consumes: Task 2 `values_for`; Task 1 `parse`; part 1 `product_info`.
- Produces:
  - `class MissingValue(ValueError)`.
  - `PLCDispatcherService.write_value(card, event) -> Union[float, str]`. For the new sources it raises `MissingValue("no value for product.lane")`, and likewise "the code", "the product name", "input.speed" and "the formula". It returns: a bool as 1.0 or 0.0, a number as `float`, text as `str`.
  - `dispatch_manual(card, code: Optional[str] = None)`.
  - The card status `"skipped"` (decision W7).

- [x] **Step 1: Write the failing tests.** Use `plc`, `FakeDriver`, `_card` and `ENDPOINT` from `tests/test_plc_failsafe.py`. Run a write with `asyncio.run(PLCDispatcherService._dispatch(card, _CardState("w1"), event))`.

  ```python
  EVENT = {"event_id": "e1", "line_id": "line-1", "result": "good", "good_count": 7, "reject_count": 1,
           "qr_code": "J-1", "qr_status": "known", "product": WIDGET_WITH_FRAGILE}   # fields lane 3, recipe "R-12", fragile True

  @pytest.mark.parametrize("extra, want", [
      ({"value_source": "product_field", "value_field": "lane"}, 3.0), ({"value_source": "product_field", "value_field": "fragile"}, 1.0),
      ({"value_source": "formula", "formula": 'if(result == "good", product.lane, 9)'}, 3.0),
      ({"value_source": "product_field", "value_field": "recipe"}, "R-12"), ({"value_source": "code"}, "J-1"),
      ({"value_source": "product_name"}, "Widget"),
  ])
  def test_each_new_source_writes_its_value(extra, want):
      assert PLCDispatcherService.write_value(_card("w1", "DB1.X", operation="WRITE", **extra), EVENT) == want

  def test_a_missing_value_skips_the_write_without_an_alarm(plc):
      endpoint, driver = plc
      card = _card("w1", "DB1.Lane", operation="WRITE", value_source="product_field", value_field="lane")
      state = _CardState("w1")
      asyncio.run(PLCDispatcherService._dispatch(card, state, {**EVENT, "product": None}))
      assert driver.ops == [] and state.status == "skipped"
      assert state.last_result["skipped"] is True and state.last_result["message"] == "Skipped: no value for product.lane"
      assert not [a for a in alarm_manager.active() if (a.details or {}).get("card_id") == "w1"]

  def test_if_missing_default_writes_the_default(plc): ...         # default_value 0 -> driver.ops == [("WRITE", "DB1.Lane", 0.0)]

  def test_an_unread_input_is_missing(plc, monkeypatch): ...      # PLCInputService.values -> {}: value_source "input" is skipped

  def test_text_goes_only_to_a_controller_that_writes_text(plc):
      endpoint, driver = plc
      card = _card("w1", "DB1.Recipe", operation="WRITE", value_source="code", on_failure="retry", retry_attempts=2)
      state = _CardState("w1")
      asyncio.run(PLCDispatcherService._dispatch(card, state, EVENT))
      assert driver.ops == [] and state.status == "failed" and "writes numbers only" in state.last_result["message"]
      assert state.last_result["attempts"] == 1
      driver.writes_text = True
      asyncio.run(PLCDispatcherService._dispatch(card, _CardState("w1"), EVENT))
      assert driver.ops == [("WRITE", "DB1.Recipe", "J-1")]

  def test_the_existing_sources_fail_as_before(plc): ...           # batch "A-1" not a number -> status "failed", alarm raised

  def test_the_test_button_can_use_a_code(...): ...                # dispatch_manual(card, code="LISTED") writes lane 3
  ```

- [x] **Step 2: Run the tests and make sure they fail.** Expected: FAIL.

- [x] **Step 3: Implement it.**
  - In `_dispatch`, catch `MissingValue` before the existing `ValueError` branch. With `if_missing == "default"`, write `default_value` (a number becomes `float`, text stays `str`). Otherwise the card is skipped.
  - In `get_status`, pass `"skipped"` through unchanged.
  - The route's test result shows the skipped message.

- [x] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_write_any_value.py tests/test_plc_failsafe.py tests/test_plc_protocols.py tests/test_batch.py tests/test_messages_and_plc_values.py`. Expected: PASS.

- [x] **Step 5: Commit.** Message: "Output actions write product values, codes, inputs and formulas; skip when there is none".

### Task 5: OPC UA String writes

**Files:**
- Modify: `app/hardware/plc/opcua_driver.py` (`OPCUADriver.write`)
- Test: `tests/test_opcua_tag_names.py`. Extend the stand-in `_Node.read_data_type_as_variant_type` so that `str` values give `ua.VariantType.String` and `float` values give `Double`.

**Interfaces:**
- Produces: `OPCUADriver.write(address: str, value: Union[float, str]) -> Tuple[bool, str]` (decision W9).

- [x] **Step 1: Write the failing test.**

  ```python
  def test_string_writes():
      drv, server = _driver(SIEMENS, {'ns=3;s="Recipe"': "", 'ns=3;s="Lane"': 0})
      assert asyncio.run(drv.write("Recipe", "R-12"))[0] is True and server.values['ns=3;s="Recipe"'] == "R-12"
      assert asyncio.run(drv.write("Recipe", 3.0))[0] is True and server.values['ns=3;s="Recipe"'] == "3"
      ok, message = asyncio.run(drv.write("Recipe", "x" * 255))
      assert not ok and "254" in message
      ok, message = asyncio.run(drv.write("Lane", "R-12"))
      assert not ok and "text cannot be written" in message and server.values['ns=3;s="Lane"'] == 0
      ok, message = asyncio.run(drv.write("Lane", 1.5))
      assert not ok and "integer" in message                       # unchanged behaviour
      assert asyncio.run(drv.write("Lane", 3.0))[0] is True and server.values['ns=3;s="Lane"'] == 3
  ```

- [x] **Step 2: Run the test and make sure it fails.** Expected: FAIL.

- [x] **Step 3: Implement it.** Use the value-only `DataValue`, as the numeric path does today; that avoids the Siemens SourceTimestamp problem.

- [x] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_opcua_tag_names.py tests/test_plc_protocols.py`. Expected: PASS.

- [x] **Step 5: Commit.** Message: "OPC UA: write text to String tags".

### Task 6: Dashboard (value to write, input value names, skipped status)

**Files:**
- Create: `assets/card_values.js`
- Modify:
  - `dashboard.html`: the output action dialog's "Value to write" block (around `PLC_VALUE_SOURCES` and `plcValueSourceInput`), `plcCardSummary`, the status chip for `skipped`, and the script tag.
  - `assets/plc_signals.js`: the "Value name" input in the input dialog.
- Test: `tests/test_card_values_js.py` (new; node, skipped without node)

**Interfaces:**
- Produces: `window.CardValues` (also `module.exports`), with:
  - `VALUE_NAMES`: `[{name, label}]` for the plain names;
  - `valueChoices({fields, inputs}) -> [{name, label}]`. `fields` is `[{key, label, type, list}]` and `inputs` is `[{value_name, name}]`; the result is the plain names, then `product.name`, `product.code`, `product.list`, each `product.<key>`, then each `input.<value_name>`;
  - `fieldChoices(line, productLists) -> fields`, taken from the lists set on the line's code-reading cameras, each key listed once;
  - `FORMULA_EXAMPLES`;
  - `valueSourceText(card) -> string`, for the summary: "writes product.lane", "writes the formula …".

  Part 3 reuses `valueChoices` for "Only when".

- [ ] **Step 1: Write the failing node test.**

  ```
  CardValues.valueChoices({fields:[{key:"lane",label:"Lane",type:"number",list:"Juices"}], inputs:[{value_name:"speed",name:"Belt speed"}]}).map(c => c.name)
  ```

  It ends with `["product.name","product.code","product.list","product.lane","input.speed"]`, and it starts with `"result"`. Also test `fieldChoices` with two cameras on the same list (the key appears once), and `valueSourceText`.

- [ ] **Step 2: Run the test and make sure it fails.** Expected: FAIL.

- [ ] **Step 3: Implement it.** In the output action dialog, when the operation is Write:
  - **Source choice.** "Value to write" adds the choices: Product field, Code, Product name, Input value, Formula.
  - **Extra settings.** Product field shows a field dropdown, and Input value shows an input dropdown; each says when it is empty. Formula shows a text box (at most 200), a **Check** button that calls the formula check and shows the error with its position or the warnings, and the examples.
  - **If no value.** "Skip the write" or "Write this value" plus a box.
  - **Controller warning.** For Code, Product name, a text field, or a formula that uses text, the dialog warns when the card's controller is not OPC UA.
  - **Test with a code.** When the source uses product values, the card's **Test** asks for an optional code (decision W11).
  - **Skipped status.** `skipped` shows as a neutral chip, "Skipped", with the message as its title.
  - **Input dialog.** It gets "Value name (optional)", with the note "Use it in formulas and rules as input.<name>."

- [ ] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_card_values_js.py tests/test_lines_api.py`. Expected: PASS.

- [ ] **Step 5: Check it in a browser** with a git-ignored `backup/scratch-tools/shots_write_values.py`. Open the dialog for each source at 1440 and at 375 px. Check: no page errors, no CSP violations, the Check button shows an error and then a good result, and the card summary reads well. Look at the screenshots.

- [ ] **Step 6: Commit.** Message: "Dashboard: choose any value to write; inputs can name their value".

### Task 7: Docs, full checks and the live check

**Files:**
- Modify: `README.md` (output actions: the value table, the formula language, If no value, text and OPC UA), `FILE_TREE.md`, `PROJECT_DESCRIPTION.md`, `API_KEY_QUICKSTART.md` (the formula check, the test `code`)
- Create: `backup/scratch-tools/opcua_sim.py` (git-ignored). It is an asyncua server on 127.0.0.1:4841 with an Int16 `Lane` and a String `Recipe`.

- [ ] **Step 1: Write the docs.** Include the juice example: Sort writes `product.lane`, Send recipe writes `product.recipe` to a String tag, and the formula `if(result == "good", product.lane, 9)`.

- [ ] **Step 2: Run the full checks.** Run `bash backup/scratch-tools/pt.sh` and ruff. Expected: no failures, and ruff clean.

- [ ] **Step 3: Run the mutation checks.** Expected: every mutation is CAUGHT.
  - the whitelist (allow `ast.Subscript`, allow `**`);
  - the keyword rename;
  - MISSING on division by zero;
  - the text check in `execute_operation`;
  - skipped versus failed;
  - the 254 limit;
  - `if_missing` default.

- [ ] **Step 4: Live check on the scratch copy, against `opcua_sim.py`.**
  1. Use the juice list from part 1 and a line whose QR reader reads J-1.
  2. Set up the cards Sort (`product.lane` to `Lane`) and Send recipe (`product.recipe` to `Recipe`).
  3. Show code J-1 and check that `Lane` = 1 and `Recipe` = `R-1` on the stand-in server.
  4. Show an unknown code and check that both cards say Skipped.
  5. Then, **only with the owner's OK**, run the same against PLCSIM, after the owner has added the two tags.

- [ ] **Step 5: Update the vault.**
  - the decision note: part 2 done, with counts;
  - Status and branches;
  - the session note.

- [ ] **Step 6: Commit.** Message: "Docs for writing any value".
