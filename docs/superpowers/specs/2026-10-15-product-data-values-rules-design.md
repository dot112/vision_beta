# Products with data, write any value, when-then rules: design

Date: 2026-10-15. Status: design agreed in conversation; **waiting for the owner's review of this spec**, then an implementation plan (`superpowers:writing-plans`).

## Goal

More flexibility in how a line uses its products, what it writes to the PLC, and what makes a card act, for **mixed-product lines**: different products pass on the same conveyor, the code read on each item says which product it is, and that product's own data decides what happens.

Owner's decisions (2026-10-15):
- Wanted: **products carry data**, **write any value**, **when-then rules**. Not wanted now: richer records (PLC values, operator, shift, pictures in records).
- Products: **mixed products** (no "active product" per line).
- Product data: **custom fields per product list**.
- The owner left the remaining choices to Claude ("choose what is best"): product fields are used for PLC writes, for deciding good / reject, and in messages and records.

## Approach

Extend the existing **cards** of Line setup (Output actions and Send results) and the product lists. Rejected: a separate rules list (logic in two places) and reviving the old flow engine (`app/engines/flow_engine.py`: not line-aware, Modbus output only, no dashboard page).

Everything a card has today keeps working unchanged: a card without the new settings behaves exactly as before.

## Current design (anchors for the plan)

- Products: `app/db/models/product.py` (`ProductList`: id, name; `Product`: list_id, code, name, description). At most 4 lists. `app/services/product_service.py`: `ProductCatalog` (thread-safe code -> product per list; `lookup(code, list_id)`), `ProductService.refresh_catalog`, CSV import / export with `_csv_safe`. Routes: `app/routes/v1/products.py`. Page: Products (in `assets/production_lines.js`).
- A camera that reads codes has one `product_list_id` and a code check action (`report`, `accept_listed`, `reject_listed`) and `qr_no_read`.
- A code read: `LineRuntime.on_qr_read` (`app/services/line_service.py`) looks the code up and builds `read` (code, known, product_name, product_list_id, ...).
- A product's one result: `LineRuntime._finish_product` -> `line_config.product_result(...)` (vision, joined cameras, code verdict, no code, station without result) -> `counter.finish_crossing(...)`. Reasons in `line_config.REJECT_REASONS`; PLC numbers in `REJECT_REASON_CODES` (1..6, 9 other).
- Cards: `app/services/card_triggers.py` (`normalize_trigger`, `trigger_matches`, `card_fires(card, event)`), used by `PLCDispatcherService.evaluate` and `SendDispatcherService`. Dashboard lists: `CARD_TRIGGERS`, `CARD_CONDITIONS` in `dashboard.html`.
- PLC writes: `PLCDispatcherService.write_value(card, event)` with `value_source` in `line_config.PLC_VALUE_SOURCES` (fixed, result_code, good_count, reject_count, total_count, class_index, reject_reason_code, batch); checked by `plc_failsafe_service.validate_write_value`. All drivers write **numbers only** today (OPC UA `write` refuses non-numeric nodes).
- Inputs: `app/services/plc_input_service.py` (`_act` per action), card fields in `line_config.normalize_plc_input`; actions in `PLC_INPUT_ACTIONS`.
- Messages: `send_dispatcher_service.MESSAGE_FIELDS`, `template_values`, `render_template`, `build_message`.
- Records: `ProductRecord` (`details` JSON), `production_records_service`, export `records_export.py`.
- Latest migration: `alembic/versions/0006_product_records.py`.

## 1. One set of values

Every event offers named values. The same names are used by conditions, by values written to the PLC, and by message templates.

| Name | Meaning | Events that have it |
|---|---|---|
| `result` | `good` / `reject` | product |
| `result_code` | 1 good, 2 reject | product |
| `reject_reason` | reason text | product |
| `class` | the class the counting camera saw | product, code (paired) |
| `confidence` | 0..1 | product |
| `code` | the code text | product (paired), code read |
| `code_status` | `known` / `unknown` / `no_read` | product (with Sync), code read |
| `good_count`, `reject_count`, `total_count` | the line's counts | all line events |
| `batch` | the line's batch number | all line events |
| `line`, `camera` | names | all line events |
| `product.name`, `product.code`, `product.list` | the product found for the code | product, code read (known code) |
| `product.<field>` | a field of that product (section 2) | same |
| `input.<value name>` | the last value an input card read (section 4) | all line events |
| `input.value`, `input.previous` | the changing input's new and old value | input event only |

A name an event does not have is **missing** (not 0, not empty text).

Value names are case-sensitive identifiers: letters, digits, `_`, starting with a letter, at most 32 characters (field keys and input value names follow the same rule).

### Small formula language (used by "write a value" and by conditions)

