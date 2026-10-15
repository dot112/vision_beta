# Part 3: When-then rules. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three new ways to control what happens:
- **Input changed:** a PLC input can fire any card.
- **Only when:** every card can have up to 5 extra rules, and it acts only when they pass.
- **Product checks:** a line can reject an item when its product's rules match. For example, "Wrong bottle": `class` ≠ `product.expected_class`.

**Architecture:**
- `app/services/rules.py` checks and evaluates "Only when" rule sets. It uses `formula.compare` (part 2), so `=` means the same everywhere.
- `card_triggers.card_fires` adds the input trigger and the rule check, so PLC cards and send cards share them.
- `PLCInputService` publishes an `input` event on each accepted change.
- `line_config` gains `product_checks` on a line and the reason `product_check`, which `product_result` gives last.

**Tech stack:** Python 3.12+, FastAPI; plain JavaScript in the dashboard.

**Spec:** [docs/superpowers/specs/2026-10-15-product-data-values-rules-design.md](../specs/2026-10-15-product-data-values-rules-design.md), section 4 and "Evaluation order". This part needs [part 1](2026-10-15-product-data-part-1-products-with-data.md) and [part 2](2026-10-15-product-data-part-2-write-any-value.md) to be done first. It uses:
- `VALUE_NAME_RE`;
- `formula.compare`, `formula.PLAIN_NAMES` and `formula.ROOTS`;
- `event_values.values_for`;
- `PLCInputService.values`;
- `CardValues.valueChoices`;
- the `value_name` on input cards;
- `SettingsPersistenceService._check_input_links`.

## Global Constraints

**Branch and commits**
- Work on the branch `claude/product-data-rules`, after parts 1 and 2.
- Commit after each task. Never push.

**Never break an existing line**
- A card without `only_when`, or a line without `product_checks`, behaves exactly as before.
- An input card's existing action keeps running.
- `SCHEMA_VERSION` stays **8**.
- When an older client leaves out the new keys (`input_id`, `only_when`, `product_checks`), they are kept.

**Limits (from the spec)**
- At most **5** rules per card or per check, and at most **10** product checks per line.
- Rule operators: `= != < <= > >= contains empty not_empty`. Match: `all` or `any`.
- The reject reason `product_check` has PLC code **7**.

