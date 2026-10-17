"""How the codes of a product list are built, and reading one code as a product ID and a batch.

A list's format (``product_lists.code_format``):

    {"mode": "whole"}     the whole code is the product ID (the default)
    {"mode": "split", "separator": ";", "product_part": 1, "batch_part": 2}
                          parts with a separator: "P-100;B-2026-07" is product P-100, batch B-2026-07
    {"mode": "gs1"}       GS1: the (01) GTIN is the product ID, (10) the batch

split_code() never raises: a code that does not fit says why in ``problem``.
No pattern is ever built from what a user typed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

MODES = ("whole", "split", "gs1")
DEFAULT_FORMAT: Dict[str, Any] = {"mode": "whole"}
MAX_PART = 10
MAX_SEPARATOR = 3

# GS1 Application Identifiers this reader knows: data length, or None for a variable length
# (ended by the GS character, or the end of the code). 31nn to 36nn (4 digits, 6 data digits)
# are matched apart. An AI that is not here stops reading a raw code (unknown GS1 part).
GS1_AIS: Dict[str, Optional[int]] = {
    "00": 18, "01": 14, "02": 14, "11": 6, "12": 6, "13": 6, "15": 6, "16": 6, "17": 6, "20": 2,
    "10": None, "21": None, "22": None, "30": None, "37": None,
    "240": None, "241": None, "250": None, "251": None, "400": None, "401": None, "402": None, "403": None,
    **{str(n): None for n in range(90, 100)},
}
GS = "\x1d"
_SYMBOLOGY_IDS = ("]Q3", "]d2", "]C1")
_READABLE = re.compile(r"(?:\(\d{2,4}\)[^()]*)+")
_READABLE_PART = re.compile(r"\((\d{2,4})\)([^()]*)")


@dataclass(frozen=True)
class CodeParts:
    """What a code says: the product ID, the batch, and why it does not fit (each None when there is none)."""
    product_id: Optional[str]
    batch: Optional[str]
    problem: Optional[str]


def _part(value: Any, name: str, default: Optional[int], allow_none: bool) -> Optional[int]:
    if value is None:
        if allow_none:
            return None
        value = default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PART:
        raise ValueError(f"Code format: the {name} part must be a number from 1 to {MAX_PART}")
    return value


def normalize_code_format(raw: Any) -> Dict[str, Any]:
    """A list's format as it is saved, with only its own keys. Raises ValueError (``Code format: ...``)."""
    if raw is None:
        return dict(DEFAULT_FORMAT)
    if not isinstance(raw, dict):
        raise ValueError("Code format: send it as an object, such as {\"mode\": \"whole\"}")
    mode = str(raw.get("mode") or "whole").strip().lower()
    if mode not in MODES:
        raise ValueError("Code format: the mode must be 'whole', 'split' or 'gs1'")
    if mode != "split":
        return {"mode": mode}
    separator = raw.get("separator", ";")
    if not isinstance(separator, str) or not 1 <= len(separator) <= MAX_SEPARATOR or not separator.strip() \
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in separator):
        raise ValueError(f"Code format: the separator must be 1 to {MAX_SEPARATOR} characters, "
                         f"not only spaces, without a line break or control character")
    product_part = _part(raw.get("product_part"), "product", 1, allow_none=False)
    batch_part = _part(raw.get("batch_part", 2), "batch", 2, allow_none=True)
    if batch_part == product_part:
        raise ValueError("Code format: the batch part must be different from the product part")
    return {"mode": "split", "separator": separator, "product_part": product_part, "batch_part": batch_part}


def split_code(text: Any, fmt: Any) -> CodeParts:
    """The product ID and the batch of one code read, by a list's format. Never raises."""
    code = str(text if text is not None else "").strip()
    try:
        fmt = normalize_code_format(fmt)
    except ValueError:
        fmt = dict(DEFAULT_FORMAT)
    if fmt["mode"] == "split":
        return _split(code, fmt)
    if fmt["mode"] == "gs1":
        return _gs1(code)
    return CodeParts(code, None, None) if code else CodeParts(None, None, "empty product ID")


def _split(code: str, fmt: Dict[str, Any]) -> CodeParts:
    separator, product_part, batch_part = fmt["separator"], fmt["product_part"], fmt["batch_part"]
    if separator not in code:
        if batch_part is None and product_part == 1 and code:
            return CodeParts(code, None, None)
        return CodeParts(None, None, "no separator")
    parts = [part.strip() for part in code.split(separator)]
    if len(parts) < product_part:
        return CodeParts(None, None, "no product part")
    product_id = parts[product_part - 1]
    if not product_id:
        return CodeParts(None, None, "empty product ID")
    if batch_part is None:
        return CodeParts(product_id, None, None)
    batch = parts[batch_part - 1] if len(parts) >= batch_part else ""
    return CodeParts(product_id, batch, None) if batch else CodeParts(product_id, None, "no batch part")


def _gs1(code: str) -> CodeParts:
    for prefix in _SYMBOLOGY_IDS:
        if code.startswith(prefix):
            code = code[len(prefix):]
            break
    code = code.lstrip(GS)
    if code.startswith("("):
        if not _READABLE.fullmatch(code):
            return CodeParts(None, None, "not GS1")
        values, problem = dict(reversed(_READABLE_PART.findall(code))), None
    elif code[:1].isdigit():
        values, problem = _walk_raw(code)
    else:
        return CodeParts(None, None, "not GS1")
    product_id = values.get("01")
    if not (product_id and len(product_id) == 14 and product_id.isdigit()):
        return CodeParts(None, values.get("10") or None, "no (01) part")
    batch = values.get("10") or None
    if problem:
        return CodeParts(product_id, batch, problem)
    return CodeParts(product_id, batch, None if batch else "no batch part")


def _ai_at(code: str, pos: int) -> Tuple[Optional[str], Optional[int]]:
    """The AI that starts at ``pos`` and its data length (None: variable), or (None, None) when unknown."""
    for size in (2, 3):
        ai = code[pos:pos + size]
        if len(ai) == size and ai in GS1_AIS:
            return ai, GS1_AIS[ai]
    ai = code[pos:pos + 4]
    if len(ai) == 4 and ai.isdigit() and "31" <= ai[:2] <= "36":
        return ai, 6
    return None, None


def _walk_raw(code: str) -> Tuple[Dict[str, str], Optional[str]]:
    """The parts of a raw GS1 code ({AI: value}, first of each), and a problem when the walk stopped early."""
    values: Dict[str, str] = {}
    pos = 0
    while pos < len(code):
        if code[pos] == GS:
            pos += 1
            continue
        ai, length = _ai_at(code, pos)
        if ai is None:
            return values, "unknown GS1 part"
        start = pos + len(ai)
        if length is None:
            end = code.find(GS, start)
            end = len(code) if end < 0 else end
        else:
            end = start + length
            if end > len(code):
                return values, "unknown GS1 part"
        values.setdefault(ai, code[start:end])
        pos = end
    return values, None

