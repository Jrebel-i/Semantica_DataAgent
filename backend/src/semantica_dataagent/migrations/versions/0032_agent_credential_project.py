"""Bind online agent credentials to one project."""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

DEFAULT_PROJECT_ID = "default"


def upgrade():
    op.add_column(
        "agent_credentials",
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default=DEFAULT_PROJECT_ID,
        ),
    )


def downgrade():
    op.drop_column("agent_credentials", "project_id")