**Running tests**
- Full suite: `bash backup/scratch-tools/pt.sh`
- One file: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/<file>.py`
- Lint: `.venv/Scripts/python.exe -m ruff check .`
- Mutation checks: `backup/scratch-tools/mutcheck.py`

**Files and editing**
- Keep each file's line endings and any BOM. Check `git diff --stat`.

**Live testing and data safety**
- Never start the server from the repository folder. Use the scratch copy on 127.0.0.1:8765, and stop it by its PID.
- PLCSIM is read only. Writes need the owner's OK, asked each time.
- Leave the owner's MQTT broker on port 1883 running.

**Dashboard**
- Put user values into the page with `esc` / `escapeHtml` or `data-` attributes, never inside quoted strings in `on*=` handlers.
- Raise the `?v=` of changed assets to `2.9.0`.
- Check pages at 1440 px and at 375 px, with no page errors and no CSP violations.

**Text and docs**
- Use plain, short English. Never put a model name in the repository. Commit messages end with the session's attribution lines.

## Decisions this plan makes

| # | Decision | Why |
|---|---|---|
| R1 | An `input` event goes out for **every accepted change** of an input card, whatever its action and whether the line runs or not. The exceptions are the `counter` data type and the starting read. | Spec 4a ("whatever the input's own action"). The starting read is not a change, as today. |
| R2 | `on` means off → on, and `off` means on → off, judged by `plc_input_service.truthy`. `change` means any accepted change. | It matches how input edges work today. |
| R3 | **The input event's message** is `{"event": "INPUT_CHANGED", "timestamp", "line_id", "line_name", "input_id", "input_name", "value", "previous"}`, plus the batch and totals, as `_with_totals` adds them. <br>• `MESSAGE_FIELDS["input"] = ("input_id", "input_name", "value", "previous")`. <br>• New placeholders: `{input_name}`, `{input.value}`, `{input.previous}` and `{input.<value_name>}`. <br>• `values_for` gives `input.value` and `input.previous` for this event. <br>• In a placeholder, `input.` is lowercase and everything after it keeps its case (part 1, P8). | Section 1 of the spec: the same names everywhere. |
| R4 | "Only when" is checked inside `card_fires`, after the line, camera, trigger and condition checks, and before re-arm and the execution policy. So a card held back by its rules does not use up its re-arm time. `values_for` runs only for cards that have rules. | It is the spec's order, and it avoids work for old cards. |
| R5 | **A rule** is `{"left", "op", "right", "right_kind"}`. <br>• `left`, and `right` when `right_kind == "value"`, are value names: `PLAIN_NAMES`, `product.name` / `code` / `list` / `<key>`, `input.<name>`, `input.value` or `input.previous`. <br>• A constant is saved as trimmed text of at most 256 characters. <br>• For `empty` / `not_empty`, `right` is saved as `""` and `right_kind` as `constant`. <br>• `contains` compares as text, ignoring case. <br>• `=` / `!=` / `<` and the others use `formula.compare`. <br>• A missing side makes the rule false; for `empty` a missing left makes it true. | Spec 4b, with W4 of part 2. |
| R6 | **Product checks run only for a product whose code is known.** That is a paired read with Sync, or a code read on a code-only line. Their rules see what is known before the result: `class`, `confidence`, `code`, `code_status`, `product.*`, `input.*`, the counts before this product, `batch`, `line` and `camera`. They never see `result`, `result_code` or `reject_reason`. | A product check is how the result is decided, so it cannot depend on the result. |
| R7 | **A line with product checks turns Sync on** when it has a product source (a vision camera or a product sensor) and a camera that reads codes. Code checks already do this today. | Without pairing there is no product to check. |
| R8 | A rejected product carries `product_check: <check name>` in its message, its PLC event and `details` of its record. The export gains the column `product_check`. The pages show "product check: <name>". | Spec 4c. |
| R9 | A product check has `id` `check_<8 hex>` (or a valid id that is sent), a `name` of 1 to 64 characters, `enabled` (default true), and `only_when` with at least 1 rule. | A check without rules would reject everything. |

## Review Focus

1. **The first read after a boot, or after an input card was changed.** Expected: no input event, and no card fires. Test: Task 3 `test_input_events_follow_accepted_changes`.
2. **Rule constants that look different from the value**: `"3"` against 3.0, `"Yes"` against true, other case, extra spaces. Expected: they compare as people mean them (decision W4 and R5). Test: Task 1 `test_one_rule`.
3. **A product check on an unknown code, on a product with no code, or a class rule on a code-only line.** Expected: the check never rejects, and the existing reasons come first. Tests: Task 4 `test_checks_need_a_known_code` and `test_a_code_only_line_runs_checks_without_class`.
4. **Deleting an input, or changing its value name, while cards still trigger on it or read it.** Expected: the save is refused, and the message names the card. Test: Task 3 `test_cards_must_name_an_input_of_their_line`.
5. **An older dashboard saves cards or lines without the new keys.** Expected: `only_when`, `input_id` and `product_checks` are kept. Tests: Task 2 `test_only_when_is_kept_and_checked`; Task 4 `test_a_line_keeps_its_checks_when_a_client_leaves_them_out`.

---

### Task 1: Rules (`rules.py`)

**Files:**
- Create: `app/services/rules.py`
- Test: `tests/test_rules.py` (new)

**Interfaces:**
- Consumes: part 2 `formula.compare`, `PLAIN_NAMES` and `ROOTS`; part 1 `VALUE_NAME_RE`.
- Produces:
  - `RULE_OPS`, `MATCHES = ("all", "any")`, `MAX_RULES = 5`.
  - `is_value_name(text: str) -> bool` (decision R5).
  - `normalize_only_when(raw: Any, label: str, required: bool = False) -> Optional[Dict]`: `None` when absent or empty, unless `required`.
  - `rules_pass(only_when: Optional[Dict], values: Mapping[str, Any]) -> bool`: `True` when `only_when` is `None`.

- [x] **Step 1: Write the failing tests.**

  ```python
  VALUES = {"result": "good", "class": "Apple", "code": "J-1", "input.speed": 12,
            "product.lane": 3, "product.expected_class": "apple", "product.fragile": True}

  @pytest.mark.parametrize("rule, want", [
      ({"left": "product.lane", "op": "=", "right": "3"}, True), ({"left": "product.lane", "op": ">=", "right": "3.0"}, True),
      ({"left": "class", "op": "=", "right": "product.expected_class", "right_kind": "value"}, True),
      ({"left": "class", "op": "!=", "right": "product.expected_class", "right_kind": "value"}, False),
      ({"left": "product.fragile", "op": "=", "right": "Yes"}, True), ({"left": "code", "op": "contains", "right": "j-"}, True),
      ({"left": "product.recipe", "op": "empty"}, True), ({"left": "product.recipe", "op": "not_empty"}, False),
      ({"left": "product.missing", "op": "!=", "right": "3"}, False),
      ({"left": "class", "op": "=", "right": "product.missing", "right_kind": "value"}, False),
      ({"left": "input.speed", "op": "<", "right": "20"}, True),
  ])
  def test_one_rule(rule, want):
      assert rules_pass(normalize_only_when({"rules": [rule]}, "Card 'x'"), VALUES) is want

  def test_all_and_any():
      yes, no = {"left": "class", "op": "=", "right": "apple"}, {"left": "class", "op": "=", "right": "pear"}
      assert rules_pass({"match": "any", "rules": [no, yes]}, VALUES) and not rules_pass({"match": "all", "rules": [no, yes]}, VALUES)
      assert rules_pass(None, VALUES)

  @pytest.mark.parametrize("raw, word", [
      ({"rules": [{"left": "speed", "op": "="}]}, "speed"), ({"rules": [{"left": "class", "op": "~", "right": "a"}]}, "operator"),
      ({"match": "most", "rules": [{"left": "class", "op": "empty"}]}, "all"),
      ({"rules": [{"left": "class", "op": "empty"}] * 6}, "5"),
      ({"rules": [{"left": "class", "op": "=", "right": "x" * 257}]}, "256"),
      ({"rules": [{"left": "class", "op": "=", "right": "1x", "right_kind": "value"}]}, "value"),
  ])
  def test_bad_rules_are_refused_and_name_the_card(raw, word):
      with pytest.raises(ValueError, match=f"Card 'x'.*{word}"):
          normalize_only_when(raw, "Card 'x'")

  def test_empty_and_trimmed():
      assert normalize_only_when(None, "x") is None and normalize_only_when({"rules": []}, "x") is None
      with pytest.raises(ValueError, match="at least one rule"):
          normalize_only_when({"rules": []}, "x", required=True)
      kept = normalize_only_when({"rules": [{"left": "result", "op": "=", "right": " good "}, {"left": "code", "op": "empty", "right": "z"}]}, "x")
      assert kept == {"match": "all", "rules": [{"left": "result", "op": "=", "right": "good", "right_kind": "constant"},
                                                {"left": "code", "op": "empty", "right": "", "right_kind": "constant"}]}
  ```

- [x] **Step 2: Run the tests and make sure they fail.**

  Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_rules.py`

  Expected: FAIL (`ModuleNotFoundError`).

