"""Add conversation pinning and archiving timestamps."""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("pinned_at", sa.DateTime(), nullable=True))
    op.add_column("conversations", sa.Column("archived_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("conversations", "archived_at")
    op.drop_column("conversations", "pinned_at")
