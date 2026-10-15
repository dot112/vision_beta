"""The small formula language of output actions (app.services.formula): what it computes, and what it refuses.

Nothing a user types is ever passed to eval or exec: a formula is parsed with
ast, checked against a whitelist, and computed by a tree walker.
"""
from __future__ import annotations

import re

import pytest

from app.services.formula import MISSING, FormulaError, compare, parse

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
    ("good_count == 7 or product.missing", True), ("false or product.fragile", True),
])
def test_formulas_compute(text, value):
    assert parse(text).evaluate(V) == value


def test_numbers_keep_their_kind():
    assert parse("product.lane * 2").evaluate(V) == 6 and isinstance(parse("product.lane * 2").evaluate(V), int)
    assert parse("text(2.0)").evaluate({}) == "2" and parse("text(true)").evaluate({}) == "true"
    assert parse("round(2.5)").evaluate({}) == 2 and parse("round(3.5)").evaluate({}) == 4   # as Python rounds
    assert parse("int(-2.7)").evaluate({}) == -2


@pytest.mark.parametrize("text", ["product.missing + 1", "good_count / 0", "good_count % 0", "product.recipe * 2",
                                  '"a" + 1', "int(product.recipe)", "if(product.missing > 1, 1, 2)", "1e308 * 10",
                                  "product.missing and true", "min(product.recipe, 2)", "abs(product.recipe)"])
def test_missing_values_and_bad_types_give_missing(text):
    assert parse(text).evaluate(V) is MISSING


def test_if_only_computes_the_branch_it_takes():
    assert parse('if(result == "good", 1, product.missing)').evaluate(V) == 1
    assert parse('if(result == "good", 1, 1 / 0)').evaluate(V) == 1


def test_names_and_keywords():
    assert parse("if(class == product.expected_class, input.speed, 0)").names == frozenset(
        {"class", "product.expected_class", "input.speed"})
    assert parse("product.from + 1").evaluate({"product.from": 1}) == 2
    assert parse("product.from + 1").names == frozenset({"product.from"})
    assert parse("true").names == frozenset()


@pytest.mark.parametrize("text, word", [
    ('__import__("os")', "not a value"), ("().__class__", "not a value"), ("(1).real", "only product"),
    ("product.lane.real", "one level"),
    ("speed + 1", "unknown value 'speed'"), ("eval(1)", "unknown function 'eval'"), ("2 ** 8", "not allowed"),
    ("'x'", "double quotes"), ('f"{1}"', "not allowed"), ("lambda: 1", "not allowed"), ("[1][0]", "not allowed"),
    ("1 < good_count < 3", "two values"), ("round(1, digits=1)", "not allowed"), ("round()", "takes"),
    ("if(1, 2)", "takes"), ("1\n+1", "one line"), ("", "empty"), ("   ", "empty"), ("1" * 201, "200 characters"),
    ("True", "unknown value 'True'"), ("_kw_if(1, 2, 3)", "not a value"), ("1 +", "position"),
    ("0x1F", "number"), ("1j", "number"), ("1_000", "number"), ("good_count // 2", "not allowed"),
    ("round(1, 7)", "0 to 6"), ("product", "product.<field>"), ("(1, 2)", "not allowed"),
    ("+".join(["1"] * 70), "too long"),
])
def test_hostile_or_wrong_text_is_refused(text, word):
    with pytest.raises(FormulaError, match=re.escape(word)):
        parse(text)


ODD = ['"abc', "$", "é + 1", "product.größe", "1 if 2 else 3", "not", "(", ")", ",", "a.b", '"""x"""', "1 +* 2",
       "product.lane()", "min(*[1])", "\\", "#comment", "1 # c", "if", "class", 'b"x"', 'r"x"', "yield 1", "await x",
       "1;2", "x:=1", "...", "None", "product.__class__", "1 is 1", "1 in 2", "~1", "1 << 2", "1 @ 2", "{}",
       "print(1)", "if(1, 2, 3)(4)", "product.lane.x.y", "-", "not not", '"\\N{BULLET}" + 1']


@pytest.mark.parametrize("text", ODD)
def test_odd_text_is_refused_or_computed_never_a_crash(text):
    try:
        parse(text).evaluate({"product.lane": 3})
    except FormulaError:
        pass


def test_comments_and_odd_but_valid_text():
    with pytest.raises(FormulaError, match="not allowed"):    # the language has no comments
        parse("1 # c")
    assert parse('"""x"""').evaluate({}) == "x"
    assert parse('text("é") + "ü"').evaluate({}) == "éü"
    assert parse("  1 + 1  ").evaluate({}) == 2
    with pytest.raises(FormulaError) as caught:
        parse("  speed")
    assert caught.value.position == 3


def test_an_error_names_the_position_as_typed():
    with pytest.raises(FormulaError) as caught:
        parse("good_count + 'x'")
    assert caught.value.position == 14
    with pytest.raises(FormulaError) as caught:
        parse("if(1, 2, speed)")
    assert caught.value.position == 10 and "speed" in str(caught.value)


@pytest.mark.parametrize("left, op, right, want", [
    (3, "==", 3.0, True), ("3", "==", 3, True), ("Apple", "==", "apple", True), (True, "==", "yes", True),
    (False, "==", "No", True), (True, "!=", "no", True), (True, "<", 2, False), ("10", ">", "9", True),
    ("b", ">", "A", True), (" good", "==", "good", False), (2, "<=", 2, True), ("abc", "!=", "ABC", False),
])
def test_one_comparison_rule(left, op, right, want):
    assert compare(left, op, right) is want


def test_a_missing_side_compares_as_missing():
    assert compare(MISSING, "==", 1) is MISSING and compare(1, "!=", MISSING) is MISSING
