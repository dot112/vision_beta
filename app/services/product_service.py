"""Product lists: the database tables and the in-memory copy that cameras reading codes check against.

There are up to MAX_PRODUCT_LISTS lists. Each camera that reads codes is set
to one of them (``product_list_id`` on the camera, on Line setup). The lists
are independent: the same code may be in more than one.
"""
from __future__ import annotations

import copy
import csv
import io
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.product import DEFAULT_LIST_ID, DEFAULT_LIST_NAME, Product, ProductList, new_list_id
from app.services.code_format import DEFAULT_FORMAT, normalize_code_format, split_code
from app.services.product_fields import (
    MAX_FIELD_LABEL_LENGTH,
    MAX_FIELDS,
    clean_data,
    format_cell,
    guess_type,
    key_from_header,
    normalize_fields,
    parse_cell,
    refit_data,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

MAX_PRODUCT_LISTS = 4
MAX_LIST_NAME_LENGTH = 128
MAX_CODE_LENGTH = 256
MAX_NAME_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 512
MAX_IMPORT_ROWS = 100_000


class ProductListLimit(ValueError):
    """There are already MAX_PRODUCT_LISTS lists."""


class ProductListInUse(ValueError):
    """A camera that reads codes is set to this list."""


@dataclass
class CsvRow:
    """One product line of a CSV file; ``cells`` holds its extra columns, header -> text."""
    line_no: int
    code: str
    name: str
    description: Optional[str]
    cells: Dict[str, str] = field(default_factory=dict)


@dataclass
class CsvTable:
    """A CSV file's products, its extra column headers (as written) after code, name, description,
    and the headers that repeat an earlier one (their cells are not read)."""
    rows: List[CsvRow]
    columns: List[str]
    repeated: List[str] = field(default_factory=list)


def _typed_rows(rows: List[CsvRow], columns: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    """Each row's values for the fields its columns stand for ({header: field}); None for an empty cell.
    Raises ValueError naming the line."""
    typed = []
    for row in rows:
        values = {}
        for header, fld in columns.items():
            try:
                values[fld["key"]] = parse_cell(fld, row.cells.get(header))
            except ValueError as exc:
                raise ValueError(f"Line {row.line_no}: {exc}") from None
        typed.append(values)
    return typed


def normalize_code(code: Any) -> str:
    """Codes are compared as sent, minus surrounding whitespace."""
    return str(code if code is not None else "").strip()


@dataclass(frozen=True)
class CodeMatch:
    """One code read against a list: the product (None: not listed), the product ID and the batch the
    code holds, and why it does not fit (the first problem; None when it fits)."""
    product: Optional[Dict[str, Any]]
    product_id: Optional[str]
    batch: Optional[str]
    problem: Optional[str]


_NO_MATCH = CodeMatch(None, None, None, None)


def _readable_format(raw: Any, list_id: str) -> Dict[str, Any]:
    """A saved format as the catalog uses it; one that cannot be read (a hand-edited database) reads whole codes."""
    try:
        return normalize_code_format(raw)
    except ValueError:
        logger.warning("Product list %s has a code format that cannot be read (%r): it reads whole codes", list_id, raw)
        return dict(DEFAULT_FORMAT)


class ProductCatalog:
    """Thread-safe code -> product lookup for every product list, kept in step with the database.

    A code read never waits on the database: readers look codes up here. A
    list's lookup is replaced in one assignment, so a reader sees the old
    contents or the new ones, never an empty or half-filled list.
    """

    def __init__(self) -> None:
        self._lists: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._names: Dict[str, str] = {}
        # Each list's own fields (app.services.product_fields).
        self._fields: Dict[str, List[Dict[str, str]]] = {}
        # How each list's codes are built (app.services.code_format); a list without one reads whole codes.
        self._formats: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.loaded = False

    def replace_all(self, lists: Dict[str, Tuple[str, List[Dict[str, Any]]]],
                    fields: Optional[Dict[str, List[Dict[str, str]]]] = None,
                    formats: Optional[Dict[str, Any]] = None) -> None:
        """Every list at once: {list_id: (name, products)}, in the order the lists were created,
        {list_id: fields} and {list_id: code format}."""
        tables = {list_id: {p["code"]: p for p in products} for list_id, (_, products) in lists.items()}
        names = {list_id: name for list_id, (name, _) in lists.items()}
        readable = {list_id: _readable_format(formats[list_id], list_id) for list_id in lists if list_id in (formats or {})}
        with self._lock:
            self._lists, self._names = tables, names
            self._fields = {list_id: list(fields.get(list_id) or []) for list_id in lists} if fields else {}
            self._formats = readable
            self.loaded = True

    def replace_list(self, list_id: str, name: str, products: List[Dict[str, Any]],
                     fields: Optional[List[Dict[str, str]]] = None, code_format: Any = None) -> None:
        """One list's contents; ``fields`` and ``code_format`` left out keep what the list has."""
        table = {p["code"]: p for p in products}
        readable = _readable_format(code_format, list_id) if code_format is not None else None
        with self._lock:
            self._lists = {**self._lists, list_id: table}
            self._names = {**self._names, list_id: name}
            if fields is not None:
                self._fields = {**self._fields, list_id: list(fields)}
            if readable is not None:
                self._formats = {**self._formats, list_id: readable}

    def remove_list(self, list_id: str) -> None:
        with self._lock:
            self._lists = {k: v for k, v in self._lists.items() if k != list_id}
            self._names = {k: v for k, v in self._names.items() if k != list_id}
            self._fields = {k: v for k, v in self._fields.items() if k != list_id}
            self._formats = {k: v for k, v in self._formats.items() if k != list_id}

    def fields(self, list_id: Optional[str]) -> List[Dict[str, str]]:
        """A list's fields ([] for a list without fields, or no list)."""
        with self._lock:
            return copy.deepcopy(self._fields.get(list_id) or []) if list_id else []

    def product_info(self, product: Dict[str, Any], list_id: Optional[str]) -> Dict[str, Any]:
        """What goes with an item whose code is this product: its code, name, list and its values."""
        return {
            "code": product.get("code"),
            "name": product.get("name"),
            "list": self.list_name(list_id),
            "list_id": list_id,
            "fields": dict(product.get("data") or {}),
        }

    def formats(self) -> Dict[str, Dict[str, Any]]:
        """Each list's code format ({list_id: format}); a list missing here reads whole codes."""
        with self._lock:
            return copy.deepcopy(self._formats)

    def match(self, code: Any, list_id: Optional[str], fmt: Optional[Dict[str, Any]] = None) -> CodeMatch:
        """A code read against one list, by the list's code format (or ``fmt``, a format to try).

        A code that does not fit the format finds no product; its ``problem`` says why. A batch
        that cannot be a batch (line_config.clean_batch) is dropped: "batch not allowed".
        """
        from app.services.line_config import clean_batch

        if not list_id:
            return _NO_MATCH
        with self._lock:
            table = self._lists.get(list_id)
            own = self._formats.get(list_id)
        parts = split_code(normalize_code(code), fmt if fmt is not None else (own or DEFAULT_FORMAT))
        product = table.get(parts.product_id) if table and parts.product_id else None
        batch, problem = parts.batch, parts.problem
        if batch is not None:
            try:
                batch = clean_batch(batch) or None
            except ValueError:
                batch, problem = None, problem or "batch not allowed"
        return CodeMatch(product, parts.product_id, batch, problem)

    def lookup(self, code: str, list_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """The product a code stands for in one list, by the list's code format. None when it is not
        listed, or no list is named."""
        return self.match(code, list_id).product

    def lookup_any(self, code: str) -> Optional[Dict[str, Any]]:
        """The first list that has the code, each list reading it by its own format, oldest list first
        (for callers that name no list)."""
        for list_id in self.list_ids():
            product = self.match(code, list_id).product
            if product is not None:
                return product
        return None

    def has_list(self, list_id: Optional[str]) -> bool:
        with self._lock:
            return bool(list_id) and list_id in self._lists

    def list_name(self, list_id: Optional[str]) -> Optional[str]:
        with self._lock:
            return self._names.get(list_id) if list_id else None

    def list_ids(self) -> List[str]:
        with self._lock:
            return list(self._lists)

    def count(self, list_id: str) -> int:
        with self._lock:
            return len(self._lists.get(list_id) or {})

    def __len__(self) -> int:
        with self._lock:
            return sum(len(table) for table in self._lists.values())


product_catalog = ProductCatalog()


def _row(product: Product) -> Dict[str, Any]:
    return {
        "id": product.id,
        "list_id": product.list_id,
        "code": product.code,
        "name": product.name,
        "description": product.description,
        "data": product.data or None,
        "created_at": product.created_at.isoformat() if product.created_at else None,
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
    }


def _list_row(product_list: ProductList, count: int) -> Dict[str, Any]:
    return {
        "id": product_list.id,
        "name": product_list.name,
        "fields": list(product_list.fields or []),
        "code_format": _shown_format(product_list.code_format),
        "count": count,
        "created_at": product_list.created_at.isoformat() if product_list.created_at else None,
        "updated_at": product_list.updated_at.isoformat() if product_list.updated_at else None,
    }


def _shown_format(raw: Any) -> Dict[str, Any]:
    """A list's code format as the API shows it (one that cannot be read shows as whole codes, as it reads)."""
    try:
        return normalize_code_format(raw)
    except ValueError:
        return dict(DEFAULT_FORMAT)


def _validate(code: Any, name: Any, description: Any) -> Tuple[str, str, Optional[str]]:
    code_s = normalize_code(code)
    if not code_s:
        raise ValueError("Product code is required")
    if len(code_s) > MAX_CODE_LENGTH:
        raise ValueError(f"Product code must be {MAX_CODE_LENGTH} characters or fewer")
    name_s = str(name if name is not None else "").strip()
    if not name_s:
        raise ValueError(f"Product name is required for code '{code_s}'")
    if len(name_s) > MAX_NAME_LENGTH:
        raise ValueError(f"Product name must be {MAX_NAME_LENGTH} characters or fewer")
    desc_s = str(description).strip() if description not in (None, "") else None
    if desc_s and len(desc_s) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"Description must be {MAX_DESCRIPTION_LENGTH} characters or fewer")
    return code_s, name_s, desc_s


def _list_name(name: Any) -> str:
    text = re.sub(r"\s+", " ", str(name if name is not None else "")).strip()
    if not text:
        raise ValueError("A product list needs a name")
    if len(text) > MAX_LIST_NAME_LENGTH or any(ord(ch) < 32 for ch in text):
        raise ValueError(f"A product list name must be {MAX_LIST_NAME_LENGTH} characters or fewer")
    return text


def _new_list(name: str, list_id: Optional[str] = None, fields: Optional[List[Dict[str, str]]] = None) -> ProductList:
    # The time is set here, to the microsecond: the lists are shown oldest
    # first, and the database's own clock only counts whole seconds.
    return ProductList(id=list_id or new_list_id(), name=name, fields=list(fields or []),
                       created_at=datetime.now(timezone.utc))


def list_name_from_file(filename: Optional[str]) -> str:
    """The default name of a list made from a file: 'Summer range.csv' -> 'Summer range'."""
    base = re.split(r"[\\/]", str(filename or ""))[-1]
    base = re.sub(r"\.(csv|txt)$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"\s+", " ", "".join(ch for ch in base if ord(ch) >= 32)).strip()
    return base[:MAX_LIST_NAME_LENGTH] or "Product list"


class ProductService:
    # ── The in-memory copy ────────────────────────────────────────────────────

    @staticmethod
    async def refresh_catalog(db: AsyncSession) -> int:
        """Load every list from the database. Returns the number of product codes."""
        lists = (await db.execute(select(ProductList).order_by(ProductList.created_at, ProductList.id))).scalars().all()
        by_list: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {pl.id: (pl.name, []) for pl in lists}
        for product in (await db.execute(select(Product))).scalars().all():
            if product.list_id in by_list:
                by_list[product.list_id][1].append(_row(product))
        product_catalog.replace_all(by_list, {pl.id: list(pl.fields or []) for pl in lists},
                                    {pl.id: pl.code_format for pl in lists})
        return len(product_catalog)

    @staticmethod
    async def _refresh_list(db: AsyncSession, product_list: ProductList) -> int:
        result = await db.execute(select(Product).where(Product.list_id == product_list.id))
        rows = [_row(p) for p in result.scalars().all()]
        product_catalog.replace_list(product_list.id, product_list.name, rows, list(product_list.fields or []),
                                     code_format=product_list.code_format or DEFAULT_FORMAT)
        return len(rows)

    # ── Lists ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def _lists(db: AsyncSession) -> List[ProductList]:
        result = await db.execute(select(ProductList).order_by(ProductList.created_at, ProductList.id))
        return list(result.scalars().all())

    @staticmethod
    async def list_lists(db: AsyncSession) -> List[Dict[str, Any]]:
        counts = dict((await db.execute(select(Product.list_id, func.count()).group_by(Product.list_id))).all())
        return [_list_row(pl, int(counts.get(pl.id, 0))) for pl in await ProductService._lists(db)]

    @staticmethod
    async def get_list(db: AsyncSession, list_id: str) -> Optional[ProductList]:
        return await db.get(ProductList, list_id)

    @staticmethod
    async def resolve_list(db: AsyncSession, list_id: Optional[str], create: bool = False) -> ProductList:
        """The named list, or the oldest one when none is named (callers written for a single list).

        ``create`` makes "List 1" when there is no list at all, so a first
        product can be added by hand. Raises LookupError for an unknown list.
        """
        if list_id:
            product_list = await db.get(ProductList, str(list_id))
            if product_list is None:
                raise LookupError(f"Product list '{list_id}' not found")
            return product_list
        lists = await ProductService._lists(db)
        if lists:
            return lists[0]
        if not create:
            raise LookupError("There is no product list yet")
        product_list = _new_list(DEFAULT_LIST_NAME, DEFAULT_LIST_ID)
        db.add(product_list)
        await db.flush()
        return product_list

    @staticmethod
    async def _free_name(db: AsyncSession, wanted: str, own_id: Optional[str] = None) -> str:
        """``wanted``, or ``wanted (2)``... when another list already has that name."""
        taken = {pl.name.lower() for pl in await ProductService._lists(db) if pl.id != own_id}
        if wanted.lower() not in taken:
            return wanted
        for number in range(2, MAX_PRODUCT_LISTS + 3):
            suffix = f" ({number})"
            candidate = wanted[: MAX_LIST_NAME_LENGTH - len(suffix)] + suffix
            if candidate.lower() not in taken:
                return candidate
        raise ValueError(f"A product list named '{wanted}' already exists")

    @staticmethod
    async def create_list(db: AsyncSession, name: Any, fields: Any = None, code_format: Any = None) -> Dict[str, Any]:
        lists = await ProductService._lists(db)
        if len(lists) >= MAX_PRODUCT_LISTS:
            raise ProductListLimit(f"There are already {MAX_PRODUCT_LISTS} product lists. Delete one first.")
        wanted = _list_name(name)
        if any(pl.name.lower() == wanted.lower() for pl in lists):
            raise ValueError(f"A product list named '{wanted}' already exists")
        product_list = _new_list(wanted, fields=normalize_fields(fields))
        product_list.code_format = normalize_code_format(code_format)
        db.add(product_list)
        await db.commit()
        await db.refresh(product_list)
        product_catalog.replace_list(product_list.id, product_list.name, [], list(product_list.fields or []),
                                     code_format=product_list.code_format)
        return _list_row(product_list, 0)

    @staticmethod
    async def rename_list(db: AsyncSession, list_id: str, name: Any) -> Optional[Dict[str, Any]]:
        return await ProductService.update_list(db, list_id, name=name)

    @staticmethod
    async def update_list(db: AsyncSession, list_id: str, name: Any = None, fields: Any = None,
                          code_format: Any = None) -> Optional[Dict[str, Any]]:
        """Rename a list, change its fields or its code format. With new fields every product's values are
        checked again: what no longer fits is dropped, and the answer's ``dropped`` says how many."""
        if name is None and fields is None and code_format is None:
            raise ValueError("Send a new name for the list, its fields or its code format")
        new_format = normalize_code_format(code_format) if code_format is not None else None
        product_list = await db.get(ProductList, list_id)
        if product_list is None:
            return None
        if name is not None:
            wanted = _list_name(name)
            if any(pl.name.lower() == wanted.lower() for pl in await ProductService._lists(db) if pl.id != list_id):
                raise ValueError(f"A product list named '{wanted}' already exists")
            product_list.name = wanted
        if new_format is not None:
            product_list.code_format = new_format
        dropped = 0
        if fields is not None:
            new_fields = normalize_fields(fields)
            product_list.fields = new_fields
            result = await db.execute(select(Product).where(Product.list_id == list_id))
            for product in result.scalars().all():
                data, lost = refit_data(new_fields, product.data)
                if lost or data != product.data:
                    product.data = data
                dropped += lost
        await db.commit()
        await db.refresh(product_list)
        count = await ProductService._refresh_list(db, product_list)
        row = _list_row(product_list, count)
        if fields is not None:
            row["dropped"] = dropped
        return row

    @staticmethod
    async def delete_list(db: AsyncSession, list_id: str, used_by: Optional[List[str]] = None) -> bool:
        """Remove a list and its codes. ``used_by`` names the readers set to it; a list in use is kept."""
        product_list = await db.get(ProductList, list_id)
        if product_list is None:
            return False
        if used_by:
            raise ProductListInUse(
                f"'{product_list.name}' is used by {', '.join(used_by)}. Choose another list for it on Line setup first."
            )
        await db.execute(delete(Product).where(Product.list_id == list_id))
        await db.delete(product_list)
        await db.commit()
        product_catalog.remove_list(list_id)
        return True

    # ── Products ──────────────────────────────────────────────────────────────

    @staticmethod
    async def list_products(db: AsyncSession, list_id: str, search: Optional[str] = None,
                            limit: int = 500, offset: int = 0) -> Dict[str, Any]:
        stmt = select(Product).where(Product.list_id == list_id)
        count_stmt = select(func.count()).select_from(Product).where(Product.list_id == list_id)
        if search:
            pattern = f"%{search.strip()}%"
            cond = Product.code.ilike(pattern) | Product.name.ilike(pattern)
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        total = (await db.execute(count_stmt)).scalar_one()
        result = await db.execute(stmt.order_by(Product.code).offset(offset).limit(limit))
        return {"list_id": list_id, "total": total, "products": [_row(p) for p in result.scalars().all()]}

    @staticmethod
    async def get(db: AsyncSession, product_id: str) -> Optional[Dict[str, Any]]:
        product = await db.get(Product, product_id)
        return _row(product) if product else None

    @staticmethod
    async def _code_taken(db: AsyncSession, list_id: str, code: str) -> bool:
        stmt = select(Product.id).where(Product.list_id == list_id, Product.code == code)
        return (await db.execute(stmt)).first() is not None

    @staticmethod
    async def create(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
        code, name, description = _validate(data.get("code"), data.get("name"), data.get("description"))
        product_list = await ProductService.resolve_list(db, data.get("list_id"), create=True)
        values = clean_data(list(product_list.fields or []), data.get("data"))
        if await ProductService._code_taken(db, product_list.id, code):
            raise ValueError(f"Product code '{code}' is already in '{product_list.name}'")
        product = Product(list_id=product_list.id, code=code, name=name, description=description, data=values)
        db.add(product)
        await db.commit()
        await db.refresh(product)
        await ProductService._refresh_list(db, product_list)
        return _row(product)

    @staticmethod
    async def update(db: AsyncSession, product_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        product = await db.get(Product, product_id)
        if product is None:
            return None
        code, name, description = _validate(
            data.get("code", product.code),
            data.get("name", product.name),
            data.get("description", product.description),
        )
        product_list = await db.get(ProductList, product.list_id)
        # Left out, the product keeps its values; null empties them.
        values = product.data
        if "data" in data:
            values = clean_data(list(product_list.fields or []) if product_list else [], data["data"])
        if code != product.code and await ProductService._code_taken(db, product.list_id, code):
            raise ValueError(f"Product code '{code}' is already in this list")
        product.code, product.name, product.description = code, name, description
        if values != product.data:
            product.data = values
        await db.commit()
        await db.refresh(product)
        if product_list is not None:
            await ProductService._refresh_list(db, product_list)
        return _row(product)

    @staticmethod
    async def delete(db: AsyncSession, product_id: str) -> bool:
        product = await db.get(Product, product_id)
        if product is None:
            return False
        product_list = await db.get(ProductList, product.list_id)
        await db.delete(product)
        await db.commit()
        if product_list is not None:
            await ProductService._refresh_list(db, product_list)
        return True

    # ── CSV ───────────────────────────────────────────────────────────────────

    @staticmethod
    def parse_csv(text: str) -> CsvTable:
        """Rows from CSV text with a header naming ``code`` and ``name`` (``description`` optional).
        Any other column holds a field's values."""
        reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
        header_row = next(reader, [])
        # Each header's column; a header that comes again is not read (the first one counts).
        index: Dict[str, int] = {}
        extra: List[Tuple[str, int]] = []
        repeated: List[str] = []
        for position, header in enumerate(header_row):
            name = (header or "").strip()
            if not name:
                continue
            if name.lower() in index:
                repeated.append(name)
                continue
            index[name.lower()] = position
            if name.lower() not in ("code", "name", "description"):
                extra.append((name, position))
        if "code" not in index or "name" not in index:
            raise ValueError("The CSV file needs a header row with 'code' and 'name' columns")

        def cell(values: List[str], position: Optional[int]) -> Optional[str]:
            return values[position] if position is not None and position < len(values) else None

        rows: List[CsvRow] = []
        seen: Dict[str, int] = {}
        for line_no, raw in enumerate(reader, start=2):
            if len(rows) >= MAX_IMPORT_ROWS:
                raise ValueError(f"The CSV file has more than {MAX_IMPORT_ROWS} rows")
            code_raw = _csv_unescape(cell(raw, index["code"]))
            name_raw = _csv_unescape(cell(raw, index["name"]))
            desc_raw = _csv_unescape(cell(raw, index.get("description"))) if "description" in index else None
            if not normalize_code(code_raw) and not str(name_raw or "").strip():
                continue  # blank line
            try:
                code, name, description = _validate(code_raw, name_raw, desc_raw)
            except ValueError as exc:
                raise ValueError(f"Line {line_no}: {exc}") from None
            if code in seen:
                raise ValueError(f"Line {line_no}: code '{code}' also appears on line {seen[code]}")
            seen[code] = line_no
            cells = {header: _csv_unescape(cell(raw, position)) or "" for header, position in extra}
            rows.append(CsvRow(line_no, code, name, description, cells))
        return CsvTable(rows, [header for header, _ in extra], repeated)

    @staticmethod
    async def import_csv(db: AsyncSession, text: str, name: Any = None) -> Dict[str, Any]:
        """Make a new list from a CSV file. Refused when there are already MAX_PRODUCT_LISTS lists."""
        table = ProductService.parse_csv(text)
        # Each extra column becomes a field: its key made from the header, its type from its cells.
        # A column that cannot be one (more than MAX_FIELDS, a header that comes again, a cell no
        # field can hold) is left out, as every extra column was before lists had fields.
        taken: set = set()
        fields: List[Dict[str, str]] = []
        columns: Dict[str, Dict[str, str]] = {}
        ignored: List[str] = []
        for header in table.columns:
            cells = [row.cells.get(header) for row in table.rows]
            candidate = {"key": key_from_header(header, set(taken)), "label": header[:MAX_FIELD_LABEL_LENGTH],
                         "type": guess_type(cells)}
            try:
                if len(fields) >= MAX_FIELDS:
                    raise ValueError("too many fields")
                for value in cells:
                    parse_cell(candidate, value)
            except ValueError:
                ignored.append(header)
                continue
            taken.add(candidate["key"])
            fields.append(candidate)
            columns[header] = candidate
        fields = normalize_fields(fields)
        ignored = ignored + table.repeated      # the columns not made fields, then the headers that came again
        typed = _typed_rows(table.rows, columns)
        if len(await ProductService._lists(db)) >= MAX_PRODUCT_LISTS:
            raise ProductListLimit(
                f"There are already {MAX_PRODUCT_LISTS} product lists. Delete one first, "
                f"or use Replace contents to update a list from this file."
            )
        list_name = await ProductService._free_name(db, _list_name(name or "Product list"))
        product_list = _new_list(list_name, fields=fields)
        db.add(product_list)
        for row, values in zip(table.rows, typed):
            db.add(Product(list_id=product_list.id, code=row.code, name=row.name, description=row.description,
                           data=clean_data(fields, values)))
        await db.commit()
        await db.refresh(product_list)
        total = await ProductService._refresh_list(db, product_list)
        return {"list": _list_row(product_list, total), "added": len(table.rows), "updated": 0, "removed": 0, "total": total,
                "ignored_columns": ignored}

    @staticmethod
    async def replace_contents(db: AsyncSession, list_id: str, text: str) -> Optional[Dict[str, Any]]:
        """Make a list hold exactly the rows of a CSV file. Its id and name stay, so its readers keep working.

        A column names a field by its key or its label (any case). A field the file
        has no column for keeps each product's value; a column that is no field is refused.
        """
        table = ProductService.parse_csv(text)
        product_list = await db.get(ProductList, list_id)
        if product_list is None:
            return None
        fields = list(product_list.fields or [])
        by_name: Dict[str, Dict[str, str]] = {}
        for fld in fields:
            by_name.setdefault(fld["key"].lower(), fld)
            by_name.setdefault(fld["label"].lower(), fld)
        unknown = [header for header in table.columns if header.lower() not in by_name]
        # A list without fields reads only code, name and description, as before lists had fields.
        ignored = (unknown if not fields else []) + table.repeated
        if not fields:
            unknown = []
        if unknown:
            raise ValueError(
                f"The file has columns this list has no fields for: {', '.join(unknown)}. "
                f"Add them as fields first, or leave them out of the file."
            )
        columns = {header: by_name[header.lower()] for header in table.columns if header.lower() in by_name}
        typed = _typed_rows(table.rows, columns)
        result = await db.execute(select(Product).where(Product.list_id == list_id))
        existing = {p.code: p for p in result.scalars().all()}
        added = updated = 0
        for row, values in zip(table.rows, typed):
            product = existing.get(row.code)
            data = dict(product.data or {}) if product is not None else {}
            for key, value in values.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            data = clean_data(fields, data)
            if product is None:
                db.add(Product(list_id=list_id, code=row.code, name=row.name, description=row.description, data=data))
                added += 1
            elif product.name != row.name or product.description != row.description or data != (product.data or None):
                product.name, product.description, product.data = row.name, row.description, data
                updated += 1
        keep = {row.code for row in table.rows}
        stale = [product.id for code, product in existing.items() if code not in keep]
        for start in range(0, len(stale), 500):
            await db.execute(delete(Product).where(Product.id.in_(stale[start:start + 500])))
        await db.commit()
        await db.refresh(product_list)
        # The lookup readers use changes here, in one step, after the database has the new rows.
        total = await ProductService._refresh_list(db, product_list)
        return {"list": _list_row(product_list, total), "added": added, "updated": updated, "removed": len(stale), "total": total,
                "ignored_columns": ignored}

    @staticmethod
    async def export_csv(db: AsyncSession, list_id: str) -> str:
        product_list = await db.get(ProductList, list_id)
        fields = list(product_list.fields or []) if product_list is not None else []
        result = await db.execute(select(Product).where(Product.list_id == list_id).order_by(Product.code))
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["code", "name", "description", *(fld["key"] for fld in fields)])
        for p in result.scalars().all():
            data = p.data or {}
            # A text value could start like a spreadsheet formula; numbers and yes / no cannot.
            cells = [format_cell(data.get(fld["key"])) for fld in fields]
            cells = [_csv_safe(cell) if fld["type"] == "text" else cell for fld, cell in zip(fields, cells)]
            writer.writerow([_csv_safe(p.code), _csv_safe(p.name), _csv_safe(p.description or ""), *cells])
        return buf.getvalue()


def _csv_safe(value: str) -> str:
    """Stop spreadsheet programs from running a cell as a formula."""
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _csv_unescape(value: Optional[str]) -> Optional[str]:
    """Undo _csv_safe, so an exported file imports back unchanged."""
    if value and len(value) > 1 and value[0] == "'" and value[1] in ("=", "+", "-", "@", "\t", "\r"):
        return value[1:]
    return value
