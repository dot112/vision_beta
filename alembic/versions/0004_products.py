"""Add the products table for QR code checks (version 2)."""

from alembic import op
import sqlalchemy as sa


revision = "0004_products"
down_revision = "0003_encrypted_api_key_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "products" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "products",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=256), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_products_code", "products", ["code"], unique=True)


def downgrade() -> None:
    if "products" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_products_code", table_name="products")
        op.drop_table("products")
