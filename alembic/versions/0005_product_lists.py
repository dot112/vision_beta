"""Product lists: every product code belongs to one list; existing codes go into "List 1".

Only statements SQLite runs in place are used (no table rebuild), so the
products table is never copied. That is why list_id has no foreign key;
ProductService removes a list's products together with the list.

On a new database revision 0001 has already made the products table in its
present shape (it builds the tables from the models), so only the list table
and "List 1" are added here.
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_product_lists"
down_revision = "0004_products"
branch_labels = None
depends_on = None

DEFAULT_LIST_ID = "list-1"
DEFAULT_LIST_NAME = "List 1"


def _index_names(inspector, table: str) -> set:
    names = {index["name"] for index in inspector.get_indexes(table)}
    names |= {constraint["name"] for constraint in inspector.get_unique_constraints(table)}
    return names


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "product_lists" not in inspector.get_table_names():
        lists = op.create_table(
            "product_lists",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", name="uq_product_lists_name"),
        )
        # The one list every server had so far. Readers of existing lines keep checking against it.
        op.bulk_insert(lists, [{"id": DEFAULT_LIST_ID, "name": DEFAULT_LIST_NAME}])

    columns = {column["name"] for column in inspector.get_columns("products")}
    if "list_id" not in columns:
        op.add_column(
            "products",
            sa.Column("list_id", sa.String(length=36), nullable=False, server_default=DEFAULT_LIST_ID),
        )
    # A code is unique inside its list, no longer across all of them.
    indexes = _index_names(sa.inspect(bind), "products")
    if "ix_products_code" in indexes:
        op.drop_index("ix_products_code", table_name="products")
    op.create_index("ix_products_code", "products", ["code"], unique=False)
    if "uq_products_list_code" not in indexes:
        op.create_index("uq_products_list_code", "products", ["list_id", "code"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = _index_names(inspector, "products")
    if "uq_products_list_code" in indexes:
        op.drop_index("uq_products_list_code", table_name="products")
    columns = {column["name"] for column in inspector.get_columns("products")}
    if "list_id" in columns:
        # One list again: keep the codes of the default list, and of the other lists only codes it does not have.
        op.execute(sa.text(
            "DELETE FROM products WHERE list_id <> :keep AND code IN (SELECT code FROM products WHERE list_id = :keep)"
        ).bindparams(keep=DEFAULT_LIST_ID))
        op.execute(sa.text(
            "DELETE FROM products WHERE id NOT IN (SELECT MIN(id) FROM products GROUP BY code)"
        ))
        with op.batch_alter_table("products") as batch:
            batch.drop_column("list_id")
    if "ix_products_code" in indexes:
        op.drop_index("ix_products_code", table_name="products")
    op.create_index("ix_products_code", "products", ["code"], unique=True)
    if "product_lists" in inspector.get_table_names():
        op.drop_table("product_lists")
