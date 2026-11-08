"""Kept totals: the counts of deleted product records that still belong to a line's counter.

Also an index on detections.created_at, for deleting old detections by age. On a new
database revision 0001 leaves kept_totals out (_LATER_TABLES), so it is always made
here, and the index comes with the detections model. Inspected first, so running it
again changes nothing.
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_kept_totals"
down_revision = "0008_code_format"
branch_labels = None
depends_on = None

_INDEX = "ix_detections_created_at"


def _detection_indexes() -> set:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("detections")}


def upgrade() -> None:
    if "kept_totals" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "kept_totals",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("line_id", sa.String(length=48), nullable=False),
            sa.Column("camera_id", sa.String(length=64), nullable=False),
            sa.Column("class_name", sa.String(length=128), nullable=False),
            sa.Column("result", sa.String(length=8), nullable=False),
            sa.Column("reset_tag", sa.String(length=40), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("line_id", "camera_id", "class_name", "result", "reset_tag", name="uq_kept_totals_key"),
        )
    if _INDEX not in _detection_indexes():
        op.create_index(_INDEX, "detections", ["created_at"], unique=False)


def downgrade() -> None:
    if _INDEX in _detection_indexes():
        op.drop_index(_INDEX, table_name="detections")
    if "kept_totals" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("kept_totals")
