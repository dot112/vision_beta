"""The Products page's field helpers (assets/product_fields.js), run with node, and how the page loads them."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "assets" / "product_fields.js"


def _node(expression: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    code = f"const F = require({json.dumps(str(SCRIPT))}); console.log(JSON.stringify({expression}));"
    result = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr[:500]
    return json.loads(result.stdout)


def test_keys_from_labels_and_cell_text():
    assert _node("[F.keyFromLabel('Expected class', []), F.keyFromLabel('Lane', ['lane']), F.keyFromLabel('List', []),"
                 " F.keyFromLabel('2nd colour', []), F.keyFromLabel('  ', [])]") == [
        "expected_class", "lane_2", "list_2", "f_2nd_colour", "field"]
    assert _node("[F.cellText({type: 'yes_no'}, true), F.cellText({type: 'yes_no'}, false), F.cellText({type: 'number'}, 3),"
                 " F.cellText({type: 'number'}, 2.5), F.cellText({type: 'text'}, undefined), F.cellText({type: 'text'}, null)]") == [
        "yes", "no", "3", "2.5", "", ""]


def test_values_typed_in_the_product_dialog():
    fields = "[{key: 'lane', label: 'Lane', type: 'number'}, {key: 'recipe', label: 'Recipe', type: 'text'}, {key: 'ok', label: 'OK', type: 'yes_no'}]"
    assert _node(f"F.valuesFromInputs({fields}, {{lane: ' 3 ', recipe: ' R-1 ', ok: 'yes'}})") == {
        "data": {"lane": 3, "recipe": "R-1", "ok": True}, "error": None}
    assert _node(f"F.valuesFromInputs({fields}, {{lane: '', recipe: '', ok: ''}})") == {"data": None, "error": None}
    assert _node(f"F.valuesFromInputs({fields}, {{lane: 'three'}})")["error"] == "Lane: enter a number"


def test_the_page_loads_the_helper_before_the_products_page(client):
    page = client.get("/dashboard").text
    helper = page.index('/assets/product_fields.js?v=2.7.0')
    assert helper < page.index("/assets/production_lines.js?v=")
    assert client.get("/assets/product_fields.js").status_code == 200
