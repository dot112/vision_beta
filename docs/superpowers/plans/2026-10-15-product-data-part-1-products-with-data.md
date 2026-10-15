# Part 1: Products carry data. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each product list has its own fields (number, text, yes/no). Each product fills them in. When a code is read, that product's data goes with the item into events, messages, templates and records.

**Architecture:**
- New JSON columns: `product_lists.fields` and `products.data` (migration `0007_product_data`).
- A small pure module, `app/services/product_fields.py`, checks fields and values. The product service, the API and CSV import / export use it.
- `ProductCatalog` keeps each product's data and each list's fields. `LineRuntime.on_qr_read` puts a `product` object on the read, and from there it reaches the message payload, the PLC event, the product record and the templates.
- On the Products page, a fields editor and a product dialog with one input per field.

**Tech stack:** Python 3.12+, FastAPI, SQLAlchemy 2 (async) and Alembic on SQLite; plain JavaScript in the dashboard; pytest, with node for the JavaScript syntax check.

**Spec:** [docs/superpowers/specs/2026-10-15-product-data-values-rules-design.md](../specs/2026-10-15-product-data-values-rules-design.md). This part covers section 2, plus the `product.*` values of section 1 in events, templates and records. Parts 2 and 3: [part 2](2026-10-15-product-data-part-2-write-any-value.md) and [part 3](2026-10-15-product-data-part-3-when-then-rules.md).

## Global Constraints

**Branch and commits**
- Work on the branch `claude/product-data-rules`. Make it from `claude/triggered-industrial-cameras` only after the owner has committed the work waiting there. Never start on a working tree that has uncommitted changes.
- Commit after each task. Never push.

**Never break an existing line**
- A list, product, card or line without the new settings behaves exactly as before.
- `SCHEMA_VERSION` stays **8**, and there is no settings upgrade step.
- Existing tests pass unchanged, except where a task says otherwise.

**Limits (from the spec)**
- At most **20** fields per list. Field keys are unique within a list.
- Field types: `number`, `text`, `yes_no`.
- A text value is at most **256** characters.
- Keys follow the value-name rule: letters, digits and `_`, starting with a letter, at most **32** characters, case-sensitive.
- Field labels are at most **64** characters. This limit is the plan's choice; the spec does not give one.

