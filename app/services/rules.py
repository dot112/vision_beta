"""Only-when rules: up to MAX_RULES conditions on the values of an event, all or any of which must pass.

    {"match": "all", "rules": [
        {"left": "class", "op": "!=", "right": "product.expected_class", "right_kind": "value"},
        {"left": "product.lane", "op": "=", "right": "3", "right_kind": "constant"}]}

Every output action and send card may have one ("only_when"): it acts only when
the rules pass. A line's product checks use the same rules to reject a product.
``left`` is a value name (app.services.event_values); ``right`` is a constant
(kept as text) or another value name. Comparing follows formula.compare: numbers
as numbers, yes / no as yes / no, else text without regard to case. A rule whose
value is missing is false; ``empty`` is true for a missing value.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from app.services.formula import MISSING, PLAIN_NAMES, ROOTS, as_text, compare
from app.services.product_fields import VALUE_NAME_RE

RULE_OPS = ("=", "!=", "<", "<=", ">", ">=", "contains", "empty", "not_empty")
MATCHES = ("all", "any")
RIGHT_KINDS = ("constant", "value")
MAX_RULES = 5
MAX_CONSTANT_LENGTH = 256


def is_value_name(text: Any) -> bool:
    """A name of section 1: result, class... product.<field>, input.<value name>."""
    if not isinstance(text, str):
        return False
    if text in PLAIN_NAMES:
        return True
    root, dot, key = text.partition(".")
    return bool(dot) and root in ROOTS and bool(VALUE_NAME_RE.fullmatch(key))


def normalize_only_when(raw: Any, label: str, required: bool = False) -> Optional[Dict[str, Any]]:
    """Only-when rules as they are saved; None when there are none. Raises ValueError naming ``label``."""
    rules = raw.get("rules") if isinstance(raw, dict) else None
    if raw is not None and not isinstance(raw, dict):
        raise ValueError(f"{label}: Only when must be an object with its rules")
    if not rules:
        if required:
            raise ValueError(f"{label}: give at least one rule")
        return None
    if not isinstance(rules, list):
        raise ValueError(f"{label}: the rules of Only when must be a list")
    if len(rules) > MAX_RULES:
        raise ValueError(f"{label}: Only when has at most {MAX_RULES} rules")
    match = str(raw.get("match") or "all").strip().lower()
    if match not in MATCHES:
        raise ValueError(f"{label}: Only when must match all or any of its rules")
    cleaned = []
    for number, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            raise ValueError(f"{label}: rule {number} must be an object")
        left = str(rule.get("left") or "").strip()
        if not is_value_name(left):
            raise ValueError(f"{label}: rule {number}: '{left}' is not a value name")
        op = str(rule.get("op") or "").strip().lower()
        if op not in RULE_OPS:
            raise ValueError(f"{label}: rule {number}: '{op}' is not an operator; use one of {', '.join(RULE_OPS)}")
        if op in ("empty", "not_empty"):
            cleaned.append({"left": left, "op": op, "right": "", "right_kind": "constant"})
            continue
        kind = str(rule.get("right_kind") or "constant").strip().lower()
        if kind not in RIGHT_KINDS:
            raise ValueError(f"{label}: rule {number}: compare with a constant or with another value")
        right = rule.get("right")
        if right is None:
            right = ""
        if isinstance(right, (bool, int, float)):
            right = as_text(right)
        if not isinstance(right, str):
            raise ValueError(f"{label}: rule {number}: the value to compare with must be text or a number")
        right = right.strip()
        if kind == "value" and not is_value_name(right):
            raise ValueError(f"{label}: rule {number}: '{right}' is not a value name")
        if len(right) > MAX_CONSTANT_LENGTH:
            raise ValueError(f"{label}: rule {number}: a constant has at most {MAX_CONSTANT_LENGTH} characters")
        cleaned.append({"left": left, "op": op, "right": right, "right_kind": kind})
    return {"match": match, "rules": cleaned}


def _value(values: Mapping[str, Any], name: Any) -> Any:
    found = values.get(name, MISSING) if isinstance(name, str) else MISSING
    return MISSING if found is None else found


def _rule_passes(rule: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    left = _value(values, rule.get("left"))
    op = rule.get("op")
    if op in ("empty", "not_empty"):
        empty = left is MISSING or (isinstance(left, str) and not left.strip())
        return empty if op == "empty" else not empty
    right = _value(values, rule.get("right")) if rule.get("right_kind") == "value" else rule.get("right", "")
    if left is MISSING or right is MISSING:
        return False
    if op == "contains":
        return as_text(right).casefold() in as_text(left).casefold()
    if op not in RULE_OPS:
        return False
    return compare(left, "==" if op == "=" else op, right) is True


def rules_pass(only_when: Optional[Mapping[str, Any]], values: Mapping[str, Any]) -> bool:
    """True when there are no rules, or when all (any) of them pass."""
    rules = (only_when or {}).get("rules") or []
    if not rules:
        return True
    results = (_rule_passes(rule, values) for rule in rules if isinstance(rule, Mapping))
    return any(results) if (only_when or {}).get("match") == "any" else all(results)


def validate_card_rules(card: Dict[str, Any], label: str) -> None:
    """Check an output action's only-when rules and save them as checked (in ``card``). Raises ValueError."""
    if "only_when" in card:
        card["only_when"] = normalize_only_when(card.get("only_when"), label)
