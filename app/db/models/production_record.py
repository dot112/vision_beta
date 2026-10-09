from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProductRecord(Base):
    """One product a line decided (good or reject), or one code read that is no product.

    Written in batches by ProductionRecorder (app/services/production_records_service.py).
    Times are UTC. ``counted`` rows are the ones in the line totals: own-station
    products and code rows are kept for the record but not counted.
    """

    __tablename__ = "product_records"
    __table_args__ = (
        Index("ix_product_records_line_time", "line_id", "recorded_at"),
        Index("ix_product_records_recorded_at", "recorded_at"),
        Index("ix_product_records_batch", "batch"),
        Index("ix_product_records_code", "code"),
    )

    # An integer key, not a UUID: the table grows fast and exports page through it.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    line_id: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    line_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    camera_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    camera_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # "product" or "code"
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    counted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "good" / "reject"; None for code rows
    result: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    class_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    track_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    code: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    code_format: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # "known" / "unknown" / "no_read"
    code_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    product_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    product_list_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    batch: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # vision_result, stations (joined cameras), bbox, reject_camera_id
    details: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<ProductRecord id={self.id} line={self.line_id!r} kind={self.kind!r} result={self.result!r}>"
