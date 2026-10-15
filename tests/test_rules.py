"""Only-when rules (app.services.rules): checked once for output actions, send cards and product checks."""
from __future__ import annotations

import pytest

from app.services.rules import is_value_name, normalize_only_when, rules_pass

VALUES = {"result": "good", "class": "Apple", "code": "J-1", "input.speed": 12,
          "product.lane": 3, "product.expected_class": "apple", "product.fragile": True}


@pytest.mark.parametrize("rule, want", [
    ({"left": "product.lane", "op": "=", "right": "3"}, True), ({"left": "product.lane", "op": ">=", "right": "3.0"}, True),
    ({"left": "class", "op": "=", "right": "product.expected_class", "right_kind": "value"}, True),
    ({"left": "class", "op": "!=", "right": "product.expected_class", "right_kind": "value"}, False),
    ({"left": "product.fragile", "op": "=", "right": "Yes"}, True), ({"left": "code", "op": "contains", "right": "j-"}, True),
    ({"left": "code", "op": "contains", "right": "x"}, False),
    ({"left": "product.recipe", "op": "empty"}, True), ({"left": "product.recipe", "op": "not_empty"}, False),
    ({"left": "class", "op": "not_empty"}, True), ({"left": "class", "op": "empty"}, False),
    ({"left": "product.missing", "op": "!=", "right": "3"}, False),
    ({"left": "class", "op": "=", "right": "product.missing", "right_kind": "value"}, False),
    ({"left": "input.speed", "op": "<", "right": "20"}, True), ({"left": "input.speed", "op": ">", "right": "20"}, False),
    ({"left": "result", "op": "=", "right": " good "}, True),
])
def test_one_rule(rule, want):
    assert rules_pass(normalize_only_when({"rules": [rule]}, "Card 'x'"), VALUES) is want


def test_all_and_any():
    yes, no = {"left": "class", "op": "=", "right": "apple"}, {"left": "class", "op": "=", "right": "pear"}
    assert rules_pass(normalize_only_when({"match": "any", "rules": [no, yes]}, "x"), VALUES)
    assert not rules_pass(normalize_only_when({"match": "all", "rules": [no, yes]}, "x"), VALUES)
    assert rules_pass(normalize_only_when({"rules": [yes, yes]}, "x"), VALUES)          # "all" by default
    assert rules_pass(None, VALUES)


def test_an_empty_text_value_is_empty():
    assert rules_pass(normalize_only_when({"rules": [{"left": "code", "op": "empty"}]}, "x"), {"code": ""})


@pytest.mark.parametrize("raw, word", [
    ({"rules": [{"left": "speed", "op": "="}]}, "speed"), ({"rules": [{"left": "class", "op": "~", "right": "a"}]}, "operator"),
    ({"match": "most", "rules": [{"left": "class", "op": "empty"}]}, "all"),
    ({"rules": [{"left": "class", "op": "empty"}] * 6}, "5"),
    ({"rules": [{"left": "class", "op": "=", "right": "x" * 257}]}, "256"),
    ({"rules": [{"left": "class", "op": "=", "right": "1x", "right_kind": "value"}]}, "value"),
    ({"rules": [{"left": "class", "op": "=", "right": "a", "right_kind": "both"}]}, "value"),
    ({"rules": "class = a"}, "list"), ("class = a", "object"), ({"rules": ["class"]}, "object"),
])
def test_bad_rules_are_refused_and_name_the_card(raw, word):
    with pytest.raises(ValueError, match=f"Card 'x'.*{word}"):
        normalize_only_when(raw, "Card 'x'")


def test_empty_and_trimmed():
    assert normalize_only_when(None, "x") is None and normalize_only_when({"rules": []}, "x") is None
    assert normalize_only_when({}, "x") is None
    with pytest.raises(ValueError, match="at least one rule"):
        normalize_only_when({"rules": []}, "x", required=True)
    kept = normalize_only_when({"rules": [{"left": "result", "op": "=", "right": " good "},
                                          {"left": "code", "op": "empty", "right": "z"}]}, "x")
    assert kept == {"match": "all", "rules": [{"left": "result", "op": "=", "right": "good", "right_kind": "constant"},
                                              {"left": "code", "op": "empty", "right": "", "right_kind": "constant"}]}
    assert normalize_only_when({"rules": [{"left": "product.lane", "op": "=", "right": 3}]}, "x")["rules"][0]["right"] == "3"


@pytest.mark.parametrize("name, ok", [
    ("result", True), ("class", True), ("product.lane", True), ("product.name", True), ("input.speed", True),
    ("input.value", True), ("input.previous", True), ("speed", False), ("product", False), ("product.1x", False),
    ("product.a.b", False), ("other.lane", False), ("", False),
])
def test_value_names(name, ok):
    assert is_value_name(name) is ok
