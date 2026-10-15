"""A product list's own fields and a product's values for them.

Each list may have up to MAX_FIELDS fields: ``{"key", "label", "type"}`` with
the type number, text or yes_no. A product keeps its values as ``data``:
``{key: value}``, typed, without the fields it leaves empty (a missing value
is absent, never 0, "" or no). Cards, formulas and message templates name a
field by its key (``product.lane``), so a key never changes once saved; the
label can.

Pure functions: nothing here touches the database.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# A value name: a field key, an input's value name, a word of a formula.
VALUE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
FIELD_TYPES = ("number", "text", "yes_no")
# The product's own values: product.name, product.code, product.list.
RESERVED_KEYS = ("name", "code", "list", "description")
MAX_FIELDS = 20
MAX_TEXT_VALUE_LENGTH = 256
MAX_FIELD_LABEL_LENGTH = 64
YES_WORDS = ("yes", "true", "1")
NO_WORDS = ("no", "false", "0")

# A number as people write it in a cell: digits with an optional sign and "." decimals.
_NUMBER_RE = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")


def _has_control(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _named(field: Dict[str, str]) -> str:
    """How an error names a field: 'Lane (lane)', or just the key when the label is the key."""
    return field["key"] if field.get("label") in (None, "", field["key"]) else f"{field['label']} ({field['key']})"


def normalize_fields(raw: Any) -> List[Dict[str, str]]:
    """A list's fields as they are saved, in their order. Raises ValueError naming the field."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("The fields must be a list")
    if len(raw) > MAX_FIELDS:
        raise ValueError(f"A product list has at most {MAX_FIELDS} fields")
    fields: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Field {index} must be an object with a key, a label and a type")
        key = str(entry.get("key") if entry.get("key") is not None else "").strip()
        if not VALUE_NAME_RE.fullmatch(key):
            raise ValueError(
                f"Field {index}: a key starts with a letter and has only letters, digits and _ (at most 32 characters)"
            )
        if key in RESERVED_KEYS:
            raise ValueError(f"Field {index}: the key '{key}' is used by the product itself; choose another")
        if key in seen:
            raise ValueError(f"The key '{key}' is used twice; each field needs its own")
        seen.add(key)
        kind = str(entry.get("type") or "").strip().lower()
        if kind not in FIELD_TYPES:
            raise ValueError(f"Field '{key}': '{kind}' is not a type; use number, text or yes_no")
        label = re.sub(r"\s+", " ", str(entry.get("label") if entry.get("label") is not None else "")).strip() or key
        if len(label) > MAX_FIELD_LABEL_LENGTH:
            raise ValueError(f"Field '{key}': the label must be {MAX_FIELD_LABEL_LENGTH} characters or fewer")
        if _has_control(label):
            raise ValueError(f"Field '{key}': the label has a character that is not allowed")
        fields.append({"key": key, "label": label, "type": kind})
    return fields


def _number(text: str) -> Optional[Any]:
    text = text.strip()
    if not _NUMBER_RE.fullmatch(text):
        return None
    return float(text) if "." in text else int(text)


def clean_value(field: Dict[str, str], raw: Any) -> Any:
    """One value typed for its field; None when it is empty. Raises ValueError naming the field."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    kind = field["type"]
    if kind == "number":
        if isinstance(raw, bool):
            raise ValueError(f"{_named(field)}: enter a number")
        if isinstance(raw, (int, float)):
            if not math.isfinite(raw):
                raise ValueError(f"{_named(field)}: enter a number")
            return raw
        number = _number(raw) if isinstance(raw, str) else None
        if number is None or not math.isfinite(number):
            raise ValueError(f"{_named(field)}: enter a number")
        return number
    if kind == "yes_no":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        word = raw.strip().lower() if isinstance(raw, str) else None
        if word in YES_WORDS:
            return True
        if word in NO_WORDS:
            return False
        raise ValueError(f"{_named(field)}: enter yes or no")
    # Text: a number or a yes / no becomes its text.
    if isinstance(raw, (bool, int, float)) and not (isinstance(raw, float) and not math.isfinite(raw)):
        raw = format_cell(raw)
    if not isinstance(raw, str):
        raise ValueError(f"{_named(field)}: enter text")
    text = raw.strip()
    if len(text) > MAX_TEXT_VALUE_LENGTH:
        raise ValueError(f"{_named(field)}: at most {MAX_TEXT_VALUE_LENGTH} characters")
    if _has_control(text):
        raise ValueError(f"{_named(field)}: a line break or a control character is not allowed")
    return text or None


def clean_data(fields: List[Dict[str, str]], raw: Any) -> Optional[Dict[str, Any]]:
    """A product's values, typed and in field order; None when it has none. Raises ValueError."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("The product's data must be an object of field values")
    by_key = {field["key"]: field for field in fields}
    unknown = [str(key) for key in raw if key not in by_key]
    if unknown:
        raise ValueError(f"'{unknown[0]}' is not a field of this list")
    data = {}
    for field in fields:
        value = clean_value(field, raw.get(field["key"]))
        if value is not None:
            data[field["key"]] = value
    return data or None


def refit_data(fields: List[Dict[str, str]], data: Optional[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], int]:
    """A product's values after its list's fields changed: (what still fits, how many values were dropped)."""
    if not data:
        return None, 0
    kept: Dict[str, Any] = {}
    for field in fields:
        if field["key"] not in data:
            continue
        try:
            value = clean_value(field, data[field["key"]])
        except ValueError:
            continue
        if value is not None:
            kept[field["key"]] = value
    dropped = sum(1 for value in data.values() if value is not None) - len(kept)
    return kept or None, dropped


def parse_cell(field: Dict[str, str], text: Optional[str]) -> Any:
    """One CSV cell for its field; None when it is empty. Raises ValueError naming the field."""
    return clean_value(field, text if text is not None else "")


def format_cell(value: Any) -> str:
    """A value as it is written in a CSV cell or a message: yes / no, a whole number without '.0'."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)


def guess_type(cells: Iterable[Optional[str]]) -> str:
    """The type of a CSV column from its cells: number, then yes_no, else text."""
    filled = [cell.strip() for cell in cells if cell and cell.strip()]
    if not filled:
        return "text"
    if all(_NUMBER_RE.fullmatch(cell) for cell in filled):
        return "number"
    if all(cell.lower() in YES_WORDS + NO_WORDS for cell in filled):
        return "yes_no"
    return "text"


def key_from_header(header: str, taken: Set[str]) -> str:
    """A field key made from a CSV column header ('Expected class' -> 'expected_class'), unused so far.

    The key is added to ``taken``.
    """
    key = re.sub(r"[^a-z0-9]+", "_", str(header or "").strip().lower()).strip("_") or "field"
    if key[0].isdigit():
        key = "f_" + key
    key = key[:32].rstrip("_")
    candidate, number = key, 2
    while candidate in taken or candidate in RESERVED_KEYS:
        suffix = f"_{number}"
        candidate = key[: 32 - len(suffix)] + suffix
        number += 1
    taken.add(candidate)
    return candidate


def flat_product_values(product: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The product's values by their names: product.code, product.name, product.list, product.<key>."""
    if not product:
        return {}
    values = {f"product.{name}": product[name] for name in ("code", "name", "list") if product.get(name) is not None}
    for key, value in (product.get("fields") or {}).items():
        if value is not None:
            values[f"product.{key}"] = value
    return values


def data_text(data: Optional[Dict[str, Any]]) -> str:
    """A product's values as one text: 'lane=3; recipe=R-12'."""
    return "; ".join(f"{key}={format_cell(value)}" for key, value in (data or {}).items() if value is not None)