- [x] **Step 3: Implement it.** `rules_pass` maps `=` to `==` for `compare`, and treats a `MISSING` result as false.

- [x] **Step 4: Run the tests and make sure they pass.** Expected: PASS.

- [x] **Step 5: Commit.** Message: "Only-when rules: one check for cards and product checks".

### Task 2: "Only when" on every card

**Files:**
- Modify:
  - `app/services/card_triggers.py` (`card_fires`).
  - `app/services/line_config.py`: in `normalize_send_card`, keep a normalized `only_when`; `PLC_CARD_KEPT_KEYS` and `SEND_CARD_KEPT_KEYS` gain `"only_when"`.
  - `app/services/rules.py`: new `validate_card_rules(card: dict, label: str) -> None`, used for PLC cards.
  - `app/services/settings_persistence_service.py`: call `validate_card_rules` where `validate_write_value` is called.
  - `app/services/send_dispatcher_service.py`: `send_timer` checks the rules.
- Test: `tests/test_card_rules.py` (new)

**Interfaces:**
- Consumes: Task 1; part 2 `values_for`.
- Produces:
  - `card_fires(card, event)` honours `only_when` (decision R4).
  - `validate_card_rules`; Task 3 adds the input trigger to it.
  - A timer card's rules see `values_for({"event_type": "timer", "line_id": <its line>})`.

