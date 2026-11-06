"""Limit online agent credentials to their MCP turn."""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_credentials",
        sa.Column("digest", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("agent_credentials")
