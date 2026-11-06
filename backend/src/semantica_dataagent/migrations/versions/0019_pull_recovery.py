"""Persist pull recovery identity."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pull_records", sa.Column("source_directory", sa.Text()))
    op.add_column("pull_records", sa.Column("operation_id", sa.String()))


def downgrade():
    op.drop_column("pull_records", "operation_id")
    op.drop_column("pull_records", "source_directory")