- [x] **Step 1: Write the failing tests.**

  ```python
  LANE3 = {"match": "all", "rules": [{"left": "product.lane", "op": "=", "right": "3", "right_kind": "constant"}]}

  def test_a_card_acts_only_when_its_rules_pass():
      card = normalize_trigger({"id": "k", "trigger": "cross_line", "condition": "reject", "only_when": LANE3})
      event = {"event_type": "crossing", "line_id": "line-1", "result": "reject",
               "product": {"code": "J-3", "name": "Grape", "list": "Juices", "list_id": "l", "fields": {"lane": 3}}}
      assert card_fires(card, event)
      assert not card_fires(card, {**event, "product": {**event["product"], "fields": {"lane": 2}}})
      assert not card_fires(card, {k: v for k, v in event.items() if k != "product"})
      assert not card_fires(card, {**event, "result": "good"})          # the condition still comes first

  def test_a_card_without_rules_never_computes_values(monkeypatch):
      monkeypatch.setattr(event_values, "values_for", lambda event: (_ for _ in ()).throw(AssertionError("computed")))
      assert card_fires(normalize_trigger({"trigger": "cross_line", "condition": "any"}), {"event_type": "crossing", "line_id": "line-1"})

  def test_only_when_is_kept_and_checked(lines): ...
      # A send card with a bad rule is refused, naming the card. A PLC card with a bad rule is refused by
      # save_line and by replace_line_plc_actions, naming the card. A good only_when is saved, and is kept
      # when the card is saved again without it (both card kinds).

  def test_a_timer_card_sends_only_when_its_rules_pass(lines): ...
      # Timer card with rule batch = "B-7": send_timer(card) is False before set_batch("B-7"), True after.
  ```

- [x] **Step 2: Run the tests and make sure they fail.** Expected: FAIL.

- [x] **Step 3: Implement it.** Import `event_values` inside `card_fires`, which avoids an import cycle. The test above patches `event_values.values_for`, so call it through the module.

