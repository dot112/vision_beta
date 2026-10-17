"""Reading a code as a product ID and a batch: whole code, parts with a separator, or GS1."""
from __future__ import annotations

import pytest

from app.services.code_format import CodeParts, normalize_code_format, split_code

SPLIT = {"mode": "split", "separator": ";", "product_part": 1, "batch_part": 2}
GS1 = {"mode": "gs1"}


def test_a_whole_code_is_the_product_id():
    assert split_code("P-100;B-2026-07", {"mode": "whole"}) == CodeParts("P-100;B-2026-07", None, None)
    assert split_code("  P-100 \r\n", {"mode": "whole"}) == CodeParts("P-100", None, None)


@pytest.mark.parametrize("text, want", [
    ("P-100;B-2026-07", ("P-100", "B-2026-07", None)),        # BOX-001, -002, -004, -005
    ("P-999;B-2026-07", ("P-999", "B-2026-07", None)),        # BOX-006: parsed; unknown in the list
    ("P-100", (None, None, "no separator")),
    ("P-100;", ("P-100", None, "no batch part")),
    (";B-1", (None, None, "empty product ID")),
    ("P-100;B;7", ("P-100", "B", None)),
])
def test_split(text, want):
    parts = split_code(text, SPLIT)
    assert (parts.product_id, parts.batch, parts.problem) == want


def test_split_trims_each_part():
    assert split_code(" P-100 ; B-2026-07\r\n", SPLIT) == CodeParts("P-100", "B-2026-07", None)


def test_split_without_a_batch_part_takes_the_whole_code():
    assert split_code("P-100", {**SPLIT, "batch_part": None}) == CodeParts("P-100", None, None)
    assert split_code("P-100;X", {**SPLIT, "batch_part": None}) == CodeParts("P-100", None, None)


def test_split_by_position():
    fmt = {"mode": "split", "separator": "|", "product_part": 3, "batch_part": 1}
    assert split_code("L7|line 2|SKU-9", fmt) == CodeParts("SKU-9", "L7", None)
    assert split_code("L7|line 2", fmt) == CodeParts(None, None, "no product part")


@pytest.mark.parametrize("text, want", [
    ("(01)09501101530003(10)L-2026-07", ("09501101530003", "L-2026-07", None)),
    ("(01)09501101530003(17)271231(10)AB1", ("09501101530003", "AB1", None)),
    ("0109501101530003" + "10L-2026-07", ("09501101530003", "L-2026-07", None)),
    ("]Q3\x1d0109501101530003" + "10AB1\x1d21SN-9", ("09501101530003", "AB1", None)),
    ("]d20109501101530003" + "10AB1", ("09501101530003", "AB1", None)),
    ("010950110153000317271231" + "10AB1", ("09501101530003", "AB1", None)),
    ("(01)09501101530003", ("09501101530003", None, "no batch part")),
    ("(10)AB1", (None, "AB1", "no (01) part")),
    ("(01)123(10)AB1", (None, "AB1", "no (01) part")),
    ("P-100;B-1", (None, None, "not GS1")),
    ("(01)09501101530003junk", (None, None, "no (01) part")),     # a (01) that is not 14 digits
    ("0109501101530003" + "7003X", ("09501101530003", None, "unknown GS1 part")),
    ("0109501101530003" + "3103001250" + "10AB1", ("09501101530003", "AB1", None)),
])
def test_gs1(text, want):
    parts = split_code(text, GS1)
    assert (parts.product_id, parts.batch, parts.problem) == want


@pytest.mark.parametrize("raw, word", [
    ({"mode": "regex"}, "mode"),
    ({**SPLIT, "separator": ""}, "separator"),
    ({**SPLIT, "separator": "    "}, "separator"),
    ({**SPLIT, "separator": "\t"}, "separator"),
    ({**SPLIT, "separator": ";;;;"}, "separator"),
    ({**SPLIT, "product_part": 0}, "product"),
    ({**SPLIT, "product_part": "x"}, "product"),
    ({**SPLIT, "batch_part": 11}, "batch"),
    ({**SPLIT, "batch_part": 1}, "different"),
    ("split", "object"),
])
def test_bad_formats_are_refused(raw, word):
    with pytest.raises(ValueError, match=f"Code format:.*{word}"):
        normalize_code_format(raw)


def test_formats_are_saved_in_one_shape():
    assert normalize_code_format(None) == {"mode": "whole"}
    assert normalize_code_format({"mode": "gs1", "separator": ";"}) == {"mode": "gs1"}
    assert normalize_code_format({"mode": "split", "separator": "|"}) == \
        {"mode": "split", "separator": "|", "product_part": 1, "batch_part": 2}
    assert normalize_code_format({**SPLIT, "batch_part": None})["batch_part"] is None


def test_reading_never_raises():
    for fmt in ({"mode": "whole"}, SPLIT, GS1, {"mode": "nonsense"}, {}):
        for text in ("", "(", "((01)", "01", "\x1d", "]Q3", ";", "(01)(10)", "0" * 400):
            assert isinstance(split_code(text, fmt), CodeParts)