- Literals: numbers, text in double quotes, `true`, `false`.
- Names: the value names above (`product.lane`, `input.speed`).
- Operators: `+ - * / %`, comparisons `== != < <= > >=`, `and`, `or`, `not`, parentheses.
- Functions: `if(condition, a, b)`, `round(x)`, `round(x, digits)`, `min(a, b, ...)`, `max(a, b, ...)`, `abs(x)`, `int(x)`, `text(x)`.
- At most 200 characters. Parsed with Python's `ast` and checked against a whitelist (no attribute access except the `product.` / `input.` roots, no other calls, no subscripts, no comprehensions, no lambdas); evaluated by a small tree walker: **nothing is ever passed to `eval`/`exec`**.
- Checked when a card or check is saved: syntax, unknown functions, unknown roots, field keys that the line's lists do not define (a warning, not an error, because lists can change). Error messages name the position and the problem.
- At run time a missing name, division by zero or a type mismatch makes the result **missing**.

Module: `app/services/formula.py` (`parse(text) -> Formula`, `Formula.names`, `Formula.evaluate(values) -> value | MISSING`). Fully unit-tested, including hostile inputs (`__import__`, attribute chains, huge exponents: `**` is not allowed, very long text).

## 2. Products carry data

### Data model (migration `0007_product_data`)
- `product_lists.fields`: JSON list, default `[]`. Each field: `{"key": "lane", "label": "Lane", "type": "number" | "text" | "yes_no"}`. At most 20 fields per list; keys unique within the list.
- `products.data`: JSON object `{key: value}`, nullable. Values typed: number (int or float), text (at most 256 characters), yes/no (bool). A product may leave a field empty (missing).
- Existing lists and products: no fields, no data; behave as today.

### API (`/api/v1/products...`)
- List endpoints return and accept `fields`. Changing a field's type or deleting a field: values that no longer fit are dropped with a count in the answer.
- Product endpoints return and accept `data`; values are checked against the list's fields (422 naming the field).
- CSV import as a new list: columns after `code,name[,description]` become fields; type guessed (all values numbers -> number; all yes/no/true/false/1/0 -> yes_no; else text). Replace contents: extra columns must match the list's field keys (or labels); unknown columns refused with their names. Export writes the fields as extra columns (formula-safe, as today).
- Scopes unchanged (`configuration:write` for changes).

### Runtime
- `ProductCatalog` keeps `data` with each product; `lookup` returns it.
- `on_qr_read` adds `product: {"code", "name", "list", "fields": {...}}` to `read` for a known code.
- Product, code-read and code-product events carry `product` (and flattened `product.<key>` values for formulas and templates).
- Records: the product's fields at that moment go into `details.product` (history keeps the value as it was). Export gains one column "Product data" (`key=value; key=value`).
- Messages: new `MESSAGE_FIELDS` entry `product`; templates may use `{product.name}`, `{product.lane}`, ...

### Dashboard (Products page)
- A "Fields" editor per list: add, rename (label), change type, delete, reorder.
- The product table shows one column per field; the add / edit product form shows an input per field (number box, text box, yes/no switch).
- CSV import and export carry the fields.

## 3. Write any value

### Output action settings
`value_source` gains:

| value_source | Writes | Extra setting |
|---|---|---|
| `product_field` | a field of the event's product | `value_field` (key) |
| `code` | the code text | none |
| `product_name` | the product's name | none |
| `input` | the last value of an input card of the line | `value_input` (input value name) |
| `formula` | the formula's result | `formula` (text) |

Existing sources stay. New: `if_missing` = `skip` (default) or `default` with `default_value` (number or text): what the card does when the value is missing (unknown code, empty field, failed formula). A skipped write shows in the card's status ("Skipped: no value for product.lane") and raises no alarm.