- [x] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_card_rules.py tests/test_send_cards.py tests/test_send_timer.py tests/test_plc_action_persistence.py tests/test_joined_cameras.py`. Expected: PASS.

- [x] **Step 5: Commit.** Message: "Cards can act only when their rules pass".

### Task 3: The "Input changed" trigger

**Files:**
- Modify:
  - `app/services/line_config.py`: `CARD_TRIGGERS` gains `"input"`; new `INPUT_CONDITIONS = ("on", "off", "change")`; `PLC_INPUT_ACTIONS` gains `"none"`; data type `string` is allowed for `set_batch` and `none`; `normalize_send_card` handles the input trigger; both `*_KEPT_KEYS` gain `"input_id"`; `TEMPLATE_FIELDS` gains `input_name`; `parse_template` accepts `input.` names.
  - `app/services/card_triggers.py`: `INPUT_EVENT = "input"`, the alias, the input branch of `trigger_matches`, `camera_matches` treating it as line-wide, and `input_event(card, old, new) -> dict`.
  - `app/services/rules.py`: `validate_card_rules` checks `input_id` and the condition.
  - `app/services/plc_input_service.py`: `_took` publishes; `_act` does nothing for `none`.
  - `app/services/send_dispatcher_service.py`: `input_payload(event)`, `MESSAGE_FIELDS["input"]`, `template_values`.
  - `app/services/event_values.py`: `input.value` and `input.previous`.
  - `app/services/settings_persistence_service.py`: `_check_input_links` checks `input_id` for PLC and send cards. Call it in `replace_line_send_actions` and for send cards in `save_line`.
- Test: `tests/test_input_trigger.py` (new)

**Interfaces:**
- Produces:
  - Input event: `{"event_type": "input", "event_id": f"input_{line_id}_{input_id}_{time.time_ns()}", "line_id", "input_id", "input_name", "value", "previous", "timestamp"}`.
  - `PLCInputService._publish_input(card, old, new) -> None`. It runs on the event loop and calls `PLCDispatcherService.evaluate(event)` as a task and `SendDispatcherService.evaluate(event, input_payload(event))`.

- [x] **Step 1: Write the failing tests.**

  ```python
  def _input_card(cond, input_id="lane3"):
      return normalize_trigger({"id": f"c-{cond}", "trigger": "input", "input_id": input_id, "condition": cond})

  @pytest.mark.parametrize("previous, value, fired", [
      (False, True, {"on", "change"}), (True, False, {"off", "change"}), (0, 5, {"on", "change"}),
      (5, 7, {"change"}), ("", "FULL", {"on", "change"}),
  ])
  def test_an_input_change_fires_its_input_cards(previous, value, fired):
      event = {"event_type": "input", "line_id": "line-1", "input_id": "lane3", "value": value, "previous": previous}
      assert {c for c in ("on", "off", "change") if card_fires(_input_card(c), event)} == fired
      assert not card_fires(_input_card("change", "other"), event)
      assert not card_fires(normalize_trigger({"trigger": "cross_line", "condition": "any"}), event)
      assert not card_fires(_input_card("change"), {"event_type": "crossing", "line_id": "line-1", "result": "good"})

  def test_input_events_follow_accepted_changes(signals, monkeypatch): ...
      # Capture _publish_input. A "none" card with value_name lane3 reads False (the starting read: nothing),
      # True (one event: value True, previous False), True (nothing), then False (one event).
      # A start_line card publishes too, and still starts the line. A counter card never publishes.
      # A card with debounce 200 ms: a value that does not last publishes nothing.

  def test_the_input_message_and_its_placeholders(lines): ...
      # input_payload(event) has event INPUT_CHANGED, input_name, value, previous, line_name.
      # render_template("{input_name}: {input.value} (was {input.previous})", payload) == "Lane 3 full: True (was False)".
      # values_for(event) has input.value and input.previous.

  def test_cards_must_name_an_input_of_their_line(lines): ...
      # A send card and a PLC card with trigger input and an unknown input_id are refused, naming the card.
      # With the input on the line, both are saved. Then saving the line without that input is refused, naming the card.

  def test_the_new_input_settings_are_checked():
      assert normalize_plc_input({"action": "none", "data_type": "string"})["action"] == "none"
      with pytest.raises(ValueError, match="string"):
          normalize_plc_input({"action": "start_line", "data_type": "string"})
      with pytest.raises(ValueError, match="input"):
          normalize_send_card({"trigger": "input", "condition": "on"})
      with pytest.raises(ValueError, match="on"):
          normalize_send_card({"trigger": "input", "input_id": "lane3", "condition": "rising"})
  ```

- [x] **Step 2: Run the tests and make sure they fail.** Expected: FAIL.

- [x] **Step 3: Implement it** with decisions R1 to R3. In `_took`, publish after `change` is known and before the existing edge or action check, so both happen.

- [x] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_input_trigger.py tests/test_plc_inputs.py tests/test_inputs_see_output_pulses.py tests/test_send_cards.py`. Expected: PASS.

- [x] **Step 5: Commit.** Message: "Input changed: a PLC input can fire any card".

### Task 4: Product checks

**Files:**
- Modify:
  - `app/services/line_config.py`: `REASON_PRODUCT_CHECK = "product_check"`, added to `REJECT_REASONS`; `REJECT_REASON_CODES[REASON_PRODUCT_CHECK] = 7`; `MAX_PRODUCT_CHECKS = 10`; `new_product_check_id()`; `normalize_product_checks(raw) -> List[Dict]`. In `normalize_line`, `product_checks` follows the `plc_inputs` pattern (kept when left out), and decision R7 applies. `product_result(..., product_check: Optional[str] = None)` gives `(True, REASON_PRODUCT_CHECK, read camera id)` after every existing reason.
  - `app/services/line_service.py`: `configure` keeps the enabled checks; new `LineRuntime._failed_check(read, crossing=None) -> Optional[str]`; changes in `_finish_product`, `_emit_code_product` and `_remember`; `result` is shown when `code_checks` or `product_checks` exist.
  - `app/services/production_records_service.py`: `build_row` copies `product_check` into `details`.
  - `app/services/records_export.py`: the `product_check` column after `product_data`.
