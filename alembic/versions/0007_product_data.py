"""Products carry data: product_lists.fields (the list's own fields) and products.data (a product's values).

On a new database revision 0001 has already made the products table from the
model, data column included, while 0005 made product_lists without fields:
each column is added only where it is missing, so running this again changes
nothing.
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_product_data"
down_revision = "0006_product_records"
branch_labels = None
depends_on = None


def _columns(table: str) -> set:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "fields" not in _columns("product_lists"):
        op.add_column("product_lists", sa.Column("fields", sa.JSON(), nullable=False, server_default="[]"))
    if "data" not in _columns("products"):
        op.add_column("products", sa.Column("data", sa.JSON(), nullable=True))


def downgrade() -> None:
    if "data" in _columns("products"):
        with op.batch_alter_table("products") as batch:
            batch.drop_column("data")
    if "fields" in _columns("product_lists"):
        with op.batch_alter_table("product_lists") as batch:
            batch.drop_column("fields")
