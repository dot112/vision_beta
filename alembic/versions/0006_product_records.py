"""Product records: every product a line decides and every code read, for the Records page and exports.

On a new database revision 0001 leaves this table out (_LATER_TABLES), so it
is always made here. Inspected first, so running it again changes nothing.
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_product_records"
down_revision = "0005_product_lists"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_product_records_line_time", ["line_id", "recorded_at"]),
    ("ix_product_records_recorded_at", ["recorded_at"]),
    ("ix_product_records_batch", ["batch"]),
    ("ix_product_records_code", ["code"]),
)


def upgrade() -> None:
    bind = op.get_bind()
    if "product_records" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "product_records",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("line_id", sa.String(length=48), nullable=True),
            sa.Column("line_name", sa.String(length=64), nullable=True),
            sa.Column("camera_id", sa.String(length=64), nullable=True),
            sa.Column("camera_name", sa.String(length=128), nullable=True),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("counted", sa.Boolean(), nullable=False),
            sa.Column("result", sa.String(length=8), nullable=True),
            sa.Column("reject_reason", sa.String(length=32), nullable=True),
            sa.Column("class_name", sa.String(length=128), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("track_id", sa.Integer(), nullable=True),
            sa.Column("code", sa.String(length=512), nullable=True),
            sa.Column("code_format", sa.String(length=32), nullable=True),
            sa.Column("code_status", sa.String(length=16), nullable=True),
            sa.Column("product_name", sa.String(length=128), nullable=True),
            sa.Column("product_list_id", sa.String(length=36), nullable=True),
            sa.Column("batch", sa.String(length=64), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes("product_records")}
    for name, columns in _INDEXES:
        if name not in existing:
            op.create_index(name, "product_records", columns, unique=False)


def downgrade() -> None:
    if "product_records" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("product_records")