**Running tests**
- Full suite: `bash backup/scratch-tools/pt.sh`. It is pytest without the 8 tests that only fail on this Windows machine.
- One file: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/<file>.py`
- Lint: `.venv/Scripts/python.exe -m ruff check .`
- Mutation checks: `.venv/Scripts/python.exe backup/scratch-tools/mutcheck.py <spec.json>`. Keep the spec files in the session scratchpad.

**Files and editing**
- Keep each file's line endings (many are CRLF) and any UTF-8 BOM.
- Edit CRLF files with a script that keeps CRLF. Afterwards, check `git diff --stat` shows only the lines you meant to change.

**Live testing and data safety**
- Never start the server from the repository folder: `data/` holds the owner's real database. Use the scratch copy (`backup/scratch-tools/launcher.py`, 127.0.0.1:8765).
- Stop a test server by the PID that owns its port.
- Leave the owner's MQTT broker on port 1883 running.

**Dashboard**
- Put values that come from people (keys, labels, names, codes) into the page with `esc` / `escapeHtml`, or in `data-` attributes. Never put them inside quoted strings in `on*=` handlers. Use `encodeURIComponent` in URLs.
- Raise the `?v=` of every asset file you change to `2.7.0`.
- Check pages at 1440 px and at 375 px, with no page errors and no CSP violations.

**Text and docs**
- Use plain, short English in the UI and docs.
- Never put a model name in the repository. Commit messages end with the session's attribution lines.
- Update the parts of `README.md`, `FILE_TREE.md`, `PROJECT_DESCRIPTION.md` and `API_KEY_QUICKSTART.md` that a change touches.

## Decisions this plan makes

Places where the spec leaves a choice open:

| # | Decision | Why |
|---|---|---|
| P1 | A field's **key** never changes after it is saved. Renaming changes only the **label**. | Cards, formulas and rules name the key, so renaming must not break them. |
| P2 | The keys `name`, `code`, `list` and `description` are refused. | `product.name`, `product.code` and `product.list` are the product's own values. |
| P3 | When fields change, each value is checked again against its new field. A value that no longer fits is dropped (number → text always fits). Values of deleted fields are dropped too. The answer gives `dropped`, the number of values removed. | This is the spec's "values that no longer fit are dropped, with a count". |
| P4 | **Replace contents:** a field that has no column in the file keeps each product's current value. Only the columns in the file replace values. | An older file without the data columns must not wipe the data. |
| P5 | **Export** headers use field **keys**. **Import as a new list** turns each extra header into a key: lowercase, every run of other characters becomes `_`, `f_` goes in front of a leading digit, cut to 32 characters, and `_2`, `_3`… is added on a clash. The header as written becomes the label. | An exported file imports back to the same keys. |
| P6 | In a CSV, a yes/no cell is `yes`, `no`, `true`, `false`, `1` or `0` (any case). Export writes `yes` and `no`. A number cell is a plain decimal with `.`. | These are the spec's type-guess words. A comma decimal stays text, so nothing is lost. |
| P7 | A known code's read, event, message and record carry `product = {"code", "name", "list", "list_id", "fields"}`. `fields` is the product's data at that moment (`{}` when it has none). An unknown code has no `product` key at all. | One shape everywhere. A record keeps the values as they were when the product passed. |
| P8 | In a template placeholder, `product.` is lowercase, and everything after it keeps its case, so `{product.Lane}` is the key `Lane`. | Keys are case-sensitive; the existing placeholders stay lowercase. |
| P9 | The records export gains the column `product_data` (written `key=value; key=value`), after `stations`. | The existing headers are snake_case, so this uses the same style. |

## Review Focus

1. **A CSV with comma decimals (`1,5`) or mixed values in a column.** Expected: the column becomes text and keeps every value, nothing is dropped. Test: Task 4 `test_csv_columns_get_their_types`.
2. **Empty cells, and products that leave a field empty.** Expected: the value is missing, never `0`, `""` or `false`. A missing value is absent from `data` and from `product.fields`. Tests: Task 2 `test_values_are_typed_and_empty_means_missing`; Task 4 `test_csv_columns_get_their_types` (J-3 has no lane).
3. **Replace contents with an older file that has no data columns.** Expected: the products keep their data. Test: Task 4 `test_replace_keeps_fields_the_file_has_no_column_for`.
4. **Headers or keys that clash with the product's own names (`List`, `Name`, `name`), or with each other.** Expected: an imported header gets a suffix; a key typed in by hand is refused. Tests: Task 2 `test_bad_fields_are_refused`; Task 4 `test_header_keys_never_clash`.
5. **A code read by a camera with no list, or an unknown code.** Expected: there is no `product` key, and `{product.lane}` renders as empty text. Test: Task 5 `test_an_unknown_code_has_no_product`.

---

### Task 1: Database columns (migration 0007)

**Files:**
- Modify: `app/db/models/product.py`
- Create: `alembic/versions/0007_product_data.py`
- Test: `tests/test_product_data.py` (new)

**Interfaces:**
- Produces:
  - `ProductList.fields: list` (JSON, not null, server default `'[]'`);
  - `Product.data: Optional[dict]` (JSON, nullable);
  - revision id `"0007_product_data"`, `down_revision = "0006_product_records"`.

- [x] **Step 1: Write the failing test.**

  Copy the `_alembic` helper from `tests/test_product_lists.py:319`.

  ```python
  def test_0007_adds_fields_and_data_and_keeps_every_row(tmp_path):
      engine, config = _alembic(tmp_path, "old.db")
      with engine.begin() as conn:
          config.attributes["connection"] = conn
          command.upgrade(config, "0006_product_records")
          # A database made before this change: products has no data column yet.
          if "data" in {c["name"] for c in sa.inspect(conn).get_columns("products")}:
              conn.execute(sa.text("ALTER TABLE products DROP COLUMN data"))
          conn.execute(sa.text("INSERT INTO products (id, code, name, list_id) VALUES ('p1', 'J-1', 'Apple', 'list-1')"))
          command.upgrade(config, "head")
          assert conn.execute(sa.text("SELECT fields FROM product_lists WHERE id='list-1'")).scalar_one() == "[]"
          assert conn.execute(sa.text("SELECT data FROM products WHERE id='p1'")).scalar_one() is None
          upgraded = _shape(conn)            # sorted (table, column) of both tables
          command.upgrade(config, "head")    # again: nothing changes
          command.downgrade(config, "0006_product_records")
          assert "fields" not in {c["name"] for c in sa.inspect(conn).get_columns("product_lists")}
          assert "data" not in {c["name"] for c in sa.inspect(conn).get_columns("products")}
          assert conn.execute(sa.text("SELECT code FROM products")).scalar_one() == "J-1"
      new_engine, new_config = _alembic(tmp_path, "new.db")
      with new_engine.begin() as conn:
          new_config.attributes["connection"] = conn
          command.upgrade(new_config, "head")
          assert _shape(conn) == upgraded
  ```

- [x] **Step 2: Run it and make sure it fails.**

  Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_product_data.py`

  Expected: FAIL (no revision `0007`, or no `fields` column).