### Numbers and text
- A number goes to every controller type, as today (integer nodes still refuse fractions).
- **Text** (code, product name, text fields, a text formula) can be written to **OPC UA String nodes** only in this round: `OpcuaDriver.write` gains String support (UTF-8, length checked against the node's array dimension when the server reports one). Other controller types refuse text with a clear message ("This controller type writes numbers only"); the card dialog warns when the card's controller is not OPC UA.
- yes/no values are written as 1 / 0.

### Validation (save time)
`validate_write_value` accepts the new sources: `value_field` / `value_input` must be identifiers; `formula` must parse; `default_value` must be a number unless the controller is OPC UA. The strobe works as today after any value.

### Dashboard
The output action dialog's "Value to write" offers the new sources, a field dropdown filled from the product lists of the line's code-reading cameras, an input dropdown, a formula box with a "check" button (server parse) and examples, and the "If no value" choice.

## 4. When-then rules

### 4a. New trigger "Input changed"
- Output actions and send cards get trigger `input` with `input_id` (one of the line's input cards) and condition `on` (off -> on), `off` (on -> off) or `change`.
- `PLCInputService` publishes an `input` event on every value change it accepts (after debounce), whatever the input's own action: `{"event_type": "input", "line_id", "input_id", "input_name", "value", "previous"}`. Both dispatchers receive it; `card_fires` matches `input_id` and the edge.
- Input cards get the action `none` ("Only fires cards"), so an input can exist just to trigger cards. Existing actions keep firing too.
- Input cards get an optional `value_name` (identifier); its last read value is offered to every event of the line as `input.<value_name>`.

### 4b. "Only when" on every card
- Every output action and send card (all triggers, timer included) may have `only_when = {"match": "all" | "any", "rules": [...]}` with at most 5 rules.
- A rule: `{"left": "<value name>", "op": "=" | "!=" | "<" | "<=" | ">" | ">=" | "contains" | "empty" | "not_empty", "right": "<constant or value name>", "right_kind": "constant" | "value"}`. Numbers compare as numbers when both sides are numbers, else as text (case-insensitive for `=`, `!=`, `contains`). A rule with a missing left value is false (except `empty`).
- Checked after the trigger and its condition; the card acts only when the rules pass. Implemented once in `card_triggers.card_fires`, so both dispatchers share it.
- Dashboard: an "Only when" block in both card dialogs: rows of [value dropdown] [operator] [constant or value], an All / Any switch, add / remove rows; the card summary shows "only when ...".

### 4c. Product checks (decide good / reject)
- A line gets `product_checks`: at most 10, each `{"id", "name", "enabled", "only_when": {...same structure...}}`, meaning "reject when the rules pass".
- Evaluated in `product_result` after the existing reasons (vision, joined cameras, code verdict, no code, station without result) and only when the product's code is known (a product to check against). The first check that passes rejects with reason `product_check`, `details.product_check = <name>`; `REJECT_REASON_CODES["product_check"] = 7`; `REJECT_REASONS` gains it; dashboards and exports show "Product check: <name>".
- A code-only line (a code reader deciding alone) runs the checks too; rules that name `class` or `confidence` are false there (missing values).
- Dashboard: a "Product checks" block on Line setup (after the camera cards), visible when a camera reads codes.

## Evaluation order for one product

picture -> AI class -> code read -> product looked up (with its fields) -> Sync pairs code and product -> **result**: vision, joined cameras, code verdict, no code, missing station, **product checks** -> counters and record (product fields saved) -> every card: trigger -> condition -> **only when** -> action (the value is computed at that moment, after any travel delay).

## Compatibility

- No settings-version upgrade: all new keys are optional; a card or line without them behaves as before.
- `PLC_CARD_KEPT_KEYS` and `SEND_CARD_KEPT_KEYS` gain the new keys (`value_field`, `value_input`, `formula`, `if_missing`, `default_value`, `input_id`, `only_when`), so an older client saving a card does not drop them.
- API key scope rules: a line body with `product_checks` needs `configuration:write` (as today); `plc_actions` still needs `plc:configure`.
- The dashboard keeps the current dialogs; the new settings are extra blocks.

## Errors and limits

- Save time: every new setting is checked (422 naming the card or check and the setting).
- Run time: a missing value never raises an alarm by itself; it skips (or writes the default) and shows in the card's status.
- Limits: 20 fields per list, 10 product checks per line, 5 rules per card or check, 200 characters per formula.

## Testing

- Unit: the formula module (grammar, whitelist, hostile inputs, missing values, types); rules evaluation; catalog with data; field validation and CSV import / export with fields; `product_result` with checks; `card_fires` with `input` events and `only_when`; `write_value` for each new source and `if_missing`; OPC UA String writes against a stand-in server.
- API: lists with fields, products with data, cards and checks saved and refused, migration `0007` up from `0006`.
- Mutation checks (`backup/scratch-tools/mutcheck.py`) for each new decision.
- Dashboard: Products fields editor, card dialogs (value source, only when), product checks block; headless Chrome at 1440 and 375 px, no page errors, no CSP violations.
- Live (scratch copy, GigE simulator, PLCSIM): the juice example (three products with `recipe`, `lane`, `expected_class`; a product check "Wrong bottle"; sort and kicker cards; an input-triggered card). PLC writes need tags of the right types in the TIA project (an Int and a String tag); writing to PLCSIM needs the owner's OK each time.

## Delivery

Three parts, each test-first, live-tested and committed on its own (when the owner says to commit):
1. Products with data (section 2, plus the `product.*` values of section 1 in events, templates and records).
2. Write any value (formula module, section 3).
3. When-then rules (section 4).

## Out of scope

Richer records (PLC values, operator, shift, pictures), an "active product" per line, text writes to controllers other than OPC UA, the old flow engine, a visual rule editor.
