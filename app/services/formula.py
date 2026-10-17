"""A small formula language for the values an output action writes, and the one comparison rule.

    if(result == "good", product.lane, 9)
    round(input.speed / 4, 1)
    text(product.lane) + "-" + product.recipe

A formula is one line of at most MAX_FORMULA_LENGTH characters made of:
numbers, text in double quotes, true and false; value names (``result``,
``class``, ``product.<field>``, ``input.<name>``...); ``+ - * / %``; the
comparisons ``== != < <= > >=``; ``and``, ``or``, ``not``; parentheses; and
the functions if(), round(), min(), max(), abs(), int() and text().

The text is never passed to eval or exec. It is checked twice: its tokens
against a short list, then the tree Python's ``ast`` makes of it against a
whitelist of node types; a small tree walker computes it. A name that has no
value, a division by zero or a wrong type gives MISSING, never an error.

``if`` and ``class`` are Python keywords, which ``ast`` cannot read as names:
before parsing, each keyword except and / or / not is renamed ``_kw_<word>``
(a name typed with a leading ``_`` is refused, so nothing can clash with it).
"""
from __future__ import annotations

import ast
import functools
import io
import keyword
import math
import operator
import re
import tokenize
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple, Union

from app.services.product_fields import NO_WORDS, VALUE_NAME_RE, YES_WORDS

MAX_FORMULA_LENGTH = 200
MAX_NODES = 64
# The value names that need no root (section 1 of the product data design).
PLAIN_NAMES = ("result", "result_code", "reject_reason", "class", "confidence", "code", "code_status", "product_id",
               "good_count", "reject_count", "total_count", "batch", "line", "camera", "sequence")
ROOTS = ("product", "input")
# Function -> (fewest, most) values it takes; None = any number.
FUNCTIONS: Dict[str, Tuple[int, Optional[int]]] = {
    "if": (3, 3), "round": (1, 2), "min": (1, None), "max": (1, None), "abs": (1, 1), "int": (1, 1), "text": (1, 1),
}
_KW = "_kw_"
_OPS = {"+", "-", "*", "/", "%", "==", "!=", "<", "<=", ">", ">=", "(", ")", ",", "."}
_TOKEN_TYPES = {tokenize.NAME, tokenize.NUMBER, tokenize.STRING, tokenize.OP, tokenize.NEWLINE, tokenize.NL,
                tokenize.ENDMARKER, tokenize.ENCODING}
_NUMBER_TOKEN = re.compile(r"(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
_DECIMAL = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")
_LITERALS = {"true": True, "false": False}
_COMPARE: Dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}
_AST_COMPARE = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}


class _Missing:
    """A value an event does not have: not 0, not empty text."""
    _instance: Optional["_Missing"] = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "missing"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


class FormulaError(ValueError):
    """What is wrong with a formula; ``position`` is the column (from 1) where it is, when known."""

    def __init__(self, message: str, position: Optional[int] = None) -> None:
        super().__init__(f"{message} (at position {position})" if position else message)
        self.position = position


# ── Comparing two values (formulas and only-when rules) ───────────────────────

