"""A product list's fields and a product's values: types, checks and CSV cells (app.services.product_fields)."""
from __future__ import annotations

import pytest

from app.services.product_fields import (
    clean_data,
    data_text,
    flat_product_values,
    guess_type,
    key_from_header,
    normalize_fields,
    refit_data,
)

LANE = {"key": "lane", "label": "Lane", "type": "number"}
RECIPE = {"key": "recipe", "label": "recipe", "type": "text"}
FRAGILE = {"key": "fragile", "label": "Fragile", "type": "yes_no"}


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
    ("lane", "list"),
    (["lane"], "object"),
])
def test_bad_fields_are_refused(bad, word):
    with pytest.raises(ValueError, match=word):
        normalize_fields(bad)


def test_values_are_typed_and_empty_means_missing():
    fields = [LANE, RECIPE, FRAGILE]
    assert clean_data(fields, {"lane": "3", "recipe": " R-12 ", "fragile": "Yes"}) == {"lane": 3, "recipe": "R-12", "fragile": True}
    assert clean_data(fields, {"lane": 2.5, "fragile": False}) == {"lane": 2.5, "fragile": False}
    assert clean_data(fields, {"lane": "", "recipe": None}) is None
    assert clean_data(fields, None) is None
    # Field order, whatever order the values came in.
    assert list(clean_data(fields, {"fragile": "no", "lane": 1})) == ["lane", "fragile"]
    for raw, word in (({"lane": "three"}, "lane"), ({"colour": "red"}, "colour"), ({"recipe": "x" * 257}, "256"),
                      ({"lane": float("nan")}, "lane"), ({"fragile": "maybe"}, "fragile"), ({"lane": True}, "lane"),
                      ({"recipe": ["a"]}, "recipe"), ("lane=3", "object")):
        with pytest.raises(ValueError, match=word):
            clean_data(fields, raw)


def test_a_type_change_drops_only_what_no_longer_fits():
    as_text = {**LANE, "type": "text"}
    assert refit_data([as_text], {"lane": 3, "recipe": "R"}) == ({"lane": "3"}, 1)
    assert refit_data([{**RECIPE, "type": "number"}], {"recipe": "7"}) == ({"recipe": 7}, 0)
    assert refit_data([{**LANE, "type": "yes_no"}], {"lane": 2}) == (None, 1)
    assert refit_data([LANE], None) == (None, 0)


@pytest.mark.parametrize("cells, kind", [
    (["1", "2.5", ""], "number"), (["1", "0"], "number"), (["yes", "No", "TRUE", "0"], "yes_no"),
    (["1,5", "2"], "text"), (["", ""], "text"), (["=1+1"], "text"), (["-3", "4.5"], "number"), (["-3", "+4.5"], "text"), (["1e3"], "text"),
])
def test_guess_type(cells, kind):
    assert guess_type(cells) == kind


def test_header_keys_never_clash():
    taken = set()
    keys = [key_from_header(h, taken) for h in ("Lane", "lane", "Expected class", "2nd colour", "List", "x" * 40)]
    assert keys == ["lane", "lane_2", "expected_class", "f_2nd_colour", "list_2", "x" * 32]
    assert key_from_header("  ", set()) == "field"


def test_flat_values_and_export_text():
    product = {"code": "J-1", "name": "Apple", "list": "Juices", "list_id": "list-1", "fields": {"lane": 3, "fragile": True}}
    assert flat_product_values(product) == {"product.code": "J-1", "product.name": "Apple", "product.list": "Juices",
                                            "product.lane": 3, "product.fragile": True}
    assert flat_product_values(None) == {}
    assert data_text({"lane": 3.0, "fragile": True, "recipe": "R-1"}) == "lane=3; fragile=yes; recipe=R-1"
    assert data_text(None) == "" and data_text({}) == ""


@pytest.mark.parametrize("cells, kind", [(["0042", "17"], "text"), (["+44", "1"], "text"), (["0", "0.5", "10"], "number"),
                                         (["-0.5", "-3"], "number"), (["00.5"], "text")])
def test_leading_zeros_and_plus_signs_keep_a_column_text(cells, kind):
    assert guess_type(cells) == kind
