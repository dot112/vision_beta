"""Create the initial application schema."""

from alembic import op

from app.db.base import Base
import app.db.models.action  # noqa: F401
import app.db.models.camera  # noqa: F401
import app.db.models.detection  # noqa: F401
import app.db.models.event  # noqa: F401
import app.db.models.flow  # noqa: F401
import app.db.models.model  # noqa: F401
import app.db.models.product  # noqa: F401
import app.db.models.rule  # noqa: F401
import app.db.models.user  # noqa: F401

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


# Tables owned by later revisions. Base.metadata holds every imported model,
# including ones added after this revision, so they must be left to their own
# migration or a fresh database fails with "table already exists".
_LATER_TABLES = {"api_keys"}


def _initial_tables():
    return [table for name, table in Base.metadata.tables.items() if name not in _LATER_TABLES]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_initial_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=_initial_tables())