- [x] **Step 3: Write the migration and the model columns.**
  - `upgrade()` adds each column only when `sa.inspect` shows it is missing. On a new database, 0001 already builds `products` from the model, while 0005 builds `product_lists` without `fields`.
  - `downgrade()` drops both with `op.batch_alter_table`.
  - Follow the style of the docstring in `0005_product_lists.py`.

- [x] **Step 4: Run the new test and the two older migration tests.**

  Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_product_data.py tests/test_product_lists.py tests/test_production_records.py -k "0007 or upgrade or 0005"`

  Expected: PASS.

- [x] **Step 5: Commit.** Message: "Product lists get fields and products get data (migration 0007)".

### Task 2: Fields and values (`product_fields.py`)

**Files:**
- Create: `app/services/product_fields.py`
- Test: `tests/test_product_fields.py` (new)

**Interfaces:**
- Produces (the whole module is pure; none of it uses the database):
  - Rules and limits:
    - `VALUE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")`, used with `fullmatch`. Parts 2 and 3 use it for every value name.
    - `FIELD_TYPES = ("number", "text", "yes_no")`
    - `RESERVED_KEYS = ("name", "code", "list", "description")`
    - `MAX_FIELDS = 20`, `MAX_TEXT_VALUE_LENGTH = 256`, `MAX_FIELD_LABEL_LENGTH = 64`
  - Fields and values:
    - `normalize_fields(raw: Any) -> List[Dict[str, str]]`: each entry is `{"key", "label", "type"}`. The label defaults to the key. Raises `ValueError`.
    - `clean_value(field: Dict[str, str], raw: Any) -> Any`: returns an `int | float | str | bool`, or `None` for empty. Raises `ValueError` naming `label (key)`.
    - `clean_data(fields, raw: Any) -> Optional[Dict[str, Any]]`: the non-empty typed values in field order, or `None` when there are none. A key that is not a field is refused, and the error names it.
    - `refit_data(fields, data: Optional[Dict]) -> Tuple[Optional[Dict[str, Any]], int]`: the values that still fit, and how many were dropped (decision P3).
  - CSV cells:
    - `parse_cell(field, text: str) -> Any`: reads one CSV cell (decision P6). Empty gives `None`.
    - `format_cell(value: Any) -> str`: `True` gives `"yes"`, `False` gives `"no"`, a whole number has no `.0`, and text stays as it is.
    - `guess_type(cells: List[str]) -> str`
    - `key_from_header(header: str, taken: Set[str]) -> str` (decision P5)
  - Flat values and export text:
    - `flat_product_values(product: Optional[Dict]) -> Dict[str, Any]`: `{"product.name", "product.code", "product.list", "product.<key>"…}`, where only values that are present appear. Parts 2 and 3 use it.
    - `data_text(data: Optional[Dict]) -> str`: `"lane=3; recipe=R-12"`, using `format_cell`. Empty gives `""`.

