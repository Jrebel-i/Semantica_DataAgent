"""Persist the source of new conversation turns."""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("turns", sa.Column("source", sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table("turns") as batch:
        batch.drop_column("source")
