from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# The list the product codes of a server without lists were moved into.
DEFAULT_LIST_ID = "list-1"
DEFAULT_LIST_NAME = "List 1"


def new_list_id() -> str:
    return f"list-{uuid.uuid4().hex[:8]}"


class ProductList(Base):
    """A named list of product codes. A camera that reads codes checks them against the list it is set to."""

    __tablename__ = "product_lists"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_list_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ProductList id={self.id!r} name={self.name!r}>"


class Product(Base):
    """A product code in one product list. The same code may be in more than one list."""

    __tablename__ = "products"
    __table_args__ = (Index("uq_products_list_code", "list_id", "code", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # No database foreign key (SQLite cannot add one to an existing table):
    # ProductService removes a list's products together with the list.
    list_id: Mapped[str] = mapped_column(String(36), nullable=False, server_default=DEFAULT_LIST_ID)
    code: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Product list={self.list_id!r} code={self.code!r} name={self.name!r}>"