def _as_yes_no(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return True if value == 1 else False if value == 0 else None
    if isinstance(value, str):
        word = value.strip().lower()
        return True if word in YES_WORDS else False if word in NO_WORDS else None
    return None


def _as_number(value: Any) -> Optional[Union[int, float]]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and _DECIMAL.fullmatch(value.strip()):
        return float(value) if "." in value else int(value)
    return None


def as_text(value: Any) -> str:
    """A value as text: true / false, a whole number without '.0'."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)


def compare(left: Any, op: str, right: Any) -> Union[bool, _Missing]:
    """``left op right`` with ``op`` in == != < <= > >=. Numbers (and text that reads as a decimal number)
    compare as numbers; a yes / no against yes, no, true, false, 1 or 0 as yes / no (ordering is false);
    anything else as text, ignoring case. A MISSING side gives MISSING."""
    if left is MISSING or right is MISSING:
        return MISSING
    if isinstance(left, bool) or isinstance(right, bool):
        a, b = _as_yes_no(left), _as_yes_no(right)
        if op not in ("==", "!="):
            return False
        same = a is not None and b is not None and a == b
        return same if op == "==" else not same
    a_number, b_number = _as_number(left), _as_number(right)
    if a_number is not None and b_number is not None:
        return _COMPARE[op](a_number, b_number)
    return _COMPARE[op](as_text(left).casefold(), as_text(right).casefold())


# ── Reading a formula ─────────────────────────────────────────────────────────

def _prepare(text: str) -> Tuple[str, List[int]]:
    """The text ast reads (keywords renamed) and, for each of its characters, the column of the typed text."""
    lead = len(text) - len(text.lstrip())
    body = text.strip()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(body).readline))
    except (tokenize.TokenError, SyntaxError) as exc:
        raise FormulaError("the formula is not complete: check its quotes and brackets") from exc
    pieces: List[str] = []
    columns: List[int] = []
    done = 0
    for tok in tokens:
        if tok.type not in _TOKEN_TYPES:
            raise FormulaError(f"'{tok.string}' is not allowed", lead + tok.start[1] + 1)
        start, end = tok.start[1], tok.end[1]
        string = tok.string
        if tok.type == tokenize.OP and string not in _OPS:
            raise FormulaError(f"'{string}' is not allowed", lead + start + 1)
        if tok.type == tokenize.NUMBER and not _NUMBER_TOKEN.fullmatch(string):
            raise FormulaError(f"'{string}' is not a number: write numbers like 12, 0.5 or 2e3", lead + start + 1)
        if tok.type == tokenize.STRING and not string.startswith('"'):
            if string.startswith("'"):
                raise FormulaError("write text in double quotes, like \"good\"", lead + start + 1)
            raise FormulaError(f"'{string}' is not allowed", lead + start + 1)
        if tok.type == tokenize.NAME:
            if string.startswith("_"):
                raise FormulaError(f"'{string}' is not a value", lead + start + 1)
            if keyword.iskeyword(string) and string not in ("and", "or", "not"):
                pieces.append(body[done:start])
                columns.extend(range(lead + done, lead + start))
                pieces.append(_KW + string)
                columns.extend([lead + start] * len(_KW) + list(range(lead + start, lead + end)))
                done = end
    pieces.append(body[done:])
    columns.extend(range(lead + done, lead + len(body)))
    columns.append(lead + len(body))
    return "".join(pieces), columns


def _plain(name: str) -> str:
    return name[len(_KW):] if name.startswith(_KW) else name


class Formula:
    """A checked formula: ``names`` it uses, ``evaluate(values)`` its value (or MISSING)."""

    def __init__(self, text: str, tree: ast.Expression, names: FrozenSet[str]) -> None:
        self.text = text
        self._tree = tree
        self.names = names

    def evaluate(self, values: Mapping[str, Any]) -> Any:
        try:
            return _Walker(values).run(self._tree.body)
        except (ArithmeticError, ValueError, TypeError, RecursionError):
            return MISSING      # a value that cannot be worked out is missing, never an error

    def __repr__(self) -> str:
        return f"Formula({self.text!r})"


class _Checker:
    """Walks the tree once at parse time: every node must be on the whitelist."""

    def __init__(self, columns: List[int], source: str) -> None:
        self.columns, self.source = columns, source
        self.names: set = set()

    def at(self, node: ast.AST) -> int:
        # ast counts columns in UTF-8 bytes; the column map counts characters.
        chars = len(self.source.encode("utf-8")[: getattr(node, "col_offset", 0)].decode("utf-8", errors="ignore"))
        return self.columns[min(chars, len(self.columns) - 1)] + 1

    def fail(self, message: str, node: ast.AST) -> None:
        raise FormulaError(message, self.at(node))

    def check(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float, str)):
                self.fail("this value is not allowed", node)
            if isinstance(node.value, float) and not math.isfinite(node.value):
                self.fail("this number is too large", node)
        elif isinstance(node, ast.Name):
            name = _plain(node.id)
            if name in _LITERALS:
                return
            if name in ROOTS:
                self.fail(f"write {name}.<{'field' if name == 'product' else 'name'}>", node)
            if name not in PLAIN_NAMES:
                self.fail(f"unknown value '{name}'", node)
            self.names.add(name)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Attribute):
                self.fail("a value has one level after its root: product.<field> or input.<name>", node)
            if not (isinstance(node.value, ast.Name) and node.value.id in ROOTS):
                self.fail("only product.<field> and input.<name> use a dot", node)
            key = _plain(node.attr)
            if not VALUE_NAME_RE.fullmatch(key):
                self.fail(f"'{key}' is not a value name", node)
            self.names.add(f"{node.value.id}.{key}")
        elif isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)):
                self.fail("this operator is not allowed", node)
            self.check(node.left)
            self.check(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
                self.fail("this operator is not allowed", node)
            self.check(node.operand)
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                self.check(value)
        elif isinstance(node, ast.Compare):
            if len(node.ops) != 1 or type(node.ops[0]) not in _AST_COMPARE:
                self.fail("compare two values at a time, with == != < <= > or >=", node)
            self.check(node.left)
            self.check(node.comparators[0])
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                self.fail("this call is not allowed", node)
            name = _plain(node.func.id)
            if name not in FUNCTIONS:
                self.fail(f"unknown function '{name}'", node)
            fewest, most = FUNCTIONS[name]
            count = len(node.args)
            if any(isinstance(arg, ast.Starred) for arg in node.args) or count < fewest or (most is not None and count > most):
                takes = f"{fewest}" if fewest == most else f"{fewest} or {most}" if most else f"{fewest} or more"
                self.fail(f"{name}() takes {takes} value{'s' if (most or 2) > 1 else ''}", node)
            if name == "round" and count == 2:
                digits = node.args[1]
                if isinstance(digits, ast.Constant) and not (isinstance(digits.value, int) and 0 <= digits.value <= 6):
                    self.fail("the digits of round() are 0 to 6", digits)
            for arg in node.args:
                self.check(arg)
        else:
            self.fail(f"'{type(node).__name__}' is not allowed", node)


@functools.lru_cache(maxsize=256)
def parse(text: str) -> Formula:
    """A checked formula. Raises FormulaError naming what is wrong and where."""
    if not isinstance(text, str) or not text.strip():
        raise FormulaError("the formula is empty")
    if len(text) > MAX_FORMULA_LENGTH:
        raise FormulaError(f"a formula has at most {MAX_FORMULA_LENGTH} characters")
    if "\n" in text or "\r" in text:
        raise FormulaError("a formula is one line")
    source, columns = _prepare(text)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        index = max(0, min((exc.offset or 1) - 1, len(columns) - 1))
        raise FormulaError("the formula is not complete or has a mistake", columns[index] + 1) from None
    if sum(isinstance(node, ast.expr) for node in ast.walk(tree)) > MAX_NODES:
        raise FormulaError(f"the formula is too long: at most {MAX_NODES} parts")
    checker = _Checker(columns, source)
    checker.check(tree.body)
    return Formula(text, tree, frozenset(checker.names))


# ── Computing a formula ───────────────────────────────────────────────────────

def _number(value: Any) -> Optional[Union[int, float]]:
    """A value as a number for arithmetic: yes / no count as 1 / 0. Infinity and NaN are no numbers."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _result(value: Union[int, float]) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return MISSING
    if isinstance(value, int) and abs(value) > 2 ** 63:
        return MISSING
    return value


def _truthy(value: Any) -> bool:
    return bool(value)


class _Walker:
    def __init__(self, values: Mapping[str, Any]) -> None:
        self.values = values

    def value(self, name: str) -> Any:
        found = self.values.get(name, MISSING)
        return MISSING if found is None else found

    def run(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            name = _plain(node.id)
            return _LITERALS[name] if name in _LITERALS else self.value(name)
        if isinstance(node, ast.Attribute):
            return self.value(f"{node.value.id}.{_plain(node.attr)}")
        if isinstance(node, ast.BinOp):
            return self.binop(node)
        if isinstance(node, ast.UnaryOp):
            operand = self.run(node.operand)
            if operand is MISSING:
                return MISSING
            if isinstance(node.op, ast.Not):
                return not _truthy(operand)
            number = _number(operand)
            if number is None:
                return MISSING
            return _result(-number if isinstance(node.op, ast.USub) else +number)
        if isinstance(node, ast.BoolOp):
            want_all = isinstance(node.op, ast.And)
            for part in node.values:
                value = self.run(part)
                if value is MISSING:
                    return MISSING
                if _truthy(value) != want_all:
                    return not want_all
            return want_all
        if isinstance(node, ast.Compare):
            return compare(self.run(node.left), _AST_COMPARE[type(node.ops[0])], self.run(node.comparators[0]))
        if isinstance(node, ast.Call):
            return self.call(_plain(node.func.id), node.args)
        return MISSING

    def binop(self, node: ast.BinOp) -> Any:
        left, right = self.run(node.left), self.run(node.right)
        if left is MISSING or right is MISSING:
            return MISSING
        if isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
            return left + right
        a, b = _number(left), _number(right)
        if a is None or b is None:
            return MISSING
        if isinstance(node.op, ast.Add):
            return _result(a + b)
        if isinstance(node.op, ast.Sub):
            return _result(a - b)
        if isinstance(node.op, ast.Mult):
            return _result(a * b)
        if b == 0:
            return MISSING
        return _result(a / b if isinstance(node.op, ast.Div) else a % b)

    def call(self, name: str, args: List[ast.AST]) -> Any:
        if name == "if":
            condition = self.run(args[0])
            if condition is MISSING:
                return MISSING
            return self.run(args[1] if _truthy(condition) else args[2])
        values = [self.run(arg) for arg in args]
        if any(value is MISSING for value in values):
            return MISSING
        if name == "text":
            if isinstance(values[0], float) and not math.isfinite(values[0]):
                return MISSING
            return as_text(values[0])
        if name == "int":
            value = values[0]
            if isinstance(value, str):
                value = _as_number(value)
            number = _number(value)
            return MISSING if number is None else _result(int(number))
        numbers = [_number(value) for value in values]
        if any(number is None for number in numbers):
            return MISSING
        if name == "abs":
            return _result(abs(numbers[0]))
        if name == "min":
            return min(numbers)
        if name == "max":
            return max(numbers)
        # round
        if len(numbers) == 2:
            digits = values[1]
            if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 6:
                return MISSING
            return _result(round(numbers[0], digits))
        return _result(round(numbers[0]))
