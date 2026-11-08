"""The text reader's format notation: 9 a digit, A a letter, X either, * any one character.

A reading fits a format when, spaces left out and in capitals, each character fits its place after
the look-alike fixes (O -> 0 where a digit is wanted, 8 -> B where a letter is).
"""
from __future__ import annotations

import pytest

from app.engines.text_format import allowed_characters, check_format, fit, suggest_format


@pytest.mark.parametrize("fmt, reading, expected", [
    ("LOT A99999", "LOT A12345", ("LOT A12345", True, 0)),
    ("LOT A99999", "lot a12345", ("LOT A12345", True, 0)),            # capitals
    ("EXP 99/99/9999", "EXP12/O3/2027", ("EXP12/03/2027", True, 0)),  # spaces ignored, O -> 0 at a 9
    ("LOT A99999", "LOT 812345", ("LOT B12345", True, 0)),            # 8 -> B at an A: the shape fits
    ("LOT A99999", "L0T A12345", ("L0T A12345", False, 1)),           # literals are not fixed
    ("99/99", "12/345", ("12/345", False, 1)),                        # one too many
    ("99/99", "12/3", ("12/3", False, 1)),                            # one too few
    ("L*9", "L-5", ("L-5", True, 0)),
    ("X9", "B7", ("B7", True, 0)),                                    # nothing fixed at an X
])
def test_fit(fmt, reading, expected):
    assert tuple(fit(fmt, reading)) == expected


def test_allowed_characters():
    assert allowed_characters(["99/99"]) == " /0123456789"
    assert allowed_characters(["LOT A99999"]) == " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    assert allowed_characters(["99", "L*9"]) is None


@pytest.mark.parametrize("reading, fmt", [("LOT 12345", "LOT 99999"), ("EXP 03/2027", "EXP 99/9999"), (" a1 ", "A9")])
def test_suggest_format(reading, fmt):
    assert suggest_format(reading) == fmt


def test_check_format():
    assert check_format("  LOT 9 ", "Field") == "LOT 9"
    for bad in ("", "   ", "9" * 65, None, 5):
        with pytest.raises(ValueError, match="1 to 64 characters"):
            check_format(bad, "Field")