- Test: `tests/test_product_checks.py` (new)

**Interfaces:**
- Consumes: Task 1 `rules_pass` and `normalize_only_when(required=True)`; part 2 `values_for`.
- Produces: the `product_checks` line key; `payload["product_check"]` and `plc_event["product_check"]` (the check name).

- [x] **Step 1: Write the failing tests.**

  ```python
  CHECK = {"name": "Wrong bottle", "only_when": {"match": "all", "rules": [
      {"left": "class", "op": "!=", "right": "product.expected_class", "right_kind": "value"}]}}

  def test_product_checks_are_checked():
      check = normalize_product_checks([CHECK])[0]
      assert re.fullmatch(r"check_[0-9a-f]{8}", check["id"]) and check["enabled"] is True and check["name"] == "Wrong bottle"
      for bad, word in (([{**CHECK, "name": ""}], "name"), ([{**CHECK, "only_when": {"rules": []}}], "at least one rule"),
                        ([CHECK] * 11, "10"), ([{**CHECK, "id": "a"}, {**CHECK, "id": "a"}], "same id")):
          with pytest.raises(ValueError, match=word):
              normalize_product_checks(bad)

  def test_a_product_check_comes_last():
      read = {"camera_id": "qr", "known": True}
      assert product_result(False, read, {}, product_check="Wrong bottle") == (True, "product_check", "qr")
      assert product_result(True, read, {}, product_check="Wrong bottle")[1] == "vision_class"
      assert product_result(False, {"camera_id": "qr", "known": False}, {"qr": {"action": "accept_listed", "no_read": "ignore"}},
                            product_check="Wrong bottle")[1] == "code_not_in_list"
      assert REJECT_REASON_CODES["product_check"] == 7

  def test_a_wrong_bottle_is_rejected(lines, recorder, with_data): ...
      # Reuse part 1's helpers. Line: top (vision) + qr (report), product_checks [CHECK]; Sync turns on by itself (R7).
      # LISTED has expected_class "apple". Read LISTED, cross with class "orange" -> reject, reason product_check;
      # payload["product_check"] == "Wrong bottle"; the record's details carry it too.
      # Read LISTED, cross with class "Apple" -> good.

  def test_checks_need_a_known_code(lines, recorder, with_data): ...
      # Unknown code -> good. No code read (no_read "ignore") -> good.

  def test_a_code_only_line_runs_checks_without_class(lines, recorder, with_data): ...
      # Code-only line (qr, accept_listed). A check on product.lane = 3 rejects LISTED (lane 3) with product_check;
      # CHECK (a class rule) never rejects there.

  def test_a_line_keeps_its_checks_when_a_client_leaves_them_out(lines): ...

  def test_the_export_has_a_product_check_column(): ...
  ```

- [x] **Step 2: Run the tests and make sure they fail.** Expected: FAIL.

- [x] **Step 3: Implement it** with decisions R6 to R9.
  - `_failed_check` builds an event-like dict from the read and the crossing. It holds `class_name`, `confidence`, `qr_code`, `qr_status`, `product`, `camera_id` and `line_id`, and **no** `result`. It returns the name of the first enabled check whose rules pass.

