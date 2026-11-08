"""Record the reasoning effort used by each turn."""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("turns", sa.Column("effort", sa.String(), nullable=True))


def downgrade():
    op.drop_column("turns", "effort")