- [ ] **Step 1: Write the failing tests.**

  ```python
  LANE, RECIPE, FRAGILE = ({"key": "lane", "label": "Lane", "type": "number"},
                           {"key": "recipe", "label": "recipe", "type": "text"},
                           {"key": "fragile", "label": "Fragile", "type": "yes_no"})

  def test_fields_get_a_label_and_keep_their_order():
      assert normalize_fields([{"key": "lane", "label": " Lane ", "type": "number"}, {"key": "recipe", "type": "text"}]) == [LANE, RECIPE]
      assert normalize_fields(None) == [] and normalize_fields([]) == []

  @pytest.mark.parametrize("bad, word", [
      ([{"key": "1lane", "type": "number"}], "letter"),
      ([{"key": "lane", "type": "date"}], "type"),
      ([{"key": "lane", "type": "number"}, {"key": "lane", "type": "text"}], "twice"),
      ([{"key": "name", "type": "text"}], "product itself"),
      ([{"key": f"f{i}", "type": "text"} for i in range(21)], "20"),
      ([{"key": "a" * 33, "type": "text"}], "32"),
      ([{"key": "lane", "label": "x" * 65, "type": "text"}], "64"),
  ])
  def test_bad_fields_are_refused(bad, word):
      with pytest.raises(ValueError, match=word):
          normalize_fields(bad)

  def test_values_are_typed_and_empty_means_missing():
      fields = [LANE, RECIPE, FRAGILE]
      assert clean_data(fields, {"lane": "3", "recipe": " R-12 ", "fragile": "Yes"}) == {"lane": 3, "recipe": "R-12", "fragile": True}
      assert clean_data(fields, {"lane": 2.5, "fragile": False}) == {"lane": 2.5, "fragile": False}
      assert clean_data(fields, {"lane": "", "recipe": None}) is None
      for raw, word in (({"lane": "three"}, "lane"), ({"colour": "red"}, "colour"),
                        ({"recipe": "x" * 257}, "256"), ({"lane": float("nan")}, "lane"), ({"fragile": "maybe"}, "fragile")):
          with pytest.raises(ValueError, match=word):
              clean_data(fields, raw)

  def test_a_type_change_drops_only_what_no_longer_fits():
      as_text = {**LANE, "type": "text"}
      assert refit_data([as_text], {"lane": 3, "recipe": "R"}) == ({"lane": "3"}, 1)
      assert refit_data([{**RECIPE, "type": "number"}], {"recipe": "7"}) == ({"recipe": 7}, 0)
      assert refit_data([{**LANE, "type": "yes_no"}], {"lane": 2}) == (None, 1)

  @pytest.mark.parametrize("cells, kind", [(["1", "2.5", ""], "number"), (["1", "0"], "number"),
      (["yes", "No", "TRUE", "0"], "yes_no"), (["1,5", "2"], "text"), (["", ""], "text"), (["=1+1"], "text")])
  def test_guess_type(cells, kind):
      assert guess_type(cells) == kind

  def test_header_keys_never_clash():
      taken = set()
      keys = [key_from_header(h, taken) for h in ("Lane", "lane", "Expected class", "2nd colour", "List", "x" * 40)]
      assert keys == ["lane", "lane_2", "expected_class", "f_2nd_colour", "list_2", "x" * 32]

  def test_flat_values_and_export_text():
      product = {"code": "J-1", "name": "Apple", "list": "Juices", "list_id": "list-1", "fields": {"lane": 3, "fragile": True}}
      assert flat_product_values(product) == {"product.code": "J-1", "product.name": "Apple", "product.list": "Juices",
                                              "product.lane": 3, "product.fragile": True}
      assert flat_product_values(None) == {}
      assert data_text({"lane": 3.0, "fragile": True, "recipe": "R-1"}) == "lane=3; fragile=yes; recipe=R-1"
  ```

