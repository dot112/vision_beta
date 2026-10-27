"""The text reader's format notation (dates and lot codes printed on products).

    9   a digit            A   a letter A-Z
    X   a letter or digit  *   any one character
    any other character must be that character

Spaces are left out on both sides (printers and the reader both move them), the reading is put in
capitals, and where the format asks for one kind, look-alikes are fixed first: at a 9, O -> 0,
I -> 1, S -> 5 ...; at an A, the other way round. Nothing is changed at X, * or a literal.
No runtime imports: the settings check and the reader both use it.
"""
from __future__ import annotations

import string
from typing import Any, Iterable, NamedTuple, Optional

MAX_FORMAT_CHARS = 64
DIGIT_FIXES = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}
LETTER_FIXES = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"}
_DIGITS = frozenset(string.digits)
_LETTERS = frozenset(string.ascii_uppercase)


class Fit(NamedTuple):
    """A reading against a format: the reading after the fixes (spaces as read), whether it fits,
    and how far it is from fitting (positions that do not fit, plus the difference in length)."""
    text: str
    fits: bool
    differences: int


def check_format(value: Any, label: str) -> str:
    """A format as saved: stripped, 1 to MAX_FORMAT_CHARS characters."""
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > MAX_FORMAT_CHARS:
        raise ValueError(f"{label}: the format must be 1 to {MAX_FORMAT_CHARS} characters")
    return text


def _matches(token: str, ch: str) -> bool:
    if token == "9":
        return ch in _DIGITS
    if token == "A":
        return ch in _LETTERS
    if token == "X":
        return ch in _DIGITS or ch in _LETTERS
    return token == "*" or token == ch


def fit(fmt: str, reading: str) -> Fit:
    """Each non-space character of the reading takes the next non-space token of the format."""
    tokens = [c for c in fmt.upper() if c != " "]
    out, k, differences = [], 0, 0
    for ch in reading.upper():
        if ch == " ":
            out.append(ch)
            continue
        if k < len(tokens):
            token = tokens[k]
            ch = DIGIT_FIXES.get(ch, ch) if token == "9" else LETTER_FIXES.get(ch, ch) if token == "A" else ch
            differences += not _matches(token, ch)
            k += 1
        else:
            differences += 1
        out.append(ch)
    differences += len(tokens) - k
    return Fit("".join(out).strip(), differences == 0, differences)


def allowed_characters(formats: Iterable[str]) -> Optional[str]:
    """Every character the formats can accept, plus space, as one sorted string; None when one has *."""
    allowed = {" "}
    for fmt in formats:
        for token in fmt.upper():
            if token == "*":
                return None
            if token in ("9", "X"):
                allowed |= _DIGITS
            if token in ("A", "X"):
                allowed |= _LETTERS
            if token not in ("9", "A", "X"):
                allowed.add(token)
    return "".join(sorted(allowed))


def suggest_format(reading: str) -> str:
    """The format a reading suggests: each digit becomes 9, every other character stays as read."""
    return "".join("9" if ch in _DIGITS else ch for ch in reading.strip().upper())
