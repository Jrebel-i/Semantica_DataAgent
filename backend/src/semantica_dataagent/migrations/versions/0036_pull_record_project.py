"""Scope pull records to a project."""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

DEFAULT_PROJECT_ID = "default"


def upgrade():
    op.add_column(
        "pull_records",
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default=DEFAULT_PROJECT_ID,
        ),
    )


def downgrade():
    op.drop_column("pull_records", "project_id")