- [ ] **Step 2: Run the tests and make sure they fail.**

  Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_product_fields.py`

  Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the module.** For `key_from_header`: a header that is a reserved key, or one already in `taken`, gets `_2`, `_3`… The new key is added to `taken`.

- [ ] **Step 4: Run the tests and make sure they pass.** Run the same command. Expected: PASS.

- [ ] **Step 5: Commit.** Message: "Product fields: types, checks and CSV cells".

### Task 3: Lists with fields and products with data (service, catalog, API)

**Files:**
- Modify: `app/services/product_service.py`, `app/routes/v1/products.py`
- Test: `tests/test_product_data.py`

**Interfaces:**
- Consumes: Task 2 `normalize_fields`, `clean_data`, `refit_data`.
- Produces:
  - Rows:
    - `_row(product)` gains `"data"`, and `_list_row` gains `"fields"`. So `GET /products/lists`, `GET /products` and `GET /products/{id}` return them.
  - Service:
    - `ProductService.create_list(db, name, fields=None)`.
    - `ProductService.update_list(db, list_id, name=None, fields=None) -> Optional[Dict]`: the list row, plus `"dropped": int` when `fields` was given. `rename_list` stays as a thin wrapper.
    - `ProductService.create` / `update` read `data`. On `update`, leaving `data` out keeps the saved data, and `null` empties it.
  - Catalog:
    - `ProductCatalog.fields(list_id) -> List[Dict]` (`[]` for an unknown list).
    - `ProductCatalog.product_info(product: Dict, list_id: str) -> Dict` returns `{"code", "name", "list", "list_id", "fields"}` (decision P7).
    - `replace_all` and `replace_list` take each list's fields, as an extra optional argument.
  - API:
    - `PUT /products/lists/{list_id}` takes `{"name"?, "fields"?}`; at least one of them is required, otherwise 422.
    - `POST /products/lists` takes `fields`.
    - `POST /products` and `PUT /products/{id}` take `data`.
    - Bad fields or bad data give 422, with a message naming the field.

- [ ] **Step 1: Write the failing tests.** Use the `lists` fixture from `tests/test_product_lists.py` (import it as other tests do).

  ```python
  def test_a_list_gets_fields_and_its_products_carry_data(client, admin_headers, lists):
      made = lists("Juices.csv", "J-1")
      fields = [{"key": "lane", "label": "Lane", "type": "number"}, {"key": "recipe", "type": "text"}]
      res = client.put(f"{API}/lists/{made['id']}", json={"fields": fields}, headers=admin_headers)
      assert res.status_code == 200 and [f["key"] for f in res.json()["fields"]] == ["lane", "recipe"]
      assert res.json()["dropped"] == 0 and res.json()["name"] == "Juices"
      added = client.post(API, json={"list_id": made["id"], "code": "J-2", "name": "Orange", "data": {"lane": 2, "recipe": "R-7"}}, headers=admin_headers)
      assert added.status_code == 201 and added.json()["data"] == {"lane": 2, "recipe": "R-7"}
      assert product_catalog.lookup("J-2", made["id"])["data"] == {"lane": 2, "recipe": "R-7"}
      assert [f["key"] for f in product_catalog.fields(made["id"])] == ["lane", "recipe"]
      bad = client.post(API, json={"list_id": made["id"], "code": "J-3", "name": "Grape", "data": {"lane": "two"}}, headers=admin_headers)
      assert bad.status_code == 422 and "lane" in bad.json()["detail"]
      # A product changed without "data" keeps it; a rename keeps the fields.
      client.put(f"{API}/{added.json()['id']}", json={"name": "Orange juice"}, headers=admin_headers)
      assert client.get(f"{API}/{added.json()['id']}", headers=admin_headers).json()["data"] == {"lane": 2, "recipe": "R-7"}
      renamed = client.put(f"{API}/lists/{made['id']}", json={"name": "Juice range"}, headers=admin_headers).json()
      assert [f["key"] for f in renamed["fields"]] == ["lane", "recipe"]
      # Lane becomes yes/no and recipe is deleted: both values go, and the answer says so.
      res = client.put(f"{API}/lists/{made['id']}", json={"fields": [{"key": "lane", "type": "yes_no"}]}, headers=admin_headers)
      assert res.json()["dropped"] == 2
      assert client.get(f"{API}/{added.json()['id']}", headers=admin_headers).json()["data"] is None
      assert client.put(f"{API}/lists/{made['id']}", json={}, headers=admin_headers).status_code == 422

  def test_an_operator_cannot_change_fields(client, operator_headers, lists): ...  # 403, as for a rename
  ```

  `operator_headers` is the fixture at `tests/test_plc_inputs.py:1098`; import it the same way.

- [ ] **Step 2: Run the tests and make sure they fail.** Expected: FAIL (no `fields` in the answer).

- [ ] **Step 3: Implement it.**
  - `update_list` changes the fields and refits every product of the list in one commit.
  - Refresh the catalog after the commit, as `_refresh_list` does today.
  - The audit text says what changed: "Changed the fields of 'Juices' (2 values removed)".

- [ ] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_product_data.py tests/test_product_lists.py`. Expected: PASS.

- [ ] **Step 5: Commit.** Message: "Product lists have fields and products carry their data (API)".

### Task 4: CSV import and export with fields

**Files:**
- Modify: `app/services/product_service.py` (`parse_csv`, `import_csv`, `replace_contents`, `export_csv`)
- Test: `tests/test_product_data.py`

**Interfaces:**
- Consumes: Task 2 `guess_type`, `parse_cell`, `format_cell`, `key_from_header`, `clean_data`.
- Produces: `ProductService.parse_csv(text) -> CsvTable`, where:
  - `CsvTable` is a dataclass with `rows: List[CsvRow]` and `columns: List[str]` (the extra headers, as written);
  - `CsvRow` has `code`, `name`, `description` and `cells: Dict[str, str]` (header → text).

  Only `product_service.py` calls `parse_csv`.

