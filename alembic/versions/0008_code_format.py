"""A product list says how its codes are built: product_lists.code_format.

{"mode": "whole"} (the whole code is the product, as before), "split" (parts with a
separator) or "gs1"; see app/services/code_format.py. The column is added only where it
is missing, so running this again changes nothing.
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_code_format"
down_revision = "0007_product_data"
branch_labels = None
depends_on = None


def _columns(table: str) -> set:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "code_format" not in _columns("product_lists"):
        op.add_column("product_lists", sa.Column("code_format", sa.JSON(), nullable=False,
                                                 server_default='{"mode": "whole"}'))


def downgrade() -> None:
    if "code_format" in _columns("product_lists"):
        with op.batch_alter_table("product_lists") as batch:
            batch.drop_column("code_format")
