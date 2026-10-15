"""The card dialogs' value helpers (assets/card_values.js), run with node, and how the page loads them."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "assets" / "card_values.js"


def _node(expression: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    code = f"const C = require({json.dumps(str(SCRIPT))}); console.log(JSON.stringify({expression}));"
    result = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[:500]
    return json.loads(result.stdout)


def test_the_value_names_a_card_can_use():
    names = _node("C.valueChoices({fields: [{key: 'lane', label: 'Lane', type: 'number', list: 'Juices'}],"
                  " inputs: [{value_name: 'speed', name: 'Belt speed'}]}).map(c => c.name)")
    assert names[0] == "result"
    assert names[-5:] == ["product.name", "product.code", "product.list", "product.lane", "input.speed"]
    assert {"class", "code", "code_status", "good_count", "batch", "line", "camera"} <= set(names)
    labels = _node("C.valueChoices({fields: [{key: 'lane', label: 'Lane', type: 'number', list: 'Juices'}], inputs: []})"
                   ".filter(c => c.name === 'product.lane').map(c => c.label)")
    assert labels == ["Lane (Juices)"]


def test_the_fields_of_the_lines_code_readers():
    fields = _node("""C.fieldChoices(
        [{camera_id: 'v', role: 'vision'}, {camera_id: 'q1', role: 'qr', product_list_id: 'a'},
         {camera_id: 'q2', role: 'vision', read_codes: true, product_list_id: 'a'}, {camera_id: 'q3', role: 'qr', product_list_id: 'b'}],
        [{id: 'a', name: 'Juices', fields: [{key: 'lane', label: 'Lane', type: 'number'}]},
         {id: 'b', name: 'Bottles', fields: [{key: 'lane', label: 'Lane', type: 'number'}, {key: 'recipe', label: 'Recipe', type: 'text'}]},
         {id: 'c', name: 'Unused', fields: [{key: 'colour', label: 'Colour', type: 'text'}]}])""")
    assert [(f["key"], f["list"]) for f in fields] == [("lane", "Juices"), ("recipe", "Bottles")]


def test_how_a_card_says_what_it_writes():
    assert _node("[C.valueSourceText({value_source: 'product_field', value_field: 'lane'}),"
                 " C.valueSourceText({value_source: 'input', value_input: 'speed'}),"
                 " C.valueSourceText({value_source: 'formula', formula: 'x'}),"
                 " C.valueSourceText({value_source: 'code'}), C.valueSourceText({value_source: 'product_name'}),"
                 " C.valueSourceText({value_source: 'good_count'})]") == [
        "product.lane", "input.speed", "a formula", "the code", "the product name", None]
    fields = "[{key: 'lane', type: 'number'}, {key: 'recipe', type: 'text'}]"
    assert _node(f"[C.writesText({{value_source: 'code'}}, {fields}), C.writesText({{value_source: 'product_name'}}, {fields}),"
                 f" C.writesText({{value_source: 'product_field', value_field: 'recipe'}}, {fields}),"
                 f" C.writesText({{value_source: 'product_field', value_field: 'lane'}}, {fields}),"
                 f" C.writesText({{value_source: 'fixed'}}, {fields})]") == [True, True, True, False, False]
    assert len(_node("C.FORMULA_EXAMPLES")) >= 3


def test_the_page_loads_the_value_helpers(client):
    page = client.get("/dashboard").text
    assert "/assets/card_values.js?v=2.8.0" in page
    assert client.get("/assets/card_values.js").status_code == 200