- [ ] **Step 1: Write the failing tests.**

  ```python
  JUICES = ("code,name,description,Lane,Recipe,Fragile,Note\n"
            'J-1,Apple,,1,R-1,yes,"1,5"\nJ-2,Orange,,2,R-2,no,2\nJ-3,Grape,,,R-3,,\n')


  def test_csv_columns_get_their_types(client, admin_headers, lists):
      res = client.post(f"{API}/import", files=_upload(JUICES, "Juices.csv"), headers=admin_headers)
      assert [(f["key"], f["label"], f["type"]) for f in res.json()["list"]["fields"]] == [
          ("lane", "Lane", "number"), ("recipe", "Recipe", "text"), ("fragile", "Fragile", "yes_no"), ("note", "Note", "text")]
      shown = {p["code"]: p["data"] for p in client.get(f"{API}?list_id={res.json()['list']['id']}", headers=admin_headers).json()["products"]}
      assert shown["J-1"] == {"lane": 1, "recipe": "R-1", "fragile": True, "note": "1,5"}
      assert shown["J-3"] == {"recipe": "R-3"}            # empty cells are missing, not 0 / "" / no

  def test_an_exported_list_replaces_itself_unchanged(client, admin_headers, lists):
      list_id = client.post(f"{API}/import", files=_upload(JUICES, "Juices.csv"), headers=admin_headers).json()["list"]["id"]
      exported = client.get(f"{API}/export?list_id={list_id}", headers=admin_headers).text
      assert exported.splitlines()[0] == "code,name,description,lane,recipe,fragile,note"
      assert exported.splitlines()[1] == 'J-1,Apple,,1,R-1,yes,"1,5"'
      again = client.post(f"{API}/lists/{list_id}/replace", files=_upload(exported), headers=admin_headers).json()
      assert (again["added"], again["updated"], again["removed"]) == (0, 0, 0)

  def test_replace_keeps_fields_the_file_has_no_column_for(client, admin_headers, lists):
      # Old file with only code,name: the products keep lane, recipe, fragile, note.
      # A new value in a Lane column (header matched by label, any case) replaces only lane; updated counts it.

  def test_replace_refuses_columns_the_list_has_no_field_for(client, admin_headers, lists):
      # Header "Colour" -> 422, detail names "Colour" and says to add the field first; nothing changes.

  def test_a_formula_looking_text_value_is_exported_safely(client, admin_headers, lists):
      # recipe "=cmd|' /C calc'!A0" exports as "'=cmd|..." (_csv_safe) and imports back as typed.
  ```

- [ ] **Step 2: Run the tests and make sure they fail.** Expected: FAIL.

- [ ] **Step 3: Implement it.**
  - In `replace_contents`, a product counts as `updated` when its name, description or any data value changes.
  - A bad cell is refused with its line number, as today: `"Line 3: Lane (lane): enter a number"`.

