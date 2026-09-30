"""Known product codes: the database table and the in-memory copy QR readers check against."""
from __future__ import annotations

import csv
import io
import threading
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.product import Product
from app.utils.logger import get_logger

logger = get_logger(__name__)

MAX_CODE_LENGTH = 256
MAX_NAME_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 512
MAX_IMPORT_ROWS = 100_000


def normalize_code(code: Any) -> str:
    """Codes are compared as sent, minus surrounding whitespace."""
    return str(code if code is not None else "").strip()


class ProductCatalog:
    """Thread-safe code -> product lookup, refreshed from the database after every change.

    A QR read never waits on the database: readers look codes up here.
    """

    def __init__(self) -> None:
        self._by_code: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.loaded = False

    def replace(self, products: List[Dict[str, Any]]) -> None:
        table = {p["code"]: p for p in products}
        with self._lock:
            self._by_code = table
            self.loaded = True

    def lookup(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._by_code.get(normalize_code(code))

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_code)


product_catalog = ProductCatalog()


def _row(product: Product) -> Dict[str, Any]:
    return {
        "id": product.id,
        "code": product.code,
        "name": product.name,
        "description": product.description,
        "created_at": product.created_at.isoformat() if product.created_at else None,
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
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


class ProductService:
    @staticmethod
    async def refresh_catalog(db: AsyncSession) -> int:
        result = await db.execute(select(Product))
        rows = [_row(p) for p in result.scalars().all()]
        product_catalog.replace(rows)
        return len(rows)

    @staticmethod
    async def list_products(db: AsyncSession, search: Optional[str] = None, limit: int = 500, offset: int = 0) -> Dict[str, Any]:
        stmt = select(Product)
        count_stmt = select(func.count()).select_from(Product)
        if search:
            pattern = f"%{search.strip()}%"
            cond = Product.code.ilike(pattern) | Product.name.ilike(pattern)
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        total = (await db.execute(count_stmt)).scalar_one()
        result = await db.execute(stmt.order_by(Product.code).offset(offset).limit(limit))
        return {"total": total, "products": [_row(p) for p in result.scalars().all()]}

    @staticmethod
    async def get(db: AsyncSession, product_id: str) -> Optional[Dict[str, Any]]:
        product = await db.get(Product, product_id)
        return _row(product) if product else None

    @staticmethod
    async def create(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
        code, name, description = _validate(data.get("code"), data.get("name"), data.get("description"))
        exists = (await db.execute(select(Product).where(Product.code == code))).scalar_one_or_none()
        if exists:
            raise ValueError(f"Product code '{code}' already exists")
        product = Product(code=code, name=name, description=description)
        db.add(product)
        await db.commit()
        await db.refresh(product)
        await ProductService.refresh_catalog(db)
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
        if code != product.code:
            clash = (await db.execute(select(Product).where(Product.code == code))).scalar_one_or_none()
            if clash:
                raise ValueError(f"Product code '{code}' already exists")
        product.code, product.name, product.description = code, name, description
        await db.commit()
        await db.refresh(product)
        await ProductService.refresh_catalog(db)
        return _row(product)

    @staticmethod
    async def delete(db: AsyncSession, product_id: str) -> bool:
        product = await db.get(Product, product_id)
        if product is None:
            return False
        await db.delete(product)
        await db.commit()
        await ProductService.refresh_catalog(db)
        return True

    @staticmethod
    def parse_csv(text: str) -> List[Tuple[str, str, Optional[str]]]:
        """Rows from CSV text with a header naming ``code`` and ``name`` (``description`` optional)."""
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
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
    async def import_csv(db: AsyncSession, text: str, replace: bool = False) -> Dict[str, int]:
        """Add or update products from CSV. With replace, codes missing from the file are removed."""
        rows = ProductService.parse_csv(text)
        existing = {p.code: p for p in (await db.execute(select(Product))).scalars().all()}
        added = updated = 0
        for code, name, description in rows:
            product = existing.get(code)
            if product is None:
                db.add(Product(code=code, name=name, description=description))
                added += 1
            elif product.name != name or product.description != description:
                product.name, product.description = name, description
                updated += 1
        removed = 0
        if replace:
            keep = {r[0] for r in rows}
            stale = [code for code in existing if code not in keep]
            if stale:
                await db.execute(delete(Product).where(Product.code.in_(stale)))
                removed = len(stale)
        await db.commit()
        total = await ProductService.refresh_catalog(db)
        return {"added": added, "updated": updated, "removed": removed, "total": total}

    @staticmethod
    async def export_csv(db: AsyncSession) -> str:
        result = await db.execute(select(Product).order_by(Product.code))
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
