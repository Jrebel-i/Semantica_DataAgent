"""Bind each conversation to one project."""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

DEFAULT_PROJECT_ID = "default"


def upgrade():
    op.add_column(
        "conversations",
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default=DEFAULT_PROJECT_ID,
        ),
    )
    op.create_index("ix_conversations_project", "conversations", ["project_id"])


def downgrade():
    op.drop_index("ix_conversations_project", table_name="conversations")
    op.drop_column("conversations", "project_id")
