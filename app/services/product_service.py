"""Product lists: the database tables and the in-memory copy that cameras reading codes check against.

There are up to MAX_PRODUCT_LISTS lists. Each camera that reads codes is set
to one of them (``product_list_id`` on the camera, on Line setup). The lists
are independent: the same code may be in more than one.
"""
from __future__ import annotations

import csv
import io
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.product import DEFAULT_LIST_ID, DEFAULT_LIST_NAME, Product, ProductList, new_list_id
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


def normalize_code(code: Any) -> str:
    """Codes are compared as sent, minus surrounding whitespace."""
    return str(code if code is not None else "").strip()


class ProductCatalog:
    """Thread-safe code -> product lookup for every product list, kept in step with the database.

    A code read never waits on the database: readers look codes up here. A
    list's lookup is replaced in one assignment, so a reader sees the old
    contents or the new ones, never an empty or half-filled list.
    """

    def __init__(self) -> None:
        self._lists: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._names: Dict[str, str] = {}
        self._lock = threading.Lock()
        self.loaded = False

    def replace_all(self, lists: Dict[str, Tuple[str, List[Dict[str, Any]]]]) -> None:
        """Every list at once: {list_id: (name, products)}, in the order the lists were created."""
        tables = {list_id: {p["code"]: p for p in products} for list_id, (_, products) in lists.items()}
        names = {list_id: name for list_id, (name, _) in lists.items()}
        with self._lock:
            self._lists, self._names = tables, names
            self.loaded = True

    def replace_list(self, list_id: str, name: str, products: List[Dict[str, Any]]) -> None:
        table = {p["code"]: p for p in products}
        with self._lock:
            self._lists = {**self._lists, list_id: table}
            self._names = {**self._names, list_id: name}

    def remove_list(self, list_id: str) -> None:
        with self._lock:
            self._lists = {k: v for k, v in self._lists.items() if k != list_id}
            self._names = {k: v for k, v in self._names.items() if k != list_id}

    def lookup(self, code: str, list_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """The product a code stands for in one list. None when it is not listed, or no list is named."""
        if not list_id:
            return None
        with self._lock:
            table = self._lists.get(list_id)
        return table.get(normalize_code(code)) if table else None

    def lookup_any(self, code: str) -> Optional[Dict[str, Any]]:
        """The first list that has the code, oldest list first (for callers that name no list)."""
        code = normalize_code(code)
        with self._lock:
            tables = list(self._lists.values())
        for table in tables:
            product = table.get(code)
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
        "created_at": product.created_at.isoformat() if product.created_at else None,
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
    }


def _list_row(product_list: ProductList, count: int) -> Dict[str, Any]:
    return {
        "id": product_list.id,
        "name": product_list.name,
        "count": count,
        "created_at": product_list.created_at.isoformat() if product_list.created_at else None,
        "updated_at": product_list.updated_at.isoformat() if product_list.updated_at else None,
    }


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