- [ ] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_product_data.py tests/test_product_lists.py`. Expected: PASS. `test_product_lists.py` keeps passing unchanged: a file without extra columns makes a list with no fields.

- [ ] **Step 5: Commit.** Message: "Product CSV files carry the fields".

### Task 5: The product goes with the item (events, messages, templates, records, export)

**Files:**
- Modify: `app/services/line_service.py` (`on_qr_read`, `_finish_product`, `_qr_events`), `app/services/send_dispatcher_service.py` (`MESSAGE_FIELDS`, `template_values`), `app/services/line_config.py` (`parse_template`), `app/services/production_records_service.py` (`build_row`), `app/services/records_export.py` (`_FIELDS`)
- Test: `tests/test_product_data.py`

**Interfaces:**
- Consumes:
  - Task 3 `product_catalog.product_info`;
  - Task 2 `flat_product_values` and `data_text`.
- Produces (decision P7 gives the shape):
  - `read["product"]`;
  - `payload["product"]` and `plc_event["product"]` on product, code-read and code-product events of a known code;
  - `MESSAGE_FIELDS["product"] = ("product",)`;
  - `details["product"]` in a record;
  - the export column `product_data`;
  - placeholders `{product.name}`, `{product.code}`, `{product.list}` and `{product.<key>}`.

  Part 2 reads `event["product"]`.

- [ ] **Step 1: Write the failing tests.**

  Reuse `lines`, `recorder`, `_line`, `_cross`, `_run` and `LIST` from `tests/test_production_records.py`, as `test_messages_and_plc_values.py` reuses helpers from other test files.

  Add a `with_data` fixture. It monkeypatches the catalog so that `LISTED` has `data = {"lane": 3, "recipe": "R-12"}`, and `LIST` has the fields `lane` (number) and `recipe` (text), through the store Task 3 made. Parts 2 and 3 reuse this fixture.

  ```python
  WIDGET = {"code": "LISTED", "name": "Widget", "list": "List 1", "list_id": LIST, "fields": {"lane": 3, "recipe": "R-12"}}

  def test_a_known_code_brings_its_product_data(lines, recorder, with_data):
      runtime = _line(lines, [{"camera_id": "top"},
                              {"camera_id": "qr", "role": "qr", "product_list_id": LIST, "qr_action": "accept_listed"}])
      sent = []
      runtime.counter.dispatch_event = lambda payload, event, is_defect: sent.append((payload, event))

      async def steps():
          runtime.on_qr_read("qr", "LISTED", "QR_CODE")
          await asyncio.sleep(0.01)
          _cross(runtime.counter, "top")
          await asyncio.sleep(0.12)

      _run(steps)
      payload, event = sent[0]
      assert payload["product"] == WIDGET and event["product"] == WIDGET
      assert recorder._rows[0]["details"]["product"] == WIDGET

  def test_a_code_only_product_and_a_plain_read_carry_it_too(lines, recorder, with_data):
      # Code-only line (accept_listed, no vision): the code_product event's payload and PLC event carry WIDGET.
      # A "report" reader's QR_CODE_READ payload carries WIDGET too.

  def test_an_unknown_code_has_no_product(lines, recorder, with_data):
      # Read "NOPE": no "product" key in the payload, the event or the record details.
      assert render_template("[{product.lane}]", {"code": "NOPE"}) == "[]"

  def test_templates_name_product_values():
      assert render_template("{product.name} lane {product.lane}, {product.Lane}!", {"product": WIDGET}) == "Widget lane 3, !"
      assert parse_template("{Product.lane}") == ((True, "product.lane"),)
      assert parse_template("{product.Lane}") == ((True, "product.Lane"),)
      for bad in ("{product.}", "{product.1x}", "{product.a.b}"):
          with pytest.raises(ValueError):
              parse_template(bad)

  def test_a_message_can_list_the_product_field():
      assert build_message({"fields": ["product"]}, {"product": WIDGET, "class_name": "bottle"}) == {"product": WIDGET}

  def test_the_export_has_a_product_data_column():
      # A ProductRecord with details {"product": WIDGET}: csv_rows(...) gives "lane=3; recipe=R-12" in the product_data column,
      # right after stations; a record without details gives an empty cell.
  ```

- [ ] **Step 2: Run the tests and make sure they fail.** Expected: FAIL (`KeyError: 'product'`).

- [ ] **Step 3: Implement it.**
  - In `on_qr_read`: `read["product"] = product_catalog.product_info(product, list_id) if product else None`.
  - Copy the product only when it is not `None`.
  - In `build_row`, add `"product"` to the payload keys that are copied into `details`.
  - In `parse_template`, accept `product.` + `VALUE_NAME_RE`, plus `name`, `code` and `list`, with decision P8 on case.
  - In `template_values`, add the values from `flat_product_values(payload.get("product"))`.
  - Fix any test that checks the exact list of export headers, and say so in the commit message.

- [ ] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_product_data.py tests/test_production_records.py tests/test_send_cards.py tests/test_messages_and_plc_values.py tests/test_reader_actions.py`. Expected: PASS.

- [ ] **Step 5: Commit.** Message: "A known code's product data goes into events, messages, templates and records".

### Task 6: The Products page (fields editor, product dialog, columns)

**Files:**
- Create: `assets/product_fields.js`
- Modify: `assets/production_lines.js` (Products page, `buildProductsPage`, `refreshProducts`, `addProduct`, `editProduct`), `dashboard.html` (the script tag, before `production_lines.js`), `assets/dashboard.css` if needed
- Test: `tests/test_product_fields_js.py` (new)

