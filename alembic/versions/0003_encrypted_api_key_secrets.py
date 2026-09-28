"""Store API key secrets encrypted for admin reveal."""

from alembic import op
import sqlalchemy as sa


revision = "0003_encrypted_api_key_secrets"
down_revision = "0002_scoped_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("api_keys")}
    if "encrypted_key" not in columns:
        op.add_column("api_keys", sa.Column("encrypted_key", sa.Text(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("api_keys")}
    if "encrypted_key" in columns:
        op.drop_column("api_keys", "encrypted_key")
