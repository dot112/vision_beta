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


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
