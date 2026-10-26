from __future__ import annotations

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KeptTotal(Base):
    """How many deleted product records still belong to a counter, so its totals stay exact.

    Old records are deleted after RECORDS_RETENTION_DAYS (or to free space); the
    products among them that a counter still holds are added here first
    (ProductionRecorder.delete_chunk), and counted_totals adds them back at a restart.
    A row counts while its ``reset_tag`` is the reset its class counts from: a later
    reset changes the tag, and the hourly cleanup deletes the old rows.
    """

    __tablename__ = "kept_totals"
    __table_args__ = (
        UniqueConstraint("line_id", "camera_id", "class_name", "result", "reset_tag", name="uq_kept_totals_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    line_id: Mapped[str] = mapped_column(String(48), nullable=False)
    # "" = the line's totals, else an own-station camera's
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # The counter's key: lower case, "product" when empty
    class_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # "good" / "reject"
    result: Mapped[str] = mapped_column(String(8), nullable=False)
    # The ISO time of the reset the count belongs to; "" = never reset
    reset_tag: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<KeptTotal line={self.line_id!r} camera={self.camera_id!r} class={self.class_name!r} {self.result} {self.count}>"