**Interfaces:**
- Consumes: the Task 3 and Task 4 API.
- Produces:
  - `window.ProductFields` (also `module.exports` when `module` exists, so node can test it), with:
    - `keyFromLabel(label, takenKeys) -> string`: the same rule as `key_from_header`;
    - `cellText(field, value) -> string`: yes / no, a number as written, `""` for missing;
    - `fieldsEditorHtml(fields) -> string` and `readFieldsEditor(root) -> fields`;
    - `dataInputsHtml(fields, data) -> string` and `readDataInputs(root, fields) -> {data, error}`.
  - Part 2 reuses `cellText`.

- [ ] **Step 1: Write the failing tests.**
  - A node test (skipped when node is missing, as in `tests/test_lines_api.py:317`) runs:

    ```
    node -e "const F=require('<path>'); console.log(JSON.stringify([F.keyFromLabel('Expected class',[]), F.keyFromLabel('Lane',['lane']), F.cellText({type:'yes_no'},true), F.cellText({type:'number'},3), F.cellText({type:'text'},undefined)]))"
    ```

    It asserts the output is `["expected_class","lane_2","yes","3",""]`.
  - A second test: `/dashboard` includes `/assets/product_fields.js?v=2.7.0` before `production_lines.js`, and `test_the_dashboard_scripts_parse` still passes.

- [ ] **Step 2: Run the tests and make sure they fail.** Expected: FAIL (the file does not exist).

- [ ] **Step 3: Implement it.**
  - **Fields dialog.** A **Fields** button next to Rename (supervisor only) opens a dialog with one row per field:
    - label, then key (it can be typed only before the field is saved, and is filled in from the label), then type (Number / Text / Yes or no);
    - up and down buttons, and Delete;
    - **Add field** (at most 20).
  - **Saving fields.** Before saving a change that deletes a field or changes its type, ask: "Values that do not fit are removed." After saving, the toast says how many were removed.
  - **Product table.** One column per field, filled with `cellText`.
  - **Product dialog.** Add and Edit open the same dialog: Code, Name, Description, then one input per field (a number box, a text box, or a yes/no select with an empty choice). This replaces `window.prompt`.
  - **Errors.** The server's 422 text is shown in the dialog.

- [ ] **Step 4: Run the tests.** Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_product_fields_js.py tests/test_lines_api.py`. Expected: PASS.

- [ ] **Step 5: Check it in a browser.**
  - Write `backup/scratch-tools/shots_product_fields.py`, copying `shots_brand.py`. It is git-ignored and not committed.
  - Run it on the scratch copy:
    1. import `JUICES`;
    2. open the Fields dialog, add `brix` (number), and save;
    3. edit J-1 and set brix 11.5;
    4. at 1440 and at 375 px, check that the table shows the columns, there are no page errors, and there are no CSP violations (as in `check_security_csp.py`).
  - Look at the screenshots.

- [ ] **Step 6: Commit.** Message: "Products page: fields per list and a product dialog".

### Task 7: Docs, full checks and the live check

**Files:**
- Modify: `README.md` (a "Product data" subsection under "Models, product lists and the API"; the export column), `FILE_TREE.md`, `PROJECT_DESCRIPTION.md`, `API_KEY_QUICKSTART.md` (the new body keys)

- [ ] **Step 1: Write the docs.** Keep the wording plain. Include the juice CSV as the example.

- [ ] **Step 2: Run the full checks.** Run `bash backup/scratch-tools/pt.sh` and the ruff command. Expected: no failures, and ruff clean.

- [ ] **Step 3: Run the mutation checks.** Write mutations of these decisions, then run `mutcheck.py`. Expected: every mutation is CAUGHT.
  - the empty-means-missing rule;
  - the reserved keys;
  - refit dropping;
  - replace keeping the fields that have no column;
  - `product` only for a known code;
  - the template's case rule.

- [ ] **Step 4: Live check on the scratch copy.**
  1. Import `JUICES` and point a line's QR camera at the list.
  2. Have the stand-in camera (`fake_cams.py`) or the owner's test streamer show code J-1.
  3. Check that the Records page row's details (in the API answer) hold the product, and that the CSV export has `lane=1; …`.
  4. Check that a text send card with `{product.recipe}`, sent to a test MQTT broker on a spare port, delivers `R-1`.

  Remove the test data afterwards.

- [ ] **Step 5: Update the vault.**
  - `03 Decisions/Product data, values and rules decisions.md`: part 1 done, with test counts.
  - `01 Project/Status and branches.md`.
  - Today's session note.

- [ ] **Step 6: Commit.** Message: "Docs for product data".