- [x] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_product_checks.py tests/test_reader_actions.py tests/test_joined_cameras.py tests/test_production_records.py tests/test_lines_api.py`. Expected: PASS.

- [x] **Step 5: Commit.** Message: "Product checks: reject an item when its product's rules match".

### Task 5: Dashboard (input trigger, Only when, product checks)

**Files:**
- Modify:
  - `assets/card_values.js`: `onlyWhenHtml(onlyWhen, context)`, `readOnlyWhen(root) -> onlyWhen | null`, `onlyWhenText(onlyWhen) -> string`, and `RULE_OPS` labels.
  - `dashboard.html`:
    - `CARD_TRIGGERS` gains `{id: "input", label: "Input changed", summary: "Input changed"}`;
    - `CARD_CONDITIONS.input`;
    - an input picker in both card dialogs;
    - the "Only when" block in both dialogs;
    - both summaries;
    - the reject reason hint gains "7 product check".
  - `assets/production_lines.js`:
    - a "Product checks" block on Line setup after the camera cards, shown when a camera reads codes;
    - `REJECT_REASON.product_check = "product check"`, with the name shown where the row has it.
  - `assets/plc_signals.js`: the action `none`, "Only fires cards", with no edge; the data type `string` for `none`.
- Test: `tests/test_card_values_js.py`

- [ ] **Step 1: Write the failing node test.**
  - `onlyWhenText({match: "any", rules: [{left: "class", op: "!=", right: "product.expected_class", right_kind: "value"}, {left: "product.lane", op: "=", right: "3", right_kind: "constant"}]})` must equal `"only when class ≠ product.expected_class or product.lane = 3"`.
  - `onlyWhenText(null)` must equal `""`.

- [ ] **Step 2: Run the test and make sure it fails.** Expected: FAIL.

- [ ] **Step 3: Implement it.**
  - **Rule rows.** Each rule is a row: [value dropdown from `valueChoices`], [operator], [constant box, or value dropdown when "a value" is picked]. Add a **match all / any** switch, plus Add rule (at most 5) and Remove.
  - **Product checks block.** Each check has a name, on/off, the same rule rows, and Remove. Add check allows at most 10. The block saves with the line.
  - **Input trigger.** Its condition list is On (off to on), Off (on to off) and Any change. The input picker lists the line's inputs by name.

- [ ] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_card_values_js.py tests/test_lines_api.py`. Expected: PASS.

- [ ] **Step 5: Check it in a browser** with a git-ignored `backup/scratch-tools/shots_rules.py`, at 1440 and at 375 px:
  - both card dialogs with an input trigger and two rules;
  - the product checks block with "Wrong bottle";
  - an input card set to "Only fires cards".

  Check for no page errors and no CSP violations, and look at the screenshots.

- [ ] **Step 6: Commit.** Message: "Dashboard: Input changed trigger, Only when rules and product checks".

### Task 6: Docs, full checks and the live juice example

**Files:**
- Modify: `README.md` (when-then rules: the input trigger, Only when, product checks, reason code 7, and the juice example), `FILE_TREE.md`, `PROJECT_DESCRIPTION.md`, `API_KEY_QUICKSTART.md` (the `product_checks` line key, which needs `configuration:write`)

- [ ] **Step 1: Write the docs.** Use the spec's juice line as the worked example.

- [ ] **Step 2: Run the full checks.** Run `bash backup/scratch-tools/pt.sh` and ruff. Expected: no failures, and ruff clean.

- [ ] **Step 3: Run the mutation checks.** Expected: every mutation is CAUGHT.
  - the rules check placed before re-arm;
  - a missing left being false;
  - `empty` being true on missing;
  - `any` / `all`;
  - the starting read publishing nothing;
  - the product check coming last;
  - checks only for a known code;
  - R7 turning Sync on.

- [ ] **Step 4: Live check on the scratch copy.** Use `opcua_sim.py` from part 2, with a Bool `Lane3Full` added, and a test MQTT broker on a spare port. Set up the full juice example:
  - the list "Juices", with `recipe`, `lane` and `expected_class`;
  - the check "Wrong bottle";
  - the cards Send recipe, Sort (`if(result == "good", product.lane, 9)`), Kicker, Report (text, MQTT) and Lane 3 full (input changed → message).

  Then:
  1. Show the codes and check each write, message and record.
  2. Set `Lane3Full` on the stand-in server and check that the message goes out once.

  Against PLCSIM, test **only with the owner's OK**.

- [ ] **Step 5: Update the vault.**
  - The decision note: all three parts done, with counts.
  - Status and branches.
  - The session note.
  - Open questions: what was not tried on real hardware.

- [ ] **Step 6: Commit.** Message: "Docs for when-then rules".