def _new_list(name: str, list_id: Optional[str] = None) -> ProductList:
    # The time is set here, to the microsecond: the lists are shown oldest
    # first, and the database's own clock only counts whole seconds.
    return ProductList(id=list_id or new_list_id(), name=name, created_at=datetime.now(timezone.utc))


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
        product_catalog.replace_all(by_list)
        return len(product_catalog)

    @staticmethod
    async def _refresh_list(db: AsyncSession, product_list: ProductList) -> int:
        result = await db.execute(select(Product).where(Product.list_id == product_list.id))
        rows = [_row(p) for p in result.scalars().all()]
        product_catalog.replace_list(product_list.id, product_list.name, rows)
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
    async def create_list(db: AsyncSession, name: Any) -> Dict[str, Any]:
        lists = await ProductService._lists(db)
        if len(lists) >= MAX_PRODUCT_LISTS:
            raise ProductListLimit(f"There are already {MAX_PRODUCT_LISTS} product lists. Delete one first.")
        wanted = _list_name(name)
        if any(pl.name.lower() == wanted.lower() for pl in lists):
            raise ValueError(f"A product list named '{wanted}' already exists")
        product_list = _new_list(wanted)
        db.add(product_list)
        await db.commit()
        await db.refresh(product_list)
        product_catalog.replace_list(product_list.id, product_list.name, [])
        return _list_row(product_list, 0)

    @staticmethod
    async def rename_list(db: AsyncSession, list_id: str, name: Any) -> Optional[Dict[str, Any]]:
        product_list = await db.get(ProductList, list_id)
        if product_list is None:
            return None
        wanted = _list_name(name)
        if any(pl.name.lower() == wanted.lower() for pl in await ProductService._lists(db) if pl.id != list_id):
            raise ValueError(f"A product list named '{wanted}' already exists")
        product_list.name = wanted
        await db.commit()
        await db.refresh(product_list)
        count = await ProductService._refresh_list(db, product_list)
        return _list_row(product_list, count)

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
        if await ProductService._code_taken(db, product_list.id, code):
            raise ValueError(f"Product code '{code}' is already in '{product_list.name}'")
        product = Product(list_id=product_list.id, code=code, name=name, description=description)
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
        if code != product.code and await ProductService._code_taken(db, product.list_id, code):
            raise ValueError(f"Product code '{code}' is already in this list")
        product.code, product.name, product.description = code, name, description
        await db.commit()
        await db.refresh(product)
        product_list = await db.get(ProductList, product.list_id)
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
    def parse_csv(text: str) -> List[Tuple[str, str, Optional[str]]]:
        """Rows from CSV text with a header naming ``code`` and ``name`` (``description`` optional)."""
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        headers = {h.strip().lower(): h for h in (reader.fieldnames or []) if h}
        if "code" not in headers or "name" not in headers:
            raise ValueError("The CSV file needs a header row with 'code' and 'name' columns")
        rows: List[Tuple[str, str, Optional[str]]] = []
        seen: Dict[str, int] = {}
        for line_no, raw in enumerate(reader, start=2):
            if len(rows) >= MAX_IMPORT_ROWS:
                raise ValueError(f"The CSV file has more than {MAX_IMPORT_ROWS} rows")
            code_raw = _csv_unescape(raw.get(headers["code"]))
            name_raw = _csv_unescape(raw.get(headers["name"]))
            desc_raw = _csv_unescape(raw.get(headers["description"])) if "description" in headers else None
            if not normalize_code(code_raw) and not str(name_raw or "").strip():
                continue  # blank line
            try:
                row = _validate(code_raw, name_raw, desc_raw)
            except ValueError as exc:
                raise ValueError(f"Line {line_no}: {exc}") from None
            if row[0] in seen:
                raise ValueError(f"Line {line_no}: code '{row[0]}' also appears on line {seen[row[0]]}")
            seen[row[0]] = line_no
            rows.append(row)
        return rows

    @staticmethod
    async def import_csv(db: AsyncSession, text: str, name: Any = None) -> Dict[str, Any]:
        """Make a new list from a CSV file. Refused when there are already MAX_PRODUCT_LISTS lists."""
        rows = ProductService.parse_csv(text)
        if len(await ProductService._lists(db)) >= MAX_PRODUCT_LISTS:
            raise ProductListLimit(
                f"There are already {MAX_PRODUCT_LISTS} product lists. Delete one first, "
                f"or use Replace contents to update a list from this file."
            )
        list_name = await ProductService._free_name(db, _list_name(name or "Product list"))
        product_list = _new_list(list_name)
        db.add(product_list)
        for code, product_name, description in rows:
            db.add(Product(list_id=product_list.id, code=code, name=product_name, description=description))
        await db.commit()
        await db.refresh(product_list)
        total = await ProductService._refresh_list(db, product_list)
        return {"list": _list_row(product_list, total), "added": len(rows), "updated": 0, "removed": 0, "total": total}

    @staticmethod
    async def replace_contents(db: AsyncSession, list_id: str, text: str) -> Optional[Dict[str, Any]]:
        """Make a list hold exactly the rows of a CSV file. Its id and name stay, so its readers keep working."""
        rows = ProductService.parse_csv(text)
        product_list = await db.get(ProductList, list_id)
        if product_list is None:
            return None
        result = await db.execute(select(Product).where(Product.list_id == list_id))
        existing = {p.code: p for p in result.scalars().all()}
        added = updated = 0
        for code, name, description in rows:
            product = existing.get(code)
            if product is None:
                db.add(Product(list_id=list_id, code=code, name=name, description=description))
                added += 1
            elif product.name != name or product.description != description:
                product.name, product.description = name, description
                updated += 1
        keep = {row[0] for row in rows}
        stale = [product.id for code, product in existing.items() if code not in keep]
        for start in range(0, len(stale), 500):
            await db.execute(delete(Product).where(Product.id.in_(stale[start:start + 500])))
        await db.commit()
        await db.refresh(product_list)
        # The lookup readers use changes here, in one step, after the database has the new rows.
        total = await ProductService._refresh_list(db, product_list)
        return {"list": _list_row(product_list, total), "added": added, "updated": updated, "removed": len(stale), "total": total}

    @staticmethod
    async def export_csv(db: AsyncSession, list_id: str) -> str:
        result = await db.execute(select(Product).where(Product.list_id == list_id).order_by(Product.code))
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["code", "name", "description"])
        for p in result.scalars().all():
            writer.writerow([_csv_safe(p.code), _csv_safe(p.name), _csv_safe(p.description or "")])
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
